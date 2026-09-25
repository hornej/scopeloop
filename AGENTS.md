# ScopeLoop workflow

- Inspect Git status, configured host, actual runtime path and resource ownership
  before editing or using hardware. Preserve other worktrees and running tests.
- Follow the user's existing authorization. Build/test/publish/deploy requests do
  not imply DUT flashing, rail changes, USB hub resets or instrument firmware updates.
- Obtain an explicit idle handoff from the current bench owner before deployment
  or instrument access. Silence and an empty process list are not a handoff.
- Use the existing resource manager, driver leases and session/evidence formats.
  Hold ownership across a whole test. Never preempt an owner by client priority.
- Keep portable instrument behavior here, product mappings/results in the product
  repository, and host addresses, serials and topology in private configuration.
- Read [scope evidence](docs/scope-capture.md), [logic evidence](docs/logic-capture.md)
  [viewer exports](docs/waveform-viewers.md), and [host updates](docs/bench-operations.md)
  for the affected path. The focused
  [skill](skills/scopeloop-bench/SKILL.md) routes the same human-readable procedures.
- Replay real response shapes offline, test failure paths and run `git diff --check`.
  Test the installed wheel and MCP discovery on the named hosts. Report exact
  revisions/hashes, runtime dependencies, live tests and remaining physical limits.

Do not describe telemetry variation as AC ripple, an armed shot as a capture,
a sidecar as a native label, or a successful software test as product qualification.
