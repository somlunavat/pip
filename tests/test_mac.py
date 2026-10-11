from __future__ import annotations

import pytest

from orchestrator import mac


async def test_unknown_tool_is_refused() -> None:
    with pytest.raises(mac.MacToolError, match="not allowlisted"):
        await mac.call_tool("run_shell", {"cmd": "rm -rf /"})


async def test_unknown_args_are_refused() -> None:
    with pytest.raises(mac.MacToolError, match="unexpected args"):
        await mac.call_tool("speak", {"text": "hi", "voice": "x"})


async def test_open_url_rejects_non_http() -> None:
    with pytest.raises(mac.MacToolError, match="http"):
        await mac.call_tool("open_url", {"url": "file:///etc/passwd"})


async def test_args_are_passed_via_argv_not_source(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []

    async def fake(script: str, *args: str, timeout: float = 3.0) -> str:
        calls.append((script, args))
        return ""

    monkeypatch.setattr(mac, "run_osascript", fake)
    hostile = '" & do shell script "echo pwned'
    await mac.call_tool("speak", {"text": hostile})
    script, args = calls[0]
    assert hostile not in script
    assert args == (hostile,)


async def test_get_context_parses_front_app(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(script: str, *args: str, timeout: float = 3.0) -> str:
        if "frontmost" in script:
            return "Safari\nZillow - Studio"
        if "clipboard" in script:
            return "copied text"
        return "https://zillow.com/x"

    monkeypatch.setattr(mac, "run_osascript", fake)
    ctx = await mac.get_context()
    assert ctx == {
        "active_app": "Safari",
        "window_title": "Zillow - Studio",
        "browser_url": "https://zillow.com/x",
        "clipboard": "copied text",
    }


def test_side_effect_tools_cover_everything_but_reads() -> None:
    assert mac.SIDE_EFFECT_TOOLS | mac.READ_ONLY_TOOLS == set(mac.TOOLS)


async def test_calendar_event_passes_numeric_date_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, ...]] = []

    async def fake(script: str, *args: str, timeout: float = 3.0) -> str:
        seen.append(args)
        return "Home"

    monkeypatch.setattr(mac, "run_osascript", fake)
    out = await mac.call_tool("create_calendar_event", {
        "title": "Submit pitch", "start": "2026-10-11T16:45", "duration_minutes": "30"})
    assert seen[0][:7] == ("2026", "10", "11", "16", "45", "30", "Submit pitch")
    assert "Home" in out and "4:45 PM to 5:15 PM" in out


def test_calendar_start_with_offset_becomes_local_time() -> None:
    parsed = mac.parse_local_datetime("2026-10-11T21:45:00+00:00")
    assert parsed.tzinfo is None


async def test_calendar_rejects_bad_start() -> None:
    with pytest.raises(mac.MacToolError, match="ISO 8601"):
        await mac.create_calendar_event("x", "4:45 today")


def test_validate_args_reports_missing_and_unknown() -> None:
    assert mac.validate_args("create_reminder", {}) == "missing args for create_reminder: ['title']"
    ok = {"title": "x", "start": "2026-10-11T16:45"}
    assert mac.validate_args("create_calendar_event", ok) is None
    assert "not allowlisted" in mac.validate_args("send_imessage", {})


def test_automation_denial_is_explained() -> None:
    msg = mac._explain("execution error: Not authorized to send Apple events to Calendar. (-1743)",
                       'tell application "Calendar"')
    assert "System Settings > Privacy & Security > Automation" in msg and "Calendar" in msg
