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
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

DEFAULT_TIMEOUT_S = 3.0

# Tools that write data or act as the user; plans containing them need Proceed.
WRITE_TOOLS = {"create_reminder", "create_note", "create_calendar_event", "run_shortcut",
               "type_text", "save_text_file", "quit_app", "custom_applescript"}
# Tools that only show or adjust something and are undone by closing a window or
# flipping it back; drafts are never sent. No approval, so Pip can just do them.
DISPLAY_TOOLS = {"open_app", "open_url", "open_in_browser", "show_notification", "speak",
                 "draft_email", "draft_message", "media_control", "set_volume",
                 "set_dark_mode", "reveal_in_finder", "open_file"}
BROWSER_TOOLS = {"open_url", "open_in_browser"}
SIDE_EFFECT_TOOLS = WRITE_TOOLS | DISPLAY_TOOLS
READ_ONLY_TOOLS = {"get_context", "list_calendar_events", "list_reminders", "get_browser_tabs",
                   "get_selected_text", "capture_screen", "now_playing"}

PIP_FILES = Path.home() / "Documents" / "Pip"
SCREENS = Path("data/screens")


class MacToolError(RuntimeError):
    pass


_NOT_AUTHORIZED = re.compile(r"-1743|not authori[sz]ed to send apple events", re.IGNORECASE)
_TARGET_APP = re.compile(r'application "([^"]+)"')


_NO_ASSISTIVE = re.compile(r"-1719|-25211|assistive access|not allowed to send keystrokes",
                           re.IGNORECASE)


def _explain(err: str, script: str) -> str:
    """Turn macOS's terse permission errors into something the user can act on."""
    if _NO_ASSISTIVE.search(err):
        return ("Pip needs Accessibility access for that. Allow it in System Settings > "
                "Privacy & Security > Accessibility (or type 'setup' in Pip), then try again.")
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


# -- Reading your Mac (run immediately) ---------------------------------------------

_DAY_EVENTS = """
on run argv
    set {y, mo, d} to argv
    set dayStart to current date
    set day of dayStart to 1
    set year of dayStart to (y as integer)
    set month of dayStart to (mo as integer)
    set day of dayStart to (d as integer)
    set time of dayStart to 0
    set dayEnd to dayStart + (1 * days)
    set out to ""
    tell application "Calendar"
        repeat with c in calendars
            set evs to (every event of c whose start date >= dayStart and start date < dayEnd)
            repeat with e in evs
                set startText to (start date of e) as «class isot» as string
                set out to out & (summary of e) & tab & startText & tab & (name of c) & linefeed
            end repeat
        end repeat
    end tell
    return out
end run
"""


async def list_calendar_events(day: str = "") -> str:
    """Events on one day (default today), sorted by start. Calendar's scripting
    interface does not expand repeating events, so those can be missing."""
    when = date.fromisoformat(day.strip()[:10]) if day.strip() else date.today()
    out = await run_osascript(_DAY_EVENTS, str(when.year), str(when.month), str(when.day),
                              timeout=25)
    rows = sorted(line.split("\t") for line in out.splitlines() if line.strip())
    rows.sort(key=lambda r: r[1] if len(r) > 1 else "")
    if not rows:
        return f"No events on {when:%a %b %-d}."
    return "\n".join(f"{r[1][11:16]}  {r[0]}  ({r[2]})" if len(r) > 2 else r[0] for r in rows)


_OPEN_REMINDERS = """
tell application "Reminders"
    set out to ""
    repeat with r in (reminders whose completed is false)
        set due to ""
        try
            set due to (due date of r) as «class isot» as string
        end try
        set out to out & (name of r) & tab & due & tab & (name of container of r) & linefeed
    end repeat
    return out
end tell
"""


async def list_reminders() -> str:
    out = await run_osascript(_OPEN_REMINDERS, timeout=25)
    lines = [line.split("\t") for line in out.splitlines() if line.strip()][:40]
    if not lines:
        return "No open reminders."
    return "\n".join(
        f"- {r[0]}" + (f" (due {r[1][:16].replace('T', ' ')})" if len(r) > 1 and r[1] else "")
        + (f" [{r[2]}]" if len(r) > 2 else "")
        for r in lines
    )


