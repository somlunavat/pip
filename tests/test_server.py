from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from orchestrator.db import Database
from orchestrator.loop import Agent
from orchestrator.models import Milestone, MilestoneResult, Source
from orchestrator.router import ReadRouter, Tier
from orchestrator.security import SpendGuard
from orchestrator.server import create_app
from tests.fakes import FakeLLM

LONG = "Enough text to pass the read evidence check for this milestone result. " * 2


async def reader(m: Milestone) -> MilestoneResult:
    return MilestoneResult(milestone_id=m.id, ok=True, path="nimble", text=LONG,
                           sources=[Source(url="https://a.example")])


async def no_context() -> dict[str, str]:
    return {}


async def fake_mac(tool: str, args: dict) -> str:
    return "done"


def client(db: Database, llm: FakeLLM) -> TestClient:
    agent = Agent(db, llm, ReadRouter([Tier("nimble", reader)]), SpendGuard(db, 100),
                  sense=no_context, mac_call=fake_mac, browser=None, reveal=None)
    return TestClient(create_app(token="secret", agent=agent, llm=llm))


def drain_until(ws, kind: str) -> list[dict]:
    seen = []
    while True:
        msg = ws.receive_json()
        seen.append(msg)
        if msg["type"] == kind:
            return seen


def test_rejects_bad_token(db: Database) -> None:
    with client(db, FakeLLM()) as c, pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/ws?token=wrong") as ws:
            ws.receive_json()
    assert exc.value.code == 4401


def test_text_request_streams_events(db: Database) -> None:
    llm = FakeLLM([{"summary": "look it up", "milestones": [
        {"id": "m1", "goal": "search", "kind": "read", "query": "x"}]}])
    with client(db, llm) as c, c.websocket_connect("/ws?token=secret") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "request", "text": "find x"})
        events = drain_until(ws, "response")
    assert [e["type"] for e in events][:2] == ["thinking", "ack"]
    assert events[-1]["sources"] == [{"url": "https://a.example", "title": ""}]


def test_plan_gate_round_trip(db: Database) -> None:
    llm = FakeLLM([{"milestones": [{"id": "m1", "goal": "remind", "kind": "local",
                                    "tool": "create_reminder", "args": {"title": "t"}}]}])
    with client(db, llm) as c, c.websocket_connect("/ws?token=secret") as ws:
        ws.receive_json()
        ws.send_json({"type": "request", "text": "remind me"})
        plan = drain_until(ws, "plan")[-1]
        ws.send_json({"type": "approve", "task_id": plan["task_id"]})
        resp = drain_until(ws, "response")[-1]
    assert resp["display"] == "card" and resp["paths"] == ["mac"]


def test_audio_request_is_transcribed(db: Database) -> None:
    llm = FakeLLM(small_talk="hello back")
    audio = base64.b64encode(b"hello there").decode()
    with client(db, llm) as c, c.websocket_connect("/ws?token=secret") as ws:
        ws.receive_json()
        ws.send_json({"type": "request", "audio": audio, "mime": "audio/webm"})
        events = drain_until(ws, "response")
    transcript = next(e for e in events if e["type"] == "transcript")
    assert transcript["text"] == "hello there"
    assert events[-1]["text"] == "hello back"


def test_unknown_message_type(db: Database) -> None:
    with client(db, FakeLLM()) as c, c.websocket_connect("/ws?token=secret") as ws:
        ws.receive_json()
        ws.send_json({"type": "rm -rf"})
        assert ws.receive_json()["type"] == "error"


class FakePermissions:
    def __init__(self) -> None:
        self.granted: list[str] = []

    def report(self) -> dict:
        return {"holder": "Terminal", "items": [
            {"id": "screen", "name": "Screen Recording", "why": "see", "status":
             "granted" if "screen" in self.granted else "denied"}]}

    async def request_all(self, ids=None) -> dict:
        self.granted.extend(ids or ["screen"])
        return self.report()


def test_permissions_report_and_grant(db: Database) -> None:
    perms = FakePermissions()
    llm = FakeLLM()
    agent = Agent(db, llm, ReadRouter([Tier("nimble", reader)]), SpendGuard(db, 100),
                  sense=no_context, mac_call=fake_mac, browser=None, reveal=None)
    app = create_app(token="secret", agent=agent, llm=llm, permissions=perms)
    with TestClient(app) as c, c.websocket_connect("/ws?token=secret") as ws:
        ws.receive_json()
        ws.send_json({"type": "permissions"})
        first = ws.receive_json()
        ws.send_json({"type": "grant", "ids": ["screen"]})
        second = ws.receive_json()
    assert first["holder"] == "Terminal" and first["items"][0]["status"] == "denied"
    assert second["items"][0]["status"] == "granted" and perms.granted == ["screen"]


