"""Watchers: embed what changed on a page, not the page.

Loop per watcher, on a timer inside the orchestrator:
1. Fetch the target through Nimble and extract structured fields with Gemini.
2. Compare with the last snapshot.
3. If fields differ, write a short change description ("rent 2400 to 2200 (down)").
4. Embed the description and store it with the raw diff beside it.
5. Score against the preference vector; at or above the threshold, alert.

No embedding call when nothing changed. Watcher frequency is capped.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from orchestrator.db import Database, new_id, utcnow
from orchestrator.llm.base import LLM
from orchestrator.models import Milestone, MilestoneResult, WatchSpec
from orchestrator.nimble import NimbleClient
from orchestrator.preferences import Preferences
from orchestrator.security import BudgetExceeded, SpendGuard
from orchestrator.vectors import cosine_many, from_blob, to_blob

log = logging.getLogger("pip.watchers")

MIN_INTERVAL_S = 60
TICK_S = 15
Alert = Callable[[dict[str, Any]], Awaitable[None]]


def content_hash(fields: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").replace("$", "").strip())
        except ValueError:
            return None
    return None


def diff_fields(old: dict[str, Any], new: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key in sorted(set(old) | set(new)):
        before, after = old.get(key), new.get(key)
        if before != after:
            out[key] = {"from": before, "to": after}
    return out


def describe_change(diff: dict[str, dict[str, Any]], unchanged: list[str]) -> str:
    """Short, embeddable description. Numeric moves get a direction word."""
    parts: list[str] = []
    for key, d in diff.items():
        label = key.replace("_", " ")
        before, after = d["from"], d["to"]
        if before is None:
            parts.append(f"{label} added: {after}")
        elif after is None:
            parts.append(f"{label} removed (was {before})")
        else:
            a, b = _num(before), _num(after)
            direction = ""
            if a is not None and b is not None and a != b:
                pct = f", {abs(b - a) / abs(a) * 100:.0f}%" if a else ""
                direction = f" ({'down' if b < a else 'up'}{pct})"
            parts.append(f"{label} {before} to {after}{direction}")
    if unchanged:
        parts.append(", ".join(k.replace("_", " ") for k in unchanged[:3]) + " unchanged")
    return "; ".join(parts)


class WatcherService:
    def __init__(self, db: Database, llm: LLM, nimble: NimbleClient,
                 prefs: Preferences, alert: Alert | None = None,
                 guard: SpendGuard | None = None):
        self.db = db
        self.llm = llm
        self.nimble = nimble
        self.prefs = prefs
        self.alert = alert
        self.guard = guard
        self._task: asyncio.Task[None] | None = None

    # -- registry --------------------------------------------------------------

    async def add(self, spec: WatchSpec) -> tuple[str, dict[str, Any]]:
        interval = max(spec.interval_s, MIN_INTERVAL_S)
        watcher_id = self.db.create_watcher(spec.target, spec.extractor, interval, spec.threshold)
        fields = await self._fetch_fields(spec.target, spec.extractor)
        self.db.add_snapshot(watcher_id, fields, content_hash(fields))
        return watcher_id, fields

    async def as_milestone(self, m: Milestone, task_id: str) -> MilestoneResult:
        """Handler for `watch` milestones from the loop."""
        start = time.perf_counter()
        if m.watch is None:
            return MilestoneResult(milestone_id=m.id, ok=False, path="watcher",
                                   error="no_watch_spec")
        watcher_id, fields = await self.add(m.watch)
        summary = ", ".join(f"{k}: {v}" for k, v in fields.items()) or "no fields yet"
        return MilestoneResult(
            milestone_id=m.id, ok=True, path="watcher",
            text=f"Watching {m.watch.target} every {max(m.watch.interval_s, MIN_INTERVAL_S)}s. "
                 f"Current: {summary}",
            data={"watcher_id": watcher_id, "fields": fields},
            latency_ms=(time.perf_counter() - start) * 1000,
        )

    # -- one check ----------------------------------------------------------------

    async def _fetch_fields(self, target: str, extractor: str) -> dict[str, Any]:
        page = await self.nimble.extract(target)
        self.db.log_event("watcher.fetch", path="nimble", source="nimble")
        return await self.llm.extract_fields(page.markdown, extractor, target)

    async def check(self, watcher: Any) -> dict[str, Any] | None:
        """Fetch, diff, and (if changed) embed, score, store, and maybe alert."""
        wid = watcher["id"]
        fields = await self._fetch_fields(watcher["target"], watcher["extractor"])
        digest = content_hash(fields)
        last = self.db.last_snapshot(wid)
        self.db.add_snapshot(wid, fields, digest)
        if last is None or last["content_hash"] == digest:
            return None  # nothing changed: no embedding call

        old = json.loads(last["fields"])
        diff = diff_fields(old, fields)
        unchanged = [k for k in fields if k not in diff]
        description = describe_change(diff, unchanged)
        return await self.record_change(watcher, description, diff)

    async def record_change(self, watcher: Any, description: str,
                            diff: dict[str, Any]) -> dict[str, Any]:
        embedding = await self.llm.embed(description)
        score = self.prefs.score(embedding)
        should_alert = score is None or score >= watcher["threshold"]
        change_id = new_id("chg")
        self.db.execute(
            """INSERT INTO change_events
               (id, watcher_id, ts, description, diff, embedding, score, alerted)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (change_id, watcher["id"], utcnow(), description, json.dumps(diff, default=str),
             to_blob(embedding), score, int(should_alert)),
        )
        change = {"type": "alert", "change_id": change_id, "watcher_id": watcher["id"],
                  "target": watcher["target"], "description": description, "diff": diff,
                  "score": score, "cold_start": score is None}
        self.db.log_event("watcher.change", status="alerted" if should_alert else "suppressed")
        if should_alert and self.alert:
            await self.alert(change)
        return change

    # -- scheduler -----------------------------------------------------------------

    def due(self, now: datetime | None = None) -> list[Any]:
        now = now or datetime.now(UTC)
        out = []
        for w in self.db.active_watchers():
            last = self.db.last_snapshot(w["id"])
            if last is None or datetime.fromisoformat(last["ts"]) + timedelta(
                seconds=w["interval_s"]
            ) <= now:
                out.append(w)
        return out

    async def tick(self) -> None:
        if self.guard:
            try:
                self.guard.check()
            except BudgetExceeded:
                return  # paused until the cap resets or the kill switch is off
        for w in self.due():
            try:
                await self.check(w)
            except Exception:
                log.exception("watcher %s failed", w["id"])
                self.db.log_event("watcher.fetch", status="failed", error_code="check_failed")

    def start(self) -> None:
        async def run() -> None:
            pruned_at = 0.0
            while True:
                await self.tick()
                if time.monotonic() - pruned_at > 3600:
                    self.db.prune_snapshots()
                    pruned_at = time.monotonic()
                await asyncio.sleep(TICK_S)

        self._task = asyncio.create_task(run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    # -- feedback and retrieval ----------------------------------------------------

    async def feedback(self, change_id: str, useful: bool) -> dict[str, Any]:
        row = self.db.one("SELECT id FROM change_events WHERE id = ?", (change_id,))
        if row is None:
            return {"ok": False, "error": "unknown change"}
        self.prefs.react(change_id, useful)
        return {"ok": True, "change_id": change_id, "useful": useful,
                "reactions": len(self.prefs.reactions())}

    def ranked(self, limit: int = 20) -> list[dict[str, Any]]:
        """All stored changes ranked by the current preference vector (for before/after)."""
        rows = self.db.all("SELECT id, description, embedding FROM change_events "
                           "WHERE embedding IS NOT NULL ORDER BY ts DESC LIMIT 500")
        vec = self.prefs.vector()
        if not rows:
            return []
        if vec is None:
            return [{"id": r["id"], "description": r["description"], "score": None}
                    for r in rows[:limit]]
        matrix = np.stack([from_blob(r["embedding"]) for r in rows])
        scores = cosine_many(vec, matrix)
        order = np.argsort(-scores)[:limit]
        return [{"id": rows[i]["id"], "description": rows[i]["description"],
                 "score": float(scores[i])} for i in order]

    async def similar(self, text: str, *, days: int | None = None,
                      limit: int = 5) -> list[dict[str, Any]]:
        """'Show me changes like the price drop on that studio' across sources and weeks."""
        query = await self.llm.embed(text)
        sql = "SELECT id, watcher_id, ts, description, embedding FROM change_events"
        params: list[Any] = []
        if days is not None:
            sql += " WHERE ts >= ?"
            params.append((datetime.now(UTC) - timedelta(days=days)).isoformat())
        rows = self.db.all(sql, params)
        if not rows:
            return []
        scores = cosine_many(query, np.stack([from_blob(r["embedding"]) for r in rows]))
        order = np.argsort(-scores)[:limit]
        return [{"id": rows[i]["id"], "watcher_id": rows[i]["watcher_id"], "ts": rows[i]["ts"],
                 "description": rows[i]["description"], "score": float(scores[i])}
                for i in order]
