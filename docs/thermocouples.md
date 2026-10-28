# MAX31856 thermocouple logger

The dedicated CircuitPython logger reports Type K temperatures at approximately
2 Hz. It provides measurement validity and hardware temperature thresholds,
**not a physical power cutoff or calibrated temperature accuracy**. A DUT
controller must stop its load on invalid/stale data and require explicit re-arming.

## Firmware and private configuration

Deploy src/scopeloop/thermocouple_firmware.py as code.py on a dedicated
CircuitPython board with Adafruit's adafruit_max31856 and adafruit_bus_device
libraries. Keep the existing known-working library versions. This uses private
driver register helpers; verify configuration readback on the actual installed
library before running a load. The firmware does not configure relay GPIOs.

Create thermocouple_config.json alongside it, using the actual chip-select pin
and an explicitly chosen screening limit:

~~~json
{"cs_pin": "D5", "low_c": -20, "high_c": 85, "noise_hz": 60}
~~~

The pin above is an example, not a board-specific wiring instruction. Hardware
serials, hostnames, probe placement, chosen limit and calibration belong in private
configuration and product evidence. Integer Celsius thresholds are required.
Type K, averaging 1, zero cold-junction offset and unmasked faults are verified.
The threshold is not an approved component operating rating.

Before deployment, obtain the instrument owner's idle handoff, preserve existing
files/hashes, and check that no other workflow owns the port. Do not change a
shared USB hub or power-cycle the board as part of deployment. Code and profile
changes require an explicit logger reload, followed by identity, threshold,
fault, sequence and fresh-conversion checks before any load resumes.

## Capture and diagnostics

Use the same canonical port path and shared SCOPELOOP_LOCK_DIR as other clients.
Every connection holds a HostLease, plus serial exclusivity where supported,
through the complete operation. A second process fails without preempting it.
The CLI asserts DTR to establish the CircuitPython CDC console connection and
leaves RTS deasserted. Only use this with the intended dedicated logger:
device-specific side effects of opening other serial hardware still need checking.

~~~sh
scopeloop thermocouple capture "$PORT" "$DEVICE_ID" evidence/capture-01 85 --seconds 10
scopeloop thermocouple diagnose "$PORT" "$DEVICE_ID" evidence/diagnostic-01
~~~

Capture is passive and fails on invalid data, a changed cutoff/device/boot,
nonfinite values, sequence gaps or more than three seconds without fresh samples.
Diagnostics send an in-band JSON status request. There is no Ctrl-C/REPL probing
and no automatic reboot. Old logger firmware supports passive capture but lacks
the new diagnostic/recovery commands.

Requests begin with a newline to discard a previous client's interrupted command.
Uncorrelated console-parser errors are retained in raw/timeline evidence; they
cannot satisfy a command acknowledgement or a temperature-validity check.

Bundles contain original serial bytes, ScopeLoop timeline events and an artifact
hash manifest compatible with validate_bundle. Failed operations retain their
evidence and are marked failed. Host UTC describes receipt; sample uptime_s
and host monotonic timestamps describe timing. A correct calendar clock is not
assumed on a remote instrument host.

## Faults and explicit recovery

The first fault is latched immediately. Records include a fault ID, actual and
expected configuration values and a register snapshot. Configuration is checked
before and after a conversion. A transient clearing on its own does **not**
make later samples valid.

After the owner stops the DUT load and confirms the fixture is idle, use the
current boot and fault ID from diagnostics:

~~~sh
scopeloop thermocouple recover "$PORT" "$DEVICE_ID" evidence/recovery-01 \
  "$BOOT_ID" "$FAULT_ID" 85 --dut-idle
~~~

This acknowledges one reinitialization, not permission to restart a DUT. The
logger cannot independently verify the caller's idle declaration. It verifies
all configuration and waits for three consecutive healthy conversions before
clearing its latch. A hardware fault, cutoff, conversion timeout or SPI failure
during verification keeps it invalid. Stale tokens and concurrent recovery
requests are rejected. Original fault evidence is retained after recovery.

For embedded clients, use ThermocoupleConnection and TemperatureGuard inside
the owning test's session/resource workflow. Hold the connection across the
whole test. A successful recovery never resumes RF, enables a rail or closes a
relay. Hardware fault events must still produce the controller's stop action.
