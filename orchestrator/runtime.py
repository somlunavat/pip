"""Builds the real components from Settings and wires them together."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestrator import mac
from orchestrator.agent37 import Dispatcher, WorkerClient, WorkerPool, read_prompt, work_prompt
from orchestrator.config import Settings
from orchestrator.db import Database
from orchestrator.llm.base import LLM, Usage
from orchestrator.llm.gemini import GeminiClient
from orchestrator.loop import Agent, Emit
from orchestrator.models import Milestone, MilestoneResult
from orchestrator.nimble import NimbleClient
from orchestrator.preferences import Preferences
from orchestrator.router import ReadRouter, Tier
from orchestrator.security import SpendGuard
from orchestrator.server import Hub
from orchestrator.watchers import WatcherService

DOING_EVENTS = {"response.tool_call.started"}


@dataclass
class Runtime:
    settings: Settings
    db: Database
    llm: LLM
    nimble: NimbleClient
    pool: WorkerPool
    dispatcher: Dispatcher
    agent: Agent
    watchers: WatcherService
    hub: Hub

    async def aclose(self) -> None:
        await self.watchers.stop()
        await self.nimble.aclose()
        await self.pool.aclose()
        self.db.close()


def worker_hooks(db: Database, task_id: str, m: Milestone, emit: Emit | None):
    sub_id = f"{task_id}:{m.id}"

    async def on_event(event: str, payload: dict[str, Any]) -> None:
        if emit and event in DOING_EVENTS:
            label = payload.get("label") or payload.get("tool") or "working"
            await emit({"type": "doing", "task_id": task_id, "milestone_id": m.id,
                        "label": f"Worker: {label}", "path": "worker"})

    async def on_dispatch(worker_id: str, session_id: str, event: str) -> None:
        fields: dict[str, Any] = {"worker_id": worker_id, "session_id": session_id}
        if event.startswith("response:"):
            fields["response_id"] = event.split(":", 1)[1]
        db.update_subtask(sub_id, **fields)

    return on_event, on_dispatch


def build_runtime(settings: Settings) -> Runtime:
    db = Database(settings.db_path)

    def on_usage(event: str, usage: Usage) -> None:
        db.log_event(event, latency_ms=usage.latency_ms, tokens_in=usage.tokens_in,
                     tokens_out=usage.tokens_out, cost_usd=usage.cost_usd, source="gemini")

    llm = GeminiClient(
        settings.gemini_api_key, plan_model=settings.gemini_plan_model,
        fast_model=settings.gemini_fast_model, embed_model=settings.gemini_embed_model,
        embed_dim=settings.embed_dim, on_usage=on_usage,
    )
    nimble = NimbleClient(settings.nimble_api_key, base_url=settings.nimble_base_url,
                          concurrency=settings.nimble_concurrency)
    workers = [WorkerClient(w, settings.agent37_api_key) for w in settings.agent37_workers]
    pool = WorkerPool(
        workers, settings.agent37_sessions_per_worker,
        on_change=lambda wid, status, n: db.upsert_worker(wid, status, n),
    )
    dispatcher = Dispatcher(pool, queue_wait_s=settings.agent37_queue_wait_s)

    async def work(m: Milestone, task_id: str, emit: Emit,
                   upstream: list[MilestoneResult]) -> MilestoneResult:
        on_event, on_dispatch = worker_hooks(db, task_id, m, emit)
        return await dispatcher.run(m, task_id=task_id, prompt=work_prompt(m, upstream),
                                    on_event=on_event, on_dispatch=on_dispatch)

    async def worker_read(m: Milestone) -> MilestoneResult:
        return await dispatcher.run(m, task_id="read-fallback", prompt=read_prompt(m))

    tiers = [Tier("nimble", nimble.read)]
    if workers:
        tiers.append(Tier("worker", worker_read))
    router = ReadRouter(tiers)
    guard = SpendGuard(db, settings.daily_cap_usd, settings.kill_switch)
    hub = Hub()

    async def alert(change: dict[str, Any]) -> None:
        """Show in the overlay; fall back to a macOS notification if it is closed."""
        if hub.clients:
            await hub.broadcast(change)
        else:
            try:
                await mac.show_notification("pip", change["description"][:200])
            except mac.MacToolError:
                pass

    watchers = WatcherService(db, llm, nimble, Preferences(db), alert, guard)
    agent = Agent(db, llm, router, guard, work=work if workers else None,
                  watch=watchers.as_milestone,
                  browser=mac.open_in_browser if settings.open_results else None,
                  worker_capacity=len(workers) * settings.agent37_sessions_per_worker)
    return Runtime(settings, db, llm, nimble, pool, dispatcher, agent, watchers, hub)
