from __future__ import annotations

import json

import httpx

from orchestrator.models import Milestone, MilestoneResult, Source
from orchestrator.nimble import NimbleClient
from orchestrator.router import ReadRouter, Tier, needs_worker, path_for

LONG = "Studio apartment, $2,200 a month, 450 sq ft, available now. " * 3


def client_for(handler) -> NimbleClient:
    return NimbleClient("key", transport=httpx.MockTransport(handler), max_retries=2)


async def test_search_and_extract_run_together() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        assert req.headers["authorization"] == "Bearer key"
        body = json.loads(req.content)
        if req.url.path == "/v2/search":
            assert body["query"] == "cheap studio austin"
            return httpx.Response(200, json={"request_id": "r", "total_results": 1, "results": [
                {"title": "A", "url": "https://a.example", "description": "d", "content": LONG,
                 "metadata": {}}]})
        assert body["formats"] == ["markdown", "links"]
        return httpx.Response(200, json={"task_id": "t", "url": body["url"], "status": "success",
                                         "data": {"markdown": LONG, "links": []}, "metadata": {}})

    nimble = client_for(handler)
    m = Milestone(id="m1", goal="g", kind="read", query="cheap studio austin",
                  url="https://b.example")
    r = await nimble.read(m)
    assert r.ok and r.path == "nimble"
    assert {s.url for s in r.sources} == {"https://a.example", "https://b.example"}
    assert sorted(seen) == ["/v2/extract", "/v2/search"]


async def test_429_is_retried_with_retry_after() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"retry_after": 0})
        return httpx.Response(200, json={"results": [], "total_results": 0, "request_id": "r"})

    hits = await client_for(handler).search("x")
    assert hits == [] and calls["n"] == 2


async def test_http_error_becomes_failed_result() -> None:
    r = await client_for(lambda req: httpx.Response(401, text="bad key")).read(
        Milestone(id="m1", goal="g", kind="read", query="x")
    )
    assert not r.ok and r.error == "http_401"


async def test_empty_result_is_not_ok() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [], "total_results": 0, "request_id": "r"})

    r = await client_for(handler).read(Milestone(id="m1", goal="g", kind="read", query="x"))
    assert not r.ok and r.error == "empty_result"


def test_routing_rule() -> None:
    read = Milestone(id="m", goal="g", kind="read")
    assert path_for(read) == "nimble"
    assert path_for(read.model_copy(update={"needs_login": True})) == "worker"
    assert path_for(read.model_copy(update={"estimated_seconds": 120})) == "worker"
    assert path_for(Milestone(id="m", goal="g", kind="local", tool="speak")) == "mac"
    assert path_for(Milestone(id="m", goal="g", kind="watch")) == "watcher"
    assert not needs_worker(read)


async def test_router_escalates_blocked_read_to_next_tier() -> None:
    escalations: list[tuple[str, str, str]] = []

    async def nimble(m: Milestone) -> MilestoneResult:
        return MilestoneResult(milestone_id=m.id, ok=True, path="nimble",
                               text="Access denied. Are you a robot?",
                               sources=[Source(url="https://x")])

    async def worker(m: Milestone) -> MilestoneResult:
        return MilestoneResult(milestone_id=m.id, ok=True, path="worker", text=LONG,
                               sources=[Source(url="https://x")])

    async def on_escalate(a: str, b: str, why: str) -> None:
        escalations.append((a, b, why))

    router = ReadRouter([Tier("nimble", nimble), Tier("worker", worker)])
    r = await router.read(Milestone(id="m1", goal="g", kind="read", query="q"),
                          on_escalate=on_escalate)
    assert r.ok and r.path == "worker" and r.tier == 2
    assert escalations == [("nimble", "worker", "thin_result")]


async def test_router_returns_last_failure_when_all_tiers_fail() -> None:
    async def empty(m: Milestone) -> MilestoneResult:
        return MilestoneResult(milestone_id=m.id, ok=False, path="nimble", error="empty_result")

    r = await ReadRouter([Tier("a", empty), Tier("b", empty)]).read(
        Milestone(id="m1", goal="g", kind="read")
    )
    assert not r.ok and r.tier == 2
