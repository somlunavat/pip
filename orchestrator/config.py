"""Runtime settings, read once from the environment (and .env if present)."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_list(name: str) -> list[str]:
    return [part.strip() for part in _env(name).split(",") if part.strip()]


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str = ""
    gemini_plan_model: str = "gemini-3.5-flash"
    gemini_fast_model: str = "gemini-3.5-flash-lite"
    gemini_embed_model: str = "gemini-embedding-2"
    embed_dim: int = 768

    nimble_api_key: str = ""
    nimble_base_url: str = "https://sdk.nimbleway.com"
    nimble_concurrency: int = 4

    agent37_api_key: str = ""
    agent37_api_url: str = "https://api.agent37.com"
    agent37_workers: list[str] = field(default_factory=list)
    agent37_sessions_per_worker: int = 2

    host: str = "127.0.0.1"
    port: int = 8779
    token: str = ""
    db_path: Path = Path("data/pip.db")
    daily_cap_usd: float = 10.0
    kill_switch: bool = False

    def missing(self) -> list[str]:
        """Required keys that are not set (workers are optional)."""
        required = {"GEMINI_API_KEY": self.gemini_api_key, "NIMBLE_API_KEY": self.nimble_api_key}
        if self.agent37_workers:
            required["AGENT37_API_KEY"] = self.agent37_api_key
        return [name for name, value in required.items() if not value]

    @classmethod
    def from_env(cls, env_file: str | None = ".env") -> Settings:
        if env_file:
            load_dotenv(env_file, override=False)
        return cls(
            gemini_api_key=_env("GEMINI_API_KEY"),
            gemini_plan_model=_env("GEMINI_PLAN_MODEL", cls.gemini_plan_model),
            gemini_fast_model=_env("GEMINI_FAST_MODEL", cls.gemini_fast_model),
            gemini_embed_model=_env("GEMINI_EMBED_MODEL", cls.gemini_embed_model),
            embed_dim=int(_env("EMBED_DIM", str(cls.embed_dim))),
            nimble_api_key=_env("NIMBLE_API_KEY"),
            nimble_concurrency=int(_env("NIMBLE_CONCURRENCY", str(cls.nimble_concurrency))),
            agent37_api_key=_env("AGENT37_API_KEY"),
            agent37_workers=_env_list("AGENT37_WORKERS"),
            agent37_sessions_per_worker=int(
                _env("AGENT37_SESSIONS_PER_WORKER", str(cls.agent37_sessions_per_worker))
            ),
            host=_env("PIP_HOST", cls.host),
            port=int(_env("PIP_PORT", str(cls.port))),
            token=_env("PIP_TOKEN") or secrets.token_urlsafe(24),
            db_path=Path(_env("PIP_DB_PATH", str(cls.db_path))),
            daily_cap_usd=float(_env("PIP_DAILY_CAP_USD", str(cls.daily_cap_usd))),
            kill_switch=_env("PIP_KILL_SWITCH", "0") == "1",
        )
