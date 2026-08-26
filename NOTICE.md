# Upstream attribution

This project is a focused derivative of
[`KeonSuYun/codex-team-orchestrator`](https://github.com/KeonSuYun/codex-team-orchestrator),
reviewed at revision `2d062cddec294394611390db19c142dcb0711910` and distributed
under the MIT License included in this directory.

The derivative retains the upstream project's central idea that collaborators
can be ordinary, user-visible Codex project tasks and can continue working
through task-to-task messages.

It intentionally removes fixed role catalogs, team rosters, task boards, goal
handshakes, status-packet protocols, scheduled monitoring, and domain-specific
assignment templates. In their place it keeps a small, general contract: the
current task remains the Leader, suitable visible workers receive task-specific
context, and the Leader coordinates meaningful follow-ups through completion.

The SQLite collaboration-state helper and its selective context-delivery model
are original additions in this derivative. They provide optional deterministic
state without reintroducing the upstream project's fixed team structure.

The external-provider boundary was informed by two additional open-source
projects without importing their full runtimes:

- Orkestra (`andyyaro/orkestra`, Apache-2.0): small adapter metadata,
  argv-based invocation, incremental structured-output parsing, native session
  references, and explicit error/usage normalization.
- AGPair (`logicrw/agpair`, MIT): minimal provider lifecycle and honest
  capability declarations for start, continuation, observation, and cleanup.

The Antigravity adapter uses the installed `agy` CLI's documented
`stream-json` protocol. The DeepSeek Harness adapter wraps its official local
Python SDK and protocol types. Both remain thin translations into Visible
Team's provider-neutral state; neither vendors the upstream orchestration,
worktree, role, queue, or runtime implementations.
