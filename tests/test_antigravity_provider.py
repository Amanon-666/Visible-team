import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from providers.antigravity import (  # noqa: E402
    AntigravityProvider,
    ErrorCategory,
    ProviderError,
    RunStatus,
    parse_stream,
)


def stream_lines(*records):
    return "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n"


def sample_stream():
    return stream_lines(
        {
            "event": "init",
            "conversation_id": "conv-1",
            "init": {"cwd": "/tmp/project", "permission_mode": "request-review", "model": "gemini-3-pro"},
        },
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": "conv-1",
                "step_index": 1,
                "state": "DONE",
                "step_type": "agent_response",
                "text_delta": "hello",
                "usage": {
                    "input_tokens": 5,
                    "output_tokens": 2,
                    "thinking_tokens": 3,
                    "cache_read_tokens": 1,
                    "total_tokens": 11,
                },
            },
        },
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": "conv-1",
                "step_index": 2,
                "state": "ACTIVE",
                "step_type": "tool",
                "tool_info": {"name": "read_file", "parameters": {"path": "a.txt"}},
                "subagent_info": {
                    "subagents": [
                        {
                            "type_name": "worker",
                            "role": "reader",
                            "conversation_id": "child-1",
                            "state": "RUNNING",
                            "log_uri": "log://child-1",
                            "workspace_uris": ["workspace://child-1"],
                        }
                    ]
                },
            },
        },
        {
            "event": "result",
            "result": {
                "conversation_id": "conv-1",
                "status": "SUCCESS",
                "response": "hello",
                "duration_seconds": 1.25,
                "num_turns": 2,
                "usage": {
                    "input_tokens": 8,
                    "output_tokens": 4,
                    "thinking_tokens": 6,
                    "cache_read_tokens": 2,
                    "total_tokens": 20,
                },
            },
        },
    )


class FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[-1] == "--version":
            return FakeCompleted("agy 1.1.21\n")
        if argv[-1] == "--help":
            # The installed Go CLI writes successful flag help to stderr.
            return FakeCompleted(
                "",
                "--input-format stream-json\n--output-format stream-json\n--effort low|medium|high\n",
            )
        return FakeCompleted("", "unexpected probe", 2)


class FakeProcess:
    def __init__(self, output, *, returncode=0, stderr="", timeout=False):
        self.output = output
        self.returncode = returncode
        self.stderr = stderr
        self.timeout = timeout
        self.argv = None
        self.kwargs = None
        self.input = None
        self.terminated = False

    def communicate(self, input=None, timeout=None):
        self.input = input
        if self.timeout:
            raise subprocess.TimeoutExpired(self.argv or ["agy"], timeout)
        return self.output, self.stderr

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.terminated = True


