from __future__ import annotations

import asyncio
import json

import websockets

from orchestrator.ask import ask


async def fake_pip(events: list[dict], seen: list[dict]):
    async def handler(ws):
        await ws.send(json.dumps({"type": "hello"}))
        seen.append(json.loads(await ws.recv()))
        for event in events:
            await ws.send(json.dumps(event))
        await asyncio.sleep(0.1)

    return await websockets.serve(handler, "127.0.0.1", 0)


async def test_fire_and_forget_returns_after_the_task_starts(capsys) -> None:
    seen: list[dict] = []
    server = await fake_pip([{"type": "thinking", "task_id": "task_1", "text": "x"}], seen)
    port = server.sockets[0].getsockname()[1]
    try:
        code = await ask("find x", wait=False, port=port, token="t", timeout=5)
    finally:
        server.close()
    assert code == 0 and seen[0] == {"type": "request", "text": "find x", "background": True}
    assert "task_1" in capsys.readouterr().out


async def test_wait_prints_the_answer(capsys) -> None:
    seen: list[dict] = []
    server = await fake_pip([
        {"type": "thinking", "task_id": "task_1", "text": "x"},
        {"type": "doing", "task_id": "task_1", "label": "Search"},
        {"type": "response", "task_id": "task_1", "opened": "opened 1 tab in Safari",
         "answer": {"headline": "Cheapest is $129", "summary": "on Southwest",
                    "rows": [{"label": "southwest.com", "value": "$129"}]}},
    ], seen)
    port = server.sockets[0].getsockname()[1]
    try:
        code = await ask("flights", wait=True, port=port, token="t", timeout=5)
    finally:
        server.close()
    out = capsys.readouterr().out
    assert code == 0 and "Cheapest is $129" in out and "southwest.com: $129" in out


async def test_reports_when_pip_is_not_running(capsys) -> None:
    code = await ask("x", wait=False, port=1, token="t", timeout=1)
    assert code == 2 and "isn't running" in capsys.readouterr().err
