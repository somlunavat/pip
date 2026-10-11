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
    assert not mac.WRITE_TOOLS & mac.DISPLAY_TOOLS


@pytest.mark.parametrize(("running", "expect"), [
    ("Google Chrome", "make new window"), ("Safari", "make new document"),
    ("", "open location"), ("Arc", "open location"),
])
async def test_open_in_browser_uses_the_running_browser(
    monkeypatch: pytest.MonkeyPatch, running: str, expect: str
) -> None:
    scripts: list[tuple[str, tuple[str, ...]]] = []

    async def fake(script: str, *args: str, timeout: float = 3.0) -> str:
        scripts.append((script, args))
        return running if "exists (process" in script else ""

    monkeypatch.setattr(mac, "run_osascript", fake)
    out = await mac.open_in_browser(["https://a.example", "https://b.example"])
    script, args = scripts[-1]
    assert expect in script and "https://b.example" in args
    assert out.startswith("opened 2 tabs")


async def test_open_in_browser_rejects_non_http() -> None:
    with pytest.raises(mac.MacToolError, match="http"):
        await mac.open_in_browser(["file:///etc/passwd"])


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


# -- expanded tools (#53) -----------------------------------------------------------


def test_every_tool_is_classified_once() -> None:
    groups = [mac.READ_ONLY_TOOLS, mac.DISPLAY_TOOLS, mac.WRITE_TOOLS]
    assert set().union(*groups) == set(mac.TOOLS)
    assert sum(len(g) for g in groups) == len(mac.TOOLS)
    # Acting as the user always needs Proceed.
    assert {"run_shortcut", "type_text", "save_text_file", "quit_app"} <= mac.WRITE_TOOLS
    # Drafts never send, so they don't.
    assert {"draft_email", "draft_message"} <= mac.DISPLAY_TOOLS


async def test_draft_message_builds_prefilled_messages_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, ...]] = []

    async def fake(script: str, *args: str, timeout: float = 3.0) -> str:
        seen.append(args)
        return ""

    monkeypatch.setattr(mac, "run_osascript", fake)
    out = await mac.call_tool("draft_message", {"to": "978-221-1473", "body": "hey & hi"})
    assert seen[0] == ("sms://open?addresses=978-221-1473&body=hey%20%26%20hi",)
    assert "press Return to send" in out


async def test_draft_message_needs_a_recipient() -> None:
    with pytest.raises(mac.MacToolError, match="phone number"):
        await mac.draft_message("!!!", "hi")


async def test_media_control_rejects_unknown_action() -> None:
    with pytest.raises(mac.MacToolError, match="action must be"):
        await mac.media_control("rewind")


async def test_files_outside_home_are_refused(tmp_path) -> None:
    with pytest.raises(mac.MacToolError, match="home folder"):
        await mac.open_file("/etc/hosts")


async def test_save_text_file_never_overwrites(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(mac, "PIP_FILES", tmp_path)
    first = await mac.save_text_file("../../evil/plan", "one")
    second = await mac.save_text_file("plan", "two")
    assert first == str(tmp_path / "plan.md")
    assert second == str(tmp_path / "plan (2).md")
    assert (tmp_path / "plan.md").read_text() == "one"


async def test_type_text_is_capped() -> None:
    with pytest.raises(mac.MacToolError, match="2000"):
        await mac.type_text("x" * 2001)


def test_accessibility_denial_is_explained() -> None:
    msg = mac._explain("System Events got an error: osascript is not allowed assistive "
                       "access. (-1719)", "")
    assert "Accessibility" in msg


async def test_calendar_events_are_listed_by_time(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(script: str, *args: str, timeout: float = 3.0) -> str:
        return ("Standup\t2026-10-12T09:30:00\tWork\n"
                "Dentist\t2026-10-12T08:00:00\tHome\n")

    monkeypatch.setattr(mac, "run_osascript", fake)
    out = await mac.list_calendar_events("2026-10-12")
    assert out.splitlines() == ["08:00  Dentist  (Home)", "09:30  Standup  (Work)"]