class AntigravityProviderTests(unittest.TestCase):
    def test_stream_parser_normalizes_terminal_usage_and_progress(self):
        parsed = parse_stream(sample_stream())
        self.assertEqual(parsed.conversation_id, "conv-1")
        self.assertEqual(parsed.result.status, RunStatus.SUCCESS)
        self.assertEqual(parsed.result.response, "hello")
        self.assertEqual(parsed.usage.input_tokens, 8)
        self.assertEqual(parsed.usage.total_tokens, 20)
        self.assertEqual(parsed.usage_source, "result")
        tool_event = next(event for event in parsed.events if event.tool)
        self.assertEqual(tool_event.tool["name"], "read_file")
        self.assertEqual(tool_event.subagents[0]["conversation_id"], "child-1")
        self.assertEqual(tool_event.subagents[0]["workspace_uris"], ["workspace://child-1"])

    def test_parser_marks_bad_json_and_retains_unknown_events(self):
        parsed = parse_stream("not-json\n{" + '"event":"future_event","value":1}' + "\n")
        self.assertTrue(parsed.invalid_output)
        self.assertEqual(parsed.events[0].error.category, ErrorCategory.INVALID_OUTPUT)
        self.assertEqual(parsed.events[1].event, "unknown")
        self.assertEqual(parsed.events[1].status, RunStatus.RUNNING)

    def test_discover_is_offline_and_declares_unavailable_capabilities(self):
        runner = FakeRunner()
        provider = AntigravityProvider(binary=sys.executable, command_runner=runner)
        discovery = provider.discover()
        self.assertEqual(discovery.provider, "antigravity")
        self.assertEqual(discovery.runtime, "agy")
        self.assertEqual(discovery.version, "1.1.21")
        self.assertTrue(discovery.supports("start"))
        self.assertTrue(discovery.supports("resume"))
        self.assertTrue(discovery.supports("usage"))
        for name in ("watch", "read", "interrupt_or_cancel", "open_native", "account_usage"):
            self.assertFalse(discovery.supports(name))
        self.assertTrue(all(isinstance(call[0], list) for call in runner.calls))
        self.assertNotIn("models", " ".join(argument for call, _ in runner.calls for argument in call))
        self.assertTrue(all(call_kwargs.get("shell") is False for _, call_kwargs in runner.calls))

    def test_discover_reports_missing_binary(self):
        provider = AntigravityProvider(binary="/definitely/missing/agy")
        discovery = provider.discover()
        self.assertFalse(discovery.available)
        self.assertEqual(discovery.errors[0].category, ErrorCategory.NOT_INSTALLED)
        self.assertFalse(discovery.supports("start"))

    def test_argv_requires_explicit_model_thinking_and_safe_permissions(self):
        provider = AntigravityProvider(binary=sys.executable)
        argv = provider.build_argv(
            model="gemini-3-pro",
            thinking="high",
            project="/tmp/project",
            conversation_id="conv-1",
        )
        self.assertEqual(argv[0], sys.executable)
        self.assertIn("--input-format", argv)
        self.assertIn("--output-format", argv)
        self.assertIn("--effort", argv)
        self.assertIn("high", argv)
        self.assertIn("--conversation", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        with self.assertRaises(ValueError):
            provider.build_argv(model=None, thinking="high")
        with self.assertRaises(ValueError):
            provider.build_argv(model="gemini-3-pro", thinking=None)
        with self.assertRaises(ValueError):
            provider.build_argv(model="gemini-3-pro", effort="low", thinking="high")
        with self.assertRaises(ValueError):
            provider.build_argv(model="gemini-3-pro", thinking="high", conversation_id="c", continue_latest=True)
        dangerous = provider.build_argv(model="gemini-3-pro", thinking="high", dangerously_skip_permissions=True)
        self.assertIn("--dangerously-skip-permissions", dangerous)

    def test_start_uses_injected_process_and_records_receipt(self):
        processes = []

        def factory(argv, **kwargs):
            process = FakeProcess(sample_stream())
            process.argv = argv
            process.kwargs = kwargs
            processes.append(process)
            return process

        provider = AntigravityProvider(binary=sys.executable, process_factory=factory)
        with tempfile.TemporaryDirectory() as workdir:
            result = provider.start(
                "请读取文件",
                cwd=workdir,
                project=workdir,
                model="gemini-3-pro",
                thinking="high",
                permission_mode="safe",
            )
        process = processes[0]
        self.assertTrue(result.ok)
        self.assertEqual(result.provider, "antigravity")
        self.assertEqual(result.runtime, "agy")
        self.assertEqual(result.native_session_id, "conv-1")
        self.assertIsNone(result.native_task_id)
        self.assertEqual(result.receipt.native_session_id, "conv-1")
        self.assertIsNone(result.receipt.native_open_ref)
        self.assertEqual(result.receipt.model, "gemini-3-pro")
        self.assertEqual(result.receipt.thinking, "high")
        self.assertEqual(result.receipt.permission_boundary, "safe")
        self.assertEqual(result.receipt.token_usage.total_tokens, 20)
        self.assertEqual(json.loads(process.input)["message"]["content"], "请读取文件")
        self.assertIsInstance(process.argv, list)
        self.assertFalse(process.kwargs["shell"])
        self.assertIsNone(result.as_dict()["receipt"]["native_task_id"])

    def test_resume_uses_native_conversation_id_without_a_fake_task_id(self):
        process = FakeProcess(sample_stream())

        def factory(argv, **kwargs):
            process.argv = argv
            process.kwargs = kwargs
            return process

        provider = AntigravityProvider(binary=sys.executable, process_factory=factory)
        with tempfile.TemporaryDirectory() as workdir:
            result = provider.resume(
                "继续",
                "conv-1",
                cwd=workdir,
                model="gemini-3-pro",
                effort="medium",
            )
        self.assertEqual(result.receipt.operation, "resume")
        self.assertIn("--conversation", process.argv)
        self.assertIn("conv-1", process.argv)

    def test_native_error_is_classified(self):
        output = stream_lines(
            {"event": "init", "conversation_id": "conv-auth", "init": {}},
            {"event": "result", "result": {"status": "ERROR", "error": "not logged in"}},
        )
        process = FakeProcess(output)

        def factory(argv, **kwargs):
            return process

        provider = AntigravityProvider(binary=sys.executable, process_factory=factory)
        with tempfile.TemporaryDirectory() as workdir:
            result = provider.start("hello", cwd=workdir, model="gemini-3-pro", thinking="low")
        self.assertEqual(result.status, RunStatus.ERROR)
        self.assertEqual(result.error.category, ErrorCategory.AUTHENTICATION)

    def test_missing_result_and_timeout_are_explicit_errors(self):
        empty_process = FakeProcess("")
        provider = AntigravityProvider(binary=sys.executable, process_factory=lambda argv, **kwargs: empty_process)
        with tempfile.TemporaryDirectory() as workdir:
            with self.assertRaises(ProviderError) as raised:
                provider.start("hello", cwd=workdir, model="gemini-3-pro", thinking="low")
        self.assertEqual(raised.exception.category, ErrorCategory.INVALID_OUTPUT)

        timed_process = FakeProcess("", timeout=True)
        timed_provider = AntigravityProvider(binary=sys.executable, process_factory=lambda argv, **kwargs: timed_process)
        with tempfile.TemporaryDirectory() as workdir:
            with self.assertRaises(ProviderError) as raised:
                timed_provider.start("hello", cwd=workdir, model="gemini-3-pro", thinking="low", timeout_s=0.01)
        self.assertEqual(raised.exception.category, ErrorCategory.TIMEOUT)
        self.assertTrue(timed_process.terminated)

    def test_usage_and_runtime_operations_report_real_boundary(self):
        provider = AntigravityProvider(binary=sys.executable)
        unavailable_usage = provider.usage()
        self.assertEqual(unavailable_usage.status, "unavailable")
        self.assertFalse(unavailable_usage.available)
        for operation in ("watch", "read", "interrupt_or_cancel", "cancel", "interrupt", "open_native"):
            result = getattr(provider, operation)()
            self.assertEqual(result.status, "unavailable")
            self.assertEqual(result.operation, "interrupt_or_cancel" if operation in {"cancel", "interrupt"} else operation)


if __name__ == "__main__":
    unittest.main()
