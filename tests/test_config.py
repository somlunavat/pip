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


def test_location_setting_and_timezone_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator import config

    monkeypatch.setenv("PIP_LOCATION", "New York, NY")
    assert Settings.from_env(env_file=None).location == "New York, NY"
    assert config.describe_location("New York, NY") == "New York, NY"
    monkeypatch.setattr(config, "local_timezone", lambda: "America/New_York")
    assert "America/New_York" in config.describe_location("")
    monkeypatch.setattr(config, "local_timezone", lambda: "")
    assert config.describe_location("") == "unknown"
