# DSH compatibility record

This record is based on local read-only inspection and offline plugin tests. It does
not read or print profile credentials and does not imply a live model request.

## Checked baselines

| Baseline | Evidence | Observed contract |
| --- | --- | --- |
| DSH CLI rc.8 | `/Users/Admin/Work/worktrees/deepseek-harness-visible-team/package.json`, tag `dsh-v0.1.0-rc.8` | public packages are `0.1.0-rc.8`; Host `apiProxy` and Client `slots`/`sessions` are Cordis services/faces |
| Host API | `/Users/Admin/Work/worktrees/deepseek-harness-visible-team/packages/host/apiproxy/src/index.ts` and `packages/host/apiproxy/README.md` | `ctx.apiProxy` is the public Host gateway; `sessions.prompt` is the send contract |
| Client slots | `/Users/Admin/Work/worktrees/deepseek-harness-visible-team/packages/client/ui-slots/src/index.ts`, `packages/client/runtime/src/client/slots.ts`, and `packages/extensions/cordis-client-runner/src/client/slot-catalog.ts` | slot contributions use `ctx.slots.inject`/`ctx.slots.register`; `conversation.view` is the selected session-scoped view slot |
| DSH Desktop 2.0.2 | `/Applications/DSH Desktop.app/Contents/Resources/app.asar.unpacked/package.json` and `cordis.patch.yml` | bundled DSH packages are `0.1.1-rc.1`; classic browser module loading remains available |
| Community references | `/tmp/visible-dsh-audit.zYOg9L/subagents/package.json` and `/tmp/visible-dsh-audit.zYOg9L/workbench/package.json` | reference versions are `0.1.2` and `0.11.0`, both MIT |

## Version boundary and smallest correction

The CLI source baseline (`0.1.0-rc.8`) and Desktop bundle (`0.1.1-rc.1`) are not
assumed to be binary-identical. The plugin therefore has no runtime imports from DSH
packages or private source paths. It uses small structural interfaces and concentrates
version-sensitive behavior in the DSH Host/Client adapters:

- Host requires only the public `webServer` route face and optionally binds the public
  `apiProxy` service. If the service or `sessions.prompt` face is absent, the DSH driver
  is not registered and sends return `capability-unavailable`; attach-existing remains
  available.
- Client requires the public `slots` and `sessions` injection face and contributes only
  `conversation.view`. It does not depend on compiled DOM names or top-level shell
  ownership.
- The package bundle uses the public classic `window.__ModuleLoader__.load` carrier
  pattern observed in Desktop 2.0.2 and leaves React to the host runtime.

This is the minimum correction for the rc.8/rc.1 gap: probe capabilities at the
adapter boundary and fail closed. A future release can add a versioned capability
probe without changing core state or the action contract.

## Offline verification status

`pnpm test` covers both fake Host cases (`apiProxy` present and absent), the public
Client `slots`/`sessions` injection declaration, target-only context, bootstrap
delivery, same-space Leader validation, native-session ownership, usage accounting,
the migration from global observation IDs to `(agent, source, observationId)` scope,
and unavailable Agent creation. `pnpm build` emits the Host package and classic
browser Client bundle.

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
