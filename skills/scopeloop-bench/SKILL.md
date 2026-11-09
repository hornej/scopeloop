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
- For Hantek 2000 USB, read `docs/hantek.md`. Use the shared scope/DMM/AWG lease,
  bounded output runs and hashed evidence. Output readbacks are setpoints, not
  physical enable state. Scope voltage exports require a matching saved zero
  reference and remain nominal; do not claim calibrated ripple or single-shot freshness.
- For Saleae, read `docs/logic-capture.md`. Reuse the recipe service. Preserve the
  original SAL, make a labeled copy, reopen/resave it in Logic, and verify native
  labels before claiming them. A sidecar alone is not a labeled native capture.
- For ownership, enumeration or deployment, read `docs/bench-operations.md`.
  Reuse resource/session mechanisms; never reset a shared hub or preempt a test.
  Distinguish absent device, enumeration failure and application unresponsiveness.
- For the HMP4040, read `docs/power-supply.md`. Start with identity and a DC
  snapshot. Setpoints and channel selection require authorization for the
  specific DUT and channel. The master defaults to the front panel. In projects
  that opt in, use the master preview and operator-confirmed apply workflow.
  Show the exact affected rails and setpoints. A preview token alone is not approval.
- For MAX31856 thermocouple capture or fault recovery, read docs/thermocouples.md.
  Preserve fault evidence, stop the DUT load on invalid data, and require an
  explicit idle acknowledgement before reinitializing. Recovery never re-arms a DUT.
- For ngscopeclient or PulseView, read `docs/waveform-viewers.md`. Export saved,
  verified evidence offline. Preserve sample timing and source limits; third-party
  hardware control needs the same explicit handoff as other bench clients.

Keep product-specific pin maps, calibration and qualification in the product
repository. Keep host addresses/serials in private config. Verify installed CLI
and MCP behavior and report versions, hashes and remaining physical validation.
