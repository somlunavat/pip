from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from orchestrator.db import Database
from orchestrator.models import Milestone, WatchSpec
from orchestrator.nimble import Page
from orchestrator.preferences import Preferences
from orchestrator.vectors import cosine, from_blob, to_blob
from orchestrator.watchers import WatcherService, describe_change, diff_fields
from tests.fakes import FakeLLM


class FakeNimble:
    def __init__(self) -> None:
        self.calls = 0

    async def extract(self, url: str, **_: Any) -> Page:
        self.calls += 1
        return Page(url=url, markdown="page")


def unit(*xs: float) -> np.ndarray:
    v = np.array(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


def service(db: Database, llm: FakeLLM, alerts: list | None = None) -> WatcherService:
    async def alert(change: dict[str, Any]) -> None:
        if alerts is not None:
            alerts.append(change)

    return WatcherService(db, llm, FakeNimble(), Preferences(db), alert)


def test_blob_roundtrip() -> None:
    v = unit(1, 2, 3)
    assert np.allclose(from_blob(to_blob(v)), v)


def test_describe_change_numeric_direction() -> None:
    diff = diff_fields({"rent": "2,400", "status": "available", "photos": 5},
                       {"rent": "2,200", "status": "available", "photos": 6})
    assert set(diff) == {"rent", "photos"}
    text = describe_change(diff, ["status"])
    assert "rent 2,400 to 2,200 (down, 8%)" in text
    assert "photos 5 to 6 (up, 20%)" in text
    assert text.endswith("status unchanged")


def test_describe_added_and_removed() -> None:
    text = describe_change(diff_fields({"a": 1}, {"b": 2}), [])
    assert "a removed (was 1)" in text and "b added: 2" in text


async def test_unchanged_page_skips_embedding(db: Database) -> None:
    llm = FakeLLM(fields=[{"price": 10}, {"price": 10}])
    embedded: list[str] = []
    original = llm.embed

    async def spy(text: str) -> np.ndarray:
        embedded.append(text)
        return await original(text)

    llm.embed = spy  # type: ignore[method-assign]
    svc = service(db, llm)
    wid, _ = await svc.add(WatchSpec(target="https://x.example", extractor="price"))
    watcher = db.one("SELECT * FROM watchers WHERE id = ?", (wid,))
    assert await svc.check(watcher) is None
    assert embedded == []


async def test_change_is_embedded_stored_and_alerted_on_cold_start(db: Database) -> None:
    alerts: list = []
    llm = FakeLLM(fields=[{"price": 2400, "status": "open"}, {"price": 2200, "status": "open"}])
    svc = service(db, llm, alerts)
    wid, _ = await svc.add(WatchSpec(target="https://x.example", extractor="price, status"))
    change = await svc.check(db.one("SELECT * FROM watchers WHERE id = ?", (wid,)))
    assert change and change["cold_start"]
    assert alerts and "price 2400 to 2200 (down" in alerts[0]["description"]
    row = db.one("SELECT * FROM change_events WHERE id = ?", (change["change_id"],))
    assert row["alerted"] == 1 and len(from_blob(row["embedding"])) == llm.dim
    assert '"from": 2400' in row["diff"]  # raw diff stored beside the vector


async def test_preferences_learn_from_reactions(db: Database) -> None:
    """After a few reactions, price drops outrank cosmetic changes."""
    llm = FakeLLM()
    price_like = unit(1, 0.1, 0, 0, 0, 0, 0, 0)
    cosmetic = unit(0, 0, 1, 0.1, 0, 0, 0, 0)
    llm.vectors = {
        "price down 1": price_like, "price down 2": unit(1, 0.2, 0, 0, 0, 0, 0, 0),
        "new photo 1": cosmetic, "new photo 2": unit(0, 0, 1, 0.2, 0, 0, 0, 0),
        "price down 3": unit(1, 0, 0.1, 0, 0, 0, 0, 0),
        "new photo 3": unit(0, 0.1, 1, 0, 0, 0, 0, 0),
    }
    alerts: list = []
    svc = service(db, llm, alerts)
    wid = db.create_watcher("https://x.example", "price", 900, threshold=0.5)
    watcher = db.one("SELECT * FROM watchers WHERE id = ?", (wid,))

    ids = {}
    for name in ["price down 1", "new photo 1", "price down 2", "new photo 2"]:
        ids[name] = (await svc.record_change(watcher, name, {}))["change_id"]
    before = [r["score"] for r in svc.ranked()]
    assert before == [None] * 4  # cold start: no ranking yet

    await svc.feedback(ids["price down 1"], True)
    await svc.feedback(ids["new photo 1"], False)
    await svc.feedback(ids["price down 2"], True)
    await svc.feedback(ids["new photo 2"], False)

    ranked = svc.ranked()
    assert ranked[0]["description"].startswith("price down")
    assert ranked[-1]["description"].startswith("new photo")

    alerts.clear()
    await svc.record_change(watcher, "price down 3", {})
    await svc.record_change(watcher, "new photo 3", {})
    assert [a["description"] for a in alerts] == ["price down 3"]


async def test_old_reactions_decay(db: Database) -> None:
    llm = FakeLLM()
    a, b = unit(1, 0, 0, 0, 0, 0, 0, 0), unit(0, 1, 0, 0, 0, 0, 0, 0)
    llm.vectors = {"a": a, "b": b}
    svc = service(db, llm)
    wid = db.create_watcher("https://x", "f", 900, 0.5)
    w = db.one("SELECT * FROM watchers WHERE id = ?", (wid,))
    ca = (await svc.record_change(w, "a", {}))["change_id"]
    cb = (await svc.record_change(w, "b", {}))["change_id"]
    prefs = Preferences(db)
    now = datetime.now(UTC)
    prefs.react(ca, True, now=now - timedelta(days=30))
    vec = prefs.react(cb, True, now=now)
    assert cosine(vec, b) > 0.95 and cosine(vec, a) < 0.1


async def test_watch_milestone_registers_watcher(db: Database) -> None:
    llm = FakeLLM(fields=[{"price": 100}])
    svc = service(db, llm)
    m = Milestone(id="m1", goal="watch price", kind="watch",
                  watch=WatchSpec(target="https://x.example", extractor="price", interval_s=5))
    r = await svc.as_milestone(m, "task")
    assert r.ok and "watcher_id" in r.data
    w = db.one("SELECT * FROM watchers WHERE id = ?", (r.data["watcher_id"],))
    assert w["interval_s"] == 60  # frequency is capped


async def test_due_respects_interval(db: Database) -> None:
    llm = FakeLLM(fields=[{"p": 1}])
    svc = service(db, llm)
    await svc.add(WatchSpec(target="https://x", extractor="p", interval_s=600))
    assert svc.due() == []
    assert len(svc.due(datetime.now(UTC) + timedelta(seconds=601))) == 1


async def test_similar_filters_by_window(db: Database) -> None:
    llm = FakeLLM()
    llm.vectors = {"price drop": unit(1, 0, 0, 0, 0, 0, 0, 0),
                   "rent fell": unit(0.9, 0.1, 0, 0, 0, 0, 0, 0),
                   "photo added": unit(0, 0, 1, 0, 0, 0, 0, 0)}
    svc = service(db, llm)
    wid = db.create_watcher("https://x", "f", 900, 0.5)
    w = db.one("SELECT * FROM watchers WHERE id = ?", (wid,))
    await svc.record_change(w, "rent fell", {})
    await svc.record_change(w, "photo added", {})
    hits = await svc.similar("price drop", days=7, limit=1)
    assert hits[0]["description"] == "rent fell"


async def test_tick_pauses_when_budget_is_exhausted(db: Database) -> None:
    from orchestrator.security import SpendGuard

    llm = FakeLLM(fields=[{"p": 1}])
    svc = service(db, llm)
    await svc.add(WatchSpec(target="https://x", extractor="p", interval_s=60))
    db.execute("UPDATE snapshots SET ts = '2000-01-01T00:00:00+00:00'")
    svc.guard = SpendGuard(db, daily_cap_usd=0)
    await svc.tick()  # would pop from an empty fields list if it fetched
    assert svc.nimble.calls == 1
