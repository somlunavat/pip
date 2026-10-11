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
                  sense=no_context, mac_call=fake_mac, browser=None)
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
                  sense=no_context, mac_call=fake_mac, browser=None)
    app = create_app(token="secret", agent=agent, llm=llm, permissions=perms)
    with TestClient(app) as c, c.websocket_connect("/ws?token=secret") as ws:
        ws.receive_json()
        ws.send_json({"type": "permissions"})
        first = ws.receive_json()
        ws.send_json({"type": "grant", "ids": ["screen"]})
        second = ws.receive_json()
    assert first["holder"] == "Terminal" and first["items"][0]["status"] == "denied"
    assert second["items"][0]["status"] == "granted" and perms.granted == ["screen"]
