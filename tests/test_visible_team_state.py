from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "visible_team_state.py"
sys.path.insert(0, str(ROOT / "scripts"))
import visible_team_state as state  # noqa: E402
from visible_team_coordination import ActionEnvelope, Coordinator  # noqa: E402


class VisibleTeamStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.db = Path(self.temp_directory.name) / "state.sqlite"

    def run_cli(self, *arguments: str, expect_success: bool = True) -> dict:
        result = subprocess.run(
            [sys.executable, str(HELPER), "--db", str(self.db), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
        if expect_success and result.returncode != 0:
            self.fail(f"command failed: {result.stderr}")
        if not expect_success and result.returncode == 0:
            self.fail(f"command unexpectedly succeeded: {result.stdout}")
        payload = result.stdout if result.returncode == 0 else result.stderr
        return json.loads(payload)

    def init(self, collaboration_id: str = "project") -> dict:
        return self.run_cli(
            "init",
            "--collaboration-id",
            collaboration_id,
            "--objective",
            "完成一个通用交付物",
            "--leader-thread-id",
            "leader-1",
        )

    def plan_worker(self, worker_id: str, key: str, *, expected: int | None = None) -> dict:
        arguments = [
            "plan-worker",
            "--collaboration-id",
            "project",
            "--worker-id",
            worker_id,
            "--title",
            f"Worker {worker_id}",
            "--model",
            "gpt-5.6-luna",
            "--thinking",
            "medium",
            "--responsibility",
            "执行明确的工作",
            "--idempotency-key",
            key,
        ]
        if expected is not None:
            arguments.extend(["--expected-version", str(expected)])
        return self.run_cli(*arguments)

    def test_init_is_idempotent(self) -> None:
        first = self.init()
        second = self.init()
        self.assertEqual(first["collaboration_id"], "project")
        self.assertEqual(first["version"], 1)
        self.assertTrue(second["replayed"])
        self.assertEqual(second["version"], 1)

    def test_worker_plan_and_attach_are_idempotent(self) -> None:
        self.init()
        planned = self.plan_worker("writer", "plan-writer")
        replayed = self.plan_worker("writer", "plan-writer")
        attached = self.run_cli(
            "attach-worker",
            "--collaboration-id",
            "project",
            "--worker-id",
            "writer",
            "--thread-id",
            "thread-123",
            "--idempotency-key",
            "attach-writer",
        )
        self.assertEqual(planned["workers"][0]["status"], "planned")
        self.assertTrue(replayed["replayed"])
        self.assertEqual(attached["workers"][0]["thread_id"], "thread-123")
        self.assertEqual(attached["workers"][0]["status"], "active")

    def test_context_is_delivered_only_to_selected_worker(self) -> None:
        self.init()
        self.plan_worker("writer", "plan-writer")
        self.plan_worker("reviewer", "plan-reviewer")
        update = self.run_cli(
            "add-context",
            "--collaboration-id",
            "project",
            "--summary",
            "只修改正文措辞",
            "--source-ref",
            "draft.md",
            "--target",
            "writer",
            "--idempotency-key",
            "context-copy-edit",
        )
        writer = self.run_cli(
            "pending", "--collaboration-id", "project", "--worker-id", "writer"
        )
        reviewer = self.run_cli(
            "pending", "--collaboration-id", "project", "--worker-id", "reviewer"
        )
        self.assertEqual(len(writer["updates"]), 1)
        self.assertEqual(writer["updates"][0]["summary"], "只修改正文措辞")
        self.assertEqual(reviewer["updates"], [])

        through_version = update["context_update"]["version"]
        acknowledged = self.run_cli(
            "acknowledge",
            "--collaboration-id",
            "project",
            "--worker-id",
            "writer",
            "--through-version",
            str(through_version),
            "--idempotency-key",
            "ack-copy-edit",
        )
        pending_after = self.run_cli(
            "pending", "--collaboration-id", "project", "--worker-id", "writer"
        )
        self.assertEqual(acknowledged["acknowledged_updates"], 1)
        self.assertEqual(pending_after["updates"], [])

    def test_stale_version_is_rejected(self) -> None:
        self.init()
        result = self.run_cli(
            "plan-worker",
            "--collaboration-id",
            "project",
            "--worker-id",
            "writer",
            "--title",
            "Writer",
            "--model",
            "gpt-5.6-luna",
            "--thinking",
            "medium",
            "--responsibility",
            "撰写",
            "--idempotency-key",
            "stale-plan",
            "--expected-version",
            "0",
            expect_success=False,
        )
        self.assertIn("Stale collaboration version", result["error"])

    def test_terminal_collaboration_cannot_reopen(self) -> None:
        self.init()
        self.run_cli(
            "set-collaboration-status",
            "--collaboration-id",
            "project",
            "--status",
            "completed",
            "--idempotency-key",
            "finish-project",
        )
        result = self.run_cli(
            "set-collaboration-status",
            "--collaboration-id",
            "project",
            "--status",
            "active",
            "--idempotency-key",
            "reopen-project",
            expect_success=False,
        )
        self.assertIn("cannot be reopened", result["error"])

    def test_v1_database_migrates_without_losing_rows(self) -> None:
        connection = sqlite3.connect(self.db)
        state._create_v1_schema(connection)
        connection.execute(
            "INSERT INTO collaborations VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("old", "legacy objective", "leader-old", "active", 4, "created", "updated"),
        )
        connection.execute(
            """INSERT INTO workers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("old", "w-old", "thread-old", "Old", "legacy-model", "high", "legacy", "active", 2, "created", "updated"),
        )
        connection.execute(
            "INSERT INTO context_updates (collaboration_id, version, summary, source_ref, created_at, idempotency_key) VALUES (?, ?, ?, ?, ?, ?)",
            ("old", 5, "legacy context", "legacy.md", "created", "legacy-context"),
        )
        connection.execute("INSERT INTO context_targets (update_id, worker_id) VALUES (1, 'w-old')")
        connection.execute(
            "INSERT INTO events (collaboration_id, version, event_type, actor, payload_json, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("old", 5, "legacy_event", "leader", "{}", "legacy-event", "created"),
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
        connection.close()

        migrated = state.connect(str(self.db))
        self.assertEqual(migrated.execute("PRAGMA user_version").fetchone()[0], 2)
        snapshot = state.snapshot(migrated, "old")
        self.assertEqual(snapshot["objective"], "legacy objective")
        self.assertEqual(snapshot["version"], 4)
        self.assertEqual(snapshot["workers"][0]["thread_id"], "thread-old")
        self.assertEqual(snapshot["workers"][0]["delivery_status"], "pending")
        self.assertEqual(snapshot["workers"][0]["pending_updates"], 1)
        self.assertEqual(migrated.execute("SELECT COUNT(*) FROM events WHERE idempotency_key = 'legacy-event'").fetchone()[0], 1)
        migrated.close()

        repeated = state.connect(str(self.db))
        self.assertEqual(repeated.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(repeated.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        repeated.close()

    def test_unknown_schema_is_rejected_without_creating_or_downgrading(self) -> None:
        connection = sqlite3.connect(self.db)
        connection.execute("PRAGMA user_version = 99")
        connection.commit()
        connection.close()
        with self.assertRaises(state.StateError):
            state.connect(str(self.db))
        check = sqlite3.connect(self.db)
        self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], 99)
        self.assertEqual(check.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0], 0)
        check.close()

    def test_declared_v2_with_missing_tables_is_rejected(self) -> None:
        connection = sqlite3.connect(self.db)
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
        connection.close()
        with self.assertRaises(state.StateError):
            state.connect(str(self.db))

    def test_declared_v2_with_incomplete_base_table_is_rejected(self) -> None:
        connection = sqlite3.connect(self.db)
        state._create_v1_schema(connection)
        state._migrate_v1_to_v2(connection)
        connection.execute("ALTER TABLE collaborations RENAME TO collaborations_complete")
        connection.execute(
            """
            CREATE TABLE collaborations (
                collaboration_id TEXT PRIMARY KEY,
                skill_version TEXT NOT NULL
            )
            """
        )
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(state.StateError, "collaborations.objective"):
            state.connect(str(self.db))

    def test_migration_rolls_back_schema_and_version_on_failure(self) -> None:
        connection = sqlite3.connect(self.db)
        state._create_v1_schema(connection)
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
        original = state._migrate_v1_to_v2

        def fail_after_ddl(conn: sqlite3.Connection) -> None:
            original(conn)
            raise RuntimeError("simulated migration failure")

        state._migrate_v1_to_v2 = fail_after_ddl
        try:
            with self.assertRaises(RuntimeError):
                state.migrate(connection)
        finally:
            state._migrate_v1_to_v2 = original
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertFalse(any(row[1] == "delivery_status" for row in connection.execute("PRAGMA table_info(workers)")))
        connection.close()

    def test_worker_config_update_is_versioned_and_idempotent(self) -> None:
        self.init()
        self.plan_worker("writer", "plan-writer")
        updated = self.run_cli(
            "update-worker-config", "--collaboration-id", "project", "--worker-id", "writer",
            "--model", "gpt-5.6-luna", "--thinking", "max", "--idempotency-key", "config-writer",
        )
        replayed = self.run_cli(
            "update-worker-config", "--collaboration-id", "project", "--worker-id", "writer",
            "--model", "gpt-5.6-luna", "--thinking", "max", "--idempotency-key", "config-writer",
        )
        self.assertEqual(updated["workers"][0]["model"], "gpt-5.6-luna")
        self.assertEqual(updated["workers"][0]["thinking"], "max")
        self.assertTrue(replayed["replayed"])
        events = self.run_cli("events", "--collaboration-id", "project")
        self.assertEqual(events["events"][-1]["event_type"], "worker_config_updated")

        empty = self.run_cli(
            "update-worker-config", "--collaboration-id", "project", "--worker-id", "writer",
            "--idempotency-key", "config-empty", expect_success=False,
        )
        self.assertIn("must change", empty["error"])
        blank = self.run_cli(
            "update-worker-config", "--collaboration-id", "project", "--worker-id", "writer",
            "--model", "", "--idempotency-key", "config-blank", expect_success=False,
        )
        self.assertIn("cannot be empty", blank["error"])

    def test_worker_config_update_rejects_terminal_worker(self) -> None:
        self.init()
        self.plan_worker("writer", "plan-writer")
        self.run_cli(
            "set-delivery-status", "--collaboration-id", "project", "--worker-id", "writer",
            "--status", "submitted", "--summary", "result", "--idempotency-key", "submit-writer",
        )
        self.run_cli(
            "set-worker-status", "--collaboration-id", "project", "--worker-id", "writer",
            "--status", "completed", "--idempotency-key", "complete-writer",
        )
        rejected = self.run_cli(
            "update-worker-config", "--collaboration-id", "project", "--worker-id", "writer",
            "--thinking", "max", "--idempotency-key", "config-terminal", expect_success=False,
        )
        self.assertIn("terminal Worker", rejected["error"])

    def test_worker_config_update_rejects_terminal_collaboration(self) -> None:
        self.init()
        self.plan_worker("writer", "plan-writer")
        self.run_cli(
            "set-collaboration-status", "--collaboration-id", "project", "--status", "completed",
            "--idempotency-key", "complete-project",
        )
        rejected = self.run_cli(
            "update-worker-config", "--collaboration-id", "project", "--worker-id", "writer",
            "--thinking", "max", "--idempotency-key", "config-after-project", expect_success=False,
        )
        self.assertIn("terminal collaboration", rejected["error"])

    def test_delivery_handshake_rejects_invalid_and_unverifiable_acceptance(self) -> None:
        self.init()
        self.plan_worker("writer", "plan-writer")
        invalid = self.run_cli(
            "set-delivery-status", "--collaboration-id", "project", "--worker-id", "writer",
            "--status", "accepted", "--idempotency-key", "accept-too-early", expect_success=False,
        )
        self.assertIn("Invalid delivery transition", invalid["error"])
        self.run_cli(
            "set-delivery-status", "--collaboration-id", "project", "--worker-id", "writer",
            "--status", "submitted", "--summary", "draft result", "--idempotency-key", "submit-writer",
        )
        worker_accept = self.run_cli(
            "set-delivery-status", "--collaboration-id", "project", "--worker-id", "writer",
            "--status", "received", "--actor", "worker", "--idempotency-key", "worker-receive",
            expect_success=False,
        )
        self.assertIn("only the Leader", worker_accept["error"])
        self.run_cli(
            "set-delivery-status", "--collaboration-id", "project", "--worker-id", "writer",
            "--status", "received", "--idempotency-key", "receive-writer",
        )
        accepted = self.run_cli(
            "set-delivery-status", "--collaboration-id", "project", "--worker-id", "writer",
            "--status", "accepted", "--idempotency-key", "accept-writer",
        )
        self.assertEqual(accepted["workers"][0]["delivery_status"], "accepted")

    def test_needs_attention_cannot_reuse_old_submission_without_new_result(self) -> None:
        self.init()
        self.plan_worker("writer", "plan-writer")
        self.run_cli(
            "set-delivery-status", "--collaboration-id", "project", "--worker-id", "writer",
            "--status", "submitted", "--summary", "old result", "--idempotency-key", "old-submit",
        )
        self.run_cli(
            "record-observation", "--collaboration-id", "project", "--worker-id", "writer",
            "--task-exists", "no", "--host-status", "completed", "--result-available", "no",
            "--idempotency-key", "missing-after-submit",
        )
        rejected = self.run_cli(
            "set-delivery-status", "--collaboration-id", "project", "--worker-id", "writer",
            "--status", "received", "--idempotency-key", "reuse-old", expect_success=False,
        )
        self.assertIn("new summary or artifact reference", rejected["error"])
        recovered = self.run_cli(
            "set-delivery-status", "--collaboration-id", "project", "--worker-id", "writer",
            "--status", "received", "--summary", "new result", "--idempotency-key", "new-receive",
        )
        self.assertEqual(recovered["workers"][0]["delivery_status"], "received")

    def test_needs_attention_must_obtain_result_before_acceptance(self) -> None:
        self.init()
        self.plan_worker("writer", "plan-writer")
        self.run_cli(
            "record-observation", "--collaboration-id", "project", "--worker-id", "writer",
            "--task-exists", "no", "--host-status", "completed", "--result-available", "no",
            "--observed-at", "2026-01-01T00:00:00+00:00", "--idempotency-key", "missing-result",
        )
        rejected = self.run_cli(
            "set-delivery-status", "--collaboration-id", "project", "--worker-id", "writer",
            "--status", "accepted", "--summary", "not-enough", "--idempotency-key", "bad-accept",
            expect_success=False,
        )
        self.assertIn("Invalid delivery transition", rejected["error"])
        completed = self.run_cli(
            "set-worker-status", "--collaboration-id", "project", "--worker-id", "writer",
            "--status", "completed", "--idempotency-key", "bad-complete", expect_success=False,
        )
        self.assertIn("cannot be completed", completed["error"])

    def test_worker_completion_requires_submission_but_collaboration_is_separate(self) -> None:
        self.init()
        self.plan_worker("writer", "plan-writer")
        rejected = self.run_cli(
            "set-worker-status", "--collaboration-id", "project", "--worker-id", "writer",
            "--status", "completed", "--idempotency-key", "complete-too-early", expect_success=False,
        )
        self.assertIn("cannot be completed", rejected["error"])
        self.run_cli(
            "set-delivery-status", "--collaboration-id", "project", "--worker-id", "writer",
            "--status", "submitted", "--artifact-ref", "result://writer/1", "--idempotency-key", "submit-ref",
        )
        completed = self.run_cli(
            "set-worker-status", "--collaboration-id", "project", "--worker-id", "writer",
            "--status", "completed", "--idempotency-key", "complete-writer",
        )
        self.assertEqual(completed["workers"][0]["status"], "completed")
        self.assertEqual(completed["status"], "active")

    def test_resume_includes_missing_host_and_pending_context(self) -> None:
        self.init()
        self.plan_worker("writer", "plan-writer")
        self.plan_worker("reviewer", "plan-reviewer")
        self.run_cli(
            "attach-worker", "--collaboration-id", "project", "--worker-id", "writer",
            "--thread-id", "thread-writer", "--idempotency-key", "attach-writer",
        )
        self.run_cli(
            "add-context", "--collaboration-id", "project", "--summary", "writer-only", "--target", "writer",
            "--idempotency-key", "writer-context",
        )
        self.run_cli(
            "record-observation", "--collaboration-id", "project", "--worker-id", "reviewer",
            "--task-exists", "no", "--host-status", "missing", "--result-available", "no",
            "--lease-until", "2025-01-01T00:00:00+00:00", "--observed-at", "2026-01-01T00:00:00+00:00",
            "--idempotency-key", "reviewer-missing",
        )
        resumed = self.run_cli("resume", "--collaboration-id", "project")
        self.assertEqual(resumed["versions"]["schema"], 2)
        self.assertEqual(resumed["workers"][0]["pending_context_count"], 1)
        self.assertTrue(any("missing" in note for note in resumed["notes"]))
        self.assertTrue(any(action["kind"] == "deliver_context" for action in resumed["next_actions"]))

    def test_pending_context_without_thread_has_no_send_action(self) -> None:
        self.init()
        self.plan_worker("writer", "plan-writer")
        self.run_cli(
            "add-context", "--collaboration-id", "project", "--summary", "wait for attach", "--target", "writer",
            "--idempotency-key", "pending-before-attach",
        )
        resumed = self.run_cli("resume", "--collaboration-id", "project")
        self.assertEqual([action["kind"] for action in resumed["next_actions"]], ["create_worker"])
        self.assertTrue(any("no thread" in note for note in resumed["notes"]))

    def test_failure_categories_explain_manual_action(self) -> None:
        self.init()
        self.plan_worker("writer", "plan-writer")
        failure = self.run_cli(
            "record-failure", "--collaboration-id", "project", "--worker-id", "writer",
            "--category", "transient", "--message", "host unavailable", "--idempotency-key", "fail-1",
        )
        self.assertEqual(failure["failure"]["category"], "transient")
        self.assertFalse(failure["failure"]["automatic"])
        permanent = self.run_cli(
            "record-failure", "--collaboration-id", "project", "--worker-id", "writer",
            "--category", "authorization", "--message", "approval required", "--idempotency-key", "fail-2",
        )
        self.assertEqual(permanent["failure"]["kind"], "authorize")

    def test_coordinator_uses_targeted_actions_once_without_retry(self) -> None:
        self.init()
        self.plan_worker("writer", "plan-writer")
        self.plan_worker("reviewer", "plan-reviewer")
        self.run_cli(
            "attach-worker", "--collaboration-id", "project", "--worker-id", "writer",
            "--thread-id", "thread-writer", "--idempotency-key", "attach-writer",
        )
        self.run_cli(
            "add-context", "--collaboration-id", "project", "--summary", "writer-only", "--target", "writer",
            "--idempotency-key", "targeted-context",
        )

        class FakeAdapter:
            def __init__(self) -> None:
                self.calls: list[ActionEnvelope] = []

            def execute(self, envelope: ActionEnvelope) -> dict:
                self.calls.append(envelope)
                return {"failure": {"category": "transient", "message": "temporary"}}

        adapter = FakeAdapter()
        coordinator = Coordinator(str(self.db), adapter)
        actions = coordinator.next_actions("project")
        self.assertIn(("writer", "deliver_context"), [(action.worker_id, action.action) for action in actions])
        self.assertIn(("reviewer", "create_worker"), [(action.worker_id, action.action) for action in actions])
        writer_action = next(action for action in actions if action.worker_id == "writer")
        self.assertEqual(writer_action.thread_id, "thread-writer")
        self.assertEqual([update["summary"] for update in writer_action.payload["updates"]], ["writer-only"])
        reviewer_action = next(action for action in actions if action.worker_id == "reviewer")
        self.assertEqual(reviewer_action.payload, {"title": "Worker reviewer", "model": "gpt-5.6-luna", "thinking": "medium", "responsibility": "执行明确的工作"})
        result = coordinator.execute(writer_action, "adapter-call-1")
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(adapter.calls[0].idempotency_key, "adapter-call-1")
        self.assertEqual(result["response"]["failure"]["category"], "transient")
        repeated = coordinator.execute(writer_action, "adapter-call-1")
        self.assertEqual(len(adapter.calls), 1)
        self.assertIsNone(repeated["response"])
        self.assertGreaterEqual(len(coordinator.next_actions("project")), 1)

    def test_coordinator_requires_host_identity_before_attaching_uncertain_creation(self) -> None:
        self.init()
        self.plan_worker("writer", "plan-writer")

        class CreateAdapter:
            def __init__(self) -> None:
                self.calls = 0

            def execute(self, envelope: ActionEnvelope) -> dict:
                self.calls += 1
                if self.calls == 1:
                    return {}
                if self.calls == 2:
                    return {"creation_reconciliation": {"outcome": "missing"}}
                return {"thread_id": "thread-writer"}

        adapter = CreateAdapter()
        coordinator = Coordinator(str(self.db), adapter)
        create_action = coordinator.next_actions("project")[0]
        self.assertEqual(create_action.action, "create_worker")
        coordinator.execute(create_action, "create-attempt-1")
        self.assertEqual(adapter.calls, 1)
        duplicate = coordinator.execute(create_action, "create-attempt-1")
        self.assertEqual(adapter.calls, 1)
        self.assertTrue(duplicate["reservation"]["replayed"])
        self.assertEqual(coordinator.next_actions("project")[0].action, "reconcile_creation")
        coordinator.execute(coordinator.next_actions("project")[0], "reconcile-attempt-1")
        self.assertEqual(adapter.calls, 2)
        self.assertEqual(coordinator.next_actions("project")[0].action, "create_worker")
        coordinator.execute(create_action, "create-attempt-2")
        self.assertEqual(adapter.calls, 3)
        remaining = coordinator.next_actions("project")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].action, "observe_worker")

    def test_coordinator_observation_writeback_is_idempotent(self) -> None:
        self.init()
        self.plan_worker("writer", "plan-writer")
        self.run_cli(
            "attach-worker", "--collaboration-id", "project", "--worker-id", "writer",
            "--thread-id", "thread-writer", "--idempotency-key", "attach-writer",
        )

        class ObserveAdapter:
            def __init__(self) -> None:
                self.calls = 0

            def execute(self, envelope: ActionEnvelope) -> dict:
                self.calls += 1
                return {
                    "observation": {
                        "task_exists": True,
                        "host_status": "active",
                        "result_available": False,
                        "observed_at": "2026-01-01T00:00:00+00:00",
                    }
                }

        adapter = ObserveAdapter()
        coordinator = Coordinator(str(self.db), adapter)
        action = next(action for action in coordinator.next_actions("project") if action.action == "observe_worker")
        first = coordinator.execute(action, "observe-once")
        self.assertEqual(adapter.calls, 1)
        self.assertTrue(first["response"]["observation"]["task_exists"])
        repeated = coordinator.execute(action, "observe-once")
        self.assertEqual(adapter.calls, 1)
        self.assertIsNone(repeated["response"])
        snapshot = self.run_cli("snapshot", "--collaboration-id", "project")
        self.assertEqual(snapshot["workers"][0]["host_observation"]["host_status"], "active")


if __name__ == "__main__":
    unittest.main()
