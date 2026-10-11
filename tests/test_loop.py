from __future__ import annotations

import asyncio
from typing import Any

import pytest

from orchestrator import mac
from orchestrator.db import Database
from orchestrator.loop import Agent, PlanError, check_plan, dependency_groups, needs_approval
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
    kw.setdefault("browser", None)
    kw.setdefault("reveal", None)
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


def test_display_tools_run_without_approval() -> None:
    for tool in ("open_url", "open_in_browser", "open_app", "speak", "show_notification"):
        plan = Plan.model_validate({"milestones": [
            {"id": "m", "goal": "g", "kind": "local", "tool": tool}]})
        assert not needs_approval(plan), tool


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

    plan = {"milestones": [{"id": "a", "goal": "g", "kind": "local", "tool": "create_note",
                            "args": {"title": "t", "body": "b"}}]}
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


async def test_work_without_workers_is_caught_before_anything_runs(db: Database) -> None:
    llm = FakeLLM([
        {"milestones": [{"id": "w", "goal": "log in to the portal", "kind": "work",
                         "needs_login": True}]},
        {"milestones": [{"id": "w", "goal": "log in to the portal", "kind": "work",
                         "needs_login": True}]},
    ])
    emit = Recorder()
    out = await make_agent(db, llm).handle("x", emit, always_approve)
    assert out.status == "unsupported" and "none is configured" in out.text
    assert "w: needs an Agent37 worker" in llm.plan_calls[1]["problems"]
    assert emit.types() == ["thinking", "response"]


async def test_calendar_request_regression(db: Database) -> None:
    """Issue #37: the planner sent 'create a calendar event' to a remote worker after
    opening Calendar locally. The plan is now rejected before the gate and replanned
    onto the local calendar tool, so nothing runs half way."""
    calls: list[tuple[str, dict]] = []

    async def mac_call(tool: str, args: dict[str, Any]) -> str:
        calls.append((tool, args))
        return "event added"

    bad = {"summary": "calendar", "milestones": [
        {"id": "m1", "goal": "Open the Calendar application.", "kind": "local",
         "tool": "open_app", "args": {"name": "Calendar"}},
        {"id": "m2", "goal": "Create a calendar event at 4:45 PM today titled 'Submit "
                             "pitch for the hackathon'.", "kind": "work", "depends_on": ["m1"]},
    ]}
    good = {"summary": "add the event", "milestones": [
        {"id": "m1", "goal": "Add the event", "kind": "local", "tool": "create_calendar_event",
         "args": {"title": "Submit pitch for the hackathon", "start": "2026-10-11T16:45"}},
    ]}
    llm = FakeLLM([bad, good])
    agent = make_agent(db, llm, mac_call=mac_call,
                       work=lambda m, t, e, up: None)  # workers exist, still must not be used
    out = await agent.handle("create an event at 4:45 today", Recorder(), always_approve)
    assert out.status == "done"
    assert [c[0] for c in calls] == ["create_calendar_event"]  # Calendar was not opened first
    assert "workers are remote computers" in llm.plan_calls[1]["problems"]
    rows = db.all("SELECT status FROM task_events WHERE event = 'plan' AND task_id = ?",
                  (out.task_id,))
    assert [r["status"] for r in rows] == ["invalid", "ok"]


async def test_unsupported_request_runs_nothing(db: Database) -> None:
    called = False

    async def mac_call(tool: str, args: dict[str, Any]) -> str:
        nonlocal called
        called = True
        return "ok"

    approvals: list[str] = []

    async def approve(task_id: str, p: Plan) -> bool:
        approvals.append(task_id)
        return True

    llm = FakeLLM([{"milestones": [], "unsupported": "I can't send texts yet."}])
    emit = Recorder()
    out = await make_agent(db, llm, mac_call=mac_call).handle("text bob hey", emit, approve)
    assert out.status == "unsupported" and out.text == "I can't send texts yet."
    assert not called and not approvals and len(llm.plan_calls) == 1
    assert emit.events[-1]["display"] == "pill"


