from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from visible_team_coordination import ActionEnvelope  # noqa: E402
from visible_team_external import ExternalProviderAdapter  # noqa: E402
from providers.deepseek_harness import StartRequest  # noqa: E402


class VisibleTeamExternalTests(unittest.TestCase):
    def envelope(self, provider: str, *, authorized: bool = True) -> ActionEnvelope:
        return ActionEnvelope(
            action="create_worker",
            collaboration_id="project",
            worker_id="external",
            thread_id=None,
            provider=provider,
            payload={
                "title": "External Worker",
                "model": "explicit-model",
                "thinking": "high",
                "permission_mode": "safe",
                "responsibility": "Complete the approved bounded work.",
                "dispatch_authorized": authorized,
            },
        )

    def test_antigravity_bridge_preserves_native_identity_and_usage(self) -> None:
        class FakeProvider:
            def discover(self) -> dict:
                return {"available": True, "status": "available"}

            def start(self, prompt: str, **options: object) -> dict:
                self.prompt, self.options = prompt, options
                return {
                    "status": "success",
                    "native_session_id": "conversation-1",
                    "response": "done",
                    "usage": {
                        "input_tokens": 40,
                        "cache_read_tokens": 4,
                        "output_tokens": 9,
                        "thinking_tokens": 3,
                        "total_tokens": 49,
                    },
                }

        provider = FakeProvider()
        result = ExternalProviderAdapter(
            "antigravity", provider, cwd=str(ROOT)
        ).execute(self.envelope("antigravity"))
        self.assertEqual(result["native_task_id"], "conversation-1")
        self.assertEqual(result["usage"]["cached_input_tokens"], 4)
        self.assertEqual(result["usage"]["reasoning_output_tokens"], 3)
        self.assertEqual(result["delivery"]["summary"], "done")
        self.assertEqual(provider.options["model"], "explicit-model")
        self.assertEqual(provider.options["thinking"], "high")
        self.assertEqual(provider.options["permission_mode"], "safe")

    def test_deepseek_bridge_starts_then_sends_with_same_route(self) -> None:
        class FakeProvider:
            def discover(self) -> dict:
                return {"available": True, "status": "available"}

            def start(self, request: StartRequest) -> dict:
                self.request = request
                return {"native_session_id": "dsh-session-1"}

            def send_or_resume(self, record: dict, prompt: str, **route: object) -> dict:
                self.record, self.prompt, self.route = record, prompt, route
                return {
                    "status": "completed",
                    "native_session_id": "dsh-session-1",
                    "final_response": "report ready",
                    "usage": {
                        "input_tokens": 20,
                        "cached_input_tokens": 2,
                        "output_tokens": 6,
                        "reasoning_output_tokens": 1,
                        "total_tokens": 26,
                    },
                }

        provider = FakeProvider()
        result = ExternalProviderAdapter(
            "deepseek-harness", provider, cwd=str(ROOT)
        ).execute(self.envelope("deepseek-harness"))
        self.assertIsInstance(provider.request, StartRequest)
        self.assertEqual(provider.request.model, "explicit-model")
        self.assertEqual(provider.request.thinking, "high")
        self.assertEqual(provider.request.permissions["mode"], "safe")
        self.assertEqual(provider.route["model"], "explicit-model")
        self.assertEqual(result["native_task_id"], "dsh-session-1")
        self.assertEqual(result["delivery"]["summary"], "report ready")

    def test_bridge_refuses_unapproved_or_mismatched_dispatch(self) -> None:
        class NeverCalled:
            def discover(self) -> dict:
                raise AssertionError("discovery must not run before authorization")

        adapter = ExternalProviderAdapter("antigravity", NeverCalled(), cwd=str(ROOT))
        denied = adapter.execute(self.envelope("antigravity", authorized=False))
        self.assertEqual(denied["failure"]["category"], "authorization")
        mismatch = adapter.execute(self.envelope("deepseek-harness"))
        self.assertEqual(mismatch["failure"]["category"], "authorization")


if __name__ == "__main__":
    unittest.main()
