from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.config import Settings
from orchestrator.db import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        gemini_api_key="test",
        nimble_api_key="test",
        agent37_api_key="sk_test",
        agent37_workers=["w1", "w2"],
        token="tok",
        db_path=tmp_path / "test.db",
        embed_dim=8,
    )