# Inside a browser's tell block `tab` means a browser tab, so use character id 9.
_CHROMIUM_LIST_TABS = """
on run argv
    set sep to character id 9
    using terms from application "Google Chrome"
        tell application (item 1 of argv)
            set out to ""
            repeat with t in tabs of front window
                set out to out & (title of t) & sep & (URL of t) & linefeed
            end repeat
            return out
        end tell
    end using terms from
end run
"""

_SAFARI_LIST_TABS = """
set sep to character id 9
tell application "Safari"
    set out to ""
    repeat with t in tabs of front window
        set out to out & (name of t) & sep & (URL of t) & linefeed
    end repeat
    return out
end tell
"""


async def get_browser_tabs() -> str:
    """Titles and URLs of the tabs in the front window of the running browser."""
    browser = await _safe(run_osascript(_RUNNING_BROWSER))
    if browser in ("Google Chrome", "Brave Browser", "Microsoft Edge"):
        out = await run_osascript(_CHROMIUM_LIST_TABS, browser, timeout=8)
    elif browser == "Safari":
        out = await run_osascript(_SAFARI_LIST_TABS, timeout=8)
    else:
        return "No supported browser (Chrome, Brave, Edge, Safari) is running."
    tabs = [line.split("\t") for line in out.splitlines() if line.strip()][:30]
    return f"{browser} tabs:\n" + "\n".join(
        f"- {t[0]} {t[1]}" if len(t) > 1 else f"- {t[0]}" for t in tabs
    )


_SELECTED_TEXT = """
tell application "System Events"
    set p to first application process whose frontmost is true
    set f to value of attribute "AXFocusedUIElement" of p
    try
        return value of attribute "AXSelectedText" of f
    on error
        return ""
    end try
end tell
"""


async def get_selected_text() -> str:
    """Text selected in the front app, read through Accessibility without touching
    the clipboard. Untrusted: it is fenced before it reaches the planner."""
    text = await run_osascript(_SELECTED_TEXT, timeout=5)
    return text[:8000] if text else "Nothing is selected in the front app."


