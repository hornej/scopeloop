---
name: scopeloop-bench
description: Acquire and verify ScopeLoop scope or Saleae evidence, diagnose bench device identity, and update ScopeLoop runtimes. Use for portable instrument workflows; product mappings and private host topology stay in their own repositories or configuration.
---

# ScopeLoop bench

Locate the active ScopeLoop repository and read its `AGENTS.md`. Inspect the
actual instrument host and runtime; a desktop checkout may not run the tools.
Honor authorization already given and preserve other tests and dirty worktrees.
Require explicit handoff from the current bench owner before live access or a
deployment switch. Staging and offline tests can proceed independently.

- For scope acquisition, read `docs/scope-capture.md`. Use fresh AUTO for DC/ripple,
  SINGLE for an external event, and save raw data with setup, identity and hashes.
  Reject empty/truncated data. Record clipping, noise floor and probing limits.
- For Saleae, read `docs/logic-capture.md`. Reuse the recipe service. Preserve the
  original SAL, make a labeled copy, reopen/resave it in Logic, and verify native
  labels before claiming them. A sidecar alone is not a labeled native capture.
- For ownership, enumeration or deployment, read `docs/bench-operations.md`.
  Reuse resource/session mechanisms; never reset a shared hub or preempt a test.
  Distinguish absent device, enumeration failure and application unresponsiveness.

Keep product-specific pin maps, calibration and qualification in the product
repository. Keep host addresses/serials in private config. Verify installed CLI
and MCP behavior and report versions, hashes and remaining physical validation.