async def test_bad_tool_args_are_replanned(db: Database) -> None:
    llm = FakeLLM([
        {"milestones": [{"id": "a", "goal": "remind", "kind": "local",
                         "tool": "create_reminder", "args": {}}]},
        {"milestones": [{"id": "a", "goal": "remind", "kind": "local",
                         "tool": "create_reminder", "args": {"title": "call mom"}}]},
    ])

    async def mac_call(tool: str, args: dict[str, Any]) -> str:
        return "ok"

    out = await make_agent(db, llm, mac_call=mac_call).handle("x", Recorder(), always_approve)
    assert out.status == "done" and len(llm.plan_calls) == 2


async def test_permanent_errors_are_not_retried(db: Database) -> None:
    attempts = 0

    async def mac_call(tool: str, args: dict[str, Any]) -> str:
        nonlocal attempts
        attempts += 1
        raise mac.MacToolError("macOS blocked pip from controlling Calendar")

    llm = FakeLLM([{"milestones": [{"id": "a", "goal": "g", "kind": "local", "tool": "speak",
                                    "args": {"text": "hi"}}]}])
    out = await make_agent(db, llm, mac_call=mac_call).handle("x", Recorder(), always_approve)
    assert attempts == 1 and out.results[0].error == "MacToolError"


def test_check_plan_flags_mac_work() -> None:
    plan = Plan.model_validate({"milestones": [
        {"id": "m", "goal": "add a reminder to my Reminders", "kind": "work"}]})
    problems = check_plan(plan, has_workers=True)
    assert problems and "remote computers" in problems[0]


async def test_planner_gets_current_time(db: Database) -> None:
    seen: dict[str, str] = {}
    llm = FakeLLM([{"milestones": [read("a")]}])
    original = llm.plan

    async def spy(text, context, **kw):
        seen.update(context)
        return await original(text, context, **kw)

    llm.plan = spy  # type: ignore[method-assign]
    await make_agent(db, llm).handle("x", Recorder(), always_approve)
    assert seen["now"][:4].isdigit() and "T" in seen["now"]


async def test_budget_block_is_reported(db: Database) -> None:
    db.log_event("worker.turn", cost_usd=5)
    agent = make_agent(db, FakeLLM())
    agent.guard.daily_cap_usd = 1
    emit = Recorder()
    out = await agent.handle("x", emit, always_approve)
    assert out.status == "blocked" and "daily cap" in emit.events[-1]["text"]


async def test_results_are_pulled_up_in_the_browser(db: Database) -> None:
    opened: list[list[str]] = []

    async def browser(urls: list[str]) -> str:
        opened.append(urls)
        return "opened 2 tabs in Google Chrome"

    llm = FakeLLM([{"milestones": [read("a")]}],
                  open_urls=["https://a.example/deal", "https://www.google.com/travel/flights?q=x",
                             "javascript:alert(1)", "https://evil.example/phish"])
    emit = Recorder()
    out = await make_agent(db, llm, browser=browser).handle("cheap flights", emit, always_approve)
    assert opened == [["https://a.example/deal", "https://www.google.com/travel/flights?q=x"]]
    resp = emit.events[-1]
    assert resp["opened"] == "opened 2 tabs in Google Chrome"
    assert resp["answer"]["headline"] == out.text
    assert any(e.get("label") == "Opening results" for e in emit.events)


async def test_browser_failure_does_not_fail_the_task(db: Database) -> None:
    async def browser(urls: list[str]) -> str:
        raise mac.MacToolError("macOS blocked pip from controlling Google Chrome")

    llm = FakeLLM([{"milestones": [read("a")]}], open_urls=["https://a.example/x"])
    out = await make_agent(db, llm, browser=browser).handle("x", Recorder(), always_approve)
    assert out.status == "done"
    row = db.one("SELECT status FROM task_events WHERE event = 'browser.open'")
    assert row["status"] == "failed"


async def test_no_double_open_when_plan_already_opened_pages(db: Database) -> None:
    opened: list[list[str]] = []

    async def browser(urls: list[str]) -> str:
        opened.append(urls)
        return "opened"

    async def mac_call(tool: str, args: dict[str, Any]) -> str:
        return "opened 1 tab"

    llm = FakeLLM([{"milestones": [{"id": "o", "goal": "pull up", "kind": "local",
                                    "tool": "open_url", "args": {"url": "https://a.example"}}]}],
                  open_urls=["https://a.example"])
    await make_agent(db, llm, browser=browser, mac_call=mac_call).handle(
        "pull up a.example", Recorder(), always_approve)
    assert opened == []


