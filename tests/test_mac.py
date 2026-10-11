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
