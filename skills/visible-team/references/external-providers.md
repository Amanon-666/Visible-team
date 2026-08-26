# External provider adapters

Read this reference only when the user explicitly asks to allocate a Worker to
an external application or CLI such as Antigravity or DeepSeek Harness.

## Safety and allocation

- External providers are disabled by default. Planning an external Worker does
  not authorize dispatch.
- Confirm the provider, exact model, reasoning/thinking setting, permission or
  sandbox mode, responsibility, and any material quota boundary before start.
- Record that confirmation with `authorize-worker`. The coordinator will not
  emit `create_worker` for an unauthorized external Worker.
- Never substitute another provider, model, or reasoning setting silently. An
  unavailable capability is `unavailable`, not an inferred success.
- Development and discovery should use offline capability checks and injected
  fake runners. Do not send a real prompt merely to test an adapter.

## Common provider contract

The durable ledger uses one logical Worker identity across providers:

- `provider`: `codex`, `antigravity`, or `deepseek-harness`;
- `thread_id`: Codex-visible task ID when the provider is Codex;
- `native_task_id`: the provider's verified conversation/session/task ID;
- `native_open_ref`: an optional provider-native reference that may help the
  user open the session; its presence does not prove a visible window;
- `model`, `thinking`, and `permission_mode`: the exact approved allocation;
- `latest_usage`: the latest provider-native token counters, with unknown
  fields left unavailable rather than estimated.

Adapters receive provider-neutral mechanical actions and return observed facts.
They use argument arrays, never shell interpolation. A provider should expose
offline discovery and an honest capability map. Start, continue, observe,
cancel, reopen, and usage are independent capabilities; supporting one does not
imply the others.

The supplied adapters deliberately do not run in a background daemon. The
Leader invokes them through the coordinator after authorization, persists the
native identity and usage, and performs bounded observation when useful.

Antigravity's installed CLI accepts the explicit model, thinking, and
permission values used by this bridge. The inspected DeepSeek Harness SDK does
not currently expose per-prompt model, thinking, or permission parameters. Its
adapter therefore validates and records the approved route, while the native
runtime remains responsible for its configured model and policy. Before
authorizing DSH dispatch, make this limitation visible to the user and require
acceptance of that provider-managed boundary. Never report the recorded values
as provider-confirmed settings unless the native runtime later supplies such
evidence.

## State commands

Plan an external Worker without authorizing it:

```bash
python3 scripts/visible_team_state.py --db <chosen-db> plan-worker \
  --collaboration-id <stable-id> --worker-id <worker-id> \
  --title <title> --provider antigravity|deepseek-harness \
  --model <exact-model> --thinking <exact-effort> \
  --permission-mode <exact-mode> --responsibility <scope> \
  --idempotency-key <stable-operation-id>
```

After the user confirms that exact allocation:

```bash
python3 scripts/visible_team_state.py --db <chosen-db> authorize-worker \
  --collaboration-id <stable-id> --worker-id <worker-id> \
  --approval-note <concise-user-confirmation> \
  --idempotency-key <stable-operation-id>
```

Bind only an ID observed from the provider:

```bash
python3 scripts/visible_team_state.py --db <chosen-db> attach-provider-worker \
  --collaboration-id <stable-id> --worker-id <worker-id> \
  --native-task-id <verified-native-id> \
  --native-open-ref <optional-native-reference> \
  --idempotency-key <stable-operation-id>
```

Persist native usage without guessing missing counters:

```bash
python3 scripts/visible_team_state.py --db <chosen-db> record-usage \
  --collaboration-id <stable-id> --worker-id <worker-id> \
  --source <native-source> --input-tokens <count> \
  --output-tokens <count> --total-tokens <count> \
  --idempotency-key <stable-operation-id>
```

## External bridge

Offline discovery is always safe and does not start a model session:

```bash
python3 scripts/visible_team_external.py --provider antigravity discover
python3 scripts/visible_team_external.py --provider deepseek-harness \
  --dsh-sdk-path <deepseek-harness/python/sdk/src> discover
```

After planning and authorizing one exact external Worker in the ledger, execute
only its next mechanical action:

```bash
python3 scripts/visible_team_external.py \
  --provider antigravity|deepseek-harness --cwd <workspace> execute \
  --db <chosen-db> --collaboration-id <stable-id> --worker-id <worker-id> \
  --idempotency-key <stable-operation-id> \
  --confirm-authorized-dispatch
```

When the official DSH Python SDK is present only as a source checkout, pass its
`python/sdk/src` directory with `--dsh-sdk-path`. This adds that one validated
source directory to the bridge process; it does not copy or vendor the Harness.

The confirmation switch is required only for `create_worker`; continuation and
observation still require the Worker to exist in durable state with the same
provider. The bridge executes one pending action and exits. It does not poll in
the background, switch providers, or retry uncertain requests.

## Current native visibility boundary

Codex Workers use Codex's own visible project tasks. Antigravity and DeepSeek
Harness adapters preserve their native conversation/session IDs and optional
open references. Whether those sessions appear as clickable windows depends on
the installed provider version and profile. Until verified from host facts,
report native visibility as `unavailable` or unverified; do not promise that a
CLI-created session is automatically visible in the desktop application's UI.
