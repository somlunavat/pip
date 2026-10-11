from __future__ import annotations

import pytest

from orchestrator.config import Settings


@pytest.mark.parametrize(("value", "mode"), [
    ("", "ask"), ("ask", "ask"), ("1", "ask"), ("always", "always"), ("ALWAYS", "always"),
    ("0", "never"), ("never", "never"), ("off", "never"),
])
def test_open_results_modes(monkeypatch: pytest.MonkeyPatch, value: str, mode: str) -> None:
    monkeypatch.setenv("PIP_OPEN_RESULTS", value)
    assert Settings.from_env(env_file=None).open_results == mode
