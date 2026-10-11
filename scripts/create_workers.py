"""Create the always-on Hermes worker pool ahead of the demo (P0).

    python scripts/create_workers.py --count 2 --credit 3 --cap 10

Prints the instance ids to put in AGENT37_WORKERS, then polls health until every
worker reports healthy. Requires a topped-up wallet: the free tier allows one
instance, and create needs one day of balance.
"""

from __future__ import annotations

import argparse
import asyncio
import time

from orchestrator.agent37 import Agent37Control, WorkerClient
from orchestrator.config import Settings
from orchestrator.security import new_callback_token


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", type=int, default=2)
    parser.add_argument("--credit", type=float, default=3.0, help="USD of model budget per worker")
    parser.add_argument("--cap", type=float, default=10.0, help="USD monthly cap per worker")
    parser.add_argument("--user", default="pip-demo")
    parser.add_argument("--wait", type=int, default=300, help="seconds to wait for health")
    args = parser.parse_args()

    settings = Settings.from_env()
    if not settings.agent37_api_key:
        raise SystemExit("AGENT37_API_KEY is not set")
    control = Agent37Control(settings.agent37_api_key, base_url=settings.agent37_api_url)

    ids: list[str] = []
    try:
        for i in range(args.count):
            token, digest = new_callback_token()
            inst = await control.create_worker(
                f"pip-worker-{i + 1}", credit_usd=args.credit, monthly_cap_usd=args.cap,
                user=args.user, env={"PIP_CALLBACK_TOKEN": token},
                metadata={"callback_token_sha256": digest},
            )
            ids.append(inst["id"])
            print(f"created {inst['id']} ({inst.get('status')})")
    finally:
        await control.aclose()

    print(f"\nAGENT37_WORKERS={','.join(ids)}\n")
    workers = [WorkerClient(i, settings.agent37_api_key) for i in ids]
    deadline = time.monotonic() + args.wait
    pending = set(ids)
    while pending and time.monotonic() < deadline:
        for w in workers:
            if w.instance_id in pending and await w.health():
                pending.discard(w.instance_id)
                print(f"{w.instance_id} healthy")
        if pending:
            await asyncio.sleep(5)
    await asyncio.gather(*(w.aclose() for w in workers))
    if pending:
        raise SystemExit(f"not healthy after {args.wait}s: {sorted(pending)}")


if __name__ == "__main__":
    asyncio.run(main())