async def capture_screen() -> dict[str, str]:
    """Screenshot of the whole screen for Gemini to look at (Screen Recording)."""
    if sys.platform != "darwin":
        raise MacToolError("screen capture only runs on macOS")
    path = await asyncio.to_thread(_new_screen_path)
    proc = await asyncio.create_subprocess_exec(
        "/usr/sbin/screencapture", "-x", "-t", "png", str(path),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await asyncio.wait_for(proc.communicate(), timeout=10)
    if proc.returncode != 0 or not path.exists():
        raise MacToolError("screen capture failed: allow Screen Recording in System Settings > "
                           "Privacy & Security (or type 'setup' in Pip). " + err.decode()[:200])
    await asyncio.to_thread(_prune_screens)
    return {"image_path": str(path), "text": "Captured the screen."}


def _new_screen_path() -> Path:
    SCREENS.mkdir(parents=True, exist_ok=True)
    return (SCREENS / f"screen-{int(time.time() * 1000)}.png").resolve()


def _prune_screens(keep: int = 5) -> None:
    for old in sorted(SCREENS.glob("screen-*.png"))[:-keep]:
        old.unlink(missing_ok=True)


_NOW_PLAYING = """
tell application "System Events"
    set players to {}
    if exists (process "Spotify") then set end of players to "Spotify"
    if exists (process "Music") then set end of players to "Music"
end tell
repeat with appName in players
    using terms from application "Music"
        tell application (appName as text)
            if player state is playing then ¬
                return (appName as text) & ": " & (name of current track) & " by " & ¬
                    (artist of current track)
        end tell
    end using terms from
end repeat
return "Nothing is playing."
"""


async def now_playing() -> str:
    return await run_osascript(_NOW_PLAYING, timeout=8)


# -- Showing and adjusting (reversible, run immediately) ----------------------------

_DRAFT_EMAIL = """
on run argv
    set {toAddr, theSubject, theBody} to argv
    tell application "Mail"
        set msg to make new outgoing message with properties ¬
            {subject:theSubject, content:theBody, visible:true}
        if toAddr is not "" then
            tell msg to make new to recipient at end of to recipients ¬
                with properties {address:toAddr}
        end if
        activate
    end tell
end run
"""


async def draft_email(to: str, subject: str, body: str = "") -> str:
    """Open a Mail draft. Pip never sends it; you review and press Send."""
    await run_osascript(_DRAFT_EMAIL, to.strip(), subject, body, timeout=15)
    return f"opened a Mail draft to {to or 'no one yet'}: {subject}"


_OPEN_LOCATION = "on run argv\n open location (item 1 of argv)\nend run"


async def draft_message(to: str, body: str) -> str:
    """Open Messages to a phone number or email with the text filled in. Pip never
    sends it; you press Return to send."""
    recipient = re.sub(r"[^\w+@.\-]", "", to)
    if not recipient:
        raise MacToolError("draft_message needs a phone number or email address")
    url = f"sms://open?addresses={quote(recipient)}&body={quote(body)}"
    await run_osascript(_OPEN_LOCATION, url)
    return f"opened Messages to {recipient} with the text filled in; press Return to send"


_MEDIA = """
on run argv
    set action to item 1 of argv
    tell application "System Events" to set useSpotify to exists (process "Spotify")
    if useSpotify then
        set target to "Spotify"
    else
        set target to "Music"
    end if
    using terms from application "Music"
        tell application target
            if action is "play" then
                play
            else if action is "pause" then
                pause
            else if action is "next" then
                next track
            else if action is "previous" then
                previous track
            else
                playpause
            end if
        end tell
    end using terms from
    return target
end run
"""


async def media_control(action: str) -> str:
    action = action.strip().lower()
    if action not in {"play", "pause", "playpause", "next", "previous"}:
        raise MacToolError("action must be play, pause, playpause, next, or previous")
    app = await run_osascript(_MEDIA, action, timeout=8)
    return f"{action} in {app}"


async def set_volume(percent: str) -> str:
    level = max(0, min(100, int(float(percent))))
    await run_osascript("on run argv\n set volume output volume (item 1 of argv as integer)\n"
                        "end run", str(level))
    return f"volume set to {level}%"


async def set_dark_mode(on: str) -> str:
    dark = on.strip().lower() in {"1", "true", "yes", "on", "dark"}
    await run_osascript(
        'on run argv\n tell application "System Events" to tell appearance preferences to '
        'set dark mode to ((item 1 of argv) is "true")\nend run',
        "true" if dark else "false",
    )
    return f"{'dark' if dark else 'light'} mode on"


def _home_path(path: str) -> Path:
    """Resolve a user-supplied path and require it to exist inside the home folder."""
    resolved = Path(path).expanduser().resolve()
    home = Path.home().resolve()
    if home not in resolved.parents and resolved != home:
        raise MacToolError("only files inside your home folder can be opened")
    if not resolved.exists():
        raise MacToolError(f"no such file: {resolved}")
    return resolved


async def reveal_in_finder(path: str) -> str:
    target = _home_path(path)
    await run_osascript(
        'on run argv\n tell application "Finder"\n reveal (POSIX file (item 1 of argv) as alias)'
        "\n activate\n end tell\nend run",
        str(target),
    )
    return f"revealed {target.name} in Finder"


async def open_file(path: str) -> str:
    target = _home_path(path)
    await run_osascript(
        'on run argv\n tell application "Finder" to open (POSIX file (item 1 of argv) as alias)'
        "\nend run",
        str(target),
    )
    return f"opened {target.name}"


# -- Acting as you (Proceed required) -------------------------------------------------

_RUN_SHORTCUT = """
on run argv
    tell application "Shortcuts Events"
        if (item 2 of argv) is "" then
            set r to run shortcut (item 1 of argv)
        else
            set r to run shortcut (item 1 of argv) with input (item 2 of argv)
        end if
    end tell
    try
        return r as text
    on error
        return ""
    end try
end run
"""


async def run_shortcut(name: str, input: str = "") -> str:  # noqa: A002 - matches the tool arg
    """Run one of the user's Shortcuts, which can do anything they built it to do."""
    out = await run_osascript(_RUN_SHORTCUT, name, input, timeout=120)
    return f"ran shortcut '{name}'" + (f": {out[:2000]}" if out else "")


async def type_text(text: str) -> str:
    """Type text into whatever has focus in the front app (Accessibility)."""
    if len(text) > 2000:
        raise MacToolError("type_text is limited to 2000 characters")
    await run_osascript(
        'on run argv\n tell application "System Events" to keystroke (item 1 of argv)\nend run',
        text, timeout=30,
    )
    return f"typed {len(text)} characters"


async def save_text_file(name: str, content: str) -> str:
    """Save text under ~/Documents/Pip without overwriting anything."""
    stem = re.sub(r"[^\w .\-]", "", Path(name).name).strip(" .") or "note"
    if not Path(stem).suffix:
        stem += ".md"
    PIP_FILES.mkdir(parents=True, exist_ok=True)
    target = PIP_FILES / stem
    n = 2
    while target.exists():
        target = PIP_FILES / f"{Path(stem).stem} ({n}){Path(stem).suffix}"
        n += 1
    target.write_text(content, encoding="utf-8")
    return str(target)


async def quit_app(name: str) -> str:
    await run_osascript("on run argv\n tell application (item 1 of argv) to quit\nend run",
                        name, timeout=15)
    return f"quit {name}"


# -- Custom actions: a one-off script Pip writes, shown to and approved by the user ----

_APP_NAME = re.compile(r"^[\w .&'+-]{1,60}$")
_TELL_APP = re.compile(r'\btell\s+application\s+"([^"]+)"', re.IGNORECASE)
# Whatever the user approves, these never run: they could delete, send, spend,
# escalate, exfiltrate, or run arbitrary code.
_FORBIDDEN: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p, re.IGNORECASE), why) for p, why in [
        (r"\bdo\s+shell\s+script\b|\bosascript\b", "shell commands"),
        (r"\badministrator\s+privileges\b|\bsudo\b", "admin privileges"),
        (r"\bpassword\b|\bkeychain\b|\bcredential", "passwords or the keychain"),
        (r"\bdelete\b|\berase\b|\bempty\s+(the\s+)?trash\b|\bto\s+(the\s+)?trash\b",
         "deleting things"),
        (r"\bsend\b|\breply\b|\bforward\b|\bpost\b", "sending or posting"),
        (r"\bopen\s+for\s+access\b|\bwrite\b|\bset\s+eof\b", "writing files"),
        (r"\brestart\b|\bshut\s*down\b|\blog\s*out\b", "restart or shutdown"),
        (r"\b(run|load|store)\s+script\b", "running other scripts"),
        (r"\b(do|execute)\s+javascript\b", "running JavaScript in a browser"),
        (r"\bopen\s+location\b|\bget\s+url\b|https?://", "network access"),
        (r"\bkeystroke\b|\bkey\s+code\b", "typing keys (use type_text)"),
        (r"\bpurchase\b|\bbuy\b|\bcheckout\b", "purchases"),
        (r"«", "raw Apple event codes"),
    ]
]
MAX_SCRIPT_CHARS = 4000


