# DSH compatibility record

This record is based on local read-only inspection and offline plugin tests. It does
not read or print profile credentials and does not imply a live model request.

## Checked baselines

| Baseline | Evidence | Observed contract |
| --- | --- | --- |
| DSH CLI rc.8 | `/Users/Admin/Work/worktrees/deepseek-harness-visible-team/package.json`, tag `dsh-v0.1.0-rc.8` | public packages are `0.1.0-rc.8`; Host `apiProxy` and Client `slots`/`sessions` are Cordis services/faces |
| Host API | `/Users/Admin/Work/worktrees/deepseek-harness-visible-team/packages/host/apiproxy/src/api/sessions.ts` and `packages/host/apiproxy/src/api/events.ts` | `ctx.apiProxy` exposes public `sessions.list/create/selectModel/prompt/history` and `events.mux` faces used by the DSH Driver |
| Model tools | [public DSH `dsh-tools` package](https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/core/tools), [tool cookbook](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/cookbook/adding-a-tool.zh.md), and the pinned `@deepseek-ai/dsh-tools@0.1.1-rc.1` peer/dev dependency | `defineTool({ name, description, parameters, output, execute, presentCall?, presentResult? })` followed by `ctx.tools.register`; `exec.agent.id` is the caller identity; output/presentation stay Host-owned |
| Client slots | `/Users/Admin/Work/worktrees/deepseek-harness-visible-team/packages/client/ui-slots/src/index.ts`, `packages/client/runtime/src/client/slots.ts`, and `packages/extensions/cordis-client-runner/src/client/slot-catalog.ts` | slot contributions use `ctx.slots.inject`/`ctx.slots.register`; `conversation.view` is session-scoped and `sidebar.footer.action` is the root/list footer entry |
| DSH Desktop 2.0.2 | `/Applications/DSH Desktop.app/Contents/Resources/app.asar.unpacked/package.json` and `cordis.patch.yml` | bundled DSH packages are `0.1.1-rc.1`; classic browser module loading remains available |
| DSH UI primitives | Desktop's public `@deepseek-ai/dsh-client-ui-primitives@0.1.1-rc.1` module table entry | the workbench uses public `Button`, `Input`, `Tooltip`, `useDismissOnOutsidePointer`, icons, and `--dsw-*` theme tokens |
| Community references | `/tmp/visible-dsh-audit.zYOg9L/subagents/package.json` and `/tmp/visible-dsh-audit.zYOg9L/workbench/package.json` | reference versions are `0.1.2` and `0.11.0`, both MIT |

## Version boundary and smallest correction

The CLI source baseline (`0.1.0-rc.8`) and Desktop bundle (`0.1.1-rc.1`) are not
assumed to be binary-identical. The plugin therefore has no runtime imports from DSH
private source paths. It uses small structural interfaces and concentrates
version-sensitive behavior in the DSH Host/Client adapters:

- Host requires only the public `webServer` route face and optionally binds the public
  `apiProxy` service. If the service is absent, existing native attachments remain
  state-only. When the service is present, the DSH driver advertises each available
  public face independently; missing `sessions.*`/`events.mux` faces return
  `capability-unavailable` and are never emulated.
- DSH native calls require an explicit provider/model/thinking/permission route. The
  `nativeProvider` is persisted with the Agent binding so a later send/resume does not
  silently switch provider. DSH's public prompt API has no per-prompt permission field;
  the adapter validates and records the explicit boundary, and reports that forwarding
  limitation in discovery evidence.
- Client requires the public `slots` and `sessions` injection face and contributes
  `conversation.view` plus `sidebar.footer.action`. The latter receives only the public
  `{ wide }` owner state and keeps a recognizable icon/tooltip in the collapsed rail. It
  does not depend on compiled DOM names or top-level shell ownership.
- The package bundle uses the public classic `window.__ModuleLoader__.load` carrier
  pattern observed in Desktop 2.0.2, leaves React and DSH UI primitives to the host
  runtime, and compiles its CSS Modules with the virtual-id/lightningcss pattern used by
  MIT `dsh-market`. Class maps are sorted for stable output; generated style tags carry
  `data-plugin="dsh-visible-team"` and are removed by the client effect disposer when the
  plugin fiber is torn down.
