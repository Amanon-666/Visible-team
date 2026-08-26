from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from scripts.providers.deepseek_harness import (
    DshProviderError,
    ErrorKind,
    RunResult,
    StartRequest,
    TokenUsageRecord,
    DeepSeekHarnessProvider,
    fold_token_usage,
    normalize_token_usage,
)


class FakeRunner:
    """Offline SDK seam; it never starts a process or calls a model."""

    def __init__(self, failure: BaseException | None = None, malformed: bool = False) -> None:
        self.failure = failure
        self.malformed = malformed
        self.start_requests: list[StartRequest] = []
        self.run_calls: list[tuple[str, list[dict[str, Any]]]] = []
        self.close_calls = 0

    def start(self, request: StartRequest) -> dict[str, str]:
        self.start_requests.append(request)
        return {"name": "fixture-dsh-runtime", "version": "offline"}

    def run(self, native_session_id: str, content: list[dict[str, Any]], *, on_notification: Any = None, timeout_seconds: float | None = None) -> Any:
        del timeout_seconds
        self.run_calls.append((native_session_id, content))
        if self.failure is not None:
            raise self.failure
        if self.malformed:
            return {"session_id": native_session_id, "events": [{"type": "assistant/message", "data": "bad"}]}

        events: list[dict[str, Any]] = [
            {"type": "assistant/chunk", "data": {"turn": 1, "step": 1, "chunk": {"type": "text-delta", "text": "partial"}}},
            {"type": "assistant/chunk", "data": {"turn": 1, "step": 1, "chunk": {"type": "usage", "usage": {"inputTokens": 5, "cacheReadTokens": 1, "outputTokens": 2}}}},
            {"type": "assistant/message", "data": {"turn": 1, "step": 1, "message": {"content": [{"type": "text", "text": "final"}]}, "usage": {"inputTokens": 10, "cacheReadTokens": 2, "outputTokens": 4, "reasoningTokens": 1}}},
            {"type": "assistant/message", "data": {"turn": 1, "step": 2, "message": {"content": []}}},
            {"type": "turn/end", "data": {"turn": 1, "reason": {"kind": "completed"}}},
        ]
        notifications: list[dict[str, Any]] = [
            {"method": "session.status", "payload": {"sessionId": native_session_id, "status": "running"}},
            *({"method": "session.event", "payload": {"sessionId": native_session_id, "event": event}} for event in events),
            {"method": "session.status", "payload": {"sessionId": native_session_id, "status": "idle"}},
        ]
        if on_notification is not None:
            for notification in notifications:
                on_notification(notification)
        return RunResult(native_session_id, "final", "completed", events, notifications, TokenUsageRecord())

    def close(self) -> None:
        self.close_calls += 1


