"""Routing: which path a milestone takes, and the tiered fallback for reads.

Default to the fast path. A subtask goes to a worker only if it needs a login,
submits a form, writes a file, runs code, or is estimated over ~60 s. A read that
comes back empty, blocked, or fails the evidence check escalates one tier.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from orchestrator import verifier
from orchestrator.models import Milestone, MilestoneResult

WORKER_SECONDS_THRESHOLD = 60

ReadFn = Callable[[Milestone], Awaitable[MilestoneResult]]


def needs_worker(m: Milestone) -> bool:
    return (
        m.needs_login
        or m.submits_form
        or m.writes_file
        or m.runs_code
        or m.estimated_seconds > WORKER_SECONDS_THRESHOLD
    )


def path_for(m: Milestone) -> str:
    if m.kind == "local":
        return "mac"
    if m.kind == "watch":
        return "watcher"
    if m.kind == "work" or needs_worker(m):
        return "worker"
    return "nimble"


@dataclass
class Tier:
    name: str
    fn: ReadFn


class ReadRouter:
    """Tries each tier in order and returns the first result that passes the check.

    Every result records the tier that produced it so the benchmark can report it.
    The tier list is plain data; the Chrome extension tier is an append.
    """

    def __init__(self, tiers: list[Tier]):
        if not tiers:
            raise ValueError("at least one tier is required")
        self.tiers = tiers

    async def read(
        self,
        m: Milestone,
        *,
        on_escalate: Callable[[str, str, str], Awaitable[None]] | None = None,
        start_tier: int = 0,
    ) -> MilestoneResult:
        last: MilestoneResult | None = None
        for index, tier in enumerate(self.tiers[start_tier:], start=start_tier + 1):
            result = await tier.fn(m)
            result.tier = index
            passed, reason = verifier.check(m, result)
            if passed:
                return result
            result.ok = False
            result.error = result.error or reason
            last = result
            nxt = self.tiers[index].name if index < len(self.tiers) else None
            if nxt and on_escalate:
                await on_escalate(tier.name, nxt, reason)
        assert last is not None
        return last
