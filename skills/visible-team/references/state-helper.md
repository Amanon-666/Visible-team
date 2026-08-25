# Durable collaboration state

Read this reference only when a collaboration is likely to span stages, context compaction, separate Leader tasks, or multiple Workers. Ordinary short work should use the host task history directly.

The bundled helper is a dependency-free Python CLI backed by SQLite. The Leader chooses an accessible database path for the current project or collaboration; the Skill does not prescribe one.

## Invariants

- Choose a stable collaboration ID before dispatch.
- Choose a stable logical Worker ID before creating its visible task.
- Record the Worker plan, call the host's task-creation tool, then attach the returned thread ID.
- Give every mutation a stable idempotency key. Repeating the same mutation returns the existing state instead of duplicating it.
- Use the collaboration version for compare-and-set when concurrent or stale updates are plausible.
- Add context with explicit target Worker IDs. Query and acknowledge pending updates per Worker rather than broadcasting.
- The database is a coordination ledger, not a substitute for the host's visible task tools. Reconcile it with actual task records after an uncertain host result.
- Worker lifecycle completion is separate from delivery acceptance. A completed Worker must have a submitted delivery with a summary or result/artifact reference; a host task that is complete but has no verifiable result is recorded as `needs_attention`.
- Failure categories are advisory state, not retry instructions. `transient` is retryable in principle, but the Leader must explicitly trigger any host retry; the helper and coordinator never loop or retry automatically.
- Host observations are facts supplied by the Leader or a host adapter. The SQLite helper never queries or claims to query Codex.

## Commands

Run commands from the plugin root. Add `--pretty` when human-readable JSON is useful.

Opening the helper runs the explicit migration. It can also be checked directly:

```bash
python3 scripts/visible_team_state.py --db <chosen-db> migrate
```

```bash
python3 scripts/visible_team_state.py --db <chosen-db> init \
  --collaboration-id <stable-id> \
  --objective <objective> \
  --leader-thread-id <thread-id>

python3 scripts/visible_team_state.py --db <chosen-db> plan-worker \
  --collaboration-id <stable-id> \
  --worker-id <logical-worker-id> \
  --title <title> \
  --model <model> \
  --thinking <effort> \
  --responsibility <scope> \
  --idempotency-key <stable-operation-id>

python3 scripts/visible_team_state.py --db <chosen-db> attach-worker \
  --collaboration-id <stable-id> \
  --worker-id <logical-worker-id> \
  --thread-id <created-thread-id> \
  --idempotency-key <stable-operation-id>

python3 scripts/visible_team_state.py --db <chosen-db> reconcile-worker-creation \
  --collaboration-id <stable-id> --worker-id <logical-worker-id> \
  --outcome missing|retry|attached [--thread-id <thread-id>] \
  --idempotency-key <stable-operation-id>

python3 scripts/visible_team_state.py --db <chosen-db> update-worker-config \
  --collaboration-id <stable-id> \
  --worker-id <logical-worker-id> \
  --model <model> --thinking <effort> \
  --idempotency-key <stable-operation-id>

python3 scripts/visible_team_state.py --db <chosen-db> add-context \
  --collaboration-id <stable-id> \
  --summary <changed-decision-or-evidence> \
  --source-ref <optional-artifact-reference> \
  --target <affected-worker-id> \
  --idempotency-key <stable-operation-id>

python3 scripts/visible_team_state.py --db <chosen-db> pending \
  --collaboration-id <stable-id> \
  --worker-id <logical-worker-id>

python3 scripts/visible_team_state.py --db <chosen-db> acknowledge \
  --collaboration-id <stable-id> \
  --worker-id <logical-worker-id> \
  --through-version <version> \
  --idempotency-key <stable-operation-id>

python3 scripts/visible_team_state.py --db <chosen-db> set-delivery-status \
  --collaboration-id <stable-id> --worker-id <logical-worker-id> \
  --status submitted --summary <short-result-summary> \
  --artifact-ref <optional-readable-reference> \
  --idempotency-key <stable-operation-id>

python3 scripts/visible_team_state.py --db <chosen-db> record-observation \
  --collaboration-id <stable-id> --worker-id <logical-worker-id> \
  --task-exists yes --host-status <host-state> --result-available yes \
  --lease-until <optional-iso-time> --idempotency-key <stable-operation-id>

python3 scripts/visible_team_state.py --db <chosen-db> record-failure \
  --collaboration-id <stable-id> --worker-id <logical-worker-id> \
  --category transient --message <what-failed> \
  --idempotency-key <stable-operation-id>

python3 scripts/visible_team_state.py --db <chosen-db> resume \
  --collaboration-id <stable-id>

python3 scripts/visible_team_state.py --db <chosen-db> snapshot \
  --collaboration-id <stable-id>

python3 scripts/visible_team_usage.py --db <chosen-db> \
  --collaboration-id <stable-id> [--codex-home <codex-home>] [--json]
```

`visible_team_usage.py` 只读宿主 rollout 的最新 `token_count`；rollout 明细不可用时才使用 `state_*.sqlite` 的总量回退，不显示价格、额度或限制。

`--target all` is available for a genuinely global change. Prefer explicit affected Worker IDs.

Use `set-worker-status` and `set-collaboration-status` for lifecycle changes. Pass `--expected-version` when a stale writer must be rejected.

Delivery states advance as `pending → submitted → received → accepted`; a
Leader may request revision or mark `needs_attention`. `needs_attention` cannot
skip back to `accepted`: first obtain a verifiable result and submit/receive it.
An adapter creation request is durably reserved before the host call. If the
host result is uncertain, `resume` exposes `reconcile_creation`; the Leader must
confirm `missing`/`retry` or `attached` before another create request is allowed.
The optional `scripts/visible_team_coordination.py` module provides a small
`HostAdapter` protocol and `ActionEnvelope`; it does not implement a Codex API,
background polling, or automatic retries.
