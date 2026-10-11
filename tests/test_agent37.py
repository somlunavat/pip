from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from orchestrator.agent37 import (
    Agent37Control,
    Dispatcher,
    NoWorkerAvailable,
    WorkerClient,
    WorkerPool,
    iter_sse,
)
from orchestrator.models import Milestone


def sse(*events: tuple[str, dict]) -> bytes:
    out = [":keepalive\n\n"]
    for name, payload in events:
        out.append(f"event: {name}\ndata: {json.dumps(payload)}\n\n")
    return "".join(out).encode()


def completed(text: str = "Answer. Source: https://a.example/x.", cost: float = 0.02) -> bytes:
    return sse(
        ("response.created", {"id": "resp_1", "session_id": "s"}),
        ("response.tool_call.started", {"tool": "browser", "label": "Opening page"}),
        ("response.output_text.delta", {"text": "Answer"}),
        ("response.completed", {"output_text": text,
                                "usage": {"input_tokens": 10, "output_tokens": 5,
                                          "cost_usd": cost}}),
    )


def worker(wid: str, handler: Callable[[httpx.Request], httpx.Response]) -> WorkerClient:
    return WorkerClient(wid, "sk_test", transport=httpx.MockTransport(handler))


MILESTONE = Milestone(id="m1", goal="log in and download invoice", kind="work",
                      needs_login=True, success_signal="invoice saved")


async def test_iter_sse_skips_keepalives_and_parses_events() -> None:
    async def lines():
        for line in completed().decode().split("\n"):
            yield line

    events = [name async for name, _ in iter_sse(lines())]
    assert events == ["response.created", "response.tool_call.started",
                      "response.output_text.delta", "response.completed"]


async def test_run_turn_collects_output_and_metadata() -> None:
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["x-agent37-key"] == "sk_test"
        assert req.url.host == "w1.agent37.app"
        seen.append(json.loads(req.content))
        return httpx.Response(200, content=completed())

    events: list[str] = []

    async def on_event(name: str, payload: dict) -> None:
        events.append(name)

    turn = await worker("w1", handler).run_turn(
        "pip-abc", "hi", metadata={"task_id": "t"}, on_event=on_event
    )
    assert turn.status == "completed" and turn.response_id == "resp_1"
    assert turn.cost_usd == 0.02
    assert seen[0]["session_id"] == "pip-abc" and seen[0]["stream"] is True
    assert "response.tool_call.started" in events


async def test_run_turn_reattaches_after_truncated_stream() -> None:
    paths: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        paths.append(f"{req.method} {req.url.path}")
        if req.method == "POST":
            return httpx.Response(200, content=sse(("response.created", {"id": "resp_9"})))
        return httpx.Response(200, content=completed())

    turn = await worker("w1", handler).run_turn("s", "hi")
    assert turn.status == "completed"
    assert paths == ["POST /v1/responses", "GET /v1/responses/resp_9/stream"]


async def test_dispatch_retries_failed_turn_on_another_worker() -> None:
    def failing(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse(
            ("response.created", {"id": "r"}),
            ("response.failed", {"error": {"code": "agent_error", "message": "boom"}}),
        ))

    def ok(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=completed())

    pool = WorkerPool([worker("w1", failing), worker("w2", ok)], sessions_per_worker=2)
    r = await Dispatcher(pool).run(MILESTONE, task_id="t")
    assert r.ok and r.data["worker_id"] == "w2"
    assert [s.url for s in r.sources] == ["https://a.example/x"]
    assert pool.in_flight == {"w1": 0, "w2": 0}


async def test_dispatch_mints_new_session_on_409() -> None:
    sessions: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sessions.append(json.loads(req.content)["session_id"])
        if len(sessions) == 1:
            return httpx.Response(409, json={"error": {"code": "session_busy"}})
        return httpx.Response(200, content=completed())

    r = await Dispatcher(WorkerPool([worker("w1", handler)])).run(MILESTONE, task_id="t")
    assert r.ok and len(set(sessions)) == 2


async def test_dispatch_402_marks_worker_and_moves_on() -> None:
    def broke(req: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": {"code": "quota_exhausted"}})

    def ok(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=completed())

    pool = WorkerPool([worker("w1", broke), worker("w2", ok)], sessions_per_worker=1)
    # w1 is picked first because both are idle and it sorts first.
    r = await Dispatcher(pool).run(MILESTONE, task_id="t")
    assert r.ok and "w1" in pool.unhealthy


async def test_dispatch_with_no_workers_fails_cleanly() -> None:
    r = await Dispatcher(WorkerPool([])).run(MILESTONE, task_id="t")
    assert not r.ok and r.error == "no_worker"


async def test_pool_respects_cap() -> None:
    pool = WorkerPool([worker("w1", lambda r: httpx.Response(200))], sessions_per_worker=1)
    w = await pool.acquire()
    with pytest.raises(NoWorkerAvailable):
        await pool.acquire(timeout=0.05)
    await pool.release(w)
    assert (await pool.acquire()).instance_id == "w1"


async def test_create_worker_disables_risky_plugins_and_sets_budget() -> None:
    bodies: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer sk_test"
        bodies.append(json.loads(req.content))
        return httpx.Response(201, json={"id": "ab12", "status": "running",
                                         "url": "https://ab12.agent37.app"})

    control = Agent37Control("sk_test", transport=httpx.MockTransport(handler))
    out = await control.create_worker("w", credit_usd=2, monthly_cap_usd=5, user="som")
    body = bodies[0]
    assert out["id"] == "ab12"
    assert body["auto_sleep"] is False
    assert body["budget"]["credit_micros"] == 2_000_000
    assert body["env"]["AGENT37_MANAGED_PLUGIN_PERFLO_ENABLED"] == "false"
    assert body["env"]["AGENT37_MANAGED_PLUGIN_COMPOSIO_ENABLED"] == "false"
    assert body["metadata"]["role"] == "worker"


async def test_health_uses_documented_shape() -> None:
    def ok(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "agent": "hermes", "healthy": True})

    def gateway_down(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "agent": "hermes", "healthy": True})

    assert await worker("w1", ok).health()
    assert not await worker("w2", gateway_down).health()


async def test_504_reads_answer_from_session_history_after_turn_finishes() -> None:
    """Issue #45: the transcript lives in `history`, and the turn may still be running."""
    polls = {"n": 0}
    documented = {
        "id": "s", "agent": "hermes", "active_response_id": None,
        "history": [
            {"role": "user", "content": "Research the top 3 EV makers"},
            {"role": "assistant", "content": "Here is the memo. Source: https://ev.example/a",
             "thinking": "..."},
        ],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(504, json={"error": {"code": "upstream_timeout"}})
        polls["n"] += 1
        if polls["n"] == 1:
            return httpx.Response(200, json={**documented, "active_response_id": "resp_1",
                                             "history": documented["history"][:1]})
        return httpx.Response(200, json=documented)

    d = Dispatcher(WorkerPool([worker("w1", handler)]), transcript_poll_s=0)
    r = await d.run(MILESTONE, task_id="t")
    assert r.ok and r.text.startswith("Here is the memo") and polls["n"] == 2
    assert [s.url for s in r.sources] == ["https://ev.example/a"]
