"""Local WebSocket server the overlay talks to.

Bound to 127.0.0.1 and gated by a random token presented on connect. Client
messages: request (text or audio), approve, cancel, feedback. Server messages:
thinking, ack, doing, plan, response, alert, error.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from orchestrator.llm.base import LLM
from orchestrator.loop import Agent
from orchestrator.models import Plan
from orchestrator.security import token_matches

log = logging.getLogger("pip.server")

MAX_AUDIO_BYTES = 10 * 1024 * 1024
APPROVAL_TIMEOUT_S = 300
FeedbackFn = Callable[[str, bool], Awaitable[dict[str, Any]]]


class PermissionsApi(Protocol):
    def report(self) -> dict[str, Any]: ...

    async def request_all(self, ids: list[str] | None = None) -> dict[str, Any]: ...


class Hub:
    """Connected overlay clients, for broadcasts such as watcher alerts."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def broadcast(self, message: dict[str, Any]) -> None:
        for ws in list(self.clients):
            try:
                await ws.send_json(message)
            except Exception:  # a dead socket should not block the others
                self.clients.discard(ws)


class Session:
    """One overlay connection: runs requests concurrently and routes approvals."""

    def __init__(self, ws: WebSocket, agent: Agent, llm: LLM, feedback: FeedbackFn | None,
                 permissions: PermissionsApi | None = None):
        self.ws = ws
        self.agent = agent
        self.llm = llm
        self.feedback = feedback
        self.permissions = permissions
        self.approvals: dict[str, asyncio.Future[bool]] = {}
        self.tasks: set[asyncio.Task[Any]] = set()
        self.running: dict[str, asyncio.Task[Any]] = {}  # task_id -> request task
        self._send_lock = asyncio.Lock()

    async def send(self, message: dict[str, Any]) -> None:
        async with self._send_lock:
            await self.ws.send_json(message)

    async def approve(self, task_id: str, plan: Plan) -> bool:
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.approvals[task_id] = fut
        try:
            return await asyncio.wait_for(fut, APPROVAL_TIMEOUT_S)
        except TimeoutError:
            return False
        finally:
            self.approvals.pop(task_id, None)

    async def handle(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "request":
            task = asyncio.create_task(self._request(msg))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        elif kind == "stop":
            task_id = str(msg.get("task_id"))
            fut = self.approvals.get(task_id)
            if fut and not fut.done():
                fut.set_result(False)
            running = self.running.get(task_id)
            if running and not running.done():
                running.cancel()
        elif kind in ("approve", "cancel"):
            fut = self.approvals.get(str(msg.get("task_id")))
            if fut and not fut.done():
                fut.set_result(kind == "approve")
        elif kind == "feedback" and self.feedback:
            result = await self.feedback(str(msg.get("change_id")), bool(msg.get("useful")))
            await self.send({"type": "feedback_ack", **result})
        elif kind == "permissions" and self.permissions:
            await self.send({"type": "permissions", **self.permissions.report()})
        elif kind == "grant" and self.permissions:
            # Waits on macOS prompts, so it runs alongside other messages.
            ids = msg.get("ids")
            task = asyncio.create_task(self._grant(ids if isinstance(ids, list) else None))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        elif kind == "ping":
            await self.send({"type": "pong"})
        else:
            await self.send({"type": "error", "message": f"unknown message type: {kind}"})

    async def _grant(self, ids: list[str] | None) -> None:
        assert self.permissions is not None
        try:
            result = await self.permissions.request_all([str(i) for i in ids] if ids else None)
        except Exception as exc:  # report, don't drop the connection
            log.exception("permission request failed")
            await self.send({"type": "error", "message": f"Permission setup failed: {exc}"})
            return
        await self.send({"type": "permissions", **result})

    async def _request(self, msg: dict[str, Any]) -> None:
        me = asyncio.current_task()

        async def emit(event: dict[str, Any]) -> None:
            task_id = event.get("task_id")
            if task_id and me is not None:
                self.running.setdefault(task_id, me)  # lets Stop find this request
            await self.send(event)

        try:
            text = str(msg.get("text") or "").strip()
            if msg.get("audio"):
                await self.send({"type": "thinking", "label": "Transcribing"})
                audio = base64.b64decode(msg["audio"], validate=True)
                if len(audio) > MAX_AUDIO_BYTES:
                    raise ValueError("recording too large")
                text = await self.llm.transcribe(audio, str(msg.get("mime") or "audio/webm"))
                await self.send({"type": "transcript", "text": text})
            if not text:
                await self.send({"type": "error", "message": "empty request"})
                return
            await self.agent.handle(text, emit, self.approve)
        except asyncio.CancelledError:
            pass  # stopped by the user; the agent already said so
        except (ValueError, binascii.Error) as exc:
            await self.send({"type": "error", "message": str(exc)})
        except Exception:
            log.exception("request failed")  # the agent already emitted an error event
        finally:
            for task_id in [t for t, task in self.running.items() if task is me]:
                del self.running[task_id]

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        for fut in self.approvals.values():
            if not fut.done():
                fut.set_result(False)


def create_app(
    *,
    token: str,
    agent: Agent,
    llm: LLM,
    hub: Hub | None = None,
    feedback: FeedbackFn | None = None,
    permissions: PermissionsApi | None = None,
    lifespan: Any = None,
) -> FastAPI:
    app = FastAPI(title="pip orchestrator", lifespan=lifespan)
    app.state.hub = hub or Hub()

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        presented = ws.query_params.get("token") or ws.headers.get("x-pip-token")
        if not token_matches(presented, token):
            await ws.close(code=4401)
            return
        await ws.accept()
        app.state.hub.clients.add(ws)
        session = Session(ws, agent, llm, feedback, permissions)
        await session.send({"type": "hello"})
        try:
            while True:
                msg = await ws.receive_json()
                if isinstance(msg, dict):
                    await session.handle(msg)
        except WebSocketDisconnect:
            pass
        finally:
            app.state.hub.clients.discard(ws)
            await session.close()
            with contextlib.suppress(Exception):
                await ws.close()

    return app
