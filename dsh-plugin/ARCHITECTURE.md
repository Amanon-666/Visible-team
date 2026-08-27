# Visible Team plugin architecture

## Boundary

The plugin is an independent DSH package. It adds no patch to DSH source and does
not import a DSH `src` path. The package has a Host entry (`src/index.ts`) and a
browser Client entry (`src/client/entry.tsx`), plus the public `dsh.client` metadata
and `cordis.patch.yml` bundle insertion.

```text
DSH public services / slots
        │
        ├─ Host adapter: webServer + optional public apiProxy
        │                 sessions.list/create/selectModel/prompt/history + events.mux
        │                 + optional DSH tools registration
        │                 + replaceable AgentDriver seam
        │
        └─ Client adapter: required slots + sessions
                           ├─ sidebar.footer.action / workbench
                           └─ conversation.view / Team tab
                              │
                              └─ HTTP/SSE ── Host action contract
                                               │
                                               ├─ transport-neutral core types
                                               ├─ plugin-owned SQLite store
                                               └─ visible_team model adapter
```

The core does not know DSH directory Workspaces or DSH session internals. A
workspace may carry an opaque `hostBinding`, while each Agent carries a driver
provider, optional native model-provider route, native session/task identity, and
optional open reference. The DSH adapter is the
only layer that interprets `apiProxy` or DSH session opening.

## State and ownership

- `VisibleTeamStore` owns only the plugin database and its migrations.
- `leaderAgentId` is a mutable same-workspace reference, not a role or transport
  owner.
- `vt_agent_native_owner_idx` prevents the same `(provider, nativeSessionId)` from
  acquiring two bridge owners across spaces.
- Every new Agent insert also inserts one target-only bootstrap packet and increments
  the workspace/context version in the same `BEGIN IMMEDIATE` transaction. Re-attaching
  the same native identity is idempotent and does not generate another bootstrap.
- `add-context` reads and increments the workspace version only after acquiring the
  write lock. It requires a non-empty explicit target list.
- Delivery reads pending targets for exactly one Agent, calls that Agent's driver once,
  and acknowledges only after a successful send. A direct `send-agent` never injects
  pending packets, so objective/rules are not repeated on every command.
- Usage observations require an idempotency key and declare `delta` or `cumulative`.
  Delta streams are summed; cumulative streams use their latest snapshot per source.
  Missing native totals remain null; no cost or token estimate is synthesized. The
  idempotency scope is `(agent_id, source, observation_id)`, so native IDs stay
  provider-native even when two Agents both report `turn-1`. Existing databases with
  the old global `UNIQUE(observation_id)` are rebuilt by the plugin migration.

Global objective/rule edits are durable workspace state only. They do not auto-create
a broadcast packet. To notify existing Agents, the user or Leader must add a new
Context Packet with explicit target IDs.

## Public Host contract

The Host registers:

| Endpoint | Meaning |
| --- | --- |
| `GET/POST /api/visible-team/workspaces` | list/read and execute `WorkspaceAction` |
| `GET /api/visible-team/context?workspace=&agent=` | target-scoped context read |
| `GET /api/visible-team/events` | workspace change SSE for the Client |

`create-agent` is deliberately not simulated. A provider must supply
`AgentDriver.create`; otherwise the Host returns `409 capability-unavailable`.
The built-in DSH driver calls the public `sessions.create` and
`sessions.selectModel` faces only after an explicit native route is present.
`attach-agent` can remain state-only for custom drivers, while an attach-capable
driver may verify the native identity through its provider's discovery face.

When the public DSH `tools` service is available, the Host also registers one
`visible_team` ToolDefinition through the official `defineTool`/`ToolRuntime.register`
face. The model adapter is deliberately thin: it resolves `exec.agent.id` through
`VisibleTeamStore.findAgentByNativeSession("dsh", ...)`, derives that Agent's only
workspace, and then delegates writes to the same `WorkspaceAction` executor used by
HTTP. No request-provided `workspaceId` can establish identity.

The model surface is capability-checked on every call. A non-Leader may read only its
own pending target-scoped packets and submit a short packet to the Leader. Only the
same-workspace `leaderAgentId` may inspect the workspace projection or send/deliver to
another existing Agent. Unattached identities, non-DSH provider bindings, mismatched
workspace ids, unknown targets, and missing Leaders fail closed. The returned projection
contains only bounded summaries, counts, and versions; it omits full context targets,
shared rules, and native session data.

## Cordis injection decisions

The Host export has `inject = ["webServer"]`, because attachment and persistence are
valid without DSH's API gateway. Inside `apply`, it declares the optional public
service with `ctx.inject(["apiProxy"], callback)`. The callback reads the injected
`apiProxy` property and registers a DSH driver with a per-face capability matrix;
missing `sessions.*` or `events.mux` faces remain unavailable instead of being
emulated. This permits an attach-only degradation while avoiding an undeclared
`ctx.get("apiProxy")` dependency.

The model tool uses the same optional-service pattern with
`ctx.inject(["tools"], callback)`. The package declares `@deepseek-ai/dsh-tools` at the
Desktop `0.1.1-rc.1` compatibility floor and uses only the public tool definition,
output schema, generic presentation card, and registry registration methods. DSH's
own pre-execute approval pipeline remains the owner of approval policy; the plugin adds
only the per-call Store identity/Leader check and does not create a second approval
state machine.

The Client export has `inject = ["slots", "sessions"]`. It uses `ctx.slots.inject`
and `ctx.slots.register` for both `sidebar.footer.action` and `conversation.view`.
The footer entry consumes the public `{ wide }` owner state and the plugin-owned TeamClient;
the per-session Team tab receives the current session ID and the public `sessions.open`
action. The workbench uses DSH's public UI primitives and theme tokens, while its CSS
Modules follow the MIT `dsh-market` virtual-id/lightningcss pattern. No compiled DOM class,
internal file, or sidebar replacement is involved.

## Driver policy

`AgentDriver.send` is the single control path used by direct user commands and
context delivery. The Leader model tool calls the same Host action contract; it does
not own a second queue or prompt path. The built-in DSH driver selects the explicit
native model route, then sends one `apiProxy.sessions.prompt` request per action.
`resume` is the same public prompt path because DSH's host performs cold resume for a
durable session. `status`, `watch`, and `usage` are read-only native faces; usage
prefers the official `tokenUsage` history projection and otherwise folds only
provider-reported event counters. It never estimates tokens and never relays through
another model.

## Intentionally excluded

The plugin does not own DSH Workspaces, native Session lifecycle, file editing,
terminal execution, worktrees, role catalogs, model routing, automatic broadcasting,
or model-driven Agent creation/permission changes. `dsh-workbench` is a UI/bundle layout reference;
`dsh-plugin-subagents` is an optional future driver/reference, not a core state owner.
