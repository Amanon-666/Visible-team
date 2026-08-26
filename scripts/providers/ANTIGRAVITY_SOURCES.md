# Antigravity adapter sources

This provider is an original, small adapter. It vendors no third-party code.

- The `agy` stream-json event/input shapes, status values, permission behavior,
  and token fields were checked against the official headless CLI documentation:
  <https://www.agy.dev/docs/cli/headless/>.
- Orkestra was consulted for the idea of a narrow CLI lifecycle adapter and
  explicit effort validation: <https://github.com/andyyaro/orkestra>.
  Orkestra is Apache-2.0 licensed; no code was copied.
- AGPair was consulted for its external CLI executor boundary:
  <https://github.com/logicrw/agpair>. AGPair is MIT licensed; no code was
  copied.
- Agent-intern was consulted for a lightweight stream-json parser shape:
  <https://github.com/SinanTufekci/agent-intern>. Agent-intern is MIT licensed;
  no code was copied.

The local `/Users/Admin/.gemini/bin/agy` version/help output was inspected for
version 1.1.21 and supported flags. No model request was sent and no
Antigravity quota was consumed. The adapter only implements discovery, argv
construction, one-shot start/resume, stream parsing, and usage normalization.
Persistent watch/read, interrupt/cancel, native open, and account-quota usage
therefore return an explicit `unavailable` result.
