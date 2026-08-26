"""Thin, dependency-injectable adapter for the Antigravity ``agy`` CLI.

It owns one stream-json invocation only: discover, build argv, start/resume,
parse events, and normalize run tokens.  It does not implement a workflow,
background watcher, native UI, or account-usage client.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
import json
import os
import re
import shutil
import subprocess
from typing import Any, Callable, Sequence

PROVIDER_ID = "antigravity"
RUNTIME_ID = "agy"
AVAILABLE = "available"
UNAVAILABLE = "unavailable"
SUPPORTED_EFFORTS = ("low", "medium", "high")
TOKEN_FIELDS = ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens")
MIN_STREAM_JSON_VERSION = (1, 1, 8)


class ErrorCategory(str, Enum):
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    INVALID_OUTPUT = "invalid_output"
    PROCESS_CRASH = "process_crash"
    SPAWN = "spawn"
    NOT_INSTALLED = "not_installed"
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED = "unsupported"
    REMOTE_ERROR = "remote_error"


class RunStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    WAITING = "waiting"
    INVALID = "invalid"
    UNKNOWN = "unknown"


def _obj(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _str(value: Any, default: str | None = None) -> str | None:
    return value if isinstance(value, str) and value else default


def _count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _version(value: str) -> tuple[int, int, int] | None:
    match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", value)
    return tuple(int(part) for part in match.groups()) if match else None


def _id(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip() or any(c.isspace() for c in value):
        raise ValueError(f"{name} must be a non-empty identifier")
    if "\x00" in value or value.startswith("-"):
        raise ValueError(f"{name} contains unsupported characters")
    return value


def _model(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or value.startswith("-"):
        raise ValueError("model must be explicitly supplied")
    if any(c in value for c in ("\x00", "\r", "\n")):
        raise ValueError("model contains unsupported characters")
    return value


def _effort(effort: str | None, thinking: str | None) -> str:
    chosen = effort if effort is not None else thinking
    if chosen not in SUPPORTED_EFFORTS:
        raise ValueError(f"an explicit effort/thinking level is required: {SUPPORTED_EFFORTS}")
    if effort is not None and thinking is not None and effort != thinking:
        raise ValueError("effort and thinking must agree when both are supplied")
    return chosen


def classify_error(
    message: str | None = None,
    *,
    native_status: str | None = None,
    exit_code: int | None = None,
    stderr: str | None = None,
    process_failure: bool = False,
) -> ErrorCategory:
    status = (native_status or "").upper()
    text = " ".join(part for part in (message, stderr) if part).lower()
    if status in {"CANCELED", "CANCELLED"}:
        return ErrorCategory.CANCELLED
    if status in {"INTERRUPTED", "ABORTED"}:
        return ErrorCategory.INTERRUPTED
    if any(word in text for word in ("not logged in", "not authenticated", "authentication", "unauthorized", "oauth", "credential", "sign in")):
        return ErrorCategory.AUTHENTICATION
    if any(word in text for word in ("quota", "rate limit", "rate_limit", "too many requests", "resource exhausted", "limit exceeded")):
        return ErrorCategory.RATE_LIMIT
    if any(word in text for word in ("timed out", "timeout", "deadline exceeded")):
        return ErrorCategory.TIMEOUT
    if any(word in text for word in ("invalid json", "malformed json", "invalid output", "parse error", "unexpected token")):
        return ErrorCategory.INVALID_OUTPUT
    if status == "INVALID":
        return ErrorCategory.INVALID_REQUEST
    if process_failure or (exit_code is not None and exit_code != 0):
        return ErrorCategory.PROCESS_CRASH
    return ErrorCategory.REMOTE_ERROR


@dataclass(frozen=True)
class ErrorInfo:
    category: ErrorCategory
    message: str
    retryable: bool = False
    native_status: str | None = None
    exit_code: int | None = None
    stderr: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"category": self.category.value, "message": self.message, "retryable": self.retryable,
                "native_status": self.native_status, "exit_code": self.exit_code, "stderr": self.stderr,
                "details": dict(self.details)}


class ProviderError(RuntimeError):
    def __init__(self, info: ErrorInfo, *, events: Sequence["NormalizedEvent"] = (), receipt: "InvocationReceipt | None" = None):
        super().__init__(info.message)
        self.info, self.events, self.receipt = info, tuple(events), receipt

    @property
    def category(self) -> ErrorCategory:
        return self.info.category


AntigravityProviderError = ProviderError


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    cache_read_tokens: int | None = None
    total_tokens: int | None = None

    @classmethod
    def from_mapping(cls, value: Any) -> "TokenUsage":
        data = _obj(value) or {}

        def pick(*names: str) -> int | None:
            for name in names:
                result = _count(data.get(name))
                if result is not None:
                    return result
            return None

        return cls(pick("input_tokens"), pick("output_tokens"),
                   pick("thinking_tokens", "reasoning_tokens", "reasoning_output_tokens"),
                   pick("cache_read_tokens", "cached_input_tokens"), pick("total_tokens"))

    def available(self) -> bool:
        return any(getattr(self, name) is not None for name in TOKEN_FIELDS)

    def as_dict(self) -> dict[str, int | None]:
        return {name: getattr(self, name) for name in TOKEN_FIELDS}


@dataclass(frozen=True)
class NormalizedEvent:
    event: str
    status: RunStatus
    conversation_id: str | None = None
    native_status: str | None = None
    step_index: int | None = None
    step_type: str | None = None
    text_delta: str | None = None
    response: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    tool: Mapping[str, Any] | None = None
    subagents: tuple[Mapping[str, Any], ...] = ()
    error: ErrorInfo | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return self.event

    def as_dict(self) -> dict[str, Any]:
        return {"event": self.event, "status": self.status.value, "conversation_id": self.conversation_id,
                "native_status": self.native_status, "step_index": self.step_index, "step_type": self.step_type,
                "text_delta": self.text_delta, "response": self.response, "usage": self.usage.as_dict(),
                "tool": dict(self.tool) if self.tool else None, "subagents": [dict(x) for x in self.subagents],
                "error": self.error.as_dict() if self.error else None, "metadata": dict(self.metadata)}


def _bad(message: str, details: Mapping[str, Any] | None = None) -> NormalizedEvent:
    return NormalizedEvent("invalid_output", RunStatus.ERROR,
                           error=ErrorInfo(ErrorCategory.INVALID_OUTPUT, message, details=details or {}))


def _status(value: Any, *, step: bool = False) -> RunStatus:
    raw = str(value or "").strip().upper().replace("-", "_")
    if step:
        if raw in {"ERROR", "FAILED", "FAILURE"}: return RunStatus.ERROR
        if raw in {"WAITING", "WAIT", "PAUSED"}: return RunStatus.WAITING
        if raw in {"CANCELED", "CANCELLED"}: return RunStatus.CANCELLED
        if raw in {"INTERRUPTED", "ABORTED"}: return RunStatus.INTERRUPTED
        return RunStatus.RUNNING
    return {"SUCCESS": RunStatus.SUCCESS, "OK": RunStatus.SUCCESS, "DONE": RunStatus.SUCCESS,
            "COMPLETED": RunStatus.SUCCESS, "ERROR": RunStatus.ERROR, "FAILED": RunStatus.ERROR,
            "FAILURE": RunStatus.ERROR, "CANCELED": RunStatus.CANCELLED, "CANCELLED": RunStatus.CANCELLED,
            "INTERRUPTED": RunStatus.INTERRUPTED, "WAITING": RunStatus.WAITING, "RUNNING": RunStatus.RUNNING,
            "ACTIVE": RunStatus.RUNNING, "INVALID": RunStatus.INVALID}.get(raw, RunStatus.UNKNOWN)


def _cid(*values: Any) -> str | None:
    for value in values:
        found = _str((_obj(value) or {}).get("conversation_id"))
        if found:
            return found
    return None


def _native_error(data: Mapping[str, Any], native_status: str | None) -> ErrorInfo | None:
    raw = data.get("error")
    raw_obj = _obj(raw)
    message = (_str(raw_obj.get("message")) if raw_obj else _str(raw)) or _str(data.get("message"))
    if not message and native_status in {None, "", "SUCCESS", "OK", "DONE", "COMPLETED"}:
        return None
    message = message or "Antigravity returned an error"
    category = classify_error(message, native_status=native_status)
    if raw_obj and isinstance(raw_obj.get("category"), str):
        try:
            category = ErrorCategory(raw_obj["category"])
        except ValueError:
            pass
    return ErrorInfo(category, message, retryable=category in {ErrorCategory.RATE_LIMIT, ErrorCategory.TIMEOUT},
                     native_status=native_status, details=raw_obj or {})


def parse_event(record: Mapping[str, Any] | str | bytes) -> NormalizedEvent:
    if isinstance(record, bytes):
        record = record.decode("utf-8", errors="replace")
    if isinstance(record, str):
        try:
            record = json.loads(record)
        except (TypeError, ValueError) as exc:
            return _bad(f"invalid JSONL event: {exc}")
    data = _obj(record)
    if not data or not _str(data.get("event")):
        return _bad("stream event must be a JSON object with an event")
    name = data["event"]
    if name == "init":
        init = _obj(data.get("init")) or {}
        return NormalizedEvent("init", RunStatus.RUNNING, _cid(data, init),
                               metadata={key: init[key] for key in ("cwd", "tools", "permission_mode", "model", "agent") if key in init})
    if name == "step_update":
        step = _obj(data.get("step_update"))
        if not step:
            return _bad("step_update payload must be an object")
        index = _count(step.get("step_index"))
        state = step.get("state")
        tool_obj = _obj(step.get("tool_info"))
        tool = None
        if tool_obj:
            tool = {"name": _str(tool_obj.get("name"), _str(tool_obj.get("tool_name"))),
                    "state": _status(tool_obj.get("state", tool_obj.get("status", state)), step=True).value,
                    "parameters": tool_obj.get("parameters"), "output": tool_obj.get("output"),
                    "error": tool_obj.get("error"), "step_index": index}
        children = _obj(step.get("subagent_info")) or {}
        raw_children = children.get("subagents")
        if isinstance(raw_children, Mapping): raw_children = (raw_children,)
        if not isinstance(raw_children, Iterable) or isinstance(raw_children, (str, bytes)): raw_children = ()
        subagents = tuple({"type_name": _str(_obj(child).get("type_name")) if _obj(child) else None,
                           "role": _str(_obj(child).get("role")) if _obj(child) else None,
                           "conversation_id": _str(_obj(child).get("conversation_id")) if _obj(child) else None,
                           "state": _status((_obj(child) or {}).get("state", state), step=True).value,
                           "log_uri": _str((_obj(child) or {}).get("log_uri")),
                           "workspace_uris": list((_obj(child) or {}).get("workspace_uris", ())) }
                          for child in raw_children if _obj(child))
        status = _status(state, step=True)
        return NormalizedEvent("step_update", status, _cid(data, step), _str(state), index,
                               _str(step.get("step_type")), _str(step.get("text_delta")),
                               usage=TokenUsage.from_mapping(step.get("usage")), tool=tool, subagents=subagents,
                               error=_native_error(step, _str(state)) if status == RunStatus.ERROR else None,
                               metadata={key: step[key] for key in ("checkpoint_id", "checkpoint_uri", "state") if key in step})
    if name == "result":
        result = _obj(data.get("result"))
        if not result:
            return _bad("result payload must be an object")
        native = _str(result.get("status"))
        status = _status(native)
        raw_response = result.get("response")
        response = raw_response if isinstance(raw_response, str) else None
        error = _native_error(result, native)
        if status == RunStatus.UNKNOWN:
            status, error = RunStatus.ERROR, ErrorInfo(ErrorCategory.INVALID_OUTPUT, f"unsupported result status: {native!r}", native_status=native)
        elif status == RunStatus.SUCCESS and not isinstance(raw_response, str):
            status, error = RunStatus.ERROR, ErrorInfo(ErrorCategory.INVALID_OUTPUT, "successful result response must be a string", native_status=native)
        return NormalizedEvent("result", status, _cid(data, result), native, response=response,
                               usage=TokenUsage.from_mapping(result.get("usage")), error=error,
                               metadata={key: result[key] for key in ("duration_seconds", "num_turns", "structured_output", "json_schema") if key in result})
    return NormalizedEvent("unknown", RunStatus.RUNNING, _cid(data), metadata={"native_event": name, "payload": dict(data)})


@dataclass(frozen=True)
class ParsedStream:
    events: tuple[NormalizedEvent, ...]
    result: NormalizedEvent | None
    conversation_id: str | None
    usage: TokenUsage
    usage_source: str | None
    invalid_output: bool = False


def parse_stream(output: str | bytes | Iterable[str | bytes]) -> ParsedStream:
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    if output is None:
        output = ""
    lines = output.splitlines() if isinstance(output, str) else output
    events: list[NormalizedEvent] = []
    result = None
    cid = None
    usage, source, invalid = TokenUsage(), None, False
    for line in lines:
        if not str(line).strip():
            continue
        event = parse_event(line)
        events.append(event)
        cid = event.conversation_id or cid
        invalid = invalid or event.event == "invalid_output"
        if event.event == "result":
            result = event
            if event.usage.available(): usage, source = event.usage, "result"
        elif result is None and event.usage.available():
            usage, source = event.usage, "step_update"
    return ParsedStream(tuple(events), result, cid, usage, source, invalid)


parse_jsonl = parse_stream


@dataclass(frozen=True)
class InvocationReceipt:
    provider: str
    runtime: str
    native_session_id: str | None
    native_task_id: str | None
    model: str
    thinking: str
    effort: str
    permission_boundary: str
    token_usage: TokenUsage
    cwd: str
    project: str | None = None
    operation: str = "start"
    effective_permission_mode: str | None = None
    native_open_ref: str | None = None

    def as_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["token_usage"] = self.token_usage.as_dict()
        return data


@dataclass(frozen=True)
class RunResult:
    provider: str
    runtime: str
    status: RunStatus
    native_status: str | None
    response: str | None
    conversation_id: str | None
    usage: TokenUsage
    receipt: InvocationReceipt
    events: tuple[NormalizedEvent, ...]
    argv: tuple[str, ...]
    error: ErrorInfo | None = None
    duration_seconds: Any = None
    num_turns: int | None = None
    structured_output: Any = None
    exit_code: int | None = None
    stderr: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == RunStatus.SUCCESS and self.error is None

    @property
    def native_session_id(self) -> str | None:
        return self.conversation_id

    @property
    def native_task_id(self) -> None:
        return None

    def as_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data.update(status=self.status.value, usage=self.usage.as_dict(), receipt=self.receipt.as_dict(),
                    events=[event.as_dict() for event in self.events], argv=list(self.argv),
                    error=self.error.as_dict() if self.error else None)
        return data


@dataclass(frozen=True)
class UsageSnapshot:
    provider: str
    runtime: str
    native_session_id: str | None
    usage: TokenUsage
    source: str | None
    scope: str
    status: str = AVAILABLE

    @property
    def available(self) -> bool:
        return self.status == AVAILABLE and self.usage.available()

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "usage": self.usage.as_dict()}


@dataclass(frozen=True)
class DiscoveryResult:
    provider: str
    runtime: str
    binary: str
    installed: bool
    version: str | None
    capabilities: Mapping[str, Mapping[str, str]]
    errors: tuple[ErrorInfo, ...] = ()

    @property
    def available(self) -> bool:
        return self.installed and self.supports("start")

    def capability(self, name: str) -> Mapping[str, str]:
        return self.capabilities.get(name, {"status": UNAVAILABLE, "reason": "capability is not declared"})

    def supports(self, name: str) -> bool:
        return self.capability(name).get("status") == AVAILABLE

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "capabilities": {k: dict(v) for k, v in self.capabilities.items()},
                "errors": [error.as_dict() for error in self.errors]}


@dataclass(frozen=True)
class UnavailableResult:
    provider: str
    runtime: str
    operation: str
    status: str = UNAVAILABLE
    reason: str = "capability is unavailable"

    def as_dict(self) -> dict[str, str]:
        return dict(self.__dict__)


def _capabilities(installed: bool, ready: bool, reason: str) -> dict[str, Mapping[str, str]]:
    names = ("discover", "argv", "event_parse", "start", "resume", "send_or_resume", "usage")
    caps = {name: {"status": AVAILABLE if name in {"discover", "event_parse"} or (installed and (name == "argv" or ready)) else UNAVAILABLE,
                   "reason": reason} for name in names}
    caps.update({
        "watch": {"status": UNAVAILABLE, "reason": "no background process is retained"},
        "read": {"status": UNAVAILABLE, "reason": "no persistent native handle is retained"},
        "interrupt_or_cancel": {"status": UNAVAILABLE, "reason": "one-shot communicate has no exposed handle"},
        "open_native": {"status": UNAVAILABLE, "reason": "native open reference is not exposed"},
        "account_usage": {"status": UNAVAILABLE, "reason": "stream results expose run tokens, not account quota"},
    })
    return caps


class AntigravityProvider:
    def __init__(self, *, binary: str | None = None, process_factory: Callable[..., Any] | None = None,
                 command_runner: Callable[..., Any] | None = None, default_timeout_s: float = 300.0,
                 strict_output: bool = True) -> None:
        self.binary = binary or os.environ.get("AGY_BIN") or "agy"
        self.process_factory = process_factory or subprocess.Popen
        self.command_runner = command_runner or subprocess.run
        self.default_timeout_s, self.strict_output = default_timeout_s, strict_output
        self._discovery: DiscoveryResult | None = None

    def _resolved_binary(self) -> str | None:
        if os.path.dirname(self.binary):
            return self.binary if os.path.isfile(self.binary) and os.access(self.binary, os.X_OK) else None
        return shutil.which(self.binary)

    @staticmethod
    def _decode(value: Any) -> str:
        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")

    def _probe(self, argv: Sequence[str], timeout_s: float) -> tuple[str, ErrorInfo | None]:
        try:
            completed = self.command_runner(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                            timeout=timeout_s, check=False, shell=False)
        except subprocess.TimeoutExpired:
            return "", ErrorInfo(ErrorCategory.TIMEOUT, f"timed out probing {argv[0]}", retryable=True)
        except FileNotFoundError as exc:
            return "", ErrorInfo(ErrorCategory.NOT_INSTALLED, str(exc))
        except OSError as exc:
            return "", ErrorInfo(ErrorCategory.SPAWN, str(exc))
        stdout, stderr, code = self._decode(getattr(completed, "stdout", "")), self._decode(getattr(completed, "stderr", "")), getattr(completed, "returncode", None)
        if code not in (0, None):
            message = stderr.strip() or stdout.strip() or f"agy probe exited with code {code}"
            return stdout, ErrorInfo(classify_error(message, exit_code=code, stderr=stderr, process_failure=True), message, exit_code=code, stderr=stderr or None)
        # Go's flag package writes successful help text to stderr.  Preserve
        # both streams for read-only capability discovery instead of treating
        # a valid ``agy --help`` response as empty.
        output = stdout
        if stderr:
            output = f"{stdout}\n{stderr}" if stdout else stderr
        return output, None

    def discover(self, *, timeout_s: float = 10.0) -> DiscoveryResult:
        binary = self._resolved_binary()
        if not binary:
            error = ErrorInfo(ErrorCategory.NOT_INSTALLED, f"agy executable not found: {self.binary}")
            result = DiscoveryResult(PROVIDER_ID, RUNTIME_ID, self.binary, False, None, _capabilities(False, False, error.message), (error,))
            self._discovery = result
            return result
        version_out, error = self._probe((binary, "--version"), timeout_s)
        if error:
            result = DiscoveryResult(PROVIDER_ID, RUNTIME_ID, binary, True, None, _capabilities(True, False, error.message), (error,))
            self._discovery = result
            return result
        version = _version(version_out)
        if not version:
            error = ErrorInfo(ErrorCategory.INVALID_OUTPUT, "agy --version did not contain a semantic version")
            result = DiscoveryResult(PROVIDER_ID, RUNTIME_ID, binary, True, None, _capabilities(True, False, error.message), (error,))
            self._discovery = result
            return result
        help_out, error = self._probe((binary, "--help"), timeout_s)
        ready = not error and version >= MIN_STREAM_JSON_VERSION and all(flag in help_out for flag in ("--input-format", "--output-format", "stream-json"))
        errors = (error,) if error else (() if ready else (ErrorInfo(ErrorCategory.UNSUPPORTED, "agy does not advertise stream-json"),))
        reason = "agy stream-json is available" if ready else errors[0].message
        result = DiscoveryResult(PROVIDER_ID, RUNTIME_ID, binary, True, ".".join(map(str, version)), _capabilities(True, ready, reason), errors)
        self._discovery = result
        return result

    def capabilities(self) -> Mapping[str, Mapping[str, str]]:
        return self._discovery.capabilities if self._discovery else _capabilities(False, False, "run discover() first")

    @staticmethod
    def _boundary(permission_mode: str, sandbox: bool, dangerous: bool) -> str:
        mode = permission_mode.lower()
        base = "safe" if mode in {"safe", "default", "host-default", "settings-default"} else mode
        return "+".join([base] + (["sandbox"] if sandbox else []) + (["dangerously-skip-permissions"] if dangerous else []))

    def build_argv(self, *, model: str | None, effort: str | None = None, thinking: str | None = None,
                   permission_mode: str = "safe", project: str | None = None, conversation_id: str | None = None,
                   continue_latest: bool = False, sandbox: bool = False, dangerously_skip_permissions: bool = False,
                   agent: str | None = None, print_timeout: int | None = None) -> list[str]:
        model, level = _model(model), _effort(effort, thinking)
        permission = permission_mode.lower() if isinstance(permission_mode, str) else ""
        valid = {"safe", "default", "host-default", "settings-default", "plan", "accept-edits", "dangerous", "yolo", "dangerously-skip-permissions"}
        if permission not in valid:
            raise ValueError("permission_mode must be safe, plan, accept-edits, or explicitly dangerous")
        if permission in {"dangerous", "yolo", "dangerously-skip-permissions"}:
            dangerously_skip_permissions = True
        if permission in {"plan", "accept-edits"} and dangerously_skip_permissions:
            raise ValueError("plan/accept-edits cannot be combined with dangerous permissions")
        if conversation_id is not None and continue_latest:
            raise ValueError("conversation_id and continue_latest are mutually exclusive")
        if project is not None and (not isinstance(project, str) or not project or "\x00" in project):
            raise ValueError("project must be a non-empty path")
        if print_timeout is not None and (not isinstance(print_timeout, int) or isinstance(print_timeout, bool) or print_timeout <= 0):
            raise ValueError("print_timeout must be a positive integer")
        argv = [self.binary, "--input-format", "stream-json", "--output-format", "stream-json",
                "--disable-slash-commands", "--model", model, "--effort", level]
        if project is not None: argv += ["--project", project]
        if conversation_id is not None: argv += ["--conversation", _id(conversation_id, "conversation_id")]
        elif continue_latest: argv.append("--continue")
        if agent is not None: argv += ["--agent", _id(agent, "agent")]
        if permission in {"plan", "accept-edits"}: argv += ["--mode", permission]
        if sandbox: argv.append("--sandbox")
        if dangerously_skip_permissions: argv.append("--dangerously-skip-permissions")
        if print_timeout is not None: argv += ["--print-timeout", str(print_timeout)]
        return argv

    @staticmethod
    def _wire(prompt: str) -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be non-empty")
        return json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False, separators=(",", ":")) + "\n"

    @staticmethod
    def _terminate(process: Any) -> None:
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:
            try: process.kill()
            except Exception: pass

    def start(self, prompt: str, *, cwd: str | None = None, workspace: str | None = None, project: str | None = None,
              model: str | None, effort: str | None = None, thinking: str | None = None,
              conversation_id: str | None = None, continue_latest: bool = False, permission_mode: str = "safe",
              dangerously_skip_permissions: bool = False, sandbox: bool = False, agent: str | None = None,
              print_timeout: int | None = None, timeout_s: float | None = None) -> RunResult:
        if cwd and workspace and os.path.abspath(cwd) != os.path.abspath(workspace):
            raise ValueError("cwd and workspace must identify the same directory")
        selected_cwd = cwd or workspace or os.getcwd()
        if not os.path.isdir(selected_cwd):
            raise ValueError(f"cwd does not exist: {selected_cwd}")
        model, level = _model(model), _effort(effort, thinking)
        argv = self.build_argv(model=model, effort=level, permission_mode=permission_mode, project=project,
                               conversation_id=conversation_id, continue_latest=continue_latest, sandbox=sandbox,
                               dangerously_skip_permissions=dangerously_skip_permissions, agent=agent,
                               print_timeout=print_timeout)
        dangerous = dangerously_skip_permissions or permission_mode.lower() in {"dangerous", "yolo", "dangerously-skip-permissions"}
        receipt = InvocationReceipt(PROVIDER_ID, RUNTIME_ID, None, None, model, level, level,
                                    self._boundary(permission_mode, sandbox, dangerous), TokenUsage(), selected_cwd,
                                    project=project, operation="resume" if conversation_id or continue_latest else "start")
        process = None
        try:
            process = self.process_factory(list(argv), cwd=selected_cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", shell=False,
                                           start_new_session=(os.name == "posix"))
            stdout, stderr = process.communicate(input=self._wire(prompt), timeout=self.default_timeout_s if timeout_s is None else timeout_s)
        except subprocess.TimeoutExpired as exc:
            if process is not None: self._terminate(process)
            raise ProviderError(ErrorInfo(ErrorCategory.TIMEOUT, "agy invocation timed out", retryable=True,
                                          stderr=self._decode(getattr(exc, "stderr", None))), receipt=receipt) from exc
        except FileNotFoundError as exc:
            raise ProviderError(ErrorInfo(ErrorCategory.NOT_INSTALLED, str(exc)), receipt=receipt) from exc
        except OSError as exc:
            raise ProviderError(ErrorInfo(ErrorCategory.SPAWN, str(exc)), receipt=receipt) from exc
        parsed = parse_stream(self._decode(stdout))
        stderr_text, exit_code = self._decode(stderr).strip() or None, getattr(process, "returncode", None)
        effective = next((_str(event.metadata.get("permission_mode")) for event in parsed.events if event.event == "init" and event.metadata.get("permission_mode")), None)
        receipt = replace(receipt, native_session_id=parsed.conversation_id, token_usage=parsed.usage, effective_permission_mode=effective)
        if parsed.result is None:
            if parsed.invalid_output: category, message = ErrorCategory.INVALID_OUTPUT, "agy emitted no valid terminal result"
            elif exit_code not in (0, None):
                category, message = classify_error(stderr_text, exit_code=exit_code, stderr=stderr_text, process_failure=True), stderr_text or f"agy exited with code {exit_code}"
            else: category, message = ErrorCategory.INVALID_OUTPUT, "agy emitted no terminal result"
            raise ProviderError(ErrorInfo(category, message, exit_code=exit_code, stderr=stderr_text), events=parsed.events, receipt=receipt)
        terminal = parsed.result
        error = terminal.error
        if self.strict_output and parsed.invalid_output:
            error = ErrorInfo(ErrorCategory.INVALID_OUTPUT, "agy emitted an invalid stream event", native_status=terminal.native_status, stderr=stderr_text)
        if error is None and exit_code not in (0, None):
            category = classify_error(terminal.response, native_status=terminal.native_status, exit_code=exit_code, stderr=stderr_text, process_failure=True)
            error = ErrorInfo(category, stderr_text or "agy process failed after emitting a result", native_status=terminal.native_status, exit_code=exit_code, stderr=stderr_text)
        meta = terminal.metadata
        return RunResult(PROVIDER_ID, RUNTIME_ID, terminal.status, terminal.native_status, terminal.response,
                         parsed.conversation_id, parsed.usage, receipt, parsed.events, tuple(argv), error=error,
                         duration_seconds=meta.get("duration_seconds"), num_turns=_count(meta.get("num_turns")),
                         structured_output=meta.get("structured_output"), exit_code=exit_code, stderr=stderr_text)

    def resume(self, prompt: str, conversation_id: str, **options: Any) -> RunResult:
        return self.start(prompt, conversation_id=_id(conversation_id, "conversation_id"), **options)

    def send_or_resume(self, prompt: str, conversation_id: str | None = None, **options: Any) -> RunResult:
        return self.start(prompt, conversation_id=conversation_id, **options)

    def usage(self, source: RunResult | ParsedStream | NormalizedEvent | None = None) -> UsageSnapshot:
        if isinstance(source, RunResult): return UsageSnapshot(PROVIDER_ID, RUNTIME_ID, source.native_session_id, source.usage, "result", "session")
        if isinstance(source, ParsedStream): return UsageSnapshot(PROVIDER_ID, RUNTIME_ID, source.conversation_id, source.usage, source.usage_source, "session")
        if isinstance(source, NormalizedEvent): return UsageSnapshot(PROVIDER_ID, RUNTIME_ID, source.conversation_id, source.usage, source.event, "event")
        return UsageSnapshot(PROVIDER_ID, RUNTIME_ID, None, TokenUsage(), None, "account", UNAVAILABLE)

    def _unavailable(self, operation: str) -> UnavailableResult:
        return UnavailableResult(PROVIDER_ID, RUNTIME_ID, operation, reason=self.capabilities().get(operation, {}).get("reason", "capability is unavailable"))

    def watch(self, *args: Any, **kwargs: Any) -> UnavailableResult: return self._unavailable("watch")
    def read(self, *args: Any, **kwargs: Any) -> UnavailableResult: return self._unavailable("read")
    def interrupt_or_cancel(self, *args: Any, **kwargs: Any) -> UnavailableResult: return self._unavailable("interrupt_or_cancel")
    cancel = interrupt_or_cancel
    interrupt = interrupt_or_cancel
    def open_native(self, *args: Any, **kwargs: Any) -> UnavailableResult: return self._unavailable("open_native")

__all__ = ["AVAILABLE", "AntigravityProvider", "AntigravityProviderError", "DiscoveryResult", "ErrorCategory",
           "ErrorInfo", "InvocationReceipt", "MIN_STREAM_JSON_VERSION", "NormalizedEvent", "ParsedStream",
           "PROVIDER_ID", "ProviderError", "RUNTIME_ID", "RunResult", "RunStatus", "TOKEN_FIELDS", "TokenUsage",
           "UNAVAILABLE", "UnavailableResult", "UsageSnapshot", "classify_error", "normalize_status", "parse_event",
           "parse_jsonl", "parse_stream"]
normalize_status = _status
