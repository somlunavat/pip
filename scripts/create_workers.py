"""Set up the Agent37 worker pool (P0).

    python scripts/create_workers.py --count 2 --credit 3 --cap 10   # create new workers
    python scripts/create_workers.py --adopt qw5durb752 --credit 3    # use an existing one
    python scripts/create_workers.py --list                           # show instances

Creating workers needs a topped-up wallet: the free tier allows one instance at a
time. On the free tier, adopt the instance you already have instead.

New workers are hardened at create (Perflo and Composio off, planted callback
token). Those settings are write-only at create, so an adopted instance keeps
whatever it was created with; the script says so.

Prints the AGENT37_WORKERS line to put in .env, then waits until every worker is
healthy.
"""

from __future__ import annotations

import argparse
import asyncio
import time
import uuid

from orchestrator.agent37 import Agent37Control, Agent37Error, WorkerClient
from orchestrator.config import Settings
from orchestrator.security import new_callback_token


def _usd(micros: int | None) -> str:
    return "none" if micros is None else f"${micros / 1_000_000:.2f}"


def _describe(inst: dict) -> str:
    role = (inst.get("metadata") or {}).get("role", "-")
    return (f"  {inst['id']}  {inst.get('name') or '-':<16} {inst.get('status', '?'):<9} "
            f"template={inst.get('template', '?')} role={role}")


async def list_instances(control: Agent37Control) -> list[dict]:
    instances = await control.list_instances()
    if not instances:
        print("No instances in this workspace.")
    for inst in instances:
        print(_describe(inst))
    return instances


async def create(control: Agent37Control, args: argparse.Namespace) -> list[str]:
    ids: list[str] = []
    for i in range(args.count):
        token, digest = new_callback_token()
        try:
            inst = await control.create_worker(
                f"pip-worker-{i + 1}", credit_usd=args.credit, monthly_cap_usd=args.cap,
                user=args.user, env={"PIP_CALLBACK_TOKEN": token},
                metadata={"callback_token_sha256": digest},
            )
        except Agent37Error as exc:
            if exc.code == "instance_limit_reached":
                print("\nYour plan allows one instance at a time, and this workspace is at "
                      "the limit. Existing instances:")
                existing = await list_instances(control)
                if existing:
                    print(f"\nEither add funds to the workspace wallet and run this again, or "
                          f"use the one you have:\n  python scripts/create_workers.py "
                          f"--adopt {existing[0]['id']} --credit {args.credit} --cap {args.cap}")
            else:
                print(f"\nCould not create worker {i + 1}: {exc.code}: {exc}")
            break
        ids.append(inst["id"])
        print(f"created {inst['id']} ({inst.get('status')})")
    return ids


async def adopt(control: Agent37Control, args: argparse.Namespace) -> list[str]:
    inst = await control.get_instance(args.adopt)
    print(_describe(inst))
    if inst.get("template") != "agent37-hermes":
        print(f"  note: template is {inst.get('template')}; Pip expects agent37-hermes")

    # GET /v1/instances/{id} can show budget: null even when a cap is set, so read
    # the budget endpoint. Only add a cap or credit if there is no headroom left.
    budget = await control.get_budget(args.adopt)
    cap = budget.get("monthly_cap_micros") or 0
    headroom = (budget.get("monthly_remaining_micros") or 0) + (
        budget.get("credit_remaining_micros") or 0)
    print(f"  budget: monthly cap {_usd(cap)}, {_usd(headroom)} left this month")
    if not cap:
        await control.set_monthly_cap(args.adopt, args.cap)
        print(f"  set monthly cap to ${args.cap:.2f}")
    elif not headroom and args.credit:
        await control.top_up(args.adopt, args.credit, idempotency_key=f"pip-adopt-{uuid.uuid4()}")
        print(f"  added ${args.credit:.2f} of credit (a ceiling; the workspace wallet pays)")
    else:
        print("  budget is fine; nothing changed")

    print("  note: this instance was not created by Pip, so the Perflo and Composio managed\n"
          "  plugins may still be on (they can only be turned off at create). Don't connect\n"
          "  accounts to it. After topping up the wallet, a worker created by this script\n"
          "  comes with them off.")
    return [args.adopt]


async def wait_healthy(ids: list[str], api_key: str, wait_s: int) -> list[str]:
    workers = [WorkerClient(i, api_key) for i in ids]
    deadline = time.monotonic() + wait_s
    pending = set(ids)
    try:
        while pending and time.monotonic() < deadline:
            for w in workers:
                if w.instance_id in pending and await w.health():
                    pending.discard(w.instance_id)
                    print(f"{w.instance_id} healthy")
            if pending:
                await asyncio.sleep(5)
    finally:
        await asyncio.gather(*(w.aclose() for w in workers))
    return sorted(pending)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Set up the Agent37 worker pool.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--adopt", metavar="INSTANCE_ID", help="use an existing instance")
    mode.add_argument("--list", action="store_true", help="list instances and exit")
    parser.add_argument("--count", type=int, default=2, help="workers to create")
    parser.add_argument("--credit", type=float, default=3.0, help="USD of model credit per worker")
    parser.add_argument("--cap", type=float, default=10.0, help="USD monthly cap per worker")
    parser.add_argument("--user", default="pip-demo")
    parser.add_argument("--wait", type=int, default=300, help="seconds to wait for health")
    args = parser.parse_args()

    settings = Settings.from_env()
    if not settings.agent37_api_key:
        raise SystemExit("AGENT37_API_KEY is not set in .env")
    control = Agent37Control(settings.agent37_api_key, base_url=settings.agent37_api_url)
    try:
        if args.list:
            await list_instances(control)
            return
        ids = await (adopt(control, args) if args.adopt else create(control, args))
    except Agent37Error as exc:
        raise SystemExit(f"Agent37 error {exc.code}: {exc}") from None
    finally:
        await control.aclose()

    if not ids:
        raise SystemExit(1)
    print("\nPut this in .env, then restart the orchestrator:\n\n"
          f"AGENT37_WORKERS={','.join(ids)}\n")
    pending = await wait_healthy(ids, settings.agent37_api_key, args.wait)
    if pending:
        raise SystemExit(f"not healthy after {args.wait}s: {pending}")


if __name__ == "__main__":
    asyncio.run(main())
