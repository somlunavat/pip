"""Give Pip a task without opening the overlay.

    python -m orchestrator.ask "find a cheap flight to Austin next Friday"
    python -m orchestrator.ask --wait "what's on my calendar today"

By default this hands the task to the running orchestrator and exits; the task keeps
going, and the overlay app shows a notification when it finishes or needs your OK.
--wait stays connected and prints the answer instead.

Works from anywhere you can run a command: Terminal, a Shortcuts "Run Shell Script"
action, cron, or a hotkey app.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import websockets

from orchestrator.config import Settings

TOKEN_FILE = Path("data/overlay_token")


async def ask(text: str, *, wait: bool, port: int, token: str, timeout: float) -> int:
    url = f"ws://127.0.0.1:{port}/ws?token={token}"
    try:
        ws = await websockets.connect(url)
    except OSError:
        print("Pip isn't running. Start it with: python -m orchestrator", file=sys.stderr)
        return 2
    async with ws:
        await ws.recv()  # hello
        await ws.send(json.dumps({"type": "request", "text": text, "background": True}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
            kind = msg.get("type")
            if kind == "thinking" and msg.get("task_id") and not wait:
                print(f"Pip is on it ({msg['task_id']}). You'll get a notification when it's "
                      "done.")
                return 0
            if kind == "plan":
                print("Pip needs your OK for this plan; approve it in the Pip notification.")
            elif kind == "doing" and wait:
                print(f"  … {msg.get('label')}")
            elif kind == "response":
                answer = msg.get("answer") or {}
                print(answer.get("headline") or msg.get("text", ""))
                if answer.get("summary"):
                    print(answer["summary"])
                for row in answer.get("rows", []):
                    print(f"  - {row['label']}: {row.get('value', '')}")
                if msg.get("opened"):
                    print(f"  ({msg['opened']})")
                return 0
            elif kind == "error":
                print(f"Pip: {msg.get('message')}", file=sys.stderr)
                return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Give Pip a task in the background.")
    parser.add_argument("text", nargs="+", help="what to do")
    parser.add_argument("--wait", action="store_true", help="wait and print the answer")
    parser.add_argument("--timeout", type=float, default=1800, help="seconds to wait per event")
    args = parser.parse_args()

    if not TOKEN_FILE.exists():
        raise SystemExit("No token yet. Start Pip once with: python -m orchestrator")
    settings = Settings.from_env()
    code = asyncio.run(ask(" ".join(args.text), wait=args.wait, port=settings.port,
                           token=TOKEN_FILE.read_text().strip(), timeout=args.timeout))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
