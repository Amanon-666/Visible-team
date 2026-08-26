from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "visible_team_usage.py"
sys.path.insert(0, str(ROOT / "scripts"))
import visible_team_usage as usage  # noqa: E402


class VisibleTeamUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        root = Path(self.temp_directory.name)
        self.db = root / "collaboration.sqlite"
        self.codex_home = root / "codex"
        self.codex_home.mkdir()

    def make_db(self, workers: list[tuple[str, str | None, str, str]]) -> None:
        connection = sqlite3.connect(self.db)
        connection.executescript(
            """
            CREATE TABLE collaborations (
                collaboration_id TEXT PRIMARY KEY,
                leader_thread_id TEXT
            );
            CREATE TABLE workers (
                collaboration_id TEXT,
                worker_id TEXT,
                thread_id TEXT,
                model TEXT,
                thinking TEXT,
                created_at TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO collaborations VALUES ('project', 'leader-thread')"
        )
        connection.executemany(
            "INSERT INTO workers VALUES ('project', ?, ?, ?, ?, '2026-01-01')",
            workers,
        )
        connection.commit()
        connection.close()

    @staticmethod
    def token_event(
        timestamp: str,
        sequence: int,
        *,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
        reasoning_output_tokens: int,
        total_tokens: int,
    ) -> dict:
        return {
            "timestamp": timestamp,
            "sequence": sequence,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": cached_input_tokens,
                        "cache_write_input_tokens": 9999,
                        "output_tokens": output_tokens,
                        "reasoning_output_tokens": reasoning_output_tokens,
                        "total_tokens": total_tokens,
                        "model_context_window": 123456,
                    },
                    "rate_limits": {"secret": "do-not-copy"},
                    "credits": {"secret": "do-not-copy"},
                },
            },
        }

    @staticmethod
    def session_meta(thread_id: str) -> dict:
        return {"type": "session_meta", "payload": {"type": "session_meta", "id": thread_id}}

    @staticmethod
    def write_jsonl(path: Path, records: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )

    def report(self) -> dict:
        return usage.build_report(str(self.db), "project", str(self.codex_home))

    def test_latest_rollout_parent_child_total_and_no_double_counting(self) -> None:
        # The second leader file is in archived_sessions to exercise the
        # cross-directory latest-event selection.  The duplicate Worker thread
        # must not be counted twice in the aggregate.
        self.make_db([
            ("writer", "child-thread", "gpt-5.6-luna", "max"),
            ("writer-copy", "child-thread", "gpt-5.6-luna", "max"),
        ])
        self.write_jsonl(
            self.codex_home / "sessions" / "leader-old.jsonl",
            [
                self.session_meta("leader-thread"),
                self.token_event(
                    "2026-01-01T00:00:00Z", 1, input_tokens=90,
                    cached_input_tokens=10, output_tokens=20,
                    reasoning_output_tokens=5, total_tokens=110,
                ),
            ],
        )
        self.write_jsonl(
            self.codex_home / "archived_sessions" / "leader-new.jsonl",
            [
                self.session_meta("leader-thread"),
                self.token_event(
                    "2026-01-02T00:00:00Z", 2, input_tokens=100,
                    cached_input_tokens=20, output_tokens=30,
                    reasoning_output_tokens=10, total_tokens=130,
                ),
            ],
        )
        self.write_jsonl(
            self.codex_home / "sessions" / "writer.jsonl",
            [
                self.session_meta("child-thread"),
                self.token_event(
                    "2026-01-02T01:00:00Z", 3, input_tokens=40,
                    cached_input_tokens=5, output_tokens=8,
                    reasoning_output_tokens=3, total_tokens=48,
                ),
            ],
        )

        report = self.report()
        leader, writer, duplicate = report["rows"]
        self.assertEqual(leader["input_tokens"], 100)
        self.assertEqual(leader["uncached_input_tokens"], 80)
        self.assertEqual(writer["source"], "rollout")
        self.assertEqual(writer["model"], "gpt-5.6-luna")
        self.assertEqual(duplicate["total_tokens"], 48)
        total = report["total"]
        self.assertEqual(total["input_tokens"], 140)
        self.assertEqual(total["cached_input_tokens"], 25)
        self.assertEqual(total["uncached_input_tokens"], 115)
        self.assertEqual(total["output_tokens"], 38)
        self.assertEqual(total["reasoning_output_tokens"], 13)
        self.assertEqual(total["total_tokens"], 178)
        self.assertNotIn("rate_limits", json.dumps(report))
        self.assertNotIn("credits", json.dumps(report))
        self.assertNotIn("model_context_window", json.dumps(report))
        self.assertNotIn("cache_write_input_tokens", json.dumps(report))

    def test_state_db_fallback_unknown_schema_and_unavailable_are_explicit(self) -> None:
        self.make_db([
            ("writer", "child-thread", "gpt-5.6-luna", "max"),
            ("missing", "missing-thread", "gpt-5.6-luna", "medium"),
        ])
        unknown = sqlite3.connect(self.codex_home / "state_unknown.sqlite")
        unknown.execute("CREATE TABLE unrelated (value TEXT)")
        unknown.commit()
        unknown.close()
        state_db = sqlite3.connect(self.codex_home / "state_5.sqlite")
        state_db.execute("CREATE TABLE threads (id TEXT, tokens_used INTEGER)")
        state_db.execute("INSERT INTO threads VALUES ('leader-thread', 70)")
        state_db.execute("INSERT INTO threads VALUES ('child-thread', 30)")
        state_db.commit()
        state_db.close()

        report = self.report()
        leader, missing, writer = report["rows"]
        self.assertEqual(leader["source"], "codex-state-db")
        self.assertEqual(leader["total_tokens"], 70)
        self.assertEqual(leader["input_tokens"], usage.UNAVAILABLE)
        self.assertEqual(writer["total_tokens"], 30)
        self.assertEqual(missing["source"], usage.UNAVAILABLE)
        self.assertEqual(missing["total_tokens"], usage.UNAVAILABLE)
        # A partial total must not look complete when one target has no data.
        self.assertEqual(report["total"]["total_tokens"], usage.UNAVAILABLE)

    def test_json_and_table_cli_are_stable_and_read_only(self) -> None:
        self.make_db([])
        before = self.db.stat().st_mtime_ns
        command = [
            sys.executable,
            str(HELPER),
            "--db",
            str(self.db),
            "--collaboration-id",
            "project",
            "--codex-home",
            str(self.codex_home),
            "--json",
        ]
        first = subprocess.run(command, check=False, capture_output=True, text=True)
        second = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)
        payload = json.loads(first.stdout)
        self.assertEqual(payload["collaboration_id"], "project")
        self.assertEqual(payload["rows"][0]["source"], usage.UNAVAILABLE)
        self.assertEqual(self.db.stat().st_mtime_ns, before)
        table = subprocess.run(command[:-1], check=False, capture_output=True, text=True)
        self.assertEqual(table.returncode, 0, table.stderr)
        self.assertIn("role", table.stdout)
        self.assertIn("unavailable", table.stdout)

    def test_malformed_rollout_is_skipped_without_guessing(self) -> None:
        self.make_db([("writer", "child-thread", "gpt-5.6-luna", "max")])
        self.write_jsonl(
            self.codex_home / "sessions" / "bad.jsonl",
            [
                {"not": "json"},
                {"type": "event_msg", "payload": {"type": "token_count", "info": {}}},
                self.session_meta("child-thread"),
            ],
        )
        worker = self.report()["rows"][1]
        self.assertEqual(worker["source"], usage.UNAVAILABLE)
        self.assertEqual(worker["total_tokens"], usage.UNAVAILABLE)

    def test_external_provider_uses_recorded_native_usage_not_codex_rollouts(self) -> None:
        connection = sqlite3.connect(self.db)
        connection.executescript(
            """
            CREATE TABLE collaborations (
                collaboration_id TEXT PRIMARY KEY,
                leader_thread_id TEXT
            );
            CREATE TABLE workers (
                collaboration_id TEXT,
                worker_id TEXT,
                thread_id TEXT,
                provider TEXT,
                native_task_id TEXT,
                model TEXT,
                thinking TEXT,
                created_at TEXT
            );
            CREATE TABLE worker_usage (
                usage_id INTEGER PRIMARY KEY,
                collaboration_id TEXT,
                worker_id TEXT,
                source TEXT,
                observed_at TEXT,
                input_tokens INTEGER,
                cached_input_tokens INTEGER,
                output_tokens INTEGER,
                reasoning_output_tokens INTEGER,
                total_tokens INTEGER
            );
            """
        )
        connection.execute("INSERT INTO collaborations VALUES ('project', NULL)")
        connection.execute(
            "INSERT INTO workers VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "project", "agy", None, "antigravity", "conversation-1",
                "gemini-3-pro", "high", "2026-01-01",
            ),
        )
        connection.execute(
            "INSERT INTO worker_usage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1, "project", "agy", "agy-result", "2026-01-02T00:00:00Z",
                50, 5, 12, 4, 62,
            ),
        )
        connection.commit()
        connection.close()

        report = self.report()
        row = report["rows"][1]
        self.assertEqual(row["provider"], "antigravity")
        self.assertEqual(row["native_task_id"], "conversation-1")
        self.assertEqual(row["source"], "agy-result")
        self.assertEqual(row["uncached_input_tokens"], 45)
        self.assertEqual(row["total_tokens"], 62)


if __name__ == "__main__":
    unittest.main()