def vet_script(app: str, script: str) -> str | None:
    """Why a proposed script can't run, or None if it passes the static check.

    The script may only tell the one app it names, and none of _FORBIDDEN may appear.
    This sits in front of the user's approval, not instead of it.
    """
    if not _APP_NAME.match(app or ""):
        return "invalid app name"
    if len(script) > MAX_SCRIPT_CHARS:
        return f"script is longer than {MAX_SCRIPT_CHARS} characters"
    targets = {t.strip().lower() for t in _TELL_APP.findall(script)}
    if not targets:
        return f'script must use tell application "{app}"'
    if targets != {app.strip().lower()}:
        return f"script may only control {app}"
    for pattern, why in _FORBIDDEN:
        if pattern.search(script):
            return f"scripts can't do {why}"
    return None


async def custom_applescript(app: str, purpose: str, script: str) -> str:
    """Run a one-off AppleScript the user saw and approved (needs Proceed)."""
    problem = vet_script(app, script)
    if problem:
        raise MacToolError(f"custom action refused: {problem}")
    out = await run_osascript(script, timeout=30)
    return out[:4000] if out else f"done: {purpose}"


@dataclass(frozen=True)
class MacTool:
    name: str
    fn: Callable[..., Awaitable[Any]]
    params: tuple[str, ...]
    description: str


