# NOTICE

`dsh-visible-team` is released under the MIT License. The root project attribution
for the Visible Team collaboration concept is in the repository-level `NOTICE.md`.

The following open-source projects informed the plugin boundary and are retained as
references rather than vendored runtimes:

- [`dsh-workbench`](https://github.com/lee259/dsh-workbench), MIT, version `0.11.0`.
  Visible Team follows its public Cordis bundle insertion, Client entry, and Slot
  registration shape. It does not copy the file editor, file tree, terminal, diff
  capture, or workspace-file state.
- [`dsh-market`](https://github.com/dsh-market/dsh-market), MIT. Its public
  `tsdown.config.ts` is the primary reference for the virtual-id CSS Modules,
  `lightningcss` transform, stable desktop browser targets, class-map emission, and
  plugin-owned style tag shape used by this Client bundle. Visible Team adapts that
  build pattern and adds an explicit effect disposer for its style tag; it does not
  vendor dsh-market source or runtime behavior.
- [`@deepseek-ai/dsh-client-ui-primitives`](https://github.com/deepseek-ai/deepseek-harness/tree/main/packages/client/ui-primitives), MIT,
  version `0.1.1-rc.1`. The workbench consumes its public Button, Input, Tooltip,
  outside-pointer hook, icons, and theme-token contract as a host-provided external;
  it does not vendor the package.
- [`dsh-plugin-subagents`](https://github.com/Luck9Star/dsh-plugin-subagents), MIT,
  version `0.1.2`. Visible Team follows its provider/bridge seam and explicit
  capability boundary. It does not vendor its subagent tools, relay runtime, role
  catalog, queue, subprocess management, or database.

No source file from any referenced project is bundled into this package. Their names are
listed here so the architectural and implementation references remain attributable;
their original license terms apply to their respective repositories.
