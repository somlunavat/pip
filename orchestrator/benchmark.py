"""Benchmark: the same read task on the Nimble fast path vs a Hermes worker's browser.

Same infrastructure, same prompt; only the access method differs. A wrong or empty
answer counts as a failure on its path, so speed is not bought with quality. Raw
rows go to task_events so the table traces back to its numbers.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from orchestrator import verifier
from orchestrator.db import Database
from orchestrator.models import Milestone, MilestoneResult

TASKS: list[Milestone] = [
    Milestone(id="b1", kind="read", goal="Find the three cheapest round-trip flights from "
              "Austin to New York next month", query="cheapest flights Austin to New York"),
    Milestone(id="b2", kind="read", goal="Compare the current price of the Sony WH-1000XM5 "
              "across major retailers", query="Sony WH-1000XM5 price"),
    Milestone(id="b3", kind="read", goal="List three studio apartments for rent near UT "
              "Austin with their monthly rent", query="studio apartment for rent near UT Austin"),
    Milestone(id="b4", kind="read", goal="Get today's top three stories on Hacker News",
              url="https://news.ycombinator.com"),
    Milestone(id="b5", kind="read", goal="Find the release date and price of the newest "
              "MacBook Air", query="newest MacBook Air release date price"),
]

PathFn = Callable[[Milestone], Awaitable[MilestoneResult]]


@dataclass
class Run:
    task_id: str
    path: str
    seconds: float
    ok: bool
    error: str | None = None
    cost_usd: float | None = None


@dataclass
class PathStats:
    path: str
    runs: list[Run] = field(default_factory=list)

    @property
    def ok_times(self) -> list[float]:
        return [r.seconds for r in self.runs if r.ok]

    @property
    def success_rate(self) -> float:
        return sum(r.ok for r in self.runs) / len(self.runs) if self.runs else 0.0

    @property
    def median(self) -> float | None:
        return statistics.median(self.ok_times) if self.ok_times else None

    def percentile(self, p: float) -> float | None:
        times = sorted(self.ok_times)
        if not times:
            return None
        k = (len(times) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(times) - 1)
        return times[lo] + (times[hi] - times[lo]) * (k - lo)

    @property
    def cost(self) -> float:
        return sum(r.cost_usd or 0 for r in self.runs)


async def run_benchmark(
    db: Database,
    paths: dict[str, PathFn],
    *,
    tasks: list[Milestone] = TASKS,
    repeats: int = 3,
    on_run: Callable[[Run], None] | None = None,
) -> dict[str, PathStats]:
    """Run each task on each path `repeats` times, back to back (interleaved)."""
    stats = {name: PathStats(name) for name in paths}
    for task in tasks:
        for _ in range(repeats):
            for name, fn in paths.items():
                start = time.perf_counter()
                try:
                    result = await fn(task)
                    ok, reason = verifier.check(task, result)
                    error = None if ok else (result.error or reason)
                    cost = result.cost_usd
                except Exception as exc:
                    ok, error, cost = False, type(exc).__name__, None
                seconds = time.perf_counter() - start
                run = Run(task.id, name, seconds, ok, error, cost)
                stats[name].runs.append(run)
                db.log_event("bench", task_id=f"bench:{task.id}", path=name,
                             latency_ms=seconds * 1000, cost_usd=cost,
                             status="ok" if ok else "failed", error_code=error)
                if on_run:
                    on_run(run)
    return stats


def format_table(stats: dict[str, PathStats]) -> str:
    def fmt(x: float | None) -> str:
        return f"{x:6.1f}s" if x is not None else "    —  "

    lines = [
        f"{'path':<10} {'runs':>4} {'success':>8} {'median':>8} {'p10':>8} {'p90':>8} {'cost':>8}",
        "-" * 60,
    ]
    for s in stats.values():
        lines.append(
            f"{s.path:<10} {len(s.runs):>4} {s.success_rate:>7.0%} {fmt(s.median):>8} "
            f"{fmt(s.percentile(0.1)):>8} {fmt(s.percentile(0.9)):>8} {s.cost:>7.3f}$"
        )
    medians = {k: v.median for k, v in stats.items() if v.median}
    if "nimble" in medians and "worker" in medians:
        lines.append(f"\nNimble median is {medians['worker'] / medians['nimble']:.1f}x faster "
                     "than the worker's headless browser (successful runs only).")
    return "\n".join(lines)