def test_stop_cancels_a_running_task(db: Database) -> None:
    import asyncio

    async def slow_reader(m: Milestone) -> MilestoneResult:
        await asyncio.sleep(30)
        raise AssertionError("should have been stopped")

    llm = FakeLLM([{"milestones": [{"id": "m1", "goal": "slow read", "kind": "read",
                                    "query": "x"}]}])
    agent = Agent(db, llm, ReadRouter([Tier("nimble", slow_reader)]), SpendGuard(db, 100),
                  sense=no_context, mac_call=fake_mac, browser=None, reveal=None)
    with TestClient(create_app(token="secret", agent=agent, llm=llm)) as c, \
            c.websocket_connect("/ws?token=secret") as ws:
        ws.receive_json()
        ws.send_json({"type": "request", "text": "slow"})
        doing = drain_until(ws, "doing")[-1]
        ws.send_json({"type": "stop", "task_id": doing["task_id"]})
        resp = drain_until(ws, "response")[-1]
    assert resp["text"] == "Stopped."
    row = db.one("SELECT status FROM tasks WHERE id = ?", (doing["task_id"],))
    assert row["status"] == "cancelled"


# -- background tasks (#61) ---------------------------------------------------------------


def background_client(db: Database, llm: FakeLLM, reader_fn=reader) -> TestClient:
    agent = Agent(db, llm, ReadRouter([Tier("nimble", reader_fn)]), SpendGuard(db, 100),
                  sense=no_context, mac_call=fake_mac, browser=None, reveal=None)
    return TestClient(create_app(token="secret", agent=agent, llm=llm))


def test_background_task_outlives_its_connection_and_notifies_overlay(db: Database) -> None:
    import time

    async def slow_reader(m: Milestone) -> MilestoneResult:
        import asyncio
        await asyncio.sleep(0.3)
        return await reader(m)

    llm = FakeLLM([{"summary": "look it up", "milestones": [
        {"id": "m1", "goal": "search", "kind": "read", "query": "x"}]}])
    with background_client(db, llm, slow_reader) as c, \
            c.websocket_connect("/ws?token=secret") as overlay:
        overlay.receive_json()  # hello
        with c.websocket_connect("/ws?token=secret") as cli:
            cli.receive_json()
            cli.send_json({"type": "request", "text": "find x", "background": True})
            started = cli.receive_json()
            assert started["type"] == "thinking" and started["text"] == "find x"
        # The CLI is gone; the overlay still hears how it went.
        seen = drain_until(overlay, "response")
        time.sleep(0.05)
    assert seen[0]["type"] == "thinking" and seen[0]["background"] is True
    assert seen[-1]["background"] is True and seen[-1]["task_id"] == started["task_id"]
    row = db.one("SELECT status FROM tasks WHERE id = ?", (started["task_id"],))
    assert row["status"] == "done"


def test_any_client_can_approve_a_background_plan(db: Database) -> None:
    llm = FakeLLM([{"summary": "add it", "milestones": [
        {"id": "m1", "goal": "remind", "kind": "local", "tool": "create_reminder",
         "args": {"title": "t"}}]}])
    with background_client(db, llm) as c, c.websocket_connect("/ws?token=secret") as overlay:
        overlay.receive_json()
        with c.websocket_connect("/ws?token=secret") as cli:
            cli.receive_json()
            cli.send_json({"type": "request", "text": "remind me", "background": True})
            cli.receive_json()  # thinking
        plan = drain_until(overlay, "plan")[-1]
        overlay.send_json({"type": "approve", "task_id": plan["task_id"]})
        resp = drain_until(overlay, "response")[-1]
    assert resp["paths"] == ["mac"]


def test_foreground_task_is_not_broadcast(db: Database) -> None:
    llm = FakeLLM(small_talk="hi!")
    with background_client(db, llm) as c, c.websocket_connect("/ws?token=secret") as other:
        other.receive_json()
        with c.websocket_connect("/ws?token=secret") as me:
            me.receive_json()
            me.send_json({"type": "request", "text": "hello"})
            assert drain_until(me, "response")[-1]["text"] == "hi!"
        other.send_json({"type": "ping"})
        assert other.receive_json()["type"] == "pong"  # nothing else was queued for it
