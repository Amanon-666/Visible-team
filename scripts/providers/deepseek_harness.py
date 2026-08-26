#!/usr/bin/env python3
"""Thin adapter over the official local DeepSeek Harness Python SDK.

The SDK owns runtime/protocol lifecycle; this module only validates explicit
route facts, records native session evidence, and normalizes events/usage.
It has no history-read, wire-cancel, or session-specific native-UI RPC, so
those capabilities remain explicitly unavailable.  ``runner`` is injectable
for offline tests and discovery never imports or starts a runtime.

Read-only sources: ``/Users/Admin/Work/deepseek-harness/python/sdk/src/deepseek_harness/api.py``,
``/Users/Admin/Work/deepseek-harness/packages/sdk/protocol/src/types.ts``, and
``/Users/Admin/Work/deepseek-harness/packages/llm/token-meter/src/usage-projection.ts``.
"""

from __future__ import annotations

import importlib.util
import json
import re
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

PROVIDER_ID = "deepseek-harness"
RUNTIME_ID = "deepseek-harness-sdk-runtime"
NATIVE_PROVIDER_DEFAULT = "deepseek-official"
UNAVAILABLE = "unavailable"
CAPABILITY_NAMES = ("discover", "start", "send-or-resume", "watch", "read", "interrupt-or-cancel", "usage", "open-native")
_SECRET_KEY = re.compile(r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|secret|credential|private[_-]?key|cookie)", re.I)


class ErrorKind(str, Enum):
    AUTHENTICATION = "authentication"
    QUOTA = "quota"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INVALID_OUTPUT = "invalid_output"
    PROCESS_CRASH = "process_crash"
    PROTOCOL = "protocol"
    AUTHORIZATION = "authorization"
    UNAVAILABLE = "unavailable"
    INVALID_REQUEST = "invalid_request"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"

def _redact_text(value: str) -> str:
    value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1<redacted>", value)
    value = re.sub(r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|secret|credential|private[_-]?key)\s*[:=]\s*)([^\s,;]+)", r"\1<redacted>", value)
    return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "sk-<redacted>", value)


def _safe_public(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _SECRET_KEY.search(key):
        return "<redacted>"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else UNAVAILABLE
    if isinstance(value, Mapping):
        return {str(k): _safe_public(v, key=str(k)) for k, v in value.items() if isinstance(k, (str, int, float, bool))}
    if isinstance(value, (list, tuple)):
        return [_safe_public(v) for v in value]
    return _redact_text(repr(value))

def _safe_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    result = _safe_public(value or {})
    return result if isinstance(result, dict) else {}

def _json_value(value: Any, path: str = "value") -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{path} must contain finite JSON numbers")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} mapping keys must be strings")
            result[key] = _json_value(child, f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_json_value(child, f"{path}[{index}]") for index, child in enumerate(value)]
    raise ValueError(f"{path} must be JSON-serializable")

def _argv(value: Sequence[str] | None, name: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)):
        raise ValueError(f"{name} must be an argv sequence, not a shell string")
    result = tuple(value)
    if not result or any(not isinstance(item, str) or not item for item in result):
        raise ValueError(f"{name} must contain non-empty strings")
    return result

