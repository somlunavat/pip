"""Tiny terminal client for the orchestrator (the fallback if the overlay misbehaves).

    python scripts/ws_client.py "find the three cheapest studios near UT Austin"
    python scripts/ws_client.py            # interactive
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import websockets


async def run(prompts: list[str], token: str) -> None:
    async with websockets.connect(f"ws://127.0.0.1:8779/ws?token={token}") as ws:
        await ws.recv()  # hello
        interactive = not prompts
        while True:
            if interactive:
                try:
                    text = (await asyncio.to_thread(input, "\n> ")).strip()
                except EOFError:
                    return
            elif prompts:
                text = prompts.pop(0)
            else:
                return
            if not text:
                continue
            await ws.send(json.dumps({"type": "request", "text": text}))
            while True:
                msg = json.loads(await ws.recv())
                kind = msg.get("type")
                if kind == "doing":
                    print(f"  … [{msg.get('path')}] {msg.get('label')}")
                elif kind == "ack":
                    print(f"  ✓ {msg['text']}")
                elif kind == "plan":
                    for m in msg["milestones"]:
                        print(f"  plan: {m['id']} {m['kind']} {m['goal']}")
                    answer = await asyncio.to_thread(input, "  Proceed? [y/N] ")
                    ok = answer.strip().lower() == "y"
                    await ws.send(json.dumps({"type": "approve" if ok else "cancel",
                                              "task_id": msg["task_id"]}))
                elif kind == "response":
                    print(f"\n{msg['text']}")
                    for s in msg.get("sources", []):
                        print(f"  - {s['url']}")
                    if msg.get("opened"):
                        print(f"  ↗ {msg['opened']}")
                    if "elapsed_ms" in msg:
                        print(f"  ({msg['elapsed_ms'] / 1000:.1f}s via "
                              f"{', '.join(msg.get('paths', []))})")
                    break
                elif kind == "error":
                    print(f"  ! {msg['message']}")
                    break


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1:], Path("data/overlay_token").read_text().strip()))
