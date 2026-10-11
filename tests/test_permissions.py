from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator import mac, permissions


@pytest.fixture(autouse=True)
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "permissions.json"
    monkeypatch.setattr(permissions, "STATE_FILE", path)
    monkeypatch.setattr(permissions, "accessibility_trusted", lambda: True)
    monkeypatch.setattr(permissions, "screen_recording_allowed", lambda request=False: False)
    return path


def test_report_lists_installed_apps_with_status() -> None:
    rep = permissions.report()
    ids = {i["id"]: i["status"] for i in rep["items"]}
    assert ids["accessibility"] == "granted"
    assert ids["screen"] == "denied"
    assert ids["automation:Calendar"] == "unknown"  # no silent check for Automation
    assert all(i["why"] for i in rep["items"])


def test_apps_that_are_not_installed_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    perm = permissions.Permission("automation:Nope", "Nope", "x", "automation", "x",
                                  "/Applications/Definitely Not Installed.app")
    monkeypatch.setattr(permissions, "PERMISSIONS", [perm])
    assert permissions.available() == []


@pytest.mark.parametrize(("error", "expected"), [
    (None, "granted"),
    ("macOS blocked pip from controlling Calendar. Allow it in System Settings > ...", "denied"),
    ("osascript timed out after 90s", "unknown"),
])
async def test_automation_request_records_the_answer(
    monkeypatch: pytest.MonkeyPatch, error: str | None, expected: str
) -> None:
    async def fake(script: str, *args: str, timeout: float = 3.0) -> str:
        if error:
            raise mac.MacToolError(error)
        return "3"

    monkeypatch.setattr(mac, "run_osascript", fake)
    assert await permissions.request("automation:Calendar") == expected
    assert permissions.status("automation:Calendar") == expected


async def test_probes_are_read_only() -> None:
    writes = ("make new", "delete", "set ", "quit", "keystroke", "run shortcut")
    for perm in permissions.PERMISSIONS:
        assert not any(w in perm.probe for w in writes), perm.id


async def test_request_all_skips_granted(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    async def fake_request(pid: str) -> str:
        asked.append(pid)
        return "granted"

    monkeypatch.setattr(permissions, "request", fake_request)
    await permissions.request_all(["accessibility", "screen", "automation:Notes"])
    assert asked == ["screen", "automation:Notes"]


def test_holder_names_the_launching_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    assert permissions.holder() == "Terminal"
    monkeypatch.setenv("TERM_PROGRAM", "something-else")
    assert permissions.holder() == "the app that started Pip"
