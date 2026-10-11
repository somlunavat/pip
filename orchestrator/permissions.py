"""macOS permissions Pip can use, and a guided way to grant them.

macOS attaches privacy permissions to the app that launched the orchestrator
(Terminal, iTerm, VS Code), not to Python. Every grant goes through a macOS prompt
or System Settings; nothing here changes a permission by itself.

    python -m orchestrator.permissions          # walk through every prompt
    python -m orchestrator.permissions --check  # just show the status
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestrator import mac

STATE_FILE = Path("data/permissions.json")
_PRIVACY = "x-apple.systempreferences:com.apple.preference.security?Privacy_"
PANES = {
    "accessibility": _PRIVACY + "Accessibility",
    "screen": _PRIVACY + "ScreenCapture",
    "automation": _PRIVACY + "Automation",
}
_HOLDERS = {"Apple_Terminal": "Terminal", "iTerm.app": "iTerm", "vscode": "VS Code",
            "WarpTerminal": "Warp", "ghostty": "Ghostty"}


@dataclass(frozen=True)
class Permission:
    id: str
    name: str
    why: str
    kind: str  # automation | accessibility | screen
    probe: str = ""  # read-only AppleScript that triggers the Automation prompt
    requires: str = ""  # app bundle that must be installed


PERMISSIONS: list[Permission] = [
    Permission("accessibility", "Accessibility", "read selected text and type into apps",
               "accessibility"),
    Permission("screen", "Screen Recording", "see your screen when you ask about it", "screen"),
    Permission("automation:System Events", "System Events",
               "know which app and window are in front", "automation",
               'tell application "System Events" to get name of first process whose frontmost '
               "is true"),
    Permission("automation:Calendar", "Calendar", "read and add events", "automation",
               'tell application "Calendar" to count calendars'),
    Permission("automation:Reminders", "Reminders", "read and add reminders", "automation",
               'tell application "Reminders" to count lists'),
    Permission("automation:Notes", "Notes", "create notes", "automation",
               'tell application "Notes" to count folders'),
    Permission("automation:Mail", "Mail", "open email drafts for you to send", "automation",
               'tell application "Mail" to count accounts'),
    Permission("automation:Music", "Music", "play, pause, and see what's playing", "automation",
               'tell application "Music" to get player state'),
    Permission("automation:Finder", "Finder", "show and open your files", "automation",
               'tell application "Finder" to get name of startup disk'),
    Permission("automation:Shortcuts Events", "Shortcuts", "run your Shortcuts", "automation",
               'tell application "Shortcuts Events" to count shortcuts'),
    Permission("automation:Google Chrome", "Google Chrome", "open results and read your tabs",
               "automation", 'tell application "Google Chrome" to count windows',
               "/Applications/Google Chrome.app"),
    Permission("automation:Safari", "Safari", "open results and read your tabs", "automation",
               'tell application "Safari" to count windows', "/Applications/Safari.app"),
]
BY_ID = {p.id: p for p in PERMISSIONS}

# What each Mac tool needs. Browser tools depend on which browser is running and
# handle their own errors, so they aren't listed.
TOOL_NEEDS: dict[str, list[str]] = {
    "get_context": ["automation:System Events"],
    "list_calendar_events": ["automation:Calendar"],
    "create_calendar_event": ["automation:Calendar"],
    "list_reminders": ["automation:Reminders"],
    "create_reminder": ["automation:Reminders"],
    "create_note": ["automation:Notes"],
    "draft_email": ["automation:Mail"],
    "now_playing": ["automation:System Events"],
    "media_control": ["automation:System Events"],
    "set_dark_mode": ["automation:System Events"],
    "reveal_in_finder": ["automation:Finder"],
    "open_file": ["automation:Finder"],
    "run_shortcut": ["automation:Shortcuts Events"],
    "get_selected_text": ["accessibility"],
    "type_text": ["accessibility"],
    "capture_screen": ["screen"],
}


def holder() -> str:
    """The app macOS will attach these permissions to."""
    return _HOLDERS.get(os.environ.get("TERM_PROGRAM", ""), "the app that started Pip")


def available() -> list[Permission]:
    return [p for p in PERMISSIONS if not p.requires or Path(p.requires).exists()]


# -- system APIs for the two permissions that have a status check ---------------------

def _framework(name: str) -> Any:
    return ctypes.cdll.LoadLibrary(f"/System/Library/Frameworks/{name}.framework/{name}")


def accessibility_trusted() -> bool:
    if sys.platform != "darwin":
        return False
    fn = _framework("ApplicationServices").AXIsProcessTrusted
    fn.restype = ctypes.c_bool
    return bool(fn())


def screen_recording_allowed(request: bool = False) -> bool:
    """CGPreflight... only checks; CGRequest... also shows the macOS prompt once."""
    if sys.platform != "darwin":
        return False
    cg = _framework("CoreGraphics")
    fn = cg.CGRequestScreenCaptureAccess if request else cg.CGPreflightScreenCaptureAccess
    fn.restype = ctypes.c_bool
    return bool(fn())


# -- state ------------------------------------------------------------------------------

def _load() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _record(pid: str, status: str) -> str:
    state = _load()
    state[pid] = {"status": status, "checked_at": datetime.now(UTC).isoformat()}
    _save(state)
    return status


def status(pid: str) -> str:
    """granted | denied | unknown. Automation has no silent check, so it reports
    the result of the last request."""
    perm = BY_ID[pid]
    if perm.kind == "accessibility":
        return "granted" if accessibility_trusted() else "denied"
    if perm.kind == "screen":
        return "granted" if screen_recording_allowed() else "denied"
    return _load().get(pid, {}).get("status", "unknown")


def needed_for(tools: list[str]) -> list[str]:
    """Permissions a set of tools needs, in a stable order, skipping uninstalled apps."""
    installed = {p.id for p in available()}
    out: list[str] = []
    for tool in tools:
        for pid in TOOL_NEEDS.get(tool, []):
            if pid in installed and pid not in out:
                out.append(pid)
    return out


def classify(pid: str) -> str:
    """granted | ask_os (macOS will prompt on first use) | ask_user (Pip must ask)."""
    current = status(pid)
    if current == "granted":
        return "granted"
    if BY_ID[pid].kind == "automation" and current == "unknown":
        return "ask_os"
    return "ask_user"


def describe(pid: str) -> dict[str, str]:
    perm = BY_ID[pid]
    return {"id": pid, "name": perm.name, "why": perm.why, "kind": perm.kind}


def report() -> dict[str, Any]:
    return {
        "holder": holder(),
        "items": [{"id": p.id, "name": p.name, "why": p.why, "status": status(p.id)}
                  for p in available()],
    }


# -- requests -----------------------------------------------------------------------------

async def _open_pane(kind: str) -> None:
    proc = await asyncio.create_subprocess_exec("/usr/bin/open", PANES[kind])
    await proc.wait()


async def request(pid: str) -> str:
    """Ask macOS for one permission. The user answers the system prompt."""
    perm = BY_ID[pid]
    if perm.kind == "screen":
        if screen_recording_allowed(request=True):
            return "granted"
        await _open_pane("screen")
        return "denied"
    if perm.kind == "accessibility":
        if accessibility_trusted():
            return "granted"
        # A read-only accessibility call makes macOS show its own prompt.
        try:
            await mac.get_selected_text()
        except mac.MacToolError:
            pass
        if accessibility_trusted():
            return "granted"
        await _open_pane("accessibility")
        return "denied"
    # Automation: the first Apple event shows the prompt and waits for the answer.
    try:
        await mac.run_osascript(perm.probe, timeout=90)
        return _record(pid, "granted")
    except mac.MacToolError as exc:
        text = str(exc)
        if "System Settings" in text or "-1743" in text:
            return _record(pid, "denied")
        return _record(pid, "unknown")


async def request_all(ids: list[str] | None = None) -> dict[str, Any]:
    for pid in ids or [p.id for p in available()]:
        if status(pid) != "granted":
            await request(pid)
    return report()


# -- CLI -----------------------------------------------------------------------------------

def _print(rep: dict[str, Any]) -> None:
    marks = {"granted": "✓", "denied": "✗", "unknown": "?"}
    for item in rep["items"]:
        print(f"  {marks[item['status']]} {item['name']:<16} {item['why']}")


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Grant Pip the macOS permissions it can use.")
    parser.add_argument("--check", action="store_true", help="only show the current status")
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise SystemExit("macOS only")
    print(f"Permissions are granted to {holder()}, the app running Pip.\n")
    if args.check:
        _print(report())
        return
    for perm in available():
        if status(perm.id) == "granted":
            continue
        print(f"→ {perm.name}: lets Pip {perm.why}. Answer the macOS prompt…")
        result = await request(perm.id)
        if result != "granted":
            hint = ("toggle it on in the System Settings pane that just opened"
                    if perm.kind != "automation" else
                    "allow it in System Settings > Privacy & Security > Automation")
            print(f"  not granted yet: {hint}")
    print()
    _print(report())


if __name__ == "__main__":
    asyncio.run(_main())
