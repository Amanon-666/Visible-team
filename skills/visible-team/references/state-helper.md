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

## Commands

Run commands from the plugin root. Add `--pretty` when human-readable JSON is useful.

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

python3 scripts/visible_team_state.py --db <chosen-db> snapshot \
  --collaboration-id <stable-id>
```

`--target all` is available for a genuinely global change. Prefer explicit affected Worker IDs.

Use `set-worker-status` and `set-collaboration-status` for lifecycle changes. Pass `--expected-version` when a stale writer must be rejected.

