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
