"""Evidence checks per milestone (the Verify step).

No screenshot verification: each kind has a cheap, deterministic check.
"""

from __future__ import annotations

import re

from orchestrator.models import Milestone, MilestoneResult

MIN_READ_CHARS = 80
_BLOCKED_PATTERNS = re.compile(
    r"(access denied|are you a robot|captcha|enable javascript|request blocked|"
    r"unusual traffic|403 forbidden)",
    re.IGNORECASE,
)


def looks_blocked(text: str) -> bool:
    """Short pages that are mostly a bot wall count as blocked, not as content."""
    return len(text) < 2000 and bool(_BLOCKED_PATTERNS.search(text))


def check(m: Milestone, r: MilestoneResult) -> tuple[bool, str]:
    """Return (passed, reason)."""
    if not r.ok:
        return False, r.error or "failed"
    if m.kind == "read":
        if len(r.text.strip()) < MIN_READ_CHARS:
            return False, "thin_result"
        if not r.sources:
            return False, "no_sources"
        if looks_blocked(r.text):
            return False, "blocked"
        return True, "ok"
    if m.kind == "work":
        return (bool(r.text.strip()) or bool(r.data)), "ok" if r.text or r.data else "empty_output"
    if m.kind == "local":
        return r.text != "" or "value" in r.data, "ok"
    if m.kind == "watch":
        return "watcher_id" in r.data, "ok" if "watcher_id" in r.data else "no_watcher"
    return False, "unknown_kind"
