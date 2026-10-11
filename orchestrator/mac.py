"""Allowlisted AppleScript tools for the local Mac.

Each tool is one `osascript` call with a short timeout. There is deliberately no
shell tool, no file deletion, and no message sending: the agent runs as you, so
the surface stays at the seven tools below. Arguments are passed through argv,
never interpolated into script source, so text from the web cannot inject
AppleScript.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

DEFAULT_TIMEOUT_S = 3.0

# Tools that change something on the Mac; plans containing them need approval.
SIDE_EFFECT_TOOLS = {"open_app", "open_url", "create_reminder", "create_note", "show_notification",
                     "speak"}
READ_ONLY_TOOLS = {"get_context"}


class MacToolError(RuntimeError):
    pass


async def run_osascript(script: str, *args: str, timeout: float = DEFAULT_TIMEOUT_S) -> str:
    """Run an AppleScript with argv and return stdout, stripped."""
    if sys.platform != "darwin":
        raise MacToolError("AppleScript tools only run on macOS")
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise MacToolError(f"osascript timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise MacToolError(err.decode(errors="replace").strip() or "osascript failed")
    return out.decode(errors="replace").strip()


# -- Perception (Sense step) -------------------------------------------------

_FRONT_APP = """
tell application "System Events"
    set frontApp to first application process whose frontmost is true
    set appName to name of frontApp
    set winTitle to ""
    try
        set winTitle to name of front window of frontApp
    end try
end tell
return appName & linefeed & winTitle
"""

_BROWSER_URL = """
on run argv
    set appName to item 1 of argv
    if appName is in {"Google Chrome", "Arc", "Brave Browser", "Microsoft Edge"} then
        using terms from application "Google Chrome"
            tell application appName to return URL of active tab of front window
        end using terms from
    else if appName is "Safari" then
        tell application "Safari" to return URL of front document
    end if
    return ""
end run
"""

_CLIPBOARD = "try\n return (the clipboard as text)\non error\n return \"\"\nend try"


async def _safe(coro: Awaitable[str]) -> str:
    try:
        return await coro
    except MacToolError:
        return ""


async def get_context() -> dict[str, str]:
    """Active app, window title, browser URL, and clipboard, read in parallel.

    Selected text needs Accessibility APIs that differ per app, so it is read via
    the clipboard path only when the overlay asks for it explicitly. Clipboard and
    page content are untrusted and are fenced before they reach the planner.
    """
    front, clipboard = await asyncio.gather(
        _safe(run_osascript(_FRONT_APP)), _safe(run_osascript(_CLIPBOARD))
    )
    app, _, title = front.partition("\n")
    url = await _safe(run_osascript(_BROWSER_URL, app)) if app else ""
    return {
        "active_app": app,
        "window_title": title,
        "browser_url": url,
        "clipboard": clipboard[:2000],
    }


# -- Actions (local milestones) ---------------------------------------------

async def open_app(name: str) -> str:
    await run_osascript(
        "on run argv\n tell application (item 1 of argv) to activate\nend run", name
    )
    return f"opened {name}"


async def open_url(url: str) -> str:
    if not url.startswith(("https://", "http://")):
        raise MacToolError("only http(s) URLs may be opened")
    await run_osascript("on run argv\n open location (item 1 of argv)\nend run", url)
    return f"opened {url}"


async def create_reminder(title: str, notes: str = "") -> str:
    await run_osascript(
        """on run argv
    tell application "Reminders"
        make new reminder with properties {name:(item 1 of argv), body:(item 2 of argv)}
    end tell
end run""",
        title, notes,
        timeout=8,
    )
    return f"reminder created: {title}"


async def create_note(title: str, body: str) -> str:
    await run_osascript(
        """on run argv
    tell application "Notes"
        make new note with properties {name:(item 1 of argv), body:(item 2 of argv)}
    end tell
end run""",
        title, body,
        timeout=8,
    )
    return f"note created: {title}"


async def show_notification(title: str, message: str) -> str:
    await run_osascript(
        "on run argv\n display notification (item 2 of argv) with title (item 1 of argv)\nend run",
        title, message,
    )
    return "notified"


async def speak(text: str) -> str:
    await run_osascript("on run argv\n say (item 1 of argv)\nend run", text[:500], timeout=30)
    return "spoken"


@dataclass(frozen=True)
class MacTool:
    name: str
    fn: Callable[..., Awaitable[Any]]
    params: tuple[str, ...]
    description: str


TOOLS: dict[str, MacTool] = {
    t.name: t
    for t in (
        MacTool("get_context", get_context, (), "Read active app, window, browser URL, clipboard"),
        MacTool("open_app", open_app, ("name",), "Bring an application to the front"),
        MacTool("open_url", open_url, ("url",), "Open an http(s) URL in the default browser"),
        MacTool("create_reminder", create_reminder, ("title", "notes"), "Add a Reminders item"),
        MacTool("create_note", create_note, ("title", "body"), "Create a note in Notes"),
        MacTool("show_notification", show_notification, ("title", "message"),
                "Show a macOS notification"),
        MacTool("speak", speak, ("text",), "Speak text aloud"),
    )
}


async def call_tool(name: str, args: dict[str, Any]) -> Any:
    """Run one allowlisted tool. Unknown tools and unknown args are refused."""
    tool = TOOLS.get(name)
    if tool is None:
        raise MacToolError(f"tool not allowlisted: {name}")
    unknown = set(args) - set(tool.params)
    if unknown:
        raise MacToolError(f"unexpected args for {name}: {sorted(unknown)}")
    clean = {k: str(v) for k, v in args.items()}
    return await tool.fn(**clean)


def tool_catalog() -> str:
    """Short text listing for the planner prompt."""
    return "\n".join(f"- {t.name}({', '.join(t.params)}): {t.description}" for t in TOOLS.values())