async def test_complex_task_fans_out_to_workers_and_merges(db: Database) -> None:
    """Issue #46: independent pieces run on workers at the same time, and the merge
    step receives every piece's result."""
    running = 0
    peak = 0
    merge_upstream: list[MilestoneResult] = []

    async def work(m: Milestone, task_id: str, emit, upstream: list[MilestoneResult]):
        nonlocal running, peak
        if m.id == "merge":
            merge_upstream.extend(upstream)
            return MilestoneResult(milestone_id=m.id, ok=True, path="worker",
                                   text="spreadsheet built", data={"worker_id": "w1"})
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return MilestoneResult(milestone_id=m.id, ok=True, path="worker",
                               text=f"findings for {m.id}", data={"worker_id": "w1"})

    pieces = [{"id": f"p{i}", "goal": f"Research company {i} in depth", "kind": "work",
               "estimated_seconds": 120} for i in range(4)]
    merge = {"id": "merge", "goal": "Build a comparison spreadsheet", "kind": "work",
             "writes_file": True, "depends_on": [p["id"] for p in pieces]}
    llm = FakeLLM([{"summary": "research 4 companies", "milestones": [*pieces, merge]}])
    seen_ctx: dict[str, str] = {}
    original = llm.plan

    async def spy(text, context, **kw):
        seen_ctx.update(context)
        return await original(text, context, **kw)

    llm.plan = spy  # type: ignore[method-assign]
    emit = Recorder()
    agent = make_agent(db, llm, work=work, worker_capacity=4)
    out = await agent.handle("compare 4 companies into a spreadsheet", emit, always_approve)

    assert out.status == "done" and peak == 4
    assert seen_ctx["worker_capacity"] == "4"
    assert sorted(r.milestone_id for r in merge_upstream) == ["p0", "p1", "p2", "p3"]
    done = [e for e in emit.events if e["type"] == "milestone_done"]
    assert len(done) == 5 and all(e["ok"] for e in done)


def test_too_many_worker_pieces_is_rejected() -> None:
    plan = Plan.model_validate({"milestones": [
        {"id": f"p{i}", "goal": f"research item {i}", "kind": "work"} for i in range(13)]})
    assert "too many" in check_plan(plan, has_workers=True)[0]


async def test_no_workers_means_zero_capacity(db: Database) -> None:
    seen: dict[str, str] = {}
    llm = FakeLLM([{"milestones": [read("a")]}])
    original = llm.plan

    async def spy(text, context, **kw):
        seen.update(context)
        return await original(text, context, **kw)

    llm.plan = spy  # type: ignore[method-assign]
    await make_agent(db, llm, worker_capacity=8).handle("x", Recorder(), always_approve)
    assert seen["worker_capacity"] == "0"


async def test_screenshot_result_carries_image_path(db: Database) -> None:
    async def mac_call(tool: str, args: dict[str, Any]):
        return {"image_path": "/tmp/screen.png", "text": "Captured the screen."}

    llm = FakeLLM([{"milestones": [{"id": "s", "goal": "look", "kind": "local",
                                    "tool": "capture_screen"}]}])
    out = await make_agent(db, llm, mac_call=mac_call).handle(
        "what's on my screen", Recorder(), always_approve)
    result = llm.synth_inputs[0][0]
    assert out.status == "done" and result.data["image_path"] == "/tmp/screen.png"
    assert result.text == "Captured the screen."


async def test_worker_files_are_revealed_and_reported(db: Database, tmp_path) -> None:
    revealed: list[str] = []

    async def reveal(path: str) -> str:
        revealed.append(path)
        return "revealed"

    folder = tmp_path / "task_x"

    async def work(m, task_id, emit, upstream):
        return MilestoneResult(milestone_id=m.id, ok=True, path="worker", text="done",
                               data={"files": [str(folder / m.id / "out.csv")],
                                     "folder": str(folder / m.id)})

    llm = FakeLLM([{"milestones": [
        {"id": "a", "goal": "build part a", "kind": "work", "writes_file": True},
        {"id": "b", "goal": "build part b", "kind": "work", "writes_file": True}]}])
    emit = Recorder()
    await make_agent(db, llm, work=work, reveal=reveal, worker_capacity=2).handle(
        "make two files", emit, always_approve)
    assert revealed == [str(folder)]
    assert len(emit.events[-1]["files"]) == 2 and emit.events[-1]["folder"] == str(folder)