TOOLS: dict[str, MacTool] = {
    t.name: t
    for t in (
        # Read
        MacTool("get_context", get_context, (), "Read active app, window, browser URL, clipboard"),
        MacTool("list_calendar_events", list_calendar_events, ("day",),
                "List Calendar events on a day (YYYY-MM-DD, default today)"),
        MacTool("list_reminders", list_reminders, (), "List open Reminders with due dates"),
        MacTool("get_browser_tabs", get_browser_tabs, (),
                "List titles and URLs of the tabs open in the front browser window"),
        MacTool("get_selected_text", get_selected_text, (),
                "Read the text the user has selected in the front app"),
        MacTool("capture_screen", capture_screen, (),
                "Take a screenshot so you can see what is on the user's screen"),
        MacTool("now_playing", now_playing, (), "What Music or Spotify is playing"),
        # Show / adjust (reversible)
        MacTool("open_app", open_app, ("name",), "Bring an application to the front"),
        MacTool("open_url", open_url, ("url",), "Open an http(s) URL in the default browser"),
        MacTool("open_in_browser", open_in_browser, ("urls",),
                "Open up to 5 space-separated http(s) URLs as tabs in a new browser window"),
        MacTool("open_file", open_file, ("path",), "Open a file in your home folder"),
        MacTool("reveal_in_finder", reveal_in_finder, ("path",),
                "Show a file or folder from your home folder in Finder"),
        MacTool("draft_email", draft_email, ("to", "subject", "body"),
                "Open a Mail draft for the user to review and send (never sends)"),
        MacTool("draft_message", draft_message, ("to", "body"),
                "Open Messages to a phone number or email with the text filled in; the user "
                "presses send (never sends)"),
        MacTool("media_control", media_control, ("action",),
                "Music/Spotify: play, pause, playpause, next, or previous"),
        MacTool("set_volume", set_volume, ("percent",), "Set output volume 0-100"),
        MacTool("set_dark_mode", set_dark_mode, ("on",), "Turn dark mode on (true) or off"),
        MacTool("show_notification", show_notification, ("title", "message"),
                "Show a macOS notification"),
        MacTool("speak", speak, ("text",), "Speak text aloud"),
        # Write / act as the user (needs Proceed)
        MacTool("create_reminder", create_reminder, ("title", "notes"), "Add a Reminders item"),
        MacTool("create_note", create_note, ("title", "body"), "Create a note in Notes"),
        MacTool("create_calendar_event", create_calendar_event,
                ("title", "start", "duration_minutes", "notes", "calendar"),
                "Add a Calendar event; start is local ISO time like 2026-10-11T16:45, "
                "duration_minutes defaults to 60, calendar defaults to the first writable one"),
        MacTool("run_shortcut", run_shortcut, ("name", "input"),
                "Run one of the user's Shortcuts by name, with optional text input"),
        MacTool("type_text", type_text, ("text",), "Type text into the front app"),
        MacTool("save_text_file", save_text_file, ("name", "content"),
                "Save text as a file in ~/Documents/Pip (never overwrites)"),
        MacTool("quit_app", quit_app, ("name",), "Quit an application"),
        MacTool("custom_applescript", custom_applescript, ("app", "purpose", "script"),
                "Last resort when no tool above fits: a short AppleScript that controls one "
                "scriptable app (it may only tell that app). The user sees the script and "
                "must approve it. It can't send, delete, write files, use the network or "
                "shell, or touch passwords"),
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
