# HMP4040 power supply

Use the HMP4040's raw SCPI Ethernet socket from the machine that owns the bench.
On the front panel, open **Menu → Interface → Ethernet** and read the current
IP address and port. Enter IPv4 octets without leading zeroes. The usual port
is 5025. This driver uses Python's TCP support and needs no VISA backend or
vendor software. [R&S HMP Series User Manual, chapters 7–8](https://scdn.rohde-schwarz.com/ur/pws/dl_downloads/pdm/cl_manuals/user_manual/1178_6833_01/HMPSeries_UserManual_en_03.pdf)

## First read

Add this section to the instrument host's private project `scopeloop.yaml`.
Replace the example address. Keep actual addresses, serials and wiring private.

```yaml
instruments:
  power_supply:
    type: hmp4040
    address: "192.0.2.101"
    port: 5025
```

After an idle bench handoff, identify the supply and read it.

```console
scopeloop power idn
scopeloop power status
scopeloop power status --channel 2 --output captures/supply-before-boot
```

These commands also take `--config PATH`. Copy the serial from `idn` into
`expected_serial` as a quoted string to catch address reassignment. Status
returns the programmed voltage, current limit, instrument-reported output state
and measured DC voltage/current for each requested channel. Readings occur in
sequence. The driver briefly changes the SCPI channel address to read another
channel, then restores the original address while the connection is healthy.
An optional new output directory stores telemetry, timestamped SCPI replies,
a manifest and SHA-256 hashes. Use a scope for ripple or startup transients.

## Project control

Control requires a pinned serial and explicit limits for the target channel.
Choose both ceilings from your DUT's specification and actual wiring. For
example, a project approved for up to 3.3 V and 0.2 A on channel 2 would add
the following **under `power_supply`**.

```yaml
expected_serial: "REPLACE_WITH_IDN_SERIAL"
channels:
  2:
    max_voltage_v: 3.3
    max_current_a: 0.2
```

For an authorized rail change, configure the channel while it is off. Both
values are sent in one `APPL` command and checked against instrument readback.
Enabling a channel checks the **current** setpoints against project limits,
including settings inherited from the front panel.

```console
scopeloop power configure 2 --voltage 3.3 --current-limit 0.1
scopeloop power output 2 on
scopeloop power output 2 off
```

Channel control uses `OUTP:SEL`. The front panel controls the master by default.
Before enabling the master on the panel, check every selected channel. Once
the master is on, selecting one channel can immediately energize that rail.
Status returns the instrument's `OUTP?` reply. Check measured voltage as well.
On an unexpected readback or interrupted command, inspect the instrument before
retrying. Closing a connection releases ownership without issuing output commands.

## Remote master with operator approval

To permit remote master control in a private project, set
`instruments.power_supply.allow_remote_master: true`, pin `expected_serial`, and
configure voltage and current ceilings for each channel to energize.

```console
scopeloop power master-plan on --channel 2
scopeloop power master-apply "APPROVE MASTER ON <nonce from preview>"
```

The preview reads the current master, channel selections and setpoints. It creates a
single-use request for this instrument host that expires in three minutes. In the
operator terminal, apply displays the action, serial, affected channels, voltages and
current limits and asks for confirmation. With MCP, use `scopeloop_power_master_plan`
and `scopeloop_power_master_apply`. The client must display a form and receive an
explicit positive answer from the operator. A headless CLI or a client without form
support cannot approve a change. A preview or its token alone cannot operate the switch.

For master-on, specify exactly the channels intended to receive power. With the master
off, ScopeLoop can select missing approved channels before enabling it. An unexpected
selected channel, changed setpoint, expired/reused request, or exceeded project limit
requires a new review. Master-off affects every selected channel and requires the same
approval process. Software rechecks before writing and reports the actual outputs
afterward. If transport fails during a write, inspect the supply before retrying.
The host lease coordinates ScopeLoop clients. Keep front-panel and other raw SCPI
clients idle while applying the reviewed change.

## Firmware tests and MCP

Hold a single supply session for the whole test, including waits or work with
other instruments. For example, this coroutine takes readings on both sides of a
project-owned firmware step and keeps the same connection throughout.

```python
from scopeloop.config import load_config
from scopeloop.power import PowerSupplyService, snapshot


async def measure_boot(run_boot_step, session_dir):
    service = PowerSupplyService(load_config().instruments.power_supply)
    async with service.session("firmware-test") as psu:
        await snapshot(psu, 2, session_dir / "power-before")
        await run_boot_step()
        return await snapshot(psu, 2, session_dir / "power-after")
```

Start the stdio MCP server from the private config directory using the installed
environment's `python -m scopeloop.mcp_server`. It exposes `scopeloop_power_idn`,
`scopeloop_power_status`, `scopeloop_power_configure`, `scopeloop_power_output`, and
the two remote-master tools described above.
For example, ask “Read CH2 voltage and current and save a snapshot in
`captures/power-before-boot`.” Each tool call releases the supply when it
finishes and logs its result to a running ScopeLoop session. For a sequence
that must exclude other clients between steps, use the Python context above.
Coordinate other hosts and front-panel access with the bench owner.
