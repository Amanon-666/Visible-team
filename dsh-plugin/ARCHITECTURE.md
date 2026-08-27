# Visible Team plugin architecture

## Boundary

The plugin is an independent DSH package. It adds no patch to DSH source and does
not import a DSH `src` path. The package has a Host entry (`src/index.ts`) and a
browser Client entry (`src/client/entry.tsx`), plus the public `dsh.client` metadata
and `cordis.patch.yml` bundle insertion.

```text
DSH public services / slots
        │
        ├─ Host adapter: webServer + optional apiProxy.sessions.prompt
        │                 + replaceable AgentDriver seam
        │
        └─ Client adapter: required slots + sessions
                           → conversation.view / Team tab
                              │
                              └─ HTTP/SSE ── Host action contract
                                               │
                                               ├─ transport-neutral core types
                                               └─ plugin-owned SQLite store
```

The core does not know DSH directory Workspaces or DSH session internals. A
workspace may carry an opaque `hostBinding`, while each Agent carries a provider,
native session/task identity, and optional open reference. The DSH adapter is the
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
`attach-agent` only records an already existing native identity and may be used
without a sending driver.

## Cordis injection decisions

The Host export has `inject = ["webServer"]`, because attachment and persistence are
valid without DSH's API gateway. Inside `apply`, it declares the optional public
service with `ctx.inject(["apiProxy"], callback)`. The callback reads the injected
`apiProxy` property and registers the DSH driver only when
`apiProxy.sessions.prompt` has the expected public face. This permits an attach-only
degradation while avoiding an undeclared `ctx.get("apiProxy")` dependency.

The Client export has `inject = ["slots", "sessions"]`. It uses `ctx.slots.inject`
and `ctx.slots.register` for `conversation.view`; the per-session slot injection
receives the current session ID and the public `sessions.open` action. No compiled DOM
class, document selector, internal file, or sidebar replacement is involved.

## Driver policy

`AgentDriver.send` is the single control path used by direct user commands and
context delivery. A future Leader model tool must call the same Host action contract;
it must not own a second queue or prompt path. The built-in DSH driver sends one
`apiProxy.sessions.prompt` request per action and records no usage unless a driver
returns native counters. It never estimates tokens and never relays through another
model.

## Intentionally excluded

The plugin does not own DSH Workspaces, native Session lifecycle, file editing,
terminal execution, worktrees, role catalogs, model routing, automatic broadcasting,
or an autonomous Leader model tool. `dsh-workbench` is a UI/bundle layout reference;
`dsh-plugin-subagents` is an optional future driver/reference, not a core state owner.
