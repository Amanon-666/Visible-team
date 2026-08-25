#!/usr/bin/env python3
"""Read host-native token usage for one Visible Team collaboration.

This is a dependency-free, read-only view.  Rollout ``token_count`` events
are preferred; ``threads.tokens_used`` is used only as a total-only fallback.
The helper deliberately does not expose rate limits, credits, prices, or
model context limits.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote


UNAVAILABLE = "unavailable"
RAW_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
DISPLAY_FIELDS = RAW_FIELDS[:2] + ("uncached_input_tokens",) + RAW_FIELDS[2:]
# Kept as a runtime placeholder so the script also imports on Python 3.9;
# function annotations are postponed by ``from __future__`` above.
Record = tuple


class UsageError(RuntimeError):
    """An expected input or read-only discovery error."""


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _readonly(path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(str(path.resolve()), safe="/:\\") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=1)
    connection.row_factory = sqlite3.Row
    return connection


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(
            f"PRAGMA table_info({_quote_identifier(table)})"
        )
    }


def _first(columns: set[str], names: Iterable[str]) -> str | None:
    return next((name for name in names if name in columns), None)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value)
    return value or None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value) if value >= 0 else None
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            return None
        return value if value >= 0 else None
    return None


def read_targets(db_path: str, collaboration_id: str) -> list[dict[str, Any]]:
    """Read only collaboration and Worker identity/configuration."""
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise UsageError(f"Collaboration database not found: {path}")
    try:
        connection = _readonly(path)
    except (OSError, sqlite3.Error) as error:
        raise UsageError(f"Cannot open collaboration database read-only: {error}") from error
    try:
        tables = _tables(connection)
        if not {"collaborations", "workers"}.issubset(tables):
            raise UsageError("Collaboration database is missing collaborations or workers")
        collaboration_columns = _columns(connection, "collaborations")
        if not {"collaboration_id", "leader_thread_id"}.issubset(collaboration_columns):
            raise UsageError("Unsupported collaborations table")
        collaboration = connection.execute(
            "SELECT leader_thread_id FROM collaborations WHERE collaboration_id = ?",
            (collaboration_id,),
        ).fetchone()
        if collaboration is None:
            raise UsageError(f"Unknown collaboration: {collaboration_id}")

        worker_columns = _columns(connection, "workers")
        required = {"collaboration_id", "worker_id", "thread_id"}
        if not required.issubset(worker_columns):
            raise UsageError("Unsupported workers table")
        model = _first(worker_columns, ("model",))
        thinking = _first(worker_columns, ("thinking",))
        order = "created_at, worker_id" if "created_at" in worker_columns else "worker_id"
        model_sql = _quote_identifier(model) if model else "NULL"
        thinking_sql = _quote_identifier(thinking) if thinking else "NULL"
        rows = connection.execute(
            f"""SELECT worker_id, thread_id, {model_sql} AS model,
                       {thinking_sql} AS thinking
                FROM workers WHERE collaboration_id = ? ORDER BY {order}""",
            (collaboration_id,),
        ).fetchall()
        targets = [{
            "role": "leader",
            "worker_id": "leader",
            "thread_id": _text(collaboration["leader_thread_id"]),
            "model": None,
            "thinking": None,
        }]
        targets.extend({
            "role": "worker",
            "worker_id": _text(row["worker_id"]) or UNAVAILABLE,
            "thread_id": _text(row["thread_id"]),
            "model": _text(row["model"]),
            "thinking": _text(row["thinking"]),
        } for row in rows)
        return targets
    except sqlite3.Error as error:
        raise UsageError(f"Cannot read collaboration database: {error}") from error
    finally:
        connection.close()


def _event_ids(record: dict[str, Any], payload: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for container in (record, payload):
        for key in ("thread_id", "threadId", "threadID"):
            value = _text(container.get(key))
            if value:
                ids.add(value)
        kind = container.get("type")
        if kind in {"session_meta", "session_start", "thread_meta", "thread_started"}:
            value = _text(container.get("id"))
            if value:
                ids.add(value)
    return ids


def _event_time(record: dict[str, Any], payload: dict[str, Any]) -> tuple[str | None, float | None]:
    for container in (record, payload):
        for key in ("timestamp", "observed_at", "created_at"):
            raw = container.get(key)
            if not isinstance(raw, str) or not raw:
                continue
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return raw, parsed.timestamp()
    return None, None


def _sequence(record: dict[str, Any], payload: dict[str, Any]) -> int:
    for container in (record, payload):
        for key in ("sequence", "seq", "event_id"):
            value = _integer(container.get(key))
            if value is not None:
                return value
    return -1


def _token_values(record: dict[str, Any], payload: dict[str, Any]) -> dict[str, int]:
    if record.get("type") == "token_count":
        usage = record.get("total_token_usage")
        if usage is None and isinstance(record.get("info"), dict):
            usage = record["info"].get("total_token_usage")
    else:
        info = payload.get("info")
        usage = info.get("total_token_usage") if isinstance(info, dict) else None
        if usage is None:
            usage = payload.get("total_token_usage")
    if not isinstance(usage, dict):
        return {}
    # Select only the five displayed raw counters.  Do not inspect or carry
    # rate_limits, credits, cache_write_input_tokens, or the context window.
    return {
        field: value
        for field in RAW_FIELDS
        if (value := _integer(usage.get(field))) is not None
    }


def _rollout_files(home: Path) -> list[Path]:
    files: list[Path] = []
    for directory_name in ("sessions", "archived_sessions"):
        directory = home / directory_name
        if not directory.is_dir():
            continue
        try:
            files.extend(
                path for path in directory.rglob("*")
                if path.is_file() and path.suffix.lower() == ".jsonl"
            )
        except OSError:
            continue
    return sorted(set(files), key=str)


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def find_rollout_usage(codex_home: str, targets: Iterable[dict[str, Any]]) -> dict[str, Record]:
    """Return the latest usable token_count for each known thread ID."""
    wanted = {target["thread_id"] for target in targets if target.get("thread_id")}
    if not wanted:
        return {}
    home = Path(codex_home).expanduser().resolve()
    latest: dict[str, Record] = {}
    for path in _rollout_files(home):
        path_ids = {thread_id for thread_id in wanted if thread_id in str(path)}
        events: list[tuple[int, set[str], dict[str, int], str | None, float | None, int]] = []
        file_ids: set[str] = set()
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                for line_number, line in enumerate(stream):
                    try:
                        record = json.loads(line)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    payload = record.get("payload")
                    payload = payload if isinstance(payload, dict) else {}
                    ids = _event_ids(record, payload)
                    file_ids.update(ids)
                    if record.get("type") == "token_count" or payload.get("type") == "token_count":
                        values = _token_values(record, payload)
                        if values:
                            observed_at, timestamp = _event_time(record, payload)
                            events.append(
                                (line_number, ids, values, observed_at, timestamp,
                                 _sequence(record, payload))
                            )
        except (OSError, UnicodeError):
            continue
        matched_file_ids = path_ids | (file_ids & wanted)
        for line_number, event_ids, values, observed_at, timestamp, sequence in events:
            matched = event_ids & wanted
            if not matched:
                matched = matched_file_ids if len(matched_file_ids) == 1 else set()
            if not matched:
                continue
            rank = (
                timestamp is not None,
                timestamp or 0.0,
                sequence,
                _mtime(path),
                str(path),
                line_number,
            )
            if observed_at is None:
                try:
                    observed_at = datetime.fromtimestamp(
                        path.stat().st_mtime, tz=timezone.utc
                    ).isoformat(timespec="milliseconds")
                except OSError:
                    pass
            for thread_id in matched:
                previous = latest.get(thread_id)
                if previous is None or rank > previous[3]:
                    latest[thread_id] = ("rollout", observed_at, values, rank)
    return latest


def find_state_fallback(codex_home: str, thread_ids: Iterable[str]) -> dict[str, Record]:
    """Read compatible ``threads.tokens_used`` totals, without a fixed version."""
    wanted = {thread_id for thread_id in thread_ids if thread_id}
    home = Path(codex_home).expanduser().resolve()
    try:
        candidates = sorted(
            (path for path in home.rglob("state_*.sqlite") if path.is_file()),
            key=lambda path: (_mtime(path), str(path)),
            reverse=True,
        )
    except OSError:
        candidates = []
    found: dict[str, Record] = {}
    for path in candidates:
        try:
            connection = _readonly(path)
        except (OSError, sqlite3.Error):
            continue
        try:
            if "threads" not in _tables(connection):
                continue
            columns = _columns(connection, "threads")
            thread_column = _first(columns, ("id", "thread_id", "threadId"))
            if thread_column is None or "tokens_used" not in columns or not wanted:
                continue
            placeholders = ",".join("?" for _ in wanted)
            rows = connection.execute(
                f"SELECT {_quote_identifier(thread_column)} AS thread_id, "
                f"{_quote_identifier('tokens_used')} AS tokens_used FROM threads "
                f"WHERE {_quote_identifier(thread_column)} IN ({placeholders})",
                tuple(wanted),
            ).fetchall()
            for row in rows:
                thread_id = _text(row["thread_id"])
                tokens = _integer(row["tokens_used"])
                rank = (_mtime(path), str(path))
                if thread_id and tokens is not None and (
                    thread_id not in found or rank > found[thread_id][3]
                ):
                    found[thread_id] = ("codex-state-db", None, {"total_tokens": tokens}, rank)
        except sqlite3.Error:
            continue
        finally:
            connection.close()
    return found


def _row(target: dict[str, Any], record: Record | None) -> dict[str, Any]:
    values = {field: UNAVAILABLE for field in DISPLAY_FIELDS}
    if record:
        values.update({field: value for field, value in record[2].items() if field in values})
        if (
            isinstance(values["input_tokens"], int)
            and isinstance(values["cached_input_tokens"], int)
            and values["cached_input_tokens"] <= values["input_tokens"]
        ):
            values["uncached_input_tokens"] = (
                values["input_tokens"] - values["cached_input_tokens"]
            )
    return {
        "role": target["role"],
        "worker_id": target["worker_id"],
        "thread_id": target["thread_id"] or UNAVAILABLE,
        "model": target["model"] or UNAVAILABLE,
        "thinking": target["thinking"] or UNAVAILABLE,
        "source": record[0] if record else UNAVAILABLE,
        "observed_at": record[1] if record and record[1] else UNAVAILABLE,
        **values,
    }


def _total(rows: list[dict[str, Any]], targets: list[dict[str, Any]]) -> dict[str, Any]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, (row, target) in enumerate(zip(rows, targets)):
        thread_id = target.get("thread_id")
        key = thread_id or f"missing:{index}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    values = {
        field: (
            sum(row[field] for row in unique)
            if unique and all(isinstance(row[field], int) for row in unique)
            else UNAVAILABLE
        )
        for field in DISPLAY_FIELDS
    }
    observed = [row["observed_at"] for row in unique if row["observed_at"] != UNAVAILABLE]
    return {
        "role": "total",
        "worker_id": "total",
        "thread_id": UNAVAILABLE,
        "model": UNAVAILABLE,
        "thinking": UNAVAILABLE,
        "source": "aggregate" if unique else UNAVAILABLE,
        "observed_at": max(observed) if observed else UNAVAILABLE,
        **values,
    }


def build_report(db_path: str, collaboration_id: str, codex_home: str) -> dict[str, Any]:
    targets = read_targets(db_path, collaboration_id)
    rollout = find_rollout_usage(codex_home, targets)
    fallback = find_state_fallback(codex_home, (target["thread_id"] for target in targets))
    records = dict(fallback)
    records.update(rollout)  # rollout detail always wins over fallback totals
    rows = [_row(target, records.get(target["thread_id"])) for target in targets]
    return {
        "collaboration_id": collaboration_id,
        "rows": rows,
        "total": _total(rows, targets),
    }


def format_table(report: dict[str, Any]) -> str:
    columns = (
        "role", "worker_id", "thread_id", "model", "thinking", "source",
        "observed_at", *DISPLAY_FIELDS,
    )
    rows = [*report["rows"], report["total"]]
    values = [[str(row.get(column, UNAVAILABLE)) for column in columns] for row in rows]
    widths = [max(len(column), *(len(row[index]) for row in values)) for index, column in enumerate(columns)]
    header = "  ".join(column.ljust(widths[index]) for index, column in enumerate(columns))
    body = ["  ".join(row[index].ljust(widths[index]) for index in range(len(columns))) for row in values]
    return "\n".join([header, *body])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--collaboration-id", required=True)
    parser.add_argument("--codex-home", help="defaults to CODEX_HOME or ~/.codex")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    codex_home = args.codex_home or os.environ.get("CODEX_HOME") or "~/.codex"
    try:
        report = build_report(args.db, args.collaboration_id, codex_home)
    except (UsageError, OSError, sqlite3.Error, ValueError) as error:
        if args.json:
            print(json.dumps({"error": str(error)}, ensure_ascii=False, sort_keys=True))
        else:
            print(f"error: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    else:
        print(format_table(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
