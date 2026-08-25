#!/usr/bin/env python3
"""Small durable coordination ledger for the Visible Team Codex skill."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


COLLABORATION_STATUSES = (
    "active",
    "waiting_user",
    "waiting_worker",
    "verifying",
    "blocked",
    "completed",
    "cancelled",
)
WORKER_STATUSES = (
    "planned",
    "active",
    "idle",
    "waiting",
    "blocked",
    "completed",
    "failed",
    "cancelled",
)
TERMINAL_COLLABORATION_STATUSES = {"completed", "cancelled"}
TERMINAL_WORKER_STATUSES = {"completed", "failed", "cancelled"}


class StateError(RuntimeError):
    """Expected state or validation failure."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def emit(value: Any, pretty: bool = False, *, stream: Any = sys.stdout) -> None:
    if pretty:
        text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    print(text, file=stream)


def connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS collaborations (
            collaboration_id TEXT PRIMARY KEY,
            objective TEXT NOT NULL,
            leader_thread_id TEXT,
            status TEXT NOT NULL,
            version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS workers (
            collaboration_id TEXT NOT NULL,
            worker_id TEXT NOT NULL,
            thread_id TEXT,
            title TEXT NOT NULL,
            model TEXT NOT NULL,
            thinking TEXT NOT NULL,
            responsibility TEXT NOT NULL,
            status TEXT NOT NULL,
            last_context_version INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (collaboration_id, worker_id),
            UNIQUE (collaboration_id, thread_id),
            FOREIGN KEY (collaboration_id) REFERENCES collaborations(collaboration_id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS context_updates (
            update_id INTEGER PRIMARY KEY AUTOINCREMENT,
            collaboration_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            summary TEXT NOT NULL,
            source_ref TEXT,
            created_at TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            UNIQUE (collaboration_id, version),
            UNIQUE (collaboration_id, idempotency_key),
            FOREIGN KEY (collaboration_id) REFERENCES collaborations(collaboration_id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS context_targets (
            update_id INTEGER NOT NULL,
            worker_id TEXT NOT NULL,
            delivered_at TEXT,
            PRIMARY KEY (update_id, worker_id),
            FOREIGN KEY (update_id) REFERENCES context_updates(update_id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            collaboration_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (collaboration_id, idempotency_key),
            FOREIGN KEY (collaboration_id) REFERENCES collaborations(collaboration_id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS context_targets_pending_idx
            ON context_targets(worker_id, delivered_at);
        CREATE INDEX IF NOT EXISTS events_collaboration_idx
            ON events(collaboration_id, event_id);
        PRAGMA user_version = 1;
        """
    )
    return connection


@contextmanager
def immediate_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def collaboration_row(connection: sqlite3.Connection, collaboration_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM collaborations WHERE collaboration_id = ?", (collaboration_id,)
    ).fetchone()
    if row is None:
        raise StateError(f"Unknown collaboration: {collaboration_id}")
    return row


def worker_row(
    connection: sqlite3.Connection, collaboration_id: str, worker_id: str
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM workers WHERE collaboration_id = ? AND worker_id = ?",
        (collaboration_id, worker_id),
    ).fetchone()
    if row is None:
        raise StateError(f"Unknown Worker {worker_id!r} in collaboration {collaboration_id!r}")
    return row


def replayed_event(
    connection: sqlite3.Connection, collaboration_id: str, idempotency_key: str
) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM events WHERE collaboration_id = ? AND idempotency_key = ?",
            (collaboration_id, idempotency_key),
        ).fetchone()
        is not None
    )


def bump_version(
    connection: sqlite3.Connection,
    collaboration_id: str,
    expected_version: int | None,
) -> int:
    current = collaboration_row(connection, collaboration_id)
    if expected_version is not None and current["version"] != expected_version:
        raise StateError(
            f"Stale collaboration version: expected {expected_version}, "
            f"current {current['version']}"
        )
    new_version = current["version"] + 1
    timestamp = now()
    connection.execute(
        "UPDATE collaborations SET version = ?, updated_at = ? WHERE collaboration_id = ?",
        (new_version, timestamp, collaboration_id),
    )
    return new_version


def add_event(
    connection: sqlite3.Connection,
    collaboration_id: str,
    version: int,
    event_type: str,
    actor: str,
    payload: dict[str, Any],
    idempotency_key: str,
) -> None:
    connection.execute(
        """
        INSERT INTO events (
            collaboration_id, version, event_type, actor, payload_json,
            idempotency_key, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            collaboration_id,
            version,
            event_type,
            actor,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            idempotency_key,
            now(),
        ),
    )


def snapshot(connection: sqlite3.Connection, collaboration_id: str) -> dict[str, Any]:
    collaboration = dict(collaboration_row(connection, collaboration_id))
    workers = []
    rows = connection.execute(
        """
        SELECT w.*,
               SUM(
                   CASE
                       WHEN cu.update_id IS NOT NULL AND ct.delivered_at IS NULL THEN 1
                       ELSE 0
                   END
               ) AS pending_updates
        FROM workers AS w
        LEFT JOIN context_targets AS ct ON ct.worker_id = w.worker_id
        LEFT JOIN context_updates AS cu
          ON cu.update_id = ct.update_id AND cu.collaboration_id = w.collaboration_id
        WHERE w.collaboration_id = ?
        GROUP BY w.collaboration_id, w.worker_id
        ORDER BY w.created_at, w.worker_id
        """,
        (collaboration_id,),
    ).fetchall()
    for row in rows:
        item = dict(row)
        item["pending_updates"] = int(item["pending_updates"] or 0)
        workers.append(item)
    collaboration["workers"] = workers
    return collaboration


def command_init(connection: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    collaboration_id = args.collaboration_id or str(uuid.uuid4())
    timestamp = now()
    with immediate_transaction(connection):
        existing = connection.execute(
            "SELECT * FROM collaborations WHERE collaboration_id = ?", (collaboration_id,)
        ).fetchone()
        if existing is not None:
            same = (
                existing["objective"] == args.objective
                and existing["leader_thread_id"] == args.leader_thread_id
            )
            if not same:
                raise StateError(
                    "The collaboration ID already exists with a different objective or Leader"
                )
            result = snapshot(connection, collaboration_id)
            result["replayed"] = True
            return result
        connection.execute(
            """
            INSERT INTO collaborations (
                collaboration_id, objective, leader_thread_id, status,
                version, created_at, updated_at
            ) VALUES (?, ?, ?, 'active', 1, ?, ?)
            """,
            (collaboration_id, args.objective, args.leader_thread_id, timestamp, timestamp),
        )
        add_event(
            connection,
            collaboration_id,
            1,
            "collaboration_initialized",
            "leader",
            {"objective": args.objective, "leader_thread_id": args.leader_thread_id},
            f"init:{collaboration_id}",
        )
        return snapshot(connection, collaboration_id)


def command_plan_worker(connection: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    with immediate_transaction(connection):
        if replayed_event(connection, args.collaboration_id, args.idempotency_key):
            result = snapshot(connection, args.collaboration_id)
            result["replayed"] = True
            return result
        collaboration_row(connection, args.collaboration_id)
        existing = connection.execute(
            "SELECT 1 FROM workers WHERE collaboration_id = ? AND worker_id = ?",
            (args.collaboration_id, args.worker_id),
        ).fetchone()
        if existing is not None:
            raise StateError(
                "Worker already exists; retry with the original idempotency key or choose a new Worker ID"
            )
        version = bump_version(connection, args.collaboration_id, args.expected_version)
        timestamp = now()
        connection.execute(
            """
            INSERT INTO workers (
                collaboration_id, worker_id, thread_id, title, model, thinking,
                responsibility, status, last_context_version, created_at, updated_at
            ) VALUES (?, ?, NULL, ?, ?, ?, ?, 'planned', 0, ?, ?)
            """,
            (
                args.collaboration_id,
                args.worker_id,
                args.title,
                args.model,
                args.thinking,
                args.responsibility,
                timestamp,
                timestamp,
            ),
        )
        add_event(
            connection,
            args.collaboration_id,
            version,
            "worker_planned",
            "leader",
            {
                "worker_id": args.worker_id,
                "title": args.title,
                "model": args.model,
                "thinking": args.thinking,
                "responsibility": args.responsibility,
            },
            args.idempotency_key,
        )
        return snapshot(connection, args.collaboration_id)


def command_attach_worker(connection: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    with immediate_transaction(connection):
        if replayed_event(connection, args.collaboration_id, args.idempotency_key):
            result = snapshot(connection, args.collaboration_id)
            result["replayed"] = True
            return result
        worker = worker_row(connection, args.collaboration_id, args.worker_id)
        if worker["thread_id"] is not None:
            raise StateError(
                "Worker is already attached; retry with the original idempotency key"
            )
        version = bump_version(connection, args.collaboration_id, args.expected_version)
        timestamp = now()
        try:
            connection.execute(
                """
                UPDATE workers
                SET thread_id = ?, status = 'active', updated_at = ?
                WHERE collaboration_id = ? AND worker_id = ?
                """,
                (args.thread_id, timestamp, args.collaboration_id, args.worker_id),
            )
        except sqlite3.IntegrityError as error:
            raise StateError("That visible thread is already attached to another Worker") from error
        add_event(
            connection,
            args.collaboration_id,
            version,
            "worker_attached",
            "leader",
            {"worker_id": args.worker_id, "thread_id": args.thread_id},
            args.idempotency_key,
        )
        return snapshot(connection, args.collaboration_id)


def resolve_targets(
    connection: sqlite3.Connection, collaboration_id: str, targets: list[str]
) -> list[str]:
    if not targets:
        raise StateError("At least one --target is required")
    if "all" in targets:
        if len(targets) != 1:
            raise StateError("Use --target all by itself")
        rows = connection.execute(
            """
            SELECT worker_id FROM workers
            WHERE collaboration_id = ?
              AND status NOT IN ('completed', 'failed', 'cancelled')
            ORDER BY worker_id
            """,
            (collaboration_id,),
        ).fetchall()
        resolved = [row["worker_id"] for row in rows]
    else:
        resolved = list(dict.fromkeys(targets))
        for worker_id in resolved:
            worker_row(connection, collaboration_id, worker_id)
    if not resolved:
        raise StateError("The context update has no active target Workers")
    return resolved


def command_add_context(connection: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    with immediate_transaction(connection):
        if replayed_event(connection, args.collaboration_id, args.idempotency_key):
            result = snapshot(connection, args.collaboration_id)
            result["replayed"] = True
            return result
        targets = resolve_targets(connection, args.collaboration_id, args.target)
        version = bump_version(connection, args.collaboration_id, args.expected_version)
        cursor = connection.execute(
            """
            INSERT INTO context_updates (
                collaboration_id, version, summary, source_ref, created_at, idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                args.collaboration_id,
                version,
                args.summary,
                args.source_ref,
                now(),
                args.idempotency_key,
            ),
        )
        update_id = int(cursor.lastrowid)
        connection.executemany(
            "INSERT INTO context_targets (update_id, worker_id) VALUES (?, ?)",
            [(update_id, worker_id) for worker_id in targets],
        )
        add_event(
            connection,
            args.collaboration_id,
            version,
            "context_added",
            "leader",
            {
                "update_id": update_id,
                "summary": args.summary,
                "source_ref": args.source_ref,
                "targets": targets,
            },
            args.idempotency_key,
        )
        result = snapshot(connection, args.collaboration_id)
        result["context_update"] = {"update_id": update_id, "version": version, "targets": targets}
        return result


def command_pending(connection: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    worker = worker_row(connection, args.collaboration_id, args.worker_id)
    rows = connection.execute(
        """
        SELECT cu.update_id, cu.version, cu.summary, cu.source_ref, cu.created_at
        FROM context_updates AS cu
        JOIN context_targets AS ct ON ct.update_id = cu.update_id
        WHERE cu.collaboration_id = ? AND ct.worker_id = ? AND ct.delivered_at IS NULL
        ORDER BY cu.version
        """,
        (args.collaboration_id, args.worker_id),
    ).fetchall()
    return {
        "collaboration_id": args.collaboration_id,
        "worker_id": args.worker_id,
        "thread_id": worker["thread_id"],
        "last_context_version": worker["last_context_version"],
        "updates": [dict(row) for row in rows],
    }


def command_acknowledge(connection: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    with immediate_transaction(connection):
        if replayed_event(connection, args.collaboration_id, args.idempotency_key):
            result = snapshot(connection, args.collaboration_id)
            result["replayed"] = True
            return result
        worker_row(connection, args.collaboration_id, args.worker_id)
        rows = connection.execute(
            """
            SELECT ct.update_id
            FROM context_targets AS ct
            JOIN context_updates AS cu ON cu.update_id = ct.update_id
            WHERE cu.collaboration_id = ? AND ct.worker_id = ?
              AND ct.delivered_at IS NULL AND cu.version <= ?
            """,
            (args.collaboration_id, args.worker_id, args.through_version),
        ).fetchall()
        version = bump_version(connection, args.collaboration_id, args.expected_version)
        timestamp = now()
        if rows:
            connection.executemany(
                "UPDATE context_targets SET delivered_at = ? WHERE update_id = ? AND worker_id = ?",
                [(timestamp, row["update_id"], args.worker_id) for row in rows],
            )
        connection.execute(
            """
            UPDATE workers
            SET last_context_version = MAX(last_context_version, ?), updated_at = ?
            WHERE collaboration_id = ? AND worker_id = ?
            """,
            (args.through_version, timestamp, args.collaboration_id, args.worker_id),
        )
        add_event(
            connection,
            args.collaboration_id,
            version,
            "context_acknowledged",
            args.worker_id,
            {
                "worker_id": args.worker_id,
                "through_version": args.through_version,
                "acknowledged_updates": len(rows),
            },
            args.idempotency_key,
        )
        result = snapshot(connection, args.collaboration_id)
        result["acknowledged_updates"] = len(rows)
        return result


def command_set_collaboration_status(
    connection: sqlite3.Connection, args: argparse.Namespace
) -> dict[str, Any]:
    with immediate_transaction(connection):
        if replayed_event(connection, args.collaboration_id, args.idempotency_key):
            result = snapshot(connection, args.collaboration_id)
            result["replayed"] = True
            return result
        current = collaboration_row(connection, args.collaboration_id)
        if current["status"] in TERMINAL_COLLABORATION_STATUSES and current["status"] != args.status:
            raise StateError("A terminal collaboration cannot be reopened")
        version = bump_version(connection, args.collaboration_id, args.expected_version)
        connection.execute(
            "UPDATE collaborations SET status = ?, updated_at = ? WHERE collaboration_id = ?",
            (args.status, now(), args.collaboration_id),
        )
        add_event(
            connection,
            args.collaboration_id,
            version,
            "collaboration_status_changed",
            "leader",
            {"from": current["status"], "to": args.status},
            args.idempotency_key,
        )
        return snapshot(connection, args.collaboration_id)


def command_set_worker_status(connection: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    with immediate_transaction(connection):
        if replayed_event(connection, args.collaboration_id, args.idempotency_key):
            result = snapshot(connection, args.collaboration_id)
            result["replayed"] = True
            return result
        current = worker_row(connection, args.collaboration_id, args.worker_id)
        if current["status"] in TERMINAL_WORKER_STATUSES and current["status"] != args.status:
            raise StateError("A terminal Worker cannot be reopened")
        version = bump_version(connection, args.collaboration_id, args.expected_version)
        connection.execute(
            """
            UPDATE workers SET status = ?, updated_at = ?
            WHERE collaboration_id = ? AND worker_id = ?
            """,
            (args.status, now(), args.collaboration_id, args.worker_id),
        )
        add_event(
            connection,
            args.collaboration_id,
            version,
            "worker_status_changed",
            "leader",
            {"worker_id": args.worker_id, "from": current["status"], "to": args.status},
            args.idempotency_key,
        )
        return snapshot(connection, args.collaboration_id)


def command_events(connection: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    collaboration_row(connection, args.collaboration_id)
    rows = connection.execute(
        """
        SELECT event_id, version, event_type, actor, payload_json,
               idempotency_key, created_at
        FROM events WHERE collaboration_id = ?
        ORDER BY event_id DESC LIMIT ?
        """,
        (args.collaboration_id, args.limit),
    ).fetchall()
    events = []
    for row in reversed(rows):
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        events.append(item)
    return {"collaboration_id": args.collaboration_id, "events": events}


def add_mutation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--expected-version", type=int)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Leader-chosen SQLite database path")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create or resume a collaboration")
    init_parser.add_argument("--collaboration-id")
    init_parser.add_argument("--objective", required=True)
    init_parser.add_argument("--leader-thread-id")
    init_parser.set_defaults(handler=command_init)

    plan_parser = subparsers.add_parser("plan-worker", help="Record a Worker before dispatch")
    plan_parser.add_argument("--collaboration-id", required=True)
    plan_parser.add_argument("--worker-id", required=True)
    plan_parser.add_argument("--title", required=True)
    plan_parser.add_argument("--model", required=True)
    plan_parser.add_argument("--thinking", required=True)
    plan_parser.add_argument("--responsibility", required=True)
    add_mutation_arguments(plan_parser)
    plan_parser.set_defaults(handler=command_plan_worker)

    attach_parser = subparsers.add_parser(
        "attach-worker", help="Attach a visible thread to a planned Worker"
    )
    attach_parser.add_argument("--collaboration-id", required=True)
    attach_parser.add_argument("--worker-id", required=True)
    attach_parser.add_argument("--thread-id", required=True)
    add_mutation_arguments(attach_parser)
    attach_parser.set_defaults(handler=command_attach_worker)

    context_parser = subparsers.add_parser(
        "add-context", help="Create a versioned context update for selected Workers"
    )
    context_parser.add_argument("--collaboration-id", required=True)
    context_parser.add_argument("--summary", required=True)
    context_parser.add_argument("--source-ref")
    context_parser.add_argument("--target", action="append", required=True)
    add_mutation_arguments(context_parser)
    context_parser.set_defaults(handler=command_add_context)

    pending_parser = subparsers.add_parser(
        "pending", help="List undelivered context updates for one Worker"
    )
    pending_parser.add_argument("--collaboration-id", required=True)
    pending_parser.add_argument("--worker-id", required=True)
    pending_parser.set_defaults(handler=command_pending)

    acknowledge_parser = subparsers.add_parser(
        "acknowledge", help="Mark selected context versions as delivered"
    )
    acknowledge_parser.add_argument("--collaboration-id", required=True)
    acknowledge_parser.add_argument("--worker-id", required=True)
    acknowledge_parser.add_argument("--through-version", type=int, required=True)
    add_mutation_arguments(acknowledge_parser)
    acknowledge_parser.set_defaults(handler=command_acknowledge)

    collaboration_status_parser = subparsers.add_parser(
        "set-collaboration-status", help="Change collaboration lifecycle state"
    )
    collaboration_status_parser.add_argument("--collaboration-id", required=True)
    collaboration_status_parser.add_argument("--status", choices=COLLABORATION_STATUSES, required=True)
    add_mutation_arguments(collaboration_status_parser)
    collaboration_status_parser.set_defaults(handler=command_set_collaboration_status)

    worker_status_parser = subparsers.add_parser(
        "set-worker-status", help="Change Worker lifecycle state"
    )
    worker_status_parser.add_argument("--collaboration-id", required=True)
    worker_status_parser.add_argument("--worker-id", required=True)
    worker_status_parser.add_argument("--status", choices=WORKER_STATUSES, required=True)
    add_mutation_arguments(worker_status_parser)
    worker_status_parser.set_defaults(handler=command_set_worker_status)

    snapshot_parser = subparsers.add_parser("snapshot", help="Return compact resumable state")
    snapshot_parser.add_argument("--collaboration-id", required=True)
    snapshot_parser.set_defaults(
        handler=lambda connection, arguments: snapshot(connection, arguments.collaboration_id)
    )

    events_parser = subparsers.add_parser("events", help="Return recent durable events")
    events_parser.add_argument("--collaboration-id", required=True)
    events_parser.add_argument("--limit", type=int, default=50)
    events_parser.set_defaults(handler=command_events)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    connection: sqlite3.Connection | None = None
    try:
        connection = connect(args.db)
        result = args.handler(connection, args)
        emit(result, args.pretty)
        return 0
    except (StateError, sqlite3.Error, ValueError) as error:
        emit({"error": str(error), "type": error.__class__.__name__}, args.pretty, stream=sys.stderr)
        return 2
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
