from __future__ import annotations

import asyncio
from typing import Any

import pytest

from orchestrator.db import Database
from orchestrator.loop import Agent, PlanError, dependency_groups, needs_approval
from orchestrator.models import Milestone, MilestoneResult, Plan, Source
from orchestrator.router import ReadRouter, Tier
from orchestrator.security import SpendGuard
from tests.fakes import FakeLLM

LONG = "Fact with enough detail to pass the evidence check for a read milestone. " * 2


def read(mid: str, deps: list[str] | None = None, **kw: Any) -> dict[str, Any]:
    return {"id": mid, "goal": f"read {mid}", "kind": "read", "query": mid,
            "depends_on": deps or [], **kw}


class Recorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def __call__(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]


async def no_context() -> dict[str, str]:
    return {}


async def always_approve(task_id: str, plan: Plan) -> bool:
    return True


def make_agent(db: Database, llm: FakeLLM, reader=None, **kw: Any) -> Agent:
    async def default_reader(m: Milestone) -> MilestoneResult:
        return MilestoneResult(milestone_id=m.id, ok=True, path="nimble", text=LONG,
                               sources=[Source(url=f"https://{m.id}.example")])

    router = ReadRouter([Tier("nimble", reader or default_reader)])
    return Agent(db, llm, router, SpendGuard(db, daily_cap_usd=100), sense=no_context, **kw)


def test_dependency_groups_layers() -> None:
    plan = Plan.model_validate({"milestones": [read("a"), read("b"), read("c", ["a", "b"])]})
    assert [[m.id for m in layer] for layer in dependency_groups(plan)] == [["a", "b"], ["c"]]


def test_dependency_groups_rejects_cycles_and_unknown() -> None:
    with pytest.raises(PlanError, match="cycle"):
        dependency_groups(Plan.model_validate({"milestones": [read("a", ["b"]),
                                                              read("b", ["a"])]}))
    with pytest.raises(PlanError, match="unknown"):
        dependency_groups(Plan.model_validate({"milestones": [read("a", ["zzz"])]}))


def test_needs_approval_only_for_side_effects() -> None:
    reads = Plan.model_validate({"milestones": [read("a")]})
    ctx = Plan.model_validate({"milestones": [
        {"id": "m", "goal": "g", "kind": "local", "tool": "get_context"}]})
    remind = Plan.model_validate({"milestones": [
        {"id": "m", "goal": "g", "kind": "local", "tool": "create_reminder"}]})
    assert not needs_approval(reads) and not needs_approval(ctx) and needs_approval(remind)


async def test_small_talk_skips_planner(db: Database) -> None:
    llm = FakeLLM(small_talk="Hey!")
    emit = Recorder()
    out = await make_agent(db, llm).handle("hi", emit, always_approve)
    assert out.text == "Hey!" and llm.plan_calls == []
    assert emit.types() == ["thinking", "response"]


async def test_independent_reads_run_in_parallel(db: Database) -> None:
    running = 0
    peak = 0

    async def slow_reader(m: Milestone) -> MilestoneResult:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return MilestoneResult(milestone_id=m.id, ok=True, path="nimble", text=LONG,
                               sources=[Source(url="https://x.example")])

    llm = FakeLLM([{"summary": "compare", "milestones": [read("a"), read("b"), read("c")]}])
    emit = Recorder()
    out = await make_agent(db, llm, slow_reader).handle("compare", emit, always_approve)
    assert out.status == "done" and peak == 3
    resp = emit.events[-1]
    assert resp["display"] == "card" and resp["paths"] == ["nimble"]
    rows = db.all("SELECT event FROM task_events WHERE task_id = ?", (out.task_id,))
    assert [r["event"] for r in rows].count("milestone.read") == 3


async def test_local_steps_are_sequential_and_gated(db: Database) -> None:
    order: list[str] = []

    async def mac_call(tool: str, args: dict[str, Any]) -> str:
        order.append(f"start {args['title']}")
        await asyncio.sleep(0.01)
        order.append(f"end {args['title']}")
        return "ok"

    plan = {"summary": "two reminders", "milestones": [
        {"id": "a", "goal": "r1", "kind": "local", "tool": "create_reminder",
         "args": {"title": "1"}},
        {"id": "b", "goal": "r2", "kind": "local", "tool": "create_reminder",
         "args": {"title": "2"}},
    ]}
    approvals: list[str] = []

    async def approve(task_id: str, p: Plan) -> bool:
        approvals.append(task_id)
        return True

    emit = Recorder()
    out = await make_agent(db, FakeLLM([plan]), mac_call=mac_call).handle("x", emit, approve)
    assert out.status == "done" and len(approvals) == 1
    assert order == ["start 1", "end 1", "start 2", "end 2"]
    assert "plan" in emit.types()


async def test_cancelled_plan_does_nothing(db: Database) -> None:
    called = False

    async def mac_call(tool: str, args: dict[str, Any]) -> str:
        nonlocal called
        called = True
        return "ok"

    async def deny(task_id: str, p: Plan) -> bool:
        return False

    plan = {"milestones": [{"id": "a", "goal": "g", "kind": "local", "tool": "speak",
                            "args": {"text": "hi"}}]}
    out = await make_agent(db, FakeLLM([plan]), mac_call=mac_call).handle("x", Recorder(), deny)
    assert out.status == "cancelled" and not called
    assert db.one("SELECT status FROM tasks WHERE id = ?", (out.task_id,))["status"] == "cancelled"


async def test_failed_group_triggers_one_replan(db: Database) -> None:
    async def flaky(m: Milestone) -> MilestoneResult:
        if m.id == "a":
            return MilestoneResult(milestone_id=m.id, ok=False, path="nimble",
                                   error="empty_result")
        return MilestoneResult(milestone_id=m.id, ok=True, path="nimble", text=LONG,
                               sources=[Source(url="https://x.example")])

    llm = FakeLLM([
        {"milestones": [read("a"), read("b", ["a"])]},
        {"milestones": [read("alt")]},
    ])
    out = await make_agent(db, llm, flaky).handle("x", Recorder(), always_approve)
    assert len(llm.plan_calls) == 2
    assert "a: empty_result" in llm.plan_calls[1]["failed"]
    ids = {r.milestone_id for r in out.results}
    assert ids == {"a", "r1_alt"}  # b was replaced by the new plan
    assert out.status == "partial"


async def test_work_milestone_without_workers_fails_gracefully(db: Database) -> None:
    llm = FakeLLM([{"milestones": [{"id": "w", "goal": "log in", "kind": "work",
                                    "needs_login": True}]}])
    out = await make_agent(db, llm).handle("x", Recorder(), always_approve)
    assert out.results[0].error == "no_worker_configured"


async def test_budget_block_is_reported(db: Database) -> None:
    db.log_event("worker.turn", cost_usd=5)
    agent = make_agent(db, FakeLLM())
    agent.guard.daily_cap_usd = 1
    emit = Recorder()
    out = await agent.handle("x", emit, always_approve)
    assert out.status == "blocked" and "daily cap" in emit.events[-1]["text"]
