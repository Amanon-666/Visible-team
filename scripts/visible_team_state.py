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
DELIVERY_STATUSES = (
    "pending",
    "submitted",
    "received",
    "accepted",
    "revision_requested",
    "needs_attention",
)
DELIVERY_TRANSITIONS = {
    "pending": {"submitted", "needs_attention"},
    "submitted": {"received", "revision_requested", "needs_attention"},
    "received": {"accepted", "revision_requested", "needs_attention"},
    "revision_requested": {"submitted", "needs_attention"},
    "needs_attention": {"submitted", "received", "needs_attention"},
    "accepted": set(),
}
FAILURE_CATEGORIES = ("transient", "permanent", "decision", "authorization", "conflict")
FAILURE_ACTIONS = {
    "transient": {"kind": "retry", "owner": "leader", "retryable": True, "automatic": False},
    "permanent": {"kind": "stop", "owner": "leader", "retryable": False, "automatic": False},
    "decision": {"kind": "decide", "owner": "leader", "retryable": False, "automatic": False},
    "authorization": {"kind": "authorize", "owner": "user_or_leader", "retryable": False, "automatic": False},
    "conflict": {"kind": "resolve_conflict", "owner": "leader", "retryable": False, "automatic": False},
}
STATE_SCHEMA_VERSION = 2
SKILL_VERSION = "visible-team/2"


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


def _column_exists(connection: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    return any(
        row[1] == column_name
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    )


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
        ).fetchone()
        is not None
    )


def _validate_v2_schema(connection: sqlite3.Connection) -> None:
    required_tables = (
        "collaborations",
        "workers",
        "context_updates",
        "context_targets",
        "events",
        "host_observations",
        "failures",
    )
    missing = [table for table in required_tables if not _table_exists(connection, table)]
    if missing:
        raise StateError(f"Database schema 2 is incomplete; missing tables: {', '.join(missing)}")
    required_columns = {
        "collaborations": (
            "collaboration_id",
            "objective",
            "leader_thread_id",
            "status",
            "version",
            "created_at",
            "updated_at",
            "skill_version",
        ),
        "workers": (
            "collaboration_id",
            "worker_id",
            "thread_id",
            "title",
            "model",
            "thinking",
            "responsibility",
            "status",
            "last_context_version",
            "created_at",
            "updated_at",
            "delivery_status",
            "delivery_summary",
            "artifact_ref",
            "result_available",
            "delivery_note",
            "delivery_updated_at",
        ),
        "context_updates": (
            "update_id",
            "collaboration_id",
            "version",
            "summary",
            "source_ref",
            "created_at",
            "idempotency_key",
        ),
        "context_targets": ("update_id", "worker_id", "delivered_at"),
        "events": (
            "event_id",
            "collaboration_id",
            "version",
            "event_type",
            "actor",
            "payload_json",
            "idempotency_key",
            "created_at",
        ),
        "host_observations": (
            "collaboration_id",
            "worker_id",
            "observed_at",
            "task_exists",
            "host_status",
            "result_available",
            "last_contact_at",
            "lease_until",
            "needs_attention",
            "note",
            "source_ref",
            "updated_at",
        ),
        "failures": (
            "failure_id",
            "collaboration_id",
            "worker_id",
            "category",
            "message",
            "action_kind",
            "action_owner",
            "automatic_retry",
            "source_ref",
            "created_at",
            "idempotency_key",
            "resolved_at",
        ),
    }
    missing_columns = [
        f"{table}.{column}"
        for table, columns in required_columns.items()
        for column in columns
        if not _column_exists(connection, table, column)
    ]
    if missing_columns:
        raise StateError(
            f"Database schema 2 is incomplete; missing columns: {', '.join(missing_columns)}"
        )


