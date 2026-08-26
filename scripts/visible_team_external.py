#!/usr/bin/env python3
"""Thin bridge from Visible Team actions to approved external providers.

Discovery is read-only.  Executing an action requires durable state; creating
an external Worker additionally requires both the ledger authorization and the
explicit ``--confirm-authorized-dispatch`` switch.  This module never chooses
or substitutes a provider, model, thinking level, or permission mode.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Mapping

try:
    from .visible_team_coordination import ActionEnvelope, Coordinator
except ImportError:  # pragma: no cover - direct script execution.
    from visible_team_coordination import ActionEnvelope, Coordinator  # type: ignore


EXTERNAL_PROVIDERS = ("antigravity", "deepseek-harness")


class ExternalBridgeError(RuntimeError):
    pass


def _mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    for method_name in ("as_dict", "to_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            result = method()
            if isinstance(result, Mapping):
                return dict(result)
    if is_dataclass(value):
        return asdict(value)
    return {}


def _available(discovery: Any) -> tuple[bool, dict[str, Any]]:
    data = _mapping(discovery)
    value = data.get("available")
    if isinstance(value, bool):
        return value, data
    prop = getattr(discovery, "available", None)
    if isinstance(prop, bool):
        return prop, data
    return data.get("status") == "available", data


def _failure(error: BaseException, source: str) -> dict[str, Any]:
    raw_kind = getattr(error, "kind", None) or getattr(error, "category", None)
    raw_kind = getattr(raw_kind, "value", raw_kind)
    kind = str(raw_kind or "unknown").lower()
    message = str(error).lower()
    if (
        kind in {"authentication", "authorization", "not_installed", "unavailable", "unsupported"}
        or "not authorized" in message
        or "provider mismatch" in message
    ):
        category = "authorization"
    elif kind in {"quota", "rate_limit", "timeout", "process_crash", "spawn"}:
        category = "transient"
    elif kind == "conflict":
        category = "conflict"
    elif kind in {"invalid_request", "invalid_output", "protocol"}:
        category = "decision"
    else:
        category = "permanent"
    return {
        "failure": {
            "category": category,
            "message": str(error),
            "source_ref": source,
        }
    }


def _usage(data: Mapping[str, Any], provider: str) -> dict[str, Any] | None:
    candidate: Any = data.get("usage")
    if isinstance(candidate, Mapping) and isinstance(candidate.get("usage"), Mapping):
        candidate = candidate["usage"]
    if not isinstance(candidate, Mapping):
        receipt = data.get("receipt")
        if isinstance(receipt, Mapping):
            candidate = receipt.get("token_usage")
    if not isinstance(candidate, Mapping):
        return None

    def count(*names: str) -> int | None:
        for name in names:
            value = candidate.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return None

    values = {
        "input_tokens": count("input_tokens", "prompt_tokens"),
        "cached_input_tokens": count("cached_input_tokens", "cache_read_tokens"),
        "output_tokens": count("output_tokens", "completion_tokens"),
        "reasoning_output_tokens": count(
            "reasoning_output_tokens", "reasoning_tokens", "thinking_tokens"
        ),
        "total_tokens": count("total_tokens"),
    }
    if all(value is None for value in values.values()):
        return None
    return {"source": f"{provider}-native", "cumulative": True, **values}


def _native_id(data: Mapping[str, Any]) -> str | None:
    for key in ("native_task_id", "native_session_id", "conversation_id", "session_id"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    receipt = data.get("receipt")
    if isinstance(receipt, Mapping):
        for key in ("native_task_id", "native_session_id", "conversation_id"):
            value = receipt.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _final_text(data: Mapping[str, Any]) -> str | None:
    for key in ("final_response", "response", "text", "output"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


class ExternalProviderAdapter:
    """Map provider-neutral envelopes to one already selected provider."""

    def __init__(self, provider_id: str, provider: Any, *, cwd: str | None = None) -> None:
        if provider_id not in EXTERNAL_PROVIDERS:
            raise ValueError(f"unsupported external provider: {provider_id}")
        self.provider_id = provider_id
        self.provider = provider
        self.cwd = str(Path(cwd or os.getcwd()).expanduser().resolve())

    def execute(self, envelope: ActionEnvelope) -> Mapping[str, Any]:
        if envelope.provider != self.provider_id:
            return _failure(
                ExternalBridgeError(
                    f"provider mismatch: envelope={envelope.provider}, adapter={self.provider_id}"
                ),
                "external-provider-bridge",
            )
        try:
            if envelope.action == "create_worker":
                return self._create(envelope)
            if envelope.action == "deliver_context":
                return self._send(envelope)
            if envelope.action in {"observe_worker", "obtain_result"}:
                return self._observe(envelope)
            return _failure(
                ExternalBridgeError(f"unsupported external action: {envelope.action}"),
                f"{self.provider_id}:capability",
            )
        except BaseException as error:
            return _failure(error, f"{self.provider_id}:{envelope.action}")

    def _configuration(self, envelope: ActionEnvelope) -> tuple[str, str, str, dict[str, Any]]:
        model = envelope.payload.get("model")
        thinking = envelope.payload.get("thinking")
        permission_mode = envelope.payload.get("permission_mode")
        if not all(isinstance(value, str) and value for value in (model, thinking, permission_mode)):
            raise ExternalBridgeError("model, thinking, and permission_mode must be explicit")
        permissions = {"mode": permission_mode, "workspace": self.cwd}
        return model, thinking, permission_mode, permissions

    def _create(self, envelope: ActionEnvelope) -> Mapping[str, Any]:
        if envelope.payload.get("dispatch_authorized") is not True:
            raise ExternalBridgeError("external dispatch is not authorized in durable state")
        available, discovery = _available(self.provider.discover())
        if not available:
            reason = discovery.get("reason") or "provider is unavailable"
            raise ExternalBridgeError(str(reason))
        model, thinking, permission_mode, permissions = self._configuration(envelope)
        prompt = envelope.payload.get("responsibility")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ExternalBridgeError("external Worker responsibility/prompt is empty")
        if self.provider_id == "antigravity":
            result = self.provider.start(
                prompt,
                cwd=self.cwd,
                model=model,
                thinking=thinking,
                permission_mode=permission_mode,
            )
        else:
            try:
                from .providers.deepseek_harness import StartRequest
            except ImportError:  # pragma: no cover - direct script execution.
                from providers.deepseek_harness import StartRequest  # type: ignore
            record = self.provider.start(
                StartRequest(
                    model=model,
                    thinking=thinking,
                    permissions=permissions,
                    cwd=self.cwd,
                )
            )
            result = self.provider.send_or_resume(
                record,
                prompt,
                model=model,
                thinking=thinking,
                permissions=permissions,
            )
        return self._response(result)

    def _send(self, envelope: ActionEnvelope) -> Mapping[str, Any]:
        if not envelope.native_task_id:
            raise ExternalBridgeError("cannot continue without a native task/session ID")
        updates = envelope.payload.get("updates")
        summaries = [
            item.get("summary")
            for item in updates or []
            if isinstance(item, Mapping) and isinstance(item.get("summary"), str)
        ]
        if not summaries:
            raise ExternalBridgeError("no context update was supplied")
        prompt = "\n".join(summaries)
        worker = envelope.payload.get("worker", {})
        model = worker.get("model") if isinstance(worker, Mapping) else None
        thinking = worker.get("thinking") if isinstance(worker, Mapping) else None
        permission_mode = worker.get("permission_mode") if isinstance(worker, Mapping) else None
        if self.provider_id == "antigravity":
            result = self.provider.send_or_resume(
                prompt,
                conversation_id=envelope.native_task_id,
                cwd=self.cwd,
                model=model,
                thinking=thinking,
                permission_mode=permission_mode or "safe",
            )
        else:
            permissions = {"mode": permission_mode, "workspace": self.cwd}
            result = self.provider.send_or_resume(
                envelope.native_task_id,
                prompt,
                model=model,
                thinking=thinking,
                permissions=permissions,
            )
        response = dict(self._response(result))
        if updates:
            response["acknowledged_through_version"] = max(
                int(item["version"])
                for item in updates
                if isinstance(item, Mapping) and isinstance(item.get("version"), int)
            )
        return response

    def _observe(self, envelope: ActionEnvelope) -> Mapping[str, Any]:
        if not envelope.native_task_id:
            raise ExternalBridgeError("native task/session ID is missing")
        session_method = getattr(self.provider, "session", None)
        record = session_method(envelope.native_task_id) if callable(session_method) else None
        if record is None:
            return _failure(
                ExternalBridgeError(
                    "provider exposes no durable read API for this session; reconcile in its native app"
                ),
                f"{self.provider_id}:observe",
            )
        data = _mapping(record)
        status = data.get("state") or data.get("status") or "observed"
        result_available = bool(_final_text(data) or data.get("last_result"))
        response: dict[str, Any] = {
            "observation": {
                "task_exists": True,
                "host_status": str(status),
                "result_available": result_available,
                "needs_attention": False,
                "source_ref": f"{self.provider_id}:memory-session",
            }
        }
        usage = _usage(data, self.provider_id)
        if usage:
            response["usage"] = usage
        return response

    def _response(self, result: Any) -> Mapping[str, Any]:
        data = _mapping(result)
        native_id = _native_id(data)
        if not native_id:
            raise ExternalBridgeError("provider result did not include a native session/task ID")
        text = _final_text(data)
        status = str(data.get("status") or "completed")
        response: dict[str, Any] = {
            "native_task_id": native_id,
            "native_open_ref": data.get("native_open_ref"),
            "observation": {
                "task_exists": True,
                "host_status": status,
                "result_available": bool(text),
                "needs_attention": False,
                "source_ref": f"{self.provider_id}:native-result",
            },
        }
        usage = _usage(data, self.provider_id)
        if usage:
            response["usage"] = usage
        if text:
            response["delivery"] = {
                "status": "submitted",
                "summary": text,
                "artifact_ref": f"{self.provider_id}:{native_id}",
                "result_available": True,
            }
        return response


def make_provider(provider_id: str, args: argparse.Namespace) -> Any:
    if provider_id == "antigravity":
        try:
            from .providers.antigravity import AntigravityProvider
        except ImportError:  # pragma: no cover - direct script execution.
            from providers.antigravity import AntigravityProvider  # type: ignore
        return AntigravityProvider(binary=args.antigravity_binary)
    try:
        from .providers.deepseek_harness import DeepSeekHarnessProvider
    except ImportError:  # pragma: no cover - direct script execution.
        from providers.deepseek_harness import DeepSeekHarnessProvider  # type: ignore
    if args.dsh_sdk_path:
        sdk_path = Path(args.dsh_sdk_path).expanduser().resolve()
        if not (sdk_path / "deepseek_harness").is_dir():
            raise ExternalBridgeError(
                "--dsh-sdk-path must be the official Python SDK src directory"
            )
        sdk_path_text = str(sdk_path)
        if sdk_path_text not in sys.path:
            sys.path.insert(0, sdk_path_text)
    runtime_argv = list(args.dsh_runtime_argv or [])
    if args.dsh_runtime_bin:
        runtime_argv.insert(0, args.dsh_runtime_bin)
    return DeepSeekHarnessProvider(runtime_argv=runtime_argv or None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=EXTERNAL_PROVIDERS, required=True)
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--antigravity-binary")
    parser.add_argument(
        "--dsh-sdk-path",
        help="Official DeepSeek Harness python/sdk/src directory",
    )
    parser.add_argument("--dsh-runtime-bin")
    parser.add_argument("--dsh-runtime-argv", nargs="+")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("discover", help="Offline provider/capability discovery only")
    execute = subparsers.add_parser("execute", help="Execute one durable next action")
    execute.add_argument("--db", required=True)
    execute.add_argument("--collaboration-id", required=True)
    execute.add_argument("--worker-id", required=True)
    execute.add_argument("--idempotency-key", required=True)
    execute.add_argument("--confirm-authorized-dispatch", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    provider = make_provider(args.provider, args)
    if args.command == "discover":
        print(json.dumps(_mapping(provider.discover()), ensure_ascii=False, sort_keys=True))
        return 0
    adapter = ExternalProviderAdapter(args.provider, provider, cwd=args.cwd)
    coordinator = Coordinator(args.db, adapter)
    actions = [
        action
        for action in coordinator.next_actions(args.collaboration_id)
        if action.worker_id == args.worker_id
    ]
    if len(actions) != 1:
        raise ExternalBridgeError(
            f"expected one executable next action for {args.worker_id!r}; found {len(actions)}"
        )
    action = actions[0]
    if action.action == "create_worker" and not args.confirm_authorized_dispatch:
        raise ExternalBridgeError(
            "creating an external Worker requires --confirm-authorized-dispatch"
        )
    result = coordinator.execute(action, args.idempotency_key)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ExternalBridgeError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
