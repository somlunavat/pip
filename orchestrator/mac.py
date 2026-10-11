"""Allowlisted AppleScript tools for the local Mac.

Each tool is one `osascript` call with a short timeout. There is deliberately no
shell tool, no file deletion, and no message sending: the agent runs as you, so
the surface stays at the small allowlist below. Arguments are passed through argv,
never interpolated into script source, so text from the web cannot inject
AppleScript.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

DEFAULT_TIMEOUT_S = 3.0

# Tools that write data into the user's apps; plans containing them need Proceed.
WRITE_TOOLS = {"create_reminder", "create_note", "create_calendar_event"}
# Tools that only show something and are undone by closing a window; no approval,
# so results can be pulled up without an extra click.
DISPLAY_TOOLS = {"open_app", "open_url", "open_in_browser", "show_notification", "speak"}
SIDE_EFFECT_TOOLS = WRITE_TOOLS | DISPLAY_TOOLS
READ_ONLY_TOOLS = {"get_context"}


class MacToolError(RuntimeError):
    pass


_NOT_AUTHORIZED = re.compile(r"-1743|not authori[sz]ed to send apple events", re.IGNORECASE)
_TARGET_APP = re.compile(r'application "([^"]+)"')


def _explain(err: str, script: str) -> str:
    """Turn macOS's terse automation denial into something the user can act on."""
    if _NOT_AUTHORIZED.search(err):
        app = (_TARGET_APP.search(script) or _TARGET_APP.search(err))
        target = app.group(1) if app else "that app"
        return (f"macOS blocked pip from controlling {target}. Allow it in System Settings > "
                "Privacy & Security > Automation, then try again.")
    return err


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
        message = err.decode(errors="replace").strip() or "osascript failed"
        raise MacToolError(_explain(message, script))
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


_RUNNING_BROWSER = """
tell application "System Events"
    repeat with b in {"Google Chrome", "Arc", "Brave Browser", "Microsoft Edge", "Safari"}
        if exists (process (b as text)) then return b as text
    end repeat
end tell
return ""
"""

_CHROMIUM_TABS = """
on run argv
    set appName to item 1 of argv
    using terms from application "Google Chrome"
        tell application appName
            set w to make new window
            set URL of active tab of w to item 2 of argv
            repeat with i from 3 to count of argv
                tell w to make new tab with properties {URL:(item i of argv)}
            end repeat
            set active tab index of w to 1
            activate
        end tell
    end using terms from
end run
"""

_SAFARI_TABS = """
on run argv
    tell application "Safari"
        make new document with properties {URL:(item 1 of argv)}
        repeat with i from 2 to count of argv
            tell front window to make new tab with properties {URL:(item i of argv)}
        end repeat
        set current tab of front window to first tab of front window
        activate
    end tell
end run
"""

_OPEN_LOCATIONS = """
on run argv
    repeat with u in argv
        open location (u as text)
    end repeat
end run
"""


def _http_urls(urls: str | list[str]) -> list[str]:
    items = urls.split() if isinstance(urls, str) else list(urls)
    clean = [u.strip() for u in items if u.strip()]
    bad = [u for u in clean if not u.startswith(("https://", "http://"))]
    if bad or not clean:
        raise MacToolError("only http(s) URLs may be opened")
    return clean[:5]


async def open_in_browser(urls: str | list[str]) -> str:
    """Open URLs as tabs in a new window of the browser the user is running.

    Chrome-family browsers and Safari get one new window with the first URL active;
    anything else falls back to the default browser.
    """
    links = _http_urls(urls)
    browser = await _safe(run_osascript(_RUNNING_BROWSER))
    if browser in ("Google Chrome", "Brave Browser", "Microsoft Edge"):
        await run_osascript(_CHROMIUM_TABS, browser, *links, timeout=8)
    elif browser == "Safari":
        await run_osascript(_SAFARI_TABS, *links, timeout=8)
    else:
        await run_osascript(_OPEN_LOCATIONS, *links, timeout=8)
        browser = "your default browser"
    return f"opened {len(links)} tab{'s' if len(links) != 1 else ''} in {browser}"


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


