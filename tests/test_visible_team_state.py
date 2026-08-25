from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "visible_team_state.py"


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


if __name__ == "__main__":
    unittest.main()
