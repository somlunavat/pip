from __future__ import annotations

from orchestrator.db import Database


def test_schema_has_all_tables(db: Database) -> None:
    names = {r["name"] for r in db.all("SELECT name FROM sqlite_master WHERE type = 'table'")}
    expected = {
        "tasks", "subtasks", "task_events", "workers",
        "watchers", "snapshots", "change_events", "preferences",
    }
    assert expected <= names


def test_wal_mode(db: Database) -> None:
    assert db.one("PRAGMA journal_mode")[0] == "wal"


def test_task_lifecycle(db: Database) -> None:
    task_id = db.create_task("find cheap flights")
    db.create_subtask(task_id, "m1", "read", "search flights")
    db.update_subtask("m1", path="nimble", status="done")
    db.finish_task(task_id, "done")

    task = db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    assert task["status"] == "done" and task["finished_at"]
    sub = db.one("SELECT * FROM subtasks WHERE id = 'm1'")
    assert sub["path"] == "nimble"


def test_spend_since_sums_costs(db: Database) -> None:
    db.log_event("gemini.plan", cost_usd=0.01, source="gemini")
    db.log_event("worker.turn", cost_usd=0.25, source="agent37")
    db.log_event("nimble.search")
    assert abs(db.spend_since("1970-01-01") - 0.26) < 1e-9


def test_nested_transaction_helpers_do_not_deadlock(db: Database) -> None:
    with db.tx():
        db.create_task("inside a transaction")
    assert db.one("SELECT COUNT(*) AS n FROM tasks")["n"] == 1