def _path(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty path string")
    return str(Path(value).expanduser().resolve())

def _session_id(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or "/" in value or "\\" in value or "\x00" in value:
        raise ValueError("native_session_id must be a path-safe non-empty string")
    return value

class DshProviderError(RuntimeError):
    """Safe classified error; ``to_dict`` is suitable for persisted state."""

    def __init__(self, kind: ErrorKind | str, message: str, *, operation: str | None = None, native_code: str | int | None = None, details: Mapping[str, Any] | None = None) -> None:
        self.kind = kind.value if isinstance(kind, ErrorKind) else str(kind)
        self.message = _redact_text(str(message))
        self.operation = operation
        self.native_code = native_code
        self.details = _safe_mapping(details)
        super().__init__(self.message)
    @property
    def retryable(self) -> bool:
        return self.kind in {ErrorKind.QUOTA.value, ErrorKind.TIMEOUT.value, ErrorKind.PROCESS_CRASH.value}

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"kind": self.kind, "message": self.message, "retryable": self.retryable}
        if self.operation is not None:
            result["operation"] = self.operation
        if self.native_code is not None:
            result["native_code"] = self.native_code
        if self.details:
            result["details"] = self.details
        return result

class _RunnerUnavailable(Exception):
    pass


class _InvalidOutput(Exception):
    pass

class Runner(Protocol):
    """Injectable runner contract; production implementation is official SDK."""

    def start(self, request: "StartRequest") -> Mapping[str, Any] | None: ...
    def run(self, native_session_id: str, content: list[dict[str, Any]], *, on_notification: Callable[[Any], None] | None = None, timeout_seconds: float | None = None) -> Any: ...
    def close(self) -> Any: ...


@dataclass(frozen=True)
class StartRequest:
    model: str | None = None
    thinking: str | None = None
    permissions: Mapping[str, Any] | None = None
    native_provider: str = NATIVE_PROVIDER_DEFAULT
    cwd: str | None = None
    session_root: str | None = None
    max_tokens: int | None = None
    native_session_id: str | None = None
    native_open_ref: str | None = None

    def normalized(self) -> "StartRequest":
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model is required and must be a non-empty string")
        if not isinstance(self.thinking, str) or not self.thinking.strip():
            raise ValueError("thinking is required and must be a non-empty string")
        if not isinstance(self.permissions, Mapping):
            raise ValueError("permissions must be explicitly supplied as a mapping")
        permissions = _json_value(self.permissions, "permissions")
        assert isinstance(permissions, dict)
        cwd = _path(self.cwd, "cwd")
        workspace = permissions.get("workspace")
        if workspace is not None and not isinstance(workspace, str):
            raise ValueError("permissions.workspace must be a path string")
        workspace_cwd = _path(workspace, "permissions.workspace") if workspace else None
        if cwd and workspace_cwd and cwd != workspace_cwd:
            raise ValueError("cwd and permissions.workspace must identify the same boundary")
        if workspace_cwd is not None:
            permissions["workspace"] = workspace_cwd
        if not isinstance(self.native_provider, str) or not self.native_provider.strip():
            raise ValueError("native_provider must be a non-empty string")
        if self.max_tokens is not None and (isinstance(self.max_tokens, bool) or not isinstance(self.max_tokens, int) or self.max_tokens <= 0):
            raise ValueError("max_tokens must be a positive integer")
        if self.native_open_ref is not None and not isinstance(self.native_open_ref, str):
            raise ValueError("native_open_ref must be a string or None")
        return StartRequest(
            model=self.model,
            thinking=self.thinking,
            permissions=permissions,
            native_provider=self.native_provider,
            cwd=cwd or workspace_cwd or str(Path.cwd().resolve()),
            session_root=_path(self.session_root, "session_root"),
            max_tokens=self.max_tokens,
            native_session_id=_session_id(self.native_session_id) or f"session-{uuid.uuid4().hex}",
            native_open_ref=self.native_open_ref,
        )

    def route_key(self) -> tuple[Any, ...]:
        request = self.normalized()
        return (request.native_provider, request.model, request.cwd, request.session_root, request.max_tokens, json.dumps(_safe_mapping(request.permissions), sort_keys=True, separators=(",", ":")))


@dataclass
class TokenUsageRecord:
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None
    source: str = "dsh.session-event"

    @property
    def available(self) -> bool:
        return any(getattr(self, name) is not None for name in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens"))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"status": "available" if self.available else UNAVAILABLE, "source": self.source}
        for name in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens"):
            value = getattr(self, name)
            result[name] = value if value is not None else UNAVAILABLE
        return result


def _nonnegative(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    raise ValueError(f"{name} must be a non-negative integer")


def normalize_token_usage(value: Any) -> TokenUsageRecord:
    """Normalize DSH TokenUsage and DeepSeek-compatible usage fields."""

    if isinstance(value, TokenUsageRecord):
        return value
    if value is None:
        return TokenUsageRecord()
    if not isinstance(value, Mapping):
        names = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "reasoningTokens", "totalTokens")
        value = {name: getattr(value, name) for name in names if hasattr(value, name)}
    if not isinstance(value, Mapping):
        raise ValueError("usage must be an object")

    def first(*names: str) -> Any:
        return next((value[name] for name in names if name in value), None)

    cached = first("cacheReadTokens", "cached_input_tokens", "cachedTokens", "prompt_cache_hit_tokens")
    details = value.get("prompt_tokens_details")
    if cached is None and isinstance(details, Mapping):
        cached = details.get("cached_tokens")
    reasoning = first("reasoningTokens", "reasoning_output_tokens", "reasoning_tokens")
    completion_details = value.get("completion_tokens_details")
    if reasoning is None and isinstance(completion_details, Mapping):
        reasoning = completion_details.get("reasoning_tokens")
    input_tokens = first("inputTokens", "input_tokens")
    if input_tokens is None and "prompt_tokens" in value:
        input_tokens = _nonnegative(value["prompt_tokens"], "prompt_tokens")
        input_tokens = input_tokens - (_nonnegative(cached, "cached_input_tokens") or 0) if input_tokens is not None else None
        if input_tokens is not None and input_tokens < 0:
            raise ValueError("prompt_tokens cannot be smaller than cached input tokens")
    record = TokenUsageRecord(
        _nonnegative(input_tokens, "input_tokens"),
        _nonnegative(cached, "cached_input_tokens"),
        _nonnegative(first("cacheWriteTokens", "cache_write_input_tokens", "prompt_cache_write_tokens"), "cache_write_input_tokens"),
        _nonnegative(first("outputTokens", "output_tokens", "completion_tokens"), "output_tokens"),
        _nonnegative(reasoning, "reasoning_output_tokens"),
        _nonnegative(first("totalTokens", "total_tokens"), "total_tokens"),
    )
    if record.total_tokens is None and record.available:
        record.total_tokens = sum(getattr(record, name) or 0 for name in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens"))
    return record


def _usage_sample(event: Mapping[str, Any]) -> tuple[tuple[Any, ...], TokenUsageRecord] | None:
    data = event.get("data")
    if not isinstance(data, Mapping):
        return None
    raw = data.get("usage")
    if raw is None and isinstance(data.get("chunk"), Mapping) and data["chunk"].get("type") == "usage":
        raw = data["chunk"].get("usage", data["chunk"])
    if raw is None:
        return None
    turn, step = data.get("turn"), data.get("step")
    key = (turn, step) if isinstance(turn, int) and isinstance(step, int) else ("event", id(event))
    try:
        return key, normalize_token_usage(raw)
    except ValueError as error:
        raise _InvalidOutput(f"invalid usage in session event: {error}") from error


def fold_token_usage(events: Sequence[Mapping[str, Any]]) -> TokenUsageRecord:
    """Fold event samples; a repeated turn/step replaces its prior sample."""

    samples: dict[tuple[Any, ...], TokenUsageRecord] = {}
    for event in events:
        sample = _usage_sample(event)
        if sample is not None:
            samples[sample[0]] = sample[1]
    if not samples:
        return TokenUsageRecord()
    names = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
    totals = {name: 0 for name in names}
    present: set[str] = set()
    for sample in samples.values():
        for name in names:
            value = getattr(sample, name)
            if value is not None:
                totals[name] += value
                present.add(name)
    if "total_tokens" not in present:
        totals["total_tokens"] = sum(totals[name] for name in names[:4])
        present.add("total_tokens")
    return TokenUsageRecord(**{name: totals[name] if name in present else None for name in names})


def normalize_event(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _InvalidOutput("session event must be an object")
    event = _json_value(value, "event")
    assert isinstance(event, dict)
    if not isinstance(event.get("type"), str):
        raise _InvalidOutput("session event requires a string type")
    if event["type"] in {"assistant/message", "assistant/chunk"} and event.get("data") is not None and not isinstance(event["data"], Mapping):
        raise _InvalidOutput(f"{event['type']} data must be an object")
    return _safe_public(event)


def normalize_notification(value: Any) -> dict[str, Any]:
    method = value.get("method") if isinstance(value, Mapping) else getattr(value, "method", None)
    payload = (value.get("payload", value.get("params", {})) if isinstance(value, Mapping) else getattr(value, "payload", getattr(value, "params", {})))
    if not isinstance(method, str):
        raise _InvalidOutput("notification requires a string method")
    if payload is None:
        payload = {}
    if not isinstance(payload, Mapping):
        raise _InvalidOutput("notification payload must be an object")
    payload = _json_value(payload, "notification.payload")
    assert isinstance(payload, dict)
    if method == "session.event" and "event" in payload:
        payload["event"] = normalize_event(payload["event"])
    return {"method": method, "payload": _safe_public(payload)}


@dataclass
class RunResult:
    session_id: str
    final_response: str
    finish_reason: str | None
    events: list[dict[str, Any]]
    notifications: list[dict[str, Any]]
    usage: TokenUsageRecord


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def normalize_run_result(value: Any, *, expected_session_id: str) -> RunResult:
    if _field(value, "session_id", expected_session_id) != expected_session_id:
        raise _InvalidOutput("runner returned a different native session id")
    raw_events = _field(value, "events", []) or []
    raw_notifications = _field(value, "notifications", []) or []
    if not isinstance(raw_events, (list, tuple)) or not isinstance(raw_notifications, (list, tuple)):
        raise _InvalidOutput("runner events and notifications must be lists")
    events = [normalize_event(item) for item in raw_events]
    notifications = [normalize_notification(item) for item in raw_notifications]
    response = _field(value, "final_response")
    if not isinstance(response, str):
        raise _InvalidOutput("runner final_response must be a string")
    reason = _field(value, "finish_reason")
    if reason is not None and not isinstance(reason, str):
        raise _InvalidOutput("runner finish_reason must be a string or null")
    try:
        usage = normalize_token_usage(_field(value, "usage", None)) if _field(value, "usage", None) is not None else fold_token_usage(events)
    except ValueError as error:
        raise _InvalidOutput(f"invalid runner usage: {error}") from error
    return RunResult(expected_session_id, _redact_text(response), reason, events, notifications, usage)


@dataclass
class SessionRecord:
    provider: str
    runtime: str
    native_provider: str
    native_session_id: str
    model: str
    thinking: str
    permissions: dict[str, Any]
    cwd: str
    session_root: str | None
    native_open_ref: str | None
    created_at: str
    state: str = "created"
    native_session_materialized: bool = False
    usage: TokenUsageRecord = field(default_factory=TokenUsageRecord)
    last_error: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    notifications: list[dict[str, Any]] = field(default_factory=list)
    _runner: Runner | None = field(default=None, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

class OfficialSdkRunner:
    """Very small wrapper; the installed SDK owns runtime and subscriptions."""

    def __init__(self, *, runtime_argv: tuple[str, ...] | None, request_timeout_seconds: float | None, shutdown_timeout_seconds: float | None) -> None:
        self.options = {"launch_args_override": runtime_argv, "request_timeout_seconds": request_timeout_seconds, "shutdown_timeout_seconds": shutdown_timeout_seconds}
        self._harness: Any = None

    def start(self, request: StartRequest) -> Mapping[str, Any]:
        try:
            from deepseek_harness import DeepSeekHarness  # type: ignore
        except ImportError as error:
            raise _RunnerUnavailable("official deepseek_harness Python SDK is not installed") from error
        options = {key: value for key, value in {**self.options, "provider": request.native_provider, "model": request.model, "cwd": request.cwd, "session_root": request.session_root, "max_tokens": request.max_tokens}.items() if value is not None}
        self._harness = DeepSeekHarness(**options)
        self._harness.start()
        return {"name": RUNTIME_ID, "version": UNAVAILABLE, "transport": "official-python-sdk"}

    def run(self, native_session_id: str, content: list[dict[str, Any]], *, on_notification: Callable[[Any], None] | None = None, timeout_seconds: float | None = None) -> Any:
        if self._harness is None:
            raise _RunnerUnavailable("official SDK runner is not started")
        configured = self.options.get("request_timeout_seconds")
        if timeout_seconds is not None and configured != timeout_seconds:
            raise _RunnerUnavailable("per-send timeout is unavailable; configure request_timeout_seconds on the provider")
        callback = None if on_notification is None else lambda item: on_notification(normalize_notification(item))
        return self._harness.run(content, session_id=native_session_id, on_notification=callback)

    def close(self) -> None:
        if self._harness is not None:
            self._harness.close()
            self._harness = None


def _classify(error: BaseException, operation: str) -> DshProviderError:
    if isinstance(error, DshProviderError):
        return error
    text = _redact_text(str(error) or error.__class__.__name__)
    lower = text.lower()
    if isinstance(error, _RunnerUnavailable) or isinstance(error, ModuleNotFoundError) or isinstance(error, FileNotFoundError):
        kind = ErrorKind.UNAVAILABLE
    elif isinstance(error, TimeoutError) or "timeout" in lower or "deadline" in lower:
        kind = ErrorKind.TIMEOUT
    elif any(word in lower for word in ("cancel", "abort", "interrupt")):
        kind = ErrorKind.CANCELLED
    elif any(word in lower for word in ("api key", "authentication", "unauthorized", "401")):
        kind = ErrorKind.AUTHENTICATION
    elif any(word in lower for word in ("forbidden", "permission denied", "403")):
        kind = ErrorKind.AUTHORIZATION
    elif any(word in lower for word in ("quota", "rate limit", "too many requests", "429")):
        kind = ErrorKind.QUOTA
    elif isinstance(error, _InvalidOutput):
        kind = ErrorKind.INVALID_OUTPUT
    elif isinstance(error, (ValueError, TypeError)):
        kind = ErrorKind.INVALID_REQUEST
    elif isinstance(error, (ConnectionError, BrokenPipeError, OSError)) or "process" in lower or "crash" in lower:
        kind = ErrorKind.PROCESS_CRASH
    elif "protocol" in lower or "json-rpc" in lower:
        kind = ErrorKind.PROTOCOL
    else:
        kind = ErrorKind.UNKNOWN
    return DshProviderError(kind, text, operation=operation, native_code=getattr(error, "code", None))


def _content_blocks(content: str | Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, (str, bytes, bytearray)) or not isinstance(content, Sequence):
        raise ValueError("content must be text or a sequence of content blocks")
    result: list[dict[str, Any]] = []
    for index, block in enumerate(content):
        if not isinstance(block, Mapping):
            raise ValueError(f"content[{index}] must be an object")
        block_json = _json_value(block, f"content[{index}]")
        assert isinstance(block_json, dict)
        if not isinstance(block_json.get("type"), str):
            raise ValueError(f"content[{index}].type must be a string")
        result.append(block_json)
    return result


def _capability(available: bool, scope: str, reason: str | None = None) -> dict[str, Any]:
    result = {"available": available, "status": "available" if available else UNAVAILABLE, "scope": scope}
    if reason is not None:
        result["reason"] = reason
    return result


def _unavailable(capability: str, reason: str) -> dict[str, Any]:
    return {"available": False, "status": UNAVAILABLE, "capability": capability, "provider": PROVIDER_ID, "runtime": RUNTIME_ID, "reason": reason}


class DeepSeekHarnessProvider:
    """One route-configured DSH runtime and its in-memory session evidence."""

    def __init__(self, *, runtime_argv: Sequence[str] | None = None, request_timeout_seconds: float | None = None, shutdown_timeout_seconds: float | None = 1.0, runner: Runner | None = None) -> None:
        self.runtime_argv = _argv(runtime_argv, "runtime_argv")
        if request_timeout_seconds is not None and request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if shutdown_timeout_seconds is not None and shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
        self.request_timeout_seconds, self.shutdown_timeout_seconds = request_timeout_seconds, shutdown_timeout_seconds
        self._runner = runner
        self._records: dict[str, SessionRecord] = {}
        self._route_key: tuple[Any, ...] | None = None
        self._started = False
        self._lock = threading.RLock()

    def discover(self) -> dict[str, Any]:
        try:
            sdk = importlib.util.find_spec("deepseek_harness") is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            sdk = False
        injected = self._runner is not None
        available = sdk or injected
        return {"provider": PROVIDER_ID, "runtime": RUNTIME_ID, "available": available, "status": "available" if available else UNAVAILABLE, "sdk": {"package": "deepseek_harness", "installed": sdk}, "runner": "injected" if injected else "official-python-sdk" if sdk else UNAVAILABLE, "runtime_argv": list(self.runtime_argv) if self.runtime_argv else None, "capabilities": list(CAPABILITY_NAMES), "reason": None if available else "official deepseek_harness SDK is not installed and no runner was injected"}

    def capabilities(self) -> dict[str, Any]:
        found = self.discover()
        available = bool(found["available"])
        return {
            "provider": PROVIDER_ID,
            "runtime": RUNTIME_ID,
            "discover": _capability(True, "offline-local-sdk-detection"),
            "start": _capability(available, "sdk-runtime", found.get("reason")),
            "send-or-resume": _capability(available, "native-session-id", found.get("reason")),
            "watch": _capability(available, "during-send", "use send_or_resume(on_notification=...); standalone subscriptions are not exposed" if available else found.get("reason")),
            "read": _unavailable("read", "DSH SDK has no session history/read RPC"),
            "interrupt-or-cancel": _unavailable("interrupt-or-cancel", "DSH SDK has no wire-level prompt cancellation"),
            "usage": _capability(True, "observed-session-events", "unavailable for a session until DSH emits a usage sample"),
            "open-native": _unavailable("open-native", "DSH SDK has no session-specific native UI open RPC"),
            "configuration": {"model": {"required": True, "forwarded": True}, "thinking": {"required": True, "forwarded": False, "status": UNAVAILABLE, "reason": "initialize has no thinking field"}, "permissions": {"required": True, "forwarded": False, "cwd_forwarded": True, "status": UNAVAILABLE, "reason": "initialize only carries cwd"}},
        }

    def start(self, request: StartRequest | None = None) -> SessionRecord:
        try:
            if not isinstance(request, StartRequest):
                raise ValueError("start requires a StartRequest")
            normalized = request.normalized()
        except BaseException as error:
            raise _classify(error, "start") from error
        if not self.discover()["available"]:
            raise DshProviderError(ErrorKind.UNAVAILABLE, "DeepSeek Harness SDK is unavailable on this machine", operation="start")
        with self._lock:
            if normalized.native_session_id in self._records:
                raise DshProviderError(ErrorKind.CONFLICT, "native session id is already registered", operation="start")
            route = normalized.route_key()
            if self._route_key is not None and self._route_key != route:
                raise DshProviderError(ErrorKind.CONFLICT, "one DSH runtime cannot silently change model or permission route", operation="start")
            runner = self._runner or OfficialSdkRunner(runtime_argv=self.runtime_argv, request_timeout_seconds=self.request_timeout_seconds, shutdown_timeout_seconds=self.shutdown_timeout_seconds)
            self._runner = runner
            try:
                if not self._started:
                    info = runner.start(normalized)
                    if info is not None and not isinstance(info, Mapping):
                        raise _InvalidOutput("runner start info must be an object")
                    self._started, self._route_key = True, route
            except BaseException as error:
                raise _classify(error, "start") from error
            record = SessionRecord(provider=PROVIDER_ID, runtime=RUNTIME_ID, native_provider=normalized.native_provider, native_session_id=normalized.native_session_id or "", model=normalized.model or "", thinking=normalized.thinking or "", permissions=_safe_mapping(normalized.permissions), cwd=normalized.cwd or str(Path.cwd().resolve()), session_root=normalized.session_root, native_open_ref=normalized.native_open_ref, created_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"), _runner=runner)
            self._records[record.native_session_id] = record
            return record

    def send_or_resume(self, session: SessionRecord | str, content: str | Sequence[Mapping[str, Any]], *, on_notification: Callable[[dict[str, Any]], None] | None = None, timeout_seconds: float | None = None, model: str | None = None, thinking: str | None = None, permissions: Mapping[str, Any] | None = None) -> dict[str, Any]:
        record: SessionRecord | None = None
        try:
            blocks = _content_blocks(content)
            record = self._resolve_or_resume(session, model=model, thinking=thinking, permissions=permissions)
            self._assert_metadata(record, model, thinking, permissions)
            with record._lock:
                if record.state == "running":
                    raise DshProviderError(ErrorKind.CONFLICT, "session already has a running send", operation="send-or-resume")
                if record._runner is None:
                    raise _RunnerUnavailable("session has no runner")
                record.state, record.last_error = "running", None

                def observe(item: Any) -> None:
                    normalized = self._record_notification(record, item)
                    if on_notification is not None:
                        on_notification(normalized)

                raw = record._runner.run(record.native_session_id, blocks, on_notification=observe, timeout_seconds=timeout_seconds)
                result = normalize_run_result(raw, expected_session_id=record.native_session_id)
                for notification in result.notifications:
                    self._record_notification(record, notification)
                for event in result.events:
                    if event not in record.events:
                        record.events.append(event)
                observed = fold_token_usage(record.events)
                record.usage = observed if observed.available else result.usage
                record.native_session_materialized = True
                reason = result.finish_reason or "completed"
                record.state = "cancelled" if reason in {"cancelled", "aborted"} else "error" if reason in {"error", "failed"} else "completed"
                payload = self._result_payload(record, result)
                return payload
        except BaseException as error:
            classified = _classify(error, "send-or-resume")
            if record is not None:
                with record._lock:
                    record.state = "cancelled" if classified.kind == ErrorKind.CANCELLED.value else "error"
                    record.last_error = classified.to_dict()
            if isinstance(error, DshProviderError) and error is classified:
                raise
            raise classified from error

    def watch(self, session: SessionRecord | str) -> dict[str, Any]:
        del session
        return _unavailable("watch", "standalone subscriptions are not exposed; use send_or_resume(on_notification=...) during a turn")

    def read(self, session: SessionRecord | str) -> dict[str, Any]:
        del session
        return _unavailable("read", "DSH SDK has no session history/read RPC")

    def interrupt_or_cancel(self, session: SessionRecord | str) -> dict[str, Any]:
        del session
        return _unavailable("interrupt-or-cancel", "DSH SDK has no wire-level prompt cancellation")

    def usage(self, session: SessionRecord | str) -> dict[str, Any]:
        record = self._known(session)
        if record is None:
            return _unavailable("usage", "no local observation exists and DSH has no usage read RPC")
        return {"status": "available" if record.usage.available else UNAVAILABLE, "provider": PROVIDER_ID, "runtime": RUNTIME_ID, "native_session_id": record.native_session_id, "usage": record.usage.to_dict()}

    def open_native(self, session: SessionRecord | str) -> dict[str, Any]:
        del session
        return _unavailable("open-native", "DSH SDK has no session-specific native UI open RPC")

    def close(self) -> None:
        with self._lock:
            if self._runner is not None and self._started:
                self._runner.close()
            self._started = False
            for record in self._records.values():
                if record.state == "running":
                    record.state = "cancelled"
                elif record.state not in {"error", "cancelled"}:
                    record.state = "closed"

    def _known(self, session: SessionRecord | str) -> SessionRecord | None:
        if isinstance(session, SessionRecord):
            if session.provider != PROVIDER_ID:
                raise ValueError("session belongs to another provider")
            return self._records.get(session.native_session_id)
        if isinstance(session, str):
            return self._records.get(session)
        raise ValueError("session must be a SessionRecord or native session id")

    def _resolve_or_resume(self, session: SessionRecord | str, *, model: str | None, thinking: str | None, permissions: Mapping[str, Any] | None) -> SessionRecord:
        known = self._known(session)
        if known is not None:
            return known
        native_id = session if isinstance(session, str) else None
        if not isinstance(native_id, str):
            raise ValueError("resume requires a native session id")
        return self.start(StartRequest(
            model=model,
            thinking=thinking,
            permissions=permissions,
            native_provider=NATIVE_PROVIDER_DEFAULT,
            native_session_id=native_id,
        ))

    @staticmethod
    def _assert_metadata(record: SessionRecord, model: str | None, thinking: str | None, permissions: Mapping[str, Any] | None) -> None:
        if model is not None and model != record.model:
            raise DshProviderError(ErrorKind.CONFLICT, "resume model differs from registered route", operation="send-or-resume")
        if thinking is not None and thinking != record.thinking:
            raise DshProviderError(ErrorKind.CONFLICT, "resume thinking strength differs from registered route", operation="send-or-resume")
        candidate = _json_value(permissions, "permissions") if permissions is not None else None
        if isinstance(candidate, dict) and isinstance(candidate.get("workspace"), str):
            candidate["workspace"] = _path(candidate["workspace"], "permissions.workspace")
        if permissions is not None and candidate != record.permissions:
            raise DshProviderError(ErrorKind.CONFLICT, "resume permission boundary differs from registered route", operation="send-or-resume")

    @staticmethod
    def _record_notification(record: SessionRecord, item: Any) -> dict[str, Any]:
        normalized = normalize_notification(item)
        if normalized not in record.notifications:
            record.notifications.append(normalized)
        payload = normalized.get("payload")
        if normalized.get("method") == "session.event" and isinstance(payload, Mapping) and payload.get("sessionId") == record.native_session_id and isinstance(payload.get("event"), Mapping):
            event = dict(payload["event"])
            if event not in record.events:
                record.events.append(event)
        return normalized

    @staticmethod
    def _result_payload(record: SessionRecord, result: RunResult) -> dict[str, Any]:
        return _safe_public({"status": record.state, "provider": PROVIDER_ID, "runtime": RUNTIME_ID, "native_provider": record.native_provider, "native_session_id": record.native_session_id, "native_task_id": None, "native_open_ref": record.native_open_ref, "model": record.model, "thinking": record.thinking, "permissions": record.permissions, "permission_boundary": record.permissions, "usage": record.usage.to_dict(), "final_response": result.final_response, "finish_reason": result.finish_reason, "events": record.events, "notifications": record.notifications})


__all__ = ["CAPABILITY_NAMES", "DeepSeekHarnessProvider", "DshProviderError", "ErrorKind", "OfficialSdkRunner", "PROVIDER_ID", "RUNTIME_ID", "RunResult", "Runner", "SessionRecord", "StartRequest", "TokenUsageRecord", "UNAVAILABLE", "fold_token_usage", "normalize_event", "normalize_notification", "normalize_run_result", "normalize_token_usage"]