_CALENDAR_EVENT = """
on run argv
    set {y, mo, d, h, mi, durMin, theTitle, theNotes, calName} to argv
    set startDate to current date
    set day of startDate to 1
    set year of startDate to (y as integer)
    set month of startDate to (mo as integer)
    set day of startDate to (d as integer)
    set hours of startDate to (h as integer)
    set minutes of startDate to (mi as integer)
    set seconds of startDate to 0
    set endDate to startDate + ((durMin as integer) * minutes)
    tell application "Calendar"
        if calName is "" then
            set targetCal to first calendar whose writable is true
        else
            set targetCal to first calendar whose name is calName
        end if
        make new event at end of events of targetCal with properties ¬
            {summary:theTitle, start date:startDate, end date:endDate, description:theNotes}
        return name of targetCal
    end tell
end run
"""


def parse_local_datetime(value: str) -> datetime:
    """Accept ISO 8601 ('2026-10-11T16:45', with or without seconds/offset) as local time."""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise MacToolError(f"start must be ISO 8601 like 2026-10-11T16:45, got {value!r}") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)  # to the Mac's local time
    return parsed


async def create_calendar_event(title: str, start: str, duration_minutes: str = "60",
                                notes: str = "", calendar: str = "") -> str:
    """Add an event to Calendar. Date parts go through argv as numbers, so the
    result does not depend on the Mac's date format or locale."""
    when = parse_local_datetime(start)
    try:
        minutes = max(1, min(int(float(duration_minutes)), 24 * 60))
    except ValueError as exc:
        raise MacToolError("duration_minutes must be a number") from exc
    cal = await run_osascript(
        _CALENDAR_EVENT,
        str(when.year), str(when.month), str(when.day), str(when.hour), str(when.minute),
        str(minutes), title, notes, calendar,
        timeout=15,
    )
    end = when + timedelta(minutes=minutes)
    return (f"event '{title}' added to {cal or 'Calendar'}: "
            f"{when:%a %b %-d, %-I:%M %p} to {end:%-I:%M %p}")


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
        MacTool("open_in_browser", open_in_browser, ("urls",),
                "Open up to 5 space-separated http(s) URLs as tabs in a new browser window"),
        MacTool("create_reminder", create_reminder, ("title", "notes"), "Add a Reminders item"),
        MacTool("create_note", create_note, ("title", "body"), "Create a note in Notes"),
        MacTool("create_calendar_event", create_calendar_event,
                ("title", "start", "duration_minutes", "notes", "calendar"),
                "Add a Calendar event; start is local ISO time like 2026-10-11T16:45, "
                "duration_minutes defaults to 60, calendar defaults to the first writable one"),
        MacTool("show_notification", show_notification, ("title", "message"),
                "Show a macOS notification"),
        MacTool("speak", speak, ("text",), "Speak text aloud"),
    )
}


def validate_args(name: str, args: dict[str, Any]) -> str | None:
    """Why a planned call would fail before running it, or None if it looks fine."""
    tool = TOOLS.get(name)
    if tool is None:
        return f"tool not allowlisted: {name}"
    unknown = set(args) - set(tool.params)
    if unknown:
        return f"unexpected args for {name}: {sorted(unknown)}"
    required = [
        p.name for p in inspect.signature(tool.fn).parameters.values()
        if p.default is inspect.Parameter.empty
    ]
    missing = [p for p in required if not str(args.get(p, "")).strip()]
    if missing:
        return f"missing args for {name}: {missing}"
    return None


async def call_tool(name: str, args: dict[str, Any]) -> Any:
    """Run one allowlisted tool. Unknown tools and bad args are refused."""
    problem = validate_args(name, args)
    if problem:
        raise MacToolError(problem)
    tool = TOOLS[name]
    clean = {k: str(v) for k, v in args.items()}
    return await tool.fn(**clean)


def tool_catalog() -> str:
    """Short text listing for the planner prompt."""
    return "\n".join(f"- {t.name}({', '.join(t.params)}): {t.description}" for t in TOOLS.values())