def _create_v1_schema(connection: sqlite3.Connection) -> None:
    """Create the original schema, without silently changing its version."""
    statements = (
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
        """,
        """
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
        """,
        """
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
        """,
        """
        CREATE TABLE IF NOT EXISTS context_targets (
            update_id INTEGER NOT NULL,
            worker_id TEXT NOT NULL,
            delivered_at TEXT,
            PRIMARY KEY (update_id, worker_id),
            FOREIGN KEY (update_id) REFERENCES context_updates(update_id)
                ON DELETE CASCADE
        );
        """,
        """
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
        """,
        """
        CREATE INDEX IF NOT EXISTS context_targets_pending_idx
            ON context_targets(worker_id, delivered_at);
        """,
        """
        CREATE INDEX IF NOT EXISTS events_collaboration_idx
            ON events(collaboration_id, event_id);
        """,
    )
    for statement in statements:
        connection.execute(statement)


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    """Add reliability state while preserving every v1 row and identifier."""
    if not _column_exists(connection, "collaborations", "skill_version"):
        connection.execute(
            "ALTER TABLE collaborations ADD COLUMN skill_version TEXT NOT NULL DEFAULT 'visible-team/2'",
        )
    connection.execute(
        "UPDATE collaborations SET skill_version = 'visible-team/2' "
        "WHERE skill_version IS NULL OR skill_version = ''"
    )
    worker_columns = (
        ("delivery_status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("delivery_summary", "TEXT"),
        ("artifact_ref", "TEXT"),
        ("result_available", "INTEGER NOT NULL DEFAULT 0"),
        ("delivery_note", "TEXT"),
        ("delivery_updated_at", "TEXT"),
    )
    for column_name, definition in worker_columns:
        if not _column_exists(connection, "workers", column_name):
            connection.execute(f"ALTER TABLE workers ADD COLUMN {column_name} {definition}")
    statements = (
        """
        CREATE TABLE IF NOT EXISTS host_observations (
            collaboration_id TEXT NOT NULL,
            worker_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            task_exists INTEGER NOT NULL,
            host_status TEXT NOT NULL,
            result_available INTEGER NOT NULL,
            last_contact_at TEXT,
            lease_until TEXT,
            needs_attention INTEGER NOT NULL DEFAULT 0,
            note TEXT,
            source_ref TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (collaboration_id, worker_id),
            FOREIGN KEY (collaboration_id, worker_id)
                REFERENCES workers(collaboration_id, worker_id)
                ON DELETE CASCADE
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS failures (
            failure_id INTEGER PRIMARY KEY AUTOINCREMENT,
            collaboration_id TEXT NOT NULL,
            worker_id TEXT NOT NULL,
            category TEXT NOT NULL,
            message TEXT NOT NULL,
            action_kind TEXT NOT NULL,
            action_owner TEXT NOT NULL,
            automatic_retry INTEGER NOT NULL,
            source_ref TEXT,
            created_at TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            resolved_at TEXT,
            UNIQUE (collaboration_id, idempotency_key),
            FOREIGN KEY (collaboration_id, worker_id)
                REFERENCES workers(collaboration_id, worker_id)
                ON DELETE CASCADE
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS failures_worker_idx
            ON failures(collaboration_id, worker_id, failure_id);
        """,
    )
    for statement in statements:
        connection.execute(statement)


def migrate(connection: sqlite3.Connection) -> int:
    """Run explicit, monotonic schema migrations and return the schema version."""
    with immediate_transaction(connection):
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version < 0 or version > STATE_SCHEMA_VERSION:
            raise StateError(
                f"Database schema {version} is outside supported range 0..{STATE_SCHEMA_VERSION}"
            )
        if version == 0:
            _create_v1_schema(connection)
            version = 1
            connection.execute("PRAGMA user_version = 1")
        if version == 1:
            _migrate_v1_to_v2(connection)
            connection.execute("PRAGMA user_version = 2")
            version = 2
        if version > STATE_SCHEMA_VERSION:
            raise StateError(
                f"Database schema {version} is newer than supported schema {STATE_SCHEMA_VERSION}"
            )
        _validate_v2_schema(connection)
    return version


def connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 10000")
    migrate(connection)
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


def _bool_value(value: Any) -> bool:
    return bool(int(value)) if isinstance(value, (int, bool)) else bool(value)


def _host_observation(connection: sqlite3.Connection, collaboration_id: str, worker_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT * FROM host_observations WHERE collaboration_id = ? AND worker_id = ?",
        (collaboration_id, worker_id),
    ).fetchone()
    if row is None:
        return None
    item = dict(row)
    for key in ("task_exists", "result_available", "needs_attention"):
        item[key] = _bool_value(item[key])
    return item


def _latest_failure(connection: sqlite3.Connection, collaboration_id: str, worker_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT failure_id, category, message, action_kind, action_owner,
               automatic_retry, source_ref, created_at, resolved_at
        FROM failures
        WHERE collaboration_id = ? AND worker_id = ?
        ORDER BY failure_id DESC LIMIT 1
        """,
        (collaboration_id, worker_id),
    ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["automatic_retry"] = _bool_value(item["automatic_retry"])
    item["retryable"] = item["category"] == "transient"
    return item


def snapshot(connection: sqlite3.Connection, collaboration_id: str) -> dict[str, Any]:
    collaboration = dict(collaboration_row(connection, collaboration_id))
    collaboration["schema_version"] = STATE_SCHEMA_VERSION
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
        item["result_available"] = _bool_value(item["result_available"])
        item["host_observation"] = _host_observation(
            connection, collaboration_id, item["worker_id"]
        )
        item["latest_failure"] = _latest_failure(
            connection, collaboration_id, item["worker_id"]
        )
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
            if worker["thread_id"] == args.thread_id:
                result = snapshot(connection, args.collaboration_id)
                result["reconciled"] = True
                return result
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


def latest_worker_creation_event(
    connection: sqlite3.Connection, collaboration_id: str, worker_id: str
) -> dict[str, Any] | None:
    rows = connection.execute(
        """
        SELECT event_id, event_type, payload_json, created_at
        FROM events
        WHERE collaboration_id = ?
          AND event_type IN ('worker_creation_requested', 'worker_creation_reconciled')
        ORDER BY event_id DESC
        """,
        (collaboration_id,),
    ).fetchall()
    for row in rows:
        payload = json.loads(row["payload_json"])
        if payload.get("worker_id") == worker_id:
            return {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "created_at": row["created_at"],
                **payload,
            }
    return None


def command_reserve_host_action(
    connection: sqlite3.Connection, args: argparse.Namespace
) -> dict[str, Any]:
    """Reserve one adapter action; creation reservations are singleton per Worker."""
    with immediate_transaction(connection):
        if replayed_event(connection, args.collaboration_id, args.idempotency_key):
            return {"reserved": False, "replayed": True, "reason": "same_idempotency_key"}
        worker = worker_row(connection, args.collaboration_id, args.worker_id)
        action = args.action
        if action == "create_worker":
            if worker["status"] != "planned":
                raise StateError("Only a planned Worker without a thread can be created")
            if worker["thread_id"] is not None:
                return {"reserved": False, "reason": "already_attached"}
            latest = latest_worker_creation_event(
                connection, args.collaboration_id, args.worker_id
            )
            if latest is not None and latest["event_type"] == "worker_creation_requested":
                return {"reserved": False, "reason": "creation_uncertain"}
            event_type = "worker_creation_requested"
        else:
            event_type = "host_action_requested"
        version = bump_version(connection, args.collaboration_id, args.expected_version)
        add_event(
            connection,
            args.collaboration_id,
            version,
            event_type,
            "leader",
            {
                "action": action,
                "worker_id": args.worker_id,
                "thread_id": worker["thread_id"],
            },
            args.idempotency_key,
        )
        return {"reserved": True, "version": version, "action": action}


def command_reconcile_worker_creation(
    connection: sqlite3.Connection, args: argparse.Namespace
) -> dict[str, Any]:
    with immediate_transaction(connection):
        if replayed_event(connection, args.collaboration_id, args.idempotency_key):
            result = snapshot(connection, args.collaboration_id)
            result["replayed"] = True
            return result
        worker = worker_row(connection, args.collaboration_id, args.worker_id)
        if args.outcome not in {"missing", "retry", "attached"}:
            raise StateError("Creation reconciliation outcome must be missing, retry, or attached")
        if worker["status"] != "planned" and worker["thread_id"] is None:
            raise StateError("Only a planned Worker without a thread can reconcile creation")
        if args.outcome == "attached":
            if not args.thread_id:
                raise StateError("An attached reconciliation requires --thread-id")
            if worker["thread_id"] is not None and worker["thread_id"] != args.thread_id:
                raise StateError("Worker is already attached to a different visible thread")
        elif worker["thread_id"] is not None:
            raise StateError("An attached Worker cannot be reconciled as missing or retry")
        version = bump_version(connection, args.collaboration_id, args.expected_version)
        timestamp = now()
        if args.outcome == "attached" and worker["thread_id"] is None:
            try:
                connection.execute(
                    """
                    UPDATE workers SET thread_id = ?, status = 'active', updated_at = ?
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
                {"worker_id": args.worker_id, "thread_id": args.thread_id, "reconciled": True},
                f"{args.idempotency_key}:attached",
            )
        add_event(
            connection,
            args.collaboration_id,
            version,
            "worker_creation_reconciled",
            "leader",
            {
                "worker_id": args.worker_id,
                "outcome": args.outcome,
                "thread_id": args.thread_id,
            },
            args.idempotency_key,
        )
        result = snapshot(connection, args.collaboration_id)
        result["creation_reconciliation"] = {
            "worker_id": args.worker_id,
            "outcome": args.outcome,
            "thread_id": args.thread_id,
        }
        return result


def command_update_worker_config(
    connection: sqlite3.Connection, args: argparse.Namespace
) -> dict[str, Any]:
    with immediate_transaction(connection):
        if replayed_event(connection, args.collaboration_id, args.idempotency_key):
            result = snapshot(connection, args.collaboration_id)
            result["replayed"] = True
            return result
        collaboration = collaboration_row(connection, args.collaboration_id)
        if collaboration["status"] in TERMINAL_COLLABORATION_STATUSES:
            raise StateError("A terminal collaboration cannot update Worker configuration")
        current = worker_row(connection, args.collaboration_id, args.worker_id)
        if current["status"] in TERMINAL_WORKER_STATUSES:
            raise StateError("A terminal Worker cannot have its model or thinking updated")
        if args.model is not None and not args.model.strip():
            raise StateError("Worker model cannot be empty")
        if args.thinking is not None and not args.thinking.strip():
            raise StateError("Worker thinking cannot be empty")
        model = args.model if args.model is not None else current["model"]
        thinking = args.thinking if args.thinking is not None else current["thinking"]
        if model == current["model"] and thinking == current["thinking"]:
            raise StateError("Worker config update must change model or thinking")
        version = bump_version(connection, args.collaboration_id, args.expected_version)
        timestamp = now()
        connection.execute(
            """
            UPDATE workers SET model = ?, thinking = ?, updated_at = ?
            WHERE collaboration_id = ? AND worker_id = ?
            """,
            (model, thinking, timestamp, args.collaboration_id, args.worker_id),
        )
        add_event(
            connection,
            args.collaboration_id,
            version,
            "worker_config_updated",
            "leader",
            {
                "worker_id": args.worker_id,
                "from": {"model": current["model"], "thinking": current["thinking"]},
                "to": {"model": model, "thinking": thinking},
            },
            args.idempotency_key,
        )
        result = snapshot(connection, args.collaboration_id)
        result["worker_config_update"] = {
            "worker_id": args.worker_id,
            "model": model,
            "thinking": thinking,
            "version": version,
        }
        return result


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
            worker = worker_row(connection, collaboration_id, worker_id)
            if worker["status"] in TERMINAL_WORKER_STATUSES:
                raise StateError(f"Cannot target terminal Worker {worker_id!r} with context")
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


def _delivery_value(args: argparse.Namespace, current: sqlite3.Row, name: str) -> Any:
    value = getattr(args, name, None)
    current_name = {"summary": "delivery_summary", "artifact_ref": "artifact_ref"}.get(name, name)
    return current[current_name] if value is None else value


def command_set_delivery_status(
    connection: sqlite3.Connection, args: argparse.Namespace
) -> dict[str, Any]:
    with immediate_transaction(connection):
        if replayed_event(connection, args.collaboration_id, args.idempotency_key):
            result = snapshot(connection, args.collaboration_id)
            result["replayed"] = True
            return result
        current = worker_row(connection, args.collaboration_id, args.worker_id)
        old_status = current["delivery_status"]
        new_status = args.status
        actor = getattr(args, "actor", "leader")
        if actor == "worker" and new_status in {
            "received",
            "accepted",
            "revision_requested",
            "needs_attention",
        }:
            raise StateError("A Worker may submit a delivery, but only the Leader may confirm or revise it")
        if new_status != old_status and new_status not in DELIVERY_TRANSITIONS.get(old_status, set()):
            raise StateError(f"Invalid delivery transition: {old_status} -> {new_status}")
        summary = _delivery_value(args, current, "summary")
        artifact_ref = _delivery_value(args, current, "artifact_ref")
        if old_status == "needs_attention" and new_status in {"submitted", "received"}:
            if getattr(args, "summary", None) is None and getattr(args, "artifact_ref", None) is None:
                raise StateError(
                    "A needs_attention delivery must include a new summary or artifact reference"
                )
        result_available = bool(
            getattr(args, "result_available", False)
            or current["result_available"]
            or bool(summary or artifact_ref)
        )
        if new_status in {"submitted", "received", "accepted"} and not (summary or artifact_ref):
            raise StateError("A delivery submission needs --summary or --artifact-ref")
        if new_status == "accepted" and not result_available:
            raise StateError("A delivery cannot be accepted before a result is available")
        version = bump_version(connection, args.collaboration_id, args.expected_version)
        timestamp = now()
        connection.execute(
            """
            UPDATE workers SET delivery_status = ?, delivery_summary = ?, artifact_ref = ?,
                result_available = ?, delivery_note = ?, delivery_updated_at = ?, updated_at = ?
            WHERE collaboration_id = ? AND worker_id = ?
            """,
            (
                new_status,
                summary,
                artifact_ref,
                int(result_available),
                getattr(args, "note", None),
                timestamp,
                timestamp,
                args.collaboration_id,
                args.worker_id,
            ),
        )
        add_event(
            connection,
            args.collaboration_id,
            version,
            "delivery_status_changed",
            "worker" if actor == "worker" else "leader",
            {
                "worker_id": args.worker_id,
                "from": old_status,
                "to": new_status,
                "summary": summary,
                "artifact_ref": artifact_ref,
                "result_available": result_available,
            },
            args.idempotency_key,
        )
        result = snapshot(connection, args.collaboration_id)
        result["delivery"] = {
            "worker_id": args.worker_id,
            "from": old_status,
            "status": new_status,
            "result_available": result_available,
        }
        return result


def command_set_worker_status(connection: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    with immediate_transaction(connection):
        if replayed_event(connection, args.collaboration_id, args.idempotency_key):
            result = snapshot(connection, args.collaboration_id)
            result["replayed"] = True
            return result
        current = worker_row(connection, args.collaboration_id, args.worker_id)
        if current["status"] in TERMINAL_WORKER_STATUSES and current["status"] != args.status:
            raise StateError("A terminal Worker cannot be reopened")
        if args.status == "completed":
            if current["delivery_status"] not in {"submitted", "received", "accepted"}:
                raise StateError(
                    "A Worker cannot be completed before a delivery is submitted with a summary or result reference"
                )
            if not (current["delivery_summary"] or current["artifact_ref"]):
                raise StateError(
                    "A Worker cannot be completed without a delivery summary or result reference"
                )
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


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError("expected yes/no or true/false")


def validate_timestamp(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise StateError(f"Invalid {field_name} timestamp: {value}") from error
    return value


def timestamp_is_expired(lease_until: str | None, observed_at: str) -> bool:
    if not lease_until:
        return False
    try:
        lease_time = datetime.fromisoformat(lease_until.replace("Z", "+00:00"))
        observed_time = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if lease_time.tzinfo is None:
            lease_time = lease_time.replace(tzinfo=timezone.utc)
        if observed_time.tzinfo is None:
            observed_time = observed_time.replace(tzinfo=timezone.utc)
        return lease_time < observed_time
    except ValueError as error:
        raise StateError("Lease and observation timestamps must be ISO-8601") from error


def command_record_observation(
    connection: sqlite3.Connection, args: argparse.Namespace
) -> dict[str, Any]:
    with immediate_transaction(connection):
        if replayed_event(connection, args.collaboration_id, args.idempotency_key):
            result = snapshot(connection, args.collaboration_id)
            result["replayed"] = True
            return result
        worker = worker_row(connection, args.collaboration_id, args.worker_id)
        observed_at = validate_timestamp(getattr(args, "observed_at", None), "observed-at") or now()
        last_contact_at = validate_timestamp(
            getattr(args, "last_contact_at", None), "last-contact-at"
        ) or observed_at
        lease_until = validate_timestamp(getattr(args, "lease_until", None), "lease-until")
        expired = timestamp_is_expired(lease_until, observed_at)
        explicit_attention = getattr(args, "needs_attention", None)
        needs_attention = bool(explicit_attention) or not args.task_exists or expired or (
            args.host_status == "completed" and not args.result_available
        )
        version = bump_version(connection, args.collaboration_id, args.expected_version)
        timestamp = now()
        connection.execute(
            """
            INSERT INTO host_observations (
                collaboration_id, worker_id, observed_at, task_exists, host_status,
                result_available, last_contact_at, lease_until, needs_attention,
                note, source_ref, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(collaboration_id, worker_id) DO UPDATE SET
                observed_at = excluded.observed_at,
                task_exists = excluded.task_exists,
                host_status = excluded.host_status,
                result_available = excluded.result_available,
                last_contact_at = excluded.last_contact_at,
                lease_until = excluded.lease_until,
                needs_attention = excluded.needs_attention,
                note = excluded.note,
                source_ref = excluded.source_ref,
                updated_at = excluded.updated_at
            """,
            (
                args.collaboration_id,
                args.worker_id,
                observed_at,
                int(args.task_exists),
                args.host_status,
                int(args.result_available),
                last_contact_at,
                lease_until,
                int(needs_attention),
                getattr(args, "note", None),
                getattr(args, "source_ref", None),
                timestamp,
            ),
        )
        if needs_attention and worker["delivery_status"] != "accepted":
            connection.execute(
                """
                UPDATE workers SET delivery_status = 'needs_attention',
                    delivery_note = ?, delivery_updated_at = ?, updated_at = ?
                WHERE collaboration_id = ? AND worker_id = ?
                  AND delivery_status != 'accepted'
                """,
                (
                    getattr(args, "note", None) or "Host observation needs attention",
                    timestamp,
                    timestamp,
                    args.collaboration_id,
                    args.worker_id,
                ),
            )
        add_event(
            connection,
            args.collaboration_id,
            version,
            "host_observed",
            "leader",
            {
                "worker_id": args.worker_id,
                "task_exists": args.task_exists,
                "host_status": args.host_status,
                "result_available": args.result_available,
                "observed_at": observed_at,
                "lease_until": lease_until,
                "needs_attention": needs_attention,
            },
            args.idempotency_key,
        )
        result = snapshot(connection, args.collaboration_id)
        result["host_observation"] = _host_observation(
            connection, args.collaboration_id, args.worker_id
        )
        return result


def command_record_failure(connection: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    with immediate_transaction(connection):
        if replayed_event(connection, args.collaboration_id, args.idempotency_key):
            result = snapshot(connection, args.collaboration_id)
            result["replayed"] = True
            return result
        worker_row(connection, args.collaboration_id, args.worker_id)
        action = FAILURE_ACTIONS[args.category]
        version = bump_version(connection, args.collaboration_id, args.expected_version)
        created_at = now()
        connection.execute(
            """
            INSERT INTO failures (
                collaboration_id, worker_id, category, message, action_kind,
                action_owner, automatic_retry, source_ref, created_at, idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                args.collaboration_id,
                args.worker_id,
                args.category,
                args.message,
                action["kind"],
                action["owner"],
                int(action["automatic"]),
                getattr(args, "source_ref", None),
                created_at,
                args.idempotency_key,
            ),
        )
        add_event(
            connection,
            args.collaboration_id,
            version,
            "failure_recorded",
            "leader",
            {
                "worker_id": args.worker_id,
                "category": args.category,
                "message": args.message,
                "action": action,
            },
            args.idempotency_key,
        )
        result = snapshot(connection, args.collaboration_id)
        result["failure"] = {
            "worker_id": args.worker_id,
            "category": args.category,
            "message": args.message,
            **action,
        }
        return result


def _pending_context(connection: sqlite3.Connection, collaboration_id: str, worker_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT cu.update_id, cu.version, cu.summary, cu.source_ref, cu.created_at
        FROM context_updates AS cu
        JOIN context_targets AS ct ON ct.update_id = cu.update_id
        WHERE cu.collaboration_id = ? AND ct.worker_id = ? AND ct.delivered_at IS NULL
        ORDER BY cu.version
        """,
        (collaboration_id, worker_id),
    ).fetchall()
    return [dict(row) for row in rows]


def resume_data(connection: sqlite3.Connection, collaboration_id: str) -> dict[str, Any]:
    current = snapshot(connection, collaboration_id)
    workers = []
    unacknowledged_context: list[dict[str, Any]] = []
    notes: list[str] = []
    next_actions: list[dict[str, Any]] = []
    for worker in current["workers"]:
        pending = _pending_context(connection, collaboration_id, worker["worker_id"])
        if pending:
            unacknowledged_context.append(
                {"worker_id": worker["worker_id"], "thread_id": worker["thread_id"], "updates": pending}
            )
            if worker["thread_id"]:
                next_actions.append(
                    {"kind": "deliver_context", "worker_id": worker["worker_id"], "thread_id": worker["thread_id"]}
                )
            else:
                notes.append(f"Worker {worker['worker_id']} has pending context but no thread yet")
        observation = worker["host_observation"]
        creation_event = latest_worker_creation_event(
            connection, collaboration_id, worker["worker_id"]
        )
        creation_state = "attached" if worker["thread_id"] else (
            "uncertain"
            if creation_event is not None and creation_event["event_type"] == "worker_creation_requested"
            else "ready"
        )
        if worker["status"] == "planned" and not worker["thread_id"]:
            if creation_state == "uncertain":
                notes.append(
                    f"Worker {worker['worker_id']} has an unresolved host creation request; reconcile before retrying"
                )
                next_actions.append({"kind": "reconcile_creation", "worker_id": worker["worker_id"]})
            else:
                next_actions.append({"kind": "create_worker", "worker_id": worker["worker_id"]})
        elif observation is None and worker["thread_id"]:
            notes.append(f"Worker {worker['worker_id']} has no host observation yet")
            next_actions.append({"kind": "observe_worker", "worker_id": worker["worker_id"]})
        if observation is not None:
            if observation["needs_attention"]:
                notes.append(f"Worker {worker['worker_id']} host observation needs attention")
            if not observation["task_exists"]:
                notes.append(f"Worker {worker['worker_id']} host task is missing")
                next_actions.append({"kind": "reconcile_host_task", "worker_id": worker["worker_id"]})
            if observation["lease_until"] and timestamp_is_expired(observation["lease_until"], now()):
                notes.append(f"Worker {worker['worker_id']} lease is expired")
                next_actions.append({"kind": "observe_worker", "worker_id": worker["worker_id"]})
        if worker["delivery_status"] in {"submitted", "received"}:
            next_actions.append({"kind": "review_delivery", "worker_id": worker["worker_id"]})
        elif worker["delivery_status"] == "needs_attention":
            notes.append(f"Worker {worker['worker_id']} needs a verifiable result before acceptance")
            next_actions.append({"kind": "obtain_result", "worker_id": worker["worker_id"]})
        if worker["latest_failure"] is not None and worker["latest_failure"]["resolved_at"] is None:
            next_actions.append(
                {
                    "kind": worker["latest_failure"]["action_kind"],
                    "worker_id": worker["worker_id"],
                    "retryable": worker["latest_failure"]["retryable"],
                    "automatic": worker["latest_failure"]["automatic_retry"],
                }
            )
        workers.append(
            {
                "worker_id": worker["worker_id"],
                "thread_id": worker["thread_id"],
                "title": worker["title"],
                "model": worker["model"],
                "thinking": worker["thinking"],
                "responsibility": worker["responsibility"],
                "creation_state": creation_state,
                "status": worker["status"],
                "delivery_status": worker["delivery_status"],
                "result_available": worker["result_available"],
                "host_observation": observation,
                "pending_context_count": len(pending),
                "latest_failure": worker["latest_failure"],
            }
        )
    return {
        "collaboration_id": collaboration_id,
        "objective": current["objective"],
        "leader": {"thread_id": current["leader_thread_id"]},
        "versions": {"skill": current.get("skill_version", SKILL_VERSION), "schema": STATE_SCHEMA_VERSION},
        "status": current["status"],
        "workers": workers,
        "unacknowledged_context": unacknowledged_context,
        "notes": list(dict.fromkeys(notes)),
        "next_actions": next_actions,
    }


def command_resume(connection: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    return resume_data(connection, args.collaboration_id)


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

    migrate_parser = subparsers.add_parser("migrate", help="Run explicit SQLite schema migrations")
    migrate_parser.set_defaults(handler=lambda connection, arguments: {"schema_version": migrate(connection)})

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

    reconcile_creation_parser = subparsers.add_parser(
        "reconcile-worker-creation", help="Resolve an uncertain host Worker creation before retrying"
    )
    reconcile_creation_parser.add_argument("--collaboration-id", required=True)
    reconcile_creation_parser.add_argument("--worker-id", required=True)
    reconcile_creation_parser.add_argument("--outcome", choices=("missing", "retry", "attached"), required=True)
    reconcile_creation_parser.add_argument("--thread-id")
    add_mutation_arguments(reconcile_creation_parser)
    reconcile_creation_parser.set_defaults(handler=command_reconcile_worker_creation)

    config_parser = subparsers.add_parser(
        "update-worker-config", help="Version and record a Worker model/thinking update"
    )
    config_parser.add_argument("--collaboration-id", required=True)
    config_parser.add_argument("--worker-id", required=True)
    config_parser.add_argument("--model")
    config_parser.add_argument("--thinking")
    add_mutation_arguments(config_parser)
    config_parser.set_defaults(handler=command_update_worker_config)

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

    delivery_parser = subparsers.add_parser(
        "set-delivery-status", aliases=["set-delivery"], help="Advance the Worker delivery handshake"
    )
    delivery_parser.add_argument("--collaboration-id", required=True)
    delivery_parser.add_argument("--worker-id", required=True)
    delivery_parser.add_argument("--status", choices=DELIVERY_STATUSES, required=True)
    delivery_parser.add_argument("--summary", "--result-summary", dest="summary")
    delivery_parser.add_argument("--artifact-ref", "--result-ref", dest="artifact_ref")
    delivery_parser.add_argument("--result-available", action="store_true")
    delivery_parser.add_argument("--note")
    delivery_parser.add_argument("--actor", choices=("leader", "worker"), default="leader")
    add_mutation_arguments(delivery_parser)
    delivery_parser.set_defaults(handler=command_set_delivery_status)

    observation_parser = subparsers.add_parser(
        "record-observation", aliases=["observe-worker", "observe"], help="Record an observation obtained from the host"
    )
    observation_parser.add_argument("--collaboration-id", required=True)
    observation_parser.add_argument("--worker-id", required=True)
    observation_parser.add_argument("--task-exists", type=parse_bool, required=True)
    observation_parser.add_argument("--host-status", required=True)
    observation_parser.add_argument("--result-available", type=parse_bool, required=True)
    observation_parser.add_argument("--observed-at")
    observation_parser.add_argument("--last-contact-at")
    observation_parser.add_argument("--lease-until")
    observation_parser.add_argument("--needs-attention", type=parse_bool)
    observation_parser.add_argument("--note")
    observation_parser.add_argument("--source-ref")
    add_mutation_arguments(observation_parser)
    observation_parser.set_defaults(handler=command_record_observation)

    failure_parser = subparsers.add_parser(
        "record-failure", help="Record a classified failure without deciding whether to retry"
    )
    failure_parser.add_argument("--collaboration-id", required=True)
    failure_parser.add_argument("--worker-id", required=True)
    failure_parser.add_argument("--category", choices=FAILURE_CATEGORIES, required=True)
    failure_parser.add_argument("--message", required=True)
    failure_parser.add_argument("--source-ref")
    add_mutation_arguments(failure_parser)
    failure_parser.set_defaults(handler=command_record_failure)

    snapshot_parser = subparsers.add_parser("snapshot", help="Return compact resumable state")
    snapshot_parser.add_argument("--collaboration-id", required=True)
    snapshot_parser.set_defaults(
        handler=lambda connection, arguments: snapshot(connection, arguments.collaboration_id)
    )

    resume_parser = subparsers.add_parser("resume", help="Return a compact recovery summary and mechanical actions")
    resume_parser.add_argument("--collaboration-id", required=True)
    resume_parser.set_defaults(handler=command_resume)

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
