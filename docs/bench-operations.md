# Bench ownership, diagnostics and updates

ScopeLoop serializes each complete driver workflow and holds an OS lease for each
connected scope endpoint or Logic automation server. The existing ResourceManager
also serializes clients, including OBSERVE, and no longer permits priority takeover.
Nested ownership belongs to a task as well as a client ID. Competing processes fail
with a resource-busy error before sending commands. Leases are released on process
exit; do not delete a locked file to take ownership.

Use one instrument host and one shared `SCOPELOOP_LOCK_DIR` for every ScopeLoop
process on it. The default is `~/.scopeloop/locks`. A multi-user service installation
needs a shared directory with operator/service group access. These are cooperative
host locks: raw vendor scripts, other hosts and front-panel actions do not obey them.
Keep those clients under the same explicit bench handoff. For multi-instrument
tests retain driver connections throughout the run, inside the existing session;
do not release ownership between configuration and capture.

## Device diagnostics

`scopeloop diagnose-device --serial SERIAL` reports `present`, `absent`,
`ambiguous`, or `enumeration_failed` without opening a serial port. Linux also
supports `--by-id /dev/serial/by-id/...`. Configure serial and/or by-id when the
VID/PID is shared. Ambiguous matching fails instead of choosing the first port.
`application_status: not_probed` is intentional: enumeration does not prove the
application is responding. A Logic connection that fails while USB enumeration
succeeds is a different layer from a missing USB device.

Inspect OS USB inventory and kernel/PnP errors for non-serial devices and descriptor
failures (`lsusb`, `/sys/bus/usb/devices`, journal, or Windows PnP status). An absent
serial match alone does not prove physical disconnection. Record tool exit status
and stderr; failed enumeration is not an empty inventory. Do not reset a shared hub
to recover one device. Preserve other instruments and capture evidence first.

## Release and rollback

1. Identify the actual host, running processes, service ExecStart/environment,
   imported module path, configuration and capture filesystem. Inspect local edits.
2. Build a wheel from a scoped commit and record its SHA-256. Install it into a new
   versioned virtualenv, preserving the previous environment. Pin tested dependencies
   with the release's constraints and record `pip freeze` and `pip check`.
3. Run offline regression tests plus installed CLI help, imports and MCP `tools/list`.
   Keep Linux build/capture churn on the host's designated fast volume.
4. Obtain the active test owner's explicit idle handoff before changing launchers or
   services. Atomically point the launcher at the verified environment and retain
   the prior launcher/config for rollback. Do not overwrite a dirty checkout.
5. Verify the installed import path, wheel hash, CLI/MCP discovery and a separately
   coordinated passive capture. No OS upgrades, vendor-app updates, device firmware
   updates or reboots are required by a ScopeLoop code release.

On Windows, use the virtualenv's `Scripts/python.exe` and `Scripts/scopeloop.exe`.
On Linux, use `bin/python` and `bin/scopeloop`. Launch stdio MCP with
`python -m scopeloop.mcp_server` from the private configuration directory.
An already-running MCP process keeps its old imports until its next connection;
verify the new process rather than claiming a checkout edit updated it.

For acoustic evidence, record microphone identity, gain, placement, sample format,
load and ambient conditions. Without an acoustic calibration, report relative
levels/dBFS and spectral tones, not calibrated dBA. Host-specific ALSA names,
interfaces and topology belong in private configuration.