class DeepSeekHarnessProviderTests(unittest.TestCase):
    def request(self, workspace: str, **kwargs: Any) -> StartRequest:
        return StartRequest(model="deepseek-v4-flash", thinking="high", permissions={"workspace": workspace}, **kwargs)

    def test_discover_and_capabilities_are_offline(self) -> None:
        runner = FakeRunner()
        provider = DeepSeekHarnessProvider(runner=runner)

        discovered = provider.discover()
        capabilities = provider.capabilities()

        self.assertTrue(discovered["available"])
        self.assertEqual(discovered["runner"], "injected")
        self.assertTrue(capabilities["start"]["available"])
        self.assertEqual(capabilities["watch"]["scope"], "during-send")
        self.assertFalse(capabilities["read"]["available"])
        self.assertFalse(capabilities["interrupt-or-cancel"]["available"])
        self.assertEqual(runner.start_requests, [])

    def test_start_requires_explicit_route_and_records_native_identity(self) -> None:
        runner = FakeRunner()
        provider = DeepSeekHarnessProvider(runner=runner)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(DshProviderError) as missing:
                provider.start()
            self.assertEqual(missing.exception.kind, ErrorKind.INVALID_REQUEST.value)

            record = provider.start(self.request(directory, native_session_id="native-1", native_open_ref="dsh://native-1"))

        self.assertEqual(record.provider, "deepseek-harness")
        self.assertEqual(record.runtime, "deepseek-harness-sdk-runtime")
        self.assertEqual(record.native_session_id, "native-1")
        self.assertEqual(record.native_open_ref, "dsh://native-1")
        self.assertEqual(record.thinking, "high")
        self.assertEqual(record.permissions["workspace"], str(Path(directory).resolve()))
        self.assertEqual(runner.start_requests[0].model, "deepseek-v4-flash")
        self.assertEqual(runner.start_requests[0].thinking, "high")

    def test_send_normalizes_events_output_usage_and_callback(self) -> None:
        runner = FakeRunner()
        provider = DeepSeekHarnessProvider(runner=runner)
        with tempfile.TemporaryDirectory() as directory:
            record = provider.start(self.request(directory, native_session_id="native-2"))
            observed: list[dict[str, Any]] = []
            result = provider.send_or_resume(record, "hello", on_notification=observed.append)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["final_response"], "final")
        self.assertEqual(result["finish_reason"], "completed")
        self.assertGreater(len(observed), 0)
        self.assertEqual(result["usage"]["input_tokens"], 10)
        self.assertEqual(result["usage"]["cached_input_tokens"], 2)
        self.assertEqual(result["usage"]["output_tokens"], 4)
        self.assertEqual(result["usage"]["reasoning_output_tokens"], 1)
        self.assertEqual(result["usage"]["total_tokens"], 16)
        self.assertEqual(provider.usage(record)["usage"]["total_tokens"], 16)

    def test_send_or_resume_attaches_unknown_native_session_with_explicit_config(self) -> None:
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as directory:
            provider = DeepSeekHarnessProvider(runner=runner)
            result = provider.send_or_resume("native-resume", "continue", model="deepseek-v4-flash", thinking="medium", permissions={"workspace": directory})

        self.assertEqual(result["native_session_id"], "native-resume")
        self.assertEqual(runner.start_requests[0].native_session_id, "native-resume")
        self.assertEqual(runner.run_calls[0][1], [{"type": "text", "text": "continue"}])

    def test_unavailable_capabilities_are_explicit(self) -> None:
        runner = FakeRunner()
        provider = DeepSeekHarnessProvider(runner=runner)
        with tempfile.TemporaryDirectory() as directory:
            record = provider.start(self.request(directory, native_session_id="native-3"))

        self.assertEqual(provider.watch(record)["status"], "unavailable")
        self.assertEqual(provider.read(record)["status"], "unavailable")
        self.assertEqual(provider.interrupt_or_cancel(record)["status"], "unavailable")
        self.assertEqual(provider.open_native(record)["status"], "unavailable")
        self.assertEqual(runner.run_calls, [])

    def test_error_classes_are_stable_and_outputs_are_redacted(self) -> None:
        cases = [
            (TimeoutError("request timeout"), ErrorKind.TIMEOUT),
            (RuntimeError("401 authentication api_key=sk-123456789"), ErrorKind.AUTHENTICATION),
            (RuntimeError("429 rate limit"), ErrorKind.QUOTA),
            (ConnectionError("runtime process crashed"), ErrorKind.PROCESS_CRASH),
        ]
        for failure, expected_kind in cases:
            with self.subTest(expected_kind=expected_kind):
                runner = FakeRunner(failure=failure)
                provider = DeepSeekHarnessProvider(runner=runner)
                with tempfile.TemporaryDirectory() as directory:
                    record = provider.start(self.request(directory))
                    with self.assertRaises(DshProviderError) as raised:
                        provider.send_or_resume(record, "hello")
                self.assertEqual(raised.exception.kind, expected_kind.value)
                self.assertNotIn("sk-123456789", raised.exception.to_dict()["message"])

    def test_malformed_runner_output_is_invalid_output(self) -> None:
        runner = FakeRunner(malformed=True)
        provider = DeepSeekHarnessProvider(runner=runner)
        with tempfile.TemporaryDirectory() as directory:
            record = provider.start(self.request(directory))
            with self.assertRaises(DshProviderError) as raised:
                provider.send_or_resume(record, "hello")
        self.assertEqual(raised.exception.kind, ErrorKind.INVALID_OUTPUT.value)

    def test_usage_normalization_and_last_sample_replacement(self) -> None:
        usage = normalize_token_usage({"prompt_tokens": 12, "prompt_tokens_details": {"cached_tokens": 2}, "completion_tokens": 4, "completion_tokens_details": {"reasoning_tokens": 1}})
        self.assertEqual(usage.to_dict()["input_tokens"], 10)
        self.assertEqual(usage.to_dict()["total_tokens"], 16)
        events = [
            {"type": "assistant/chunk", "data": {"turn": 2, "step": 1, "chunk": {"type": "usage", "usage": {"inputTokens": 2, "outputTokens": 1}}}},
            {"type": "assistant/message", "data": {"turn": 2, "step": 1, "usage": {"inputTokens": 5, "outputTokens": 3}}},
        ]
        folded = fold_token_usage(events)
        self.assertEqual(folded.input_tokens, 5)
        self.assertEqual(folded.output_tokens, 3)
        self.assertEqual(folded.total_tokens, 8)

    def test_runtime_commands_must_be_argv_vectors(self) -> None:
        with self.assertRaises(ValueError):
            DeepSeekHarnessProvider(runtime_argv="dsh --serve")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
