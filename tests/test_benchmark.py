from __future__ import annotations

import asyncio

from orchestrator.benchmark import PathStats, Run, format_table, run_benchmark
from orchestrator.db import Database
from orchestrator.models import Milestone, MilestoneResult, Source

LONG = "Detailed answer text that is long enough to pass the read evidence check. " * 2
TASKS = [Milestone(id="t1", goal="g", kind="read", query="q")]


async def fast(m: Milestone) -> MilestoneResult:
    return MilestoneResult(milestone_id=m.id, ok=True, path="nimble", text=LONG,
                           sources=[Source(url="https://a")])


async def slow_but_empty(m: Milestone) -> MilestoneResult:
    await asyncio.sleep(0.01)
    return MilestoneResult(milestone_id=m.id, ok=True, path="worker", text="", cost_usd=0.01)


async def test_wrong_or_empty_answers_count_as_failures(db: Database) -> None:
    stats = await run_benchmark(db, {"nimble": fast, "worker": slow_but_empty},
                                tasks=TASKS, repeats=2)
    assert stats["nimble"].success_rate == 1.0
    assert stats["worker"].success_rate == 0.0 and stats["worker"].median is None
    assert abs(stats["worker"].cost - 0.02) < 1e-9
    rows = db.all("SELECT path, status FROM task_events WHERE event = 'bench'")
    assert len(rows) == 4


async def test_exceptions_are_recorded_not_raised(db: Database) -> None:
    async def boom(m: Milestone) -> MilestoneResult:
        raise RuntimeError("down")

    stats = await run_benchmark(db, {"worker": boom}, tasks=TASKS, repeats=1)
    assert stats["worker"].runs[0].error == "RuntimeError"


def test_percentiles_and_table() -> None:
    s = PathStats("nimble", [Run("t", "nimble", x, True) for x in (1.0, 2.0, 3.0, 4.0)])
    assert s.median == 2.5 and s.percentile(0.0) == 1.0 and s.percentile(1.0) == 4.0
    w = PathStats("worker", [Run("t", "worker", 25.0, True)])
    table = format_table({"nimble": s, "worker": w})
    assert "10.0x faster" in table
