"""Run the orchestrator: `python -m orchestrator` (or `pip-agent`)."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn

from orchestrator.config import Settings
from orchestrator.runtime import build_runtime
from orchestrator.server import create_app

TOKEN_FILE = Path("data/overlay_token")


def write_token(token: str, path: Path = TOKEN_FILE) -> None:
    """Share the connect token with the overlay through a user-only file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings.from_env()
    if settings.host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("refusing to bind outside localhost")
    if missing := settings.missing():
        raise SystemExit(f"missing settings: {', '.join(missing)} (see .env.example)")
    runtime = build_runtime(settings)
    write_token(settings.token)

    @asynccontextmanager
    async def lifespan(app):
        if runtime.pool.workers:
            status = await runtime.pool.check_health()
            logging.info("worker health: %s", status)
        yield
        await runtime.aclose()

    app = create_app(token=settings.token, agent=runtime.agent, llm=runtime.llm,
                     lifespan=lifespan)
    logging.info("listening on ws://%s:%s/ws (token in %s)", settings.host, settings.port,
                 TOKEN_FILE)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