- The model entry uses the public `dsh-tools` definition/registration face and binds it
  through optional `ctx.inject(["tools"], ...)`. It does not import DSH private source,
  instantiate a second ToolRuntime, or implement its own approval pipeline. The official
  ToolRuntime continues to own pre-execute approval/gating and UI presentation plumbing.

## Model-tool capability boundary

`visible_team` resolves a caller only from `exec.agent.id` and the Store's exact
`(provider="dsh", nativeSessionId)` binding query. The query returns the attached Agent,
whose stored workspace is then the only workspace in scope. A supplied `workspaceId` is
accepted only as an equality check; it cannot select another space. This is intentionally
fail-closed for a host that cannot provide a reliable native-session identity.

| Caller | Allowed operations | Explicitly denied |
| --- | --- | --- |
| Bound workspace Leader | short workspace list/read; own or same-workspace pending reads; send/deliver to existing same-workspace Agents; short targeted progress | other workspaces, unknown Agents, Agent creation, permission changes, broadcast |
| Bound ordinary member | own pending target-scoped read; short progress targeted to the Leader | workspace projection, another Agent's context, send/deliver, other workspaces |
| Unbound/non-DSH/missing identity | none | every model-tool operation |

Successful model results are bounded projections rather than `TeamWorkspace` snapshots:
at most 24 Agent rows, 8 pending packets, 1,000 characters per pending summary, and
2,000 characters per progress update. Results carry versions/counts and `truncated`; they
omit `sharedRules`, full context targets, native session logs, and direct message bodies.
`send_message` is capped at 8,000 characters. These caps keep prompt cost proportional
to the model's explicit read and prevent repeated full-workspace replay. Native provider
usage remains driver-owned; this adapter does not estimate or synthesize Token counts.

This is the minimum correction for the rc.8/rc.1 gap: probe capabilities at the
adapter boundary and fail closed. A future release can add a versioned capability
probe without changing core state or the action contract.

## Offline verification status

`pnpm test` covers both fake Host cases (`apiProxy` present and absent), the public
Client `slots`/`sessions` injection declaration, both client slot registrations and
their public injection payloads, target-only context, bootstrap
delivery, same-space Leader validation, native-session ownership, usage accounting,
the migration from global observation IDs to `(agent, source, observationId)` scope,
unavailable Agent creation, and the offline DSH Driver matrix over attach/create,
send/resume, status/watch, open, and usage, plus the `visible_team`
registration/identity/capability contract. `pnpm build` emits the Host package and
classic browser Client bundle.

On 2026-08-28, the final check used the Desktop bundle's CLI at
`/Applications/DSH Desktop.app/Contents/Resources/app.asar.unpacked/node_modules/@deepseek-ai/dsh/lib/bin.js`
and installed the local package into the temporary Profile
`/var/folders/5b/c7nwjw4j23v4_87ft5jhv5w80000gp/T/visible-team-final-link.a8eIbb0HYP/home/profiles/isolated`.
The resulting manifest contained the local `dsh-visible-team` dependency and the
bundle list `@deepseek-ai/dsh-base, dsh-visible-team`; `--dump-config` rendered the
`visible-team` patch entry. A following `plugin ... remove dsh-visible-team` left
only `@deepseek-ai/dsh-base`. Installation and removal touched only that temporary
Profile and did not start a model session. The formal Desktop Profile was not used.

The packed artifact was also installed into a second temporary Profile
`/var/folders/5b/c7nwjw4j23v4_87ft5jhv5w80000gp/T/visible-team-final-tar.kiT2qLPXMB/home/profiles/packed`
from `dsh-visible-team-0.1.0.tgz`; its `--dump-config` likewise rendered the
`visible-team` layer and removal left the base layer. This confirms the published
`files` set contains the bundle patch, Host/Client output, manifest, and notices
rather than relying on a source checkout link.
