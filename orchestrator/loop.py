"""The Sense, Plan, Gate, Act, Verify loop.

Structure borrowed from Moonwalk (perception, planner, sub-agent groups, verifier);
the code is written fresh. Every step writes a task_events row.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from orchestrator import mac, verifier
from orchestrator.answer import clean_answer, should_open
from orchestrator.config import describe_location
from orchestrator.db import Database
from orchestrator.llm.base import LLM
from orchestrator.models import Answer, Milestone, MilestoneResult, Plan
from orchestrator.router import ReadRouter, path_for
from orchestrator.security import BudgetExceeded, SpendGuard

Emit = Callable[[dict[str, Any]], Awaitable[None]]
Approve = Callable[[str, Plan], Awaitable[bool]]
SenseFn = Callable[[], Awaitable[dict[str, str]]]
MacCall = Callable[[str, dict[str, Any]], Awaitable[Any]]
# (milestone, task_id, emit, results of the milestones it depends on)
WorkFn = Callable[[Milestone, str, Emit, list[MilestoneResult]], Awaitable[MilestoneResult]]
WatchFn = Callable[[Milestone, str], Awaitable[MilestoneResult]]
BrowserFn = Callable[[list[str]], Awaitable[str]]
RevealFn = Callable[[str], Awaitable[str]]


class PlanError(ValueError):
    pass


# Upper bound on worker pieces in one plan; each piece is a paid worker turn.
MAX_WORK_PIECES = 12

# Errors that a retry cannot fix; retrying only delays the answer.
PERMANENT_ERRORS = {"no_worker_configured", "no_watcher_registry", "no_tool", "no_worker",
                    "no_watch_spec", "MacToolError"}

# Things that only exist on the user's Mac. A remote worker can never reach them.
_MAC_ONLY = re.compile(
    r"\b(calendar|reminders?|apple notes|notes app|imessages?|messages app|text messages?|"
    r"sms|finder|my mac|desktop|clipboard|contacts app|system settings|music app)\b",
    re.IGNORECASE,
)


def check_plan(plan: Plan, *, has_workers: bool) -> list[str]:
    """Problems that would make a plan fail part-way. Checked before the gate so a
    doomed plan never runs its first steps."""
    problems: list[str] = []
    pieces = sum(1 for m in plan.milestones if m.kind == "work")
    if pieces > MAX_WORK_PIECES:
        problems.append(f"plan: {pieces} worker pieces is too many; use at most "
                        f"{MAX_WORK_PIECES} by grouping related items")
    for m in plan.milestones:
        if m.kind == "local":
            if not m.tool:
                problems.append(f"{m.id}: local milestone has no tool")
            elif problem := mac.validate_args(m.tool, m.args):
                problems.append(f"{m.id}: {problem}")
        elif m.kind == "work" or (m.kind == "read" and path_for(m) == "worker"):
            if _MAC_ONLY.search(m.goal):
                problems.append(
                    f"{m.id}: '{m.goal}' is on the user's Mac, but workers are remote "
                    "computers that cannot reach it; use a local tool or mark it unsupported"
                )
            elif not has_workers:
                problems.append(f"{m.id}: needs an Agent37 worker and none is configured")
        elif m.kind == "watch" and m.watch is None:
            problems.append(f"{m.id}: watch milestone has no watch spec")
    return problems


def dependency_groups(plan: Plan) -> list[list[Milestone]]:
    """Topological layers: each layer depends only on earlier layers."""
    ids = {m.id for m in plan.milestones}
    if len(ids) != len(plan.milestones):
        raise PlanError("duplicate milestone ids")
    for m in plan.milestones:
        unknown = set(m.depends_on) - ids
        if unknown:
            raise PlanError(f"{m.id} depends on unknown {sorted(unknown)}")
    done: set[str] = set()
    remaining = list(plan.milestones)
    layers: list[list[Milestone]] = []
    while remaining:
        layer = [m for m in remaining if set(m.depends_on) <= done]
        if not layer:
            raise PlanError("dependency cycle in plan")
        layers.append(layer)
        done |= {m.id for m in layer}
        remaining = [m for m in remaining if m.id not in done]
    return layers


def needs_approval(plan: Plan) -> bool:
    """Plans that write into the user's apps wait for Proceed. Reading, and showing
    things (opening a page, an app, a notification), run at once."""
    return any(m.kind == "local" and m.tool in mac.WRITE_TOOLS for m in plan.milestones)


@dataclass
class TaskOutcome:
    task_id: str
    status: str
    text: str
    results: list[MilestoneResult] = field(default_factory=list)
    elapsed_ms: float = 0.0
    answer: Answer | None = None


class Agent:
    def __init__(
        self,
        db: Database,
        llm: LLM,
        router: ReadRouter,
        guard: SpendGuard,
        *,
        work: WorkFn | None = None,
        watch: WatchFn | None = None,
        sense: SenseFn = mac.get_context,
        mac_call: MacCall = mac.call_tool,
        browser: BrowserFn | None = mac.open_in_browser,
        worker_capacity: int = 0,
        reveal: RevealFn | None = mac.reveal_in_finder,
        open_results: str = "ask",
        location: str = "",
        max_replans: int = 1,
    ):
        self.db = db
        self.llm = llm
        self.router = router
        self.guard = guard
        self.work = work
        self.watch = watch
        self.sense = sense
        self.mac_call = mac_call
        self.browser = browser  # None turns off pulling results up in the browser
        self.open_results = open_results  # ask: only when the request asks for pages
        self.location = location  # described to the planner; see config.describe_location
        self.worker_capacity = worker_capacity if work else 0
        self.reveal = reveal  # shows files that workers made; None turns it off
        self.max_replans = max_replans

    async def handle(self, text: str, emit: Emit, approve: Approve,
                     *, context: dict[str, str] | None = None) -> TaskOutcome:
        start = time.perf_counter()
        task_id = self.db.create_task(text)
        await emit({"type": "thinking", "task_id": task_id, "text": text})
        try:
            outcome = await self._run(task_id, text, emit, approve, context, start)
        except asyncio.CancelledError:
            # The user pressed Stop. Worker turns cancel themselves on the way out.
            self.db.finish_task(task_id, "cancelled")
            self.db.log_event("task", task_id=task_id, latency_ms=_ms(start),
                              status="cancelled")
            with contextlib.suppress(Exception):
                await emit({"type": "response", "task_id": task_id, "text": "Stopped.",
                            "display": "pill", "sources": [], "elapsed_ms": _ms(start),
                            "paths": [], "answer": {"headline": "Stopped.", "summary": "",
                                                    "rows": []}})
            raise
        except BudgetExceeded as exc:
            outcome = TaskOutcome(task_id, "blocked", f"Stopped: {exc}")
            self.db.log_event("guard", task_id=task_id, status="blocked", error_code="budget")
            await emit({"type": "response", "task_id": task_id, "text": outcome.text,
                        "display": "pill", "sources": []})
        except Exception as exc:  # surface any failure to the overlay, then re-raise
            self.db.finish_task(task_id, "failed")
            self.db.log_event("task", task_id=task_id, status="failed",
                              error_code=type(exc).__name__)
            await emit({"type": "error", "task_id": task_id, "message": str(exc)[:300]})
            raise
        outcome.elapsed_ms = (time.perf_counter() - start) * 1000
        self.db.finish_task(task_id, outcome.status)
        self.db.log_event("task", task_id=task_id, latency_ms=outcome.elapsed_ms,
                          status=outcome.status)
        return outcome

    async def _run(self, task_id: str, text: str, emit: Emit, approve: Approve,
                   context: dict[str, str] | None, start: float) -> TaskOutcome:
        # Sense
        t0 = time.perf_counter()
        ctx = dict(context if context is not None else await self.sense())
        ctx.setdefault("now", datetime.now().astimezone().isoformat(timespec="minutes"))
        ctx.setdefault("location", describe_location(self.location))
        ctx.setdefault("worker_capacity", str(self.worker_capacity))
        self.db.log_event("sense", task_id=task_id, latency_ms=_ms(t0))

        # Fast path for small talk
        self.guard.check()
        triage = await self.llm.triage(text, ctx)
        if triage.small_talk and triage.reply:
            await emit({"type": "response", "task_id": task_id, "text": triage.reply,
                        "display": "pill", "sources": [], "elapsed_ms": _ms(start),
                        "paths": ["fast"]})
            return TaskOutcome(task_id, "done", triage.reply)

        # Plan, then validate before anything runs
        plan = await self.llm.plan(text, ctx)
        problems = [] if plan.unsupported else self._problems(plan)
        if problems:
            self.db.log_event("plan", task_id=task_id, status="invalid",
                              error_code=problems[0][:120])
            plan = await self.llm.plan(text, {**ctx, "plan_problems": "\n".join(problems)})
            problems = [] if plan.unsupported else self._problems(plan)
        if plan.unsupported or problems or not plan.milestones:
            reason = (plan.unsupported
                      or (f"I can't do that yet: {problems[0].split(': ', 1)[-1]}" if problems
                          else "I couldn't work out how to do that."))
            self.db.log_event("plan", task_id=task_id, status="unsupported")
            await emit({"type": "response", "task_id": task_id, "text": reason,
                        "display": "pill", "sources": [], "elapsed_ms": _ms(start),
                        "paths": []})
            return TaskOutcome(task_id, "unsupported", reason)
        layers = dependency_groups(plan)
        for m in plan.milestones:
            self.db.create_subtask(task_id, f"{task_id}:{m.id}", m.kind, m.goal)
        self.db.log_event("plan", task_id=task_id, status="ok")
        await emit({"type": "ack", "task_id": task_id, "steps": len(plan.milestones),
                    "text": plan.summary or f"{len(plan.milestones)} steps"})

        # Gate
        if needs_approval(plan):
            await emit({"type": "plan", "task_id": task_id, "summary": plan.summary,
                        "milestones": [_card(m) for m in plan.milestones]})
            if not await approve(task_id, plan):
                self.db.log_event("gate", task_id=task_id, status="cancelled")
                await emit({"type": "response", "task_id": task_id, "text": "Cancelled.",
                            "display": "pill", "sources": []})
                return TaskOutcome(task_id, "cancelled", "Cancelled.")
            self.db.log_event("gate", task_id=task_id, status="approved")

        # Act + Verify
        results = await self._act(task_id, text, ctx, layers, emit)

        # Aggregate, then pull the results up where the user can act on them
        self.guard.check()
        t0 = time.perf_counter()
        answer = clean_answer(
            await self.llm.synthesize(text, results, location=ctx.get("location", "")), results)
        self.db.log_event("aggregate", task_id=task_id, latency_ms=_ms(t0))
        opened = None
        if should_open(self.open_results, text):
            opened = await self._open_results(task_id, answer, results, emit)
        files = [f for r in results for f in r.data.get("files", [])]
        if files:
            await self._reveal_files(task_id, results, emit)
        sources = list({s.url: s.model_dump() for r in results if r.ok for s in r.sources}.values())
        paths = sorted({r.path for r in results})
        await emit({"type": "response", "task_id": task_id, "text": answer.to_markdown(),
                    "answer": answer.model_dump(), "opened": opened, "display": "card",
                    "files": files, "folder": _common_folder(results),
                    "sources": sources, "elapsed_ms": _ms(start), "paths": paths,
                    "tiers": {r.milestone_id: r.tier for r in results if r.path != "mac"}})
        status = "done" if all(r.ok for r in results) else "partial"
        return TaskOutcome(task_id, status, answer.headline, results, answer=answer)

    async def _reveal_files(self, task_id: str, results: list[MilestoneResult],
                            emit: Emit) -> None:
        """Pull up what the workers made: the task's folder in Finder."""
        folder = _common_folder(results)
        if not self.reveal or not folder:
            return
        await emit({"type": "doing", "task_id": task_id, "label": "Showing the files",
                    "path": "mac"})
        try:
            await self.reveal(folder)
            self.db.log_event("files.reveal", task_id=task_id, path="mac")
        except mac.MacToolError as exc:
            self.db.log_event("files.reveal", task_id=task_id, path="mac", status="failed",
                              error_code=str(exc)[:120])

    async def _open_results(self, task_id: str, answer: Answer,
                            results: list[MilestoneResult], emit: Emit) -> str | None:
        """Open the answer's URLs as browser tabs, unless the plan already opened pages."""
        already = any(r.path == "mac" and r.ok and r.data.get("tool") in mac.BROWSER_TOOLS
                      for r in results)
        if not self.browser or not answer.open_urls or already:
            return None
        await emit({"type": "doing", "task_id": task_id, "label": "Opening results",
                    "path": "mac"})
        t0 = time.perf_counter()
        try:
            opened = await self.browser(answer.open_urls)
        except mac.MacToolError as exc:
            self.db.log_event("browser.open", task_id=task_id, path="mac", status="failed",
                              error_code=str(exc)[:120])
            return None
        self.db.log_event("browser.open", task_id=task_id, path="mac", latency_ms=_ms(t0))
        return opened

    async def _act(self, task_id: str, text: str, ctx: dict[str, str],
                   layers: list[list[Milestone]], emit: Emit) -> list[MilestoneResult]:
        results: dict[str, MilestoneResult] = {}
        replans = 0
        pending = list(layers)
        while pending:
            layer = pending.pop(0)
            parallel = [m for m in layer if m.kind != "local"]
            local = [m for m in layer if m.kind == "local"]
            outs = list(await asyncio.gather(
                *(self._run_verified(task_id, m, emit, results) for m in parallel)
            ))
            # Local steps share the Mac's UI state, so they run one at a time.
            for m in local:
                outs.append(await self._run_verified(task_id, m, emit, results))
            for r in outs:
                results[r.milestone_id] = r

            failed = [r for r in outs if not r.ok]
            if failed and pending and replans < self.max_replans:
                replans += 1
                self.guard.check()
                why = "; ".join(f"{r.milestone_id}: {r.error}" for r in failed)
                await emit({"type": "doing", "task_id": task_id, "label": "Replanning",
                            "path": "plan"})
                new_plan = await self.llm.plan(text, ctx, prior=list(results.values()),
                                               failed=why)
                # Re-id to avoid colliding with milestones already run.
                prefix = f"r{replans}_"
                for m in new_plan.milestones:
                    m.id = prefix + m.id
                    m.depends_on = [prefix + d for d in m.depends_on]
                    self.db.create_subtask(task_id, f"{task_id}:{m.id}", m.kind, m.goal)
                bad = new_plan.unsupported or self._problems(new_plan)
                pending = [] if bad else dependency_groups(new_plan)
                self.db.log_event("replan", task_id=task_id, status="invalid" if bad else "ok")
        return list(results.values())

    def _problems(self, plan: Plan) -> list[str]:
        try:
            dependency_groups(plan)
        except PlanError as exc:
            return [f"plan: {exc}"]
        return check_plan(plan, has_workers=self.work is not None)

    async def _run_verified(self, task_id: str, m: Milestone, emit: Emit,
                            done: dict[str, MilestoneResult]) -> MilestoneResult:
        """Run once, verify, retry once on failure."""
        upstream = [done[d] for d in m.depends_on if d in done]
        result = await self._run_one(task_id, m, emit, upstream)
        passed, reason = verifier.check(m, result)
        retryable = result.error not in PERMANENT_ERRORS
        if not passed and retryable and m.kind != "read":  # reads already escalated
            await emit({"type": "doing", "task_id": task_id, "milestone_id": m.id,
                        "label": f"Retrying: {m.goal}", "path": result.path})
            result = await self._run_one(task_id, m, emit, upstream)
            passed, reason = verifier.check(m, result)
        if not passed:
            result.ok = False
            result.error = result.error or reason
        await emit({"type": "milestone_done", "task_id": task_id, "milestone_id": m.id,
                    "ok": result.ok, "path": result.path})
        return result

    async def _run_one(self, task_id: str, m: Milestone, emit: Emit,
                       upstream: list[MilestoneResult]) -> MilestoneResult:
        path = path_for(m)
        sub_id = f"{task_id}:{m.id}"
        await emit({"type": "doing", "task_id": task_id, "milestone_id": m.id,
                    "label": m.goal, "path": path})
        self.db.update_subtask(sub_id, path=path, status="running")
        try:
            if path == "nimble":
                self.guard.check()

                async def on_escalate(a: str, b: str, why: str) -> None:
                    self.db.log_event("escalate", task_id=task_id, subtask_id=sub_id, path=b,
                                      status="escalated", error_code=why)
                    await emit({"type": "doing", "task_id": task_id, "milestone_id": m.id,
                                "label": f"{a} came back {why}; trying {b}", "path": b})

                result = await self.router.read(m, on_escalate=on_escalate)
            elif path == "worker":
                if self.work is None:
                    result = _failed(m, "worker", "no_worker_configured")
                else:
                    self.guard.check()
                    result = await self.work(m, task_id, emit, upstream)
            elif path == "mac":
                result = await self._mac(m)
            elif path == "watcher":
                result = (await self.watch(m, task_id) if self.watch
                          else _failed(m, "watcher", "no_watcher_registry"))
            else:
                result = _failed(m, "none", f"unknown path {path}")
        except BudgetExceeded:
            raise
        except Exception as exc:
            result = _failed(m, path, type(exc).__name__, str(exc))

        self.db.update_subtask(
            sub_id, status="done" if result.ok else "failed",
            worker_id=result.data.get("worker_id"), session_id=result.data.get("session_id"),
            response_id=result.data.get("response_id"),
        )
        self.db.log_event(
            f"milestone.{m.kind}", task_id=task_id, subtask_id=sub_id, path=result.path,
            latency_ms=result.latency_ms, cost_usd=result.cost_usd,
            source={"nimble": "nimble", "worker": "agent37"}.get(result.path),
            status="ok" if result.ok else "failed", error_code=result.error,
        )
        return result

    async def _mac(self, m: Milestone) -> MilestoneResult:
        t0 = time.perf_counter()
        if not m.tool:
            return _failed(m, "mac", "no_tool")
        value = await self.mac_call(m.tool, m.args)
        data: dict[str, Any] = {"value": value, "tool": m.tool}
        if isinstance(value, dict):  # e.g. capture_screen -> {"image_path", "text"}
            data.update(value)
            text = str(value.get("text", ""))
        else:
            text = value if isinstance(value, str) else str(value)
        return MilestoneResult(milestone_id=m.id, ok=True, path="mac", text=text,
                               data=data, latency_ms=_ms(t0))


def _common_folder(results: list[MilestoneResult]) -> str | None:
    """The task's download folder (the parent of each milestone's folder)."""
    folders = {r.data["folder"] for r in results if r.data.get("folder")}
    if not folders:
        return None
    parents = {str(Path(f).parent) for f in folders}
    return parents.pop() if len(parents) == 1 else sorted(folders)[0]


def _card(m: Milestone) -> dict[str, Any]:
    return {"id": m.id, "goal": m.goal, "kind": m.kind, "tool": m.tool, "args": m.args,
            "path": path_for(m)}


def _failed(m: Milestone, path: str, code: str, detail: str = "") -> MilestoneResult:
    return MilestoneResult(milestone_id=m.id, ok=False, path=path, error=code,  # type: ignore[arg-type]
                           text=detail)


def _ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000
