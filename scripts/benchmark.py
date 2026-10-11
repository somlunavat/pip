"""Run the five-task benchmark on both paths and print the table (P4).

    python scripts/benchmark.py              # both paths, 3 repeats
    python scripts/benchmark.py --repeats 1 --only nimble

Never claim a speedup before this has recorded it.
"""

from __future__ import annotations

import argparse
import asyncio

from orchestrator.agent37 import Dispatcher, WorkerClient, WorkerPool, read_prompt
from orchestrator.benchmark import TASKS, Run, format_table, run_benchmark
from orchestrator.config import Settings
from orchestrator.db import Database
from orchestrator.models import Milestone, MilestoneResult
from orchestrator.nimble import NimbleClient


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--only", choices=["nimble", "worker"])
    args = parser.parse_args()

    settings = Settings.from_env()
    db = Database(settings.db_path)
    nimble = NimbleClient(settings.nimble_api_key, concurrency=settings.nimble_concurrency)
    workers = [WorkerClient(w, settings.agent37_api_key) for w in settings.agent37_workers]
    pool = WorkerPool(workers, settings.agent37_sessions_per_worker)
    dispatcher = Dispatcher(pool)

    async def worker_path(m: Milestone) -> MilestoneResult:
        return await dispatcher.run(m, task_id=f"bench:{m.id}", prompt=read_prompt(m))

    paths = {"nimble": nimble.read, "worker": worker_path}
    if args.only:
        paths = {args.only: paths[args.only]}
    if "worker" in paths and not workers:
        raise SystemExit("AGENT37_WORKERS is empty; create workers or pass --only nimble")

    def progress(run: Run) -> None:
        mark = "ok " if run.ok else f"FAIL ({run.error})"
        print(f"  {run.task_id} {run.path:<7} {run.seconds:6.1f}s {mark}")

    print(f"{len(TASKS)} tasks x {args.repeats} repeats x {len(paths)} paths\n")
    try:
        stats = await run_benchmark(db, paths, repeats=args.repeats, on_run=progress)
    finally:
        await nimble.aclose()
        await pool.aclose()
    print("\n" + format_table(stats))
    print(f"\nRaw rows: SELECT * FROM task_events WHERE event = 'bench'  ({settings.db_path})")


if __name__ == "__main__":
    asyncio.run(main())
