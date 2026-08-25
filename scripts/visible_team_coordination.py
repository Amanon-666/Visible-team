"""Thin, host-agnostic coordination boundary for Visible Team.

This module deliberately does not know how Codex creates tasks, sends messages, or
reads task state.  A Leader supplies a small adapter; the coordinator turns the
durable resume facts into action envelopes, invokes that adapter once, and writes
the returned observation/result back to the SQLite ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Protocol

try:  # Support both ``python -m scripts...`` and direct script imports.
    from .visible_team_state import (
        command_acknowledge,
        command_attach_worker,
        command_reconcile_worker_creation,
        command_record_failure,
        command_record_observation,
        command_reserve_host_action,
        command_set_delivery_status,
        connect,
        resume_data,
    )
except ImportError:  # pragma: no cover - exercised only by direct embedding.
    from visible_team_state import (  # type: ignore
        command_acknowledge,
        command_attach_worker,
        command_reconcile_worker_creation,
        command_record_failure,
        command_record_observation,
        command_reserve_host_action,
        command_set_delivery_status,
        connect,
        resume_data,
    )


MECHANICAL_ACTIONS = {
    "create_worker",
    "deliver_context",
    "observe_worker",
    "reconcile_host_task",
    "reconcile_creation",
    "obtain_result",
}


@dataclass(frozen=True)
class ActionEnvelope:
    """A compact request for one host-side mechanical action."""

    action: str
    collaboration_id: str
    worker_id: str
    thread_id: str | None
    payload: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None


class HostAdapter(Protocol):
    """The only host boundary the coordinator assumes.

    Implementations may call the real Codex task tools.  They must return facts
    obtained from that host; returning a fabricated success is not a coordinator
    feature.  The adapter is called once per ``execute`` invocation.
    """

    def execute(self, envelope: ActionEnvelope) -> Mapping[str, Any]:
        ...


class Coordinator:
    """Read state, issue one adapter action, then persist returned facts."""

    def __init__(self, db_path: str, adapter: HostAdapter):
        self.db_path = db_path
        self.adapter = adapter

    def next_actions(self, collaboration_id: str) -> list[ActionEnvelope]:
        connection = connect(self.db_path)
        try:
            resume = resume_data(connection, collaboration_id)
        finally:
            connection.close()
        workers = {worker["worker_id"]: worker for worker in resume["workers"]}
        envelopes: list[ActionEnvelope] = []
        seen: set[tuple[str, str]] = set()
        for action in resume["next_actions"]:
            if action["kind"] not in MECHANICAL_ACTIONS:
                continue
            identity = (action["kind"], action["worker_id"])
            if identity in seen:
                continue
            seen.add(identity)
            worker = workers[action["worker_id"]]
            payload: dict[str, Any] = {}
            if action["kind"] == "deliver_context":
                pending = next(
                    (
                        item["updates"]
                        for item in resume["unacknowledged_context"]
                        if item["worker_id"] == action["worker_id"]
                    ),
                    [],
                )
                payload["updates"] = pending
            if action["kind"] == "create_worker":
                payload.update(
                    {
                        "title": worker["title"],
                        "model": worker["model"],
                        "thinking": worker["thinking"],
                        "responsibility": worker["responsibility"],
                    }
                )
            envelopes.append(
                ActionEnvelope(
                    action=action["kind"],
                    collaboration_id=collaboration_id,
                    worker_id=action["worker_id"],
                    thread_id=worker["thread_id"],
                    payload=payload,
                )
            )
        return envelopes

    def execute(self, envelope: ActionEnvelope, idempotency_key: str) -> dict[str, Any]:
        """Execute one envelope and persist only the adapter's returned facts.

        The response contract is intentionally small and optional:
        ``observation`` may contain task_exists/host_status/result_available and
        timestamps; ``delivery`` may contain a legal delivery status plus a
        summary or artifact_ref; ``failure`` may contain one classified failure.
        No response is retried automatically.
        """
        reservation_connection = connect(self.db_path)
        try:
            reservation = command_reserve_host_action(
                reservation_connection,
                _namespace(
                    collaboration_id=envelope.collaboration_id,
                    worker_id=envelope.worker_id,
                    action=envelope.action,
                    idempotency_key=idempotency_key,
                    expected_version=None,
                ),
            )
        finally:
            reservation_connection.close()
        if not reservation.get("reserved"):
            return {
                "action": envelope.action,
                "worker_id": envelope.worker_id,
                "reservation": reservation,
                "response": None,
            }
        host_envelope = replace(envelope, idempotency_key=idempotency_key)
        response = dict(self.adapter.execute(host_envelope))
        connection = connect(self.db_path)
        try:
            observation = response.get("observation")
            if observation is not None:
                observation_args = _namespace(
                    collaboration_id=envelope.collaboration_id,
                    worker_id=envelope.worker_id,
                    task_exists=observation["task_exists"],
                    host_status=observation["host_status"],
                    result_available=observation["result_available"],
                    observed_at=observation.get("observed_at"),
                    last_contact_at=observation.get("last_contact_at"),
                    lease_until=observation.get("lease_until"),
                    needs_attention=observation.get("needs_attention"),
                    note=observation.get("note"),
                    source_ref=observation.get("source_ref"),
                    idempotency_key=f"{idempotency_key}:observation",
                    expected_version=None,
                )
                command_record_observation(connection, observation_args)
            thread_id = response.get("thread_id")
            if thread_id is not None:
                attach_args = _namespace(
                    collaboration_id=envelope.collaboration_id,
                    worker_id=envelope.worker_id,
                    thread_id=thread_id,
                    idempotency_key=f"{idempotency_key}:attach",
                    expected_version=None,
                )
                command_attach_worker(connection, attach_args)
            reconciliation = response.get("creation_reconciliation")
            if reconciliation is not None:
                reconciliation_args = _namespace(
                    collaboration_id=envelope.collaboration_id,
                    worker_id=envelope.worker_id,
                    outcome=reconciliation["outcome"],
                    thread_id=reconciliation.get("thread_id"),
                    idempotency_key=f"{idempotency_key}:creation-reconciliation",
                    expected_version=None,
                )
                command_reconcile_worker_creation(connection, reconciliation_args)
            delivery = response.get("delivery")
            if delivery is not None:
                delivery_args = _namespace(
                    collaboration_id=envelope.collaboration_id,
                    worker_id=envelope.worker_id,
                    status=delivery["status"],
                    summary=delivery.get("summary"),
                    artifact_ref=delivery.get("artifact_ref"),
                    result_available=delivery.get("result_available", False),
                    note=delivery.get("note"),
                    actor="leader",
                    idempotency_key=f"{idempotency_key}:delivery",
                    expected_version=None,
                )
                command_set_delivery_status(connection, delivery_args)
            acknowledged_through = response.get("acknowledged_through_version")
            if acknowledged_through is not None:
                acknowledge_args = _namespace(
                    collaboration_id=envelope.collaboration_id,
                    worker_id=envelope.worker_id,
                    through_version=acknowledged_through,
                    idempotency_key=f"{idempotency_key}:ack",
                    expected_version=None,
                )
                command_acknowledge(connection, acknowledge_args)
            failure = response.get("failure")
            if failure is not None:
                failure_args = _namespace(
                    collaboration_id=envelope.collaboration_id,
                    worker_id=envelope.worker_id,
                    category=failure["category"],
                    message=failure["message"],
                    source_ref=failure.get("source_ref"),
                    idempotency_key=f"{idempotency_key}:failure",
                    expected_version=None,
                )
                command_record_failure(connection, failure_args)
            return {"action": envelope.action, "worker_id": envelope.worker_id, "response": response}
        finally:
            connection.close()


def _namespace(**values: Any) -> Any:
    """Small argparse-compatible object for the state command functions."""
    return type("CommandArguments", (), values)()
