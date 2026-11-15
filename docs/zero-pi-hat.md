# Zero Pi HAT fixture support

ScopeLoop supports the **v0.1.0 shared 12 V output** on a Raspberry Pi 4 through
libgpiod 2.x. It provides read-only inspection, GPIO/fault status, on/off and a
bounded power cycle. USB switching, the auxiliary bus, ADC, temperature sensor,
EEPROM and arbitrary GPIO control are not implemented. v0.0.1 is rejected:
the available archives did not establish its revision-specific pin mapping.

## Mapping and evidence

| Signal | Mapping | Behavior |
| --- | --- | --- |
| 12V_PWR_SW | BCM7, header pin26, U1 TPS259631 EN pin3 | High enables; low disables |
| 12V_PWR_FAULT | BCM8, header pin24, U1 FLT pin6 | Low indicates fault; external 10k pull-up |
| Switched output | J9 pin2 +12V_Out, pin1 GND | Shared with the bus/touchscreen 12 V feed |
| Unclaimed enable | R8 4.75k to 3.3 V | Defaults **on** when GPIO is released |

Source: user-supplied `pi-hat.PDF`, sheet 1, SHA-256
`b04db3a50a02e80988f62fe3383a0dec4de5d4a65d5f854974cd3ab93fc229de`.
The user identified the tested board as v0.1.0. The drawing itself has an
unversioned project title block; it is not independent proof of the assembly
revision. The November 14, 2026 antenna-box bench verified GPIO7 power cycling
and sustained off using libgpiod 2.2 on Pi 4. Product loads, serials, waveforms
and RF results remain in the antenna-box repository.

This driver reports **enable GPIO readback and fault**, not measured rail
voltage or current. A low enable pin does not prove zero voltage when another
supply or signal can backpower the load. A high fault pin does not prove power
is present. Measure at the load for electrical shutdown verification.

## Configure on the Pi

Install the built wheel with its optional Linux dependency, e.g.
`python -m pip install '/path/to/scopeloop-0.3.0-py3-none-any.whl[zero-pi-hat]'`.
This branch is not a published package release. The host also needs Raspberry
Pi's `pinctrl` utility for read-only pin-mux checks and GPIO-device permissions.
The implementation uses the [libgpiod Python API](https://libgpiod.readthedocs.io/en/v2.3/python_api.html).
It verifies the Pi model, CPU serial, chip label and GPIO names before claiming
anything. Pi 5 and unknown chip mappings are deliberately not assumed compatible.

Add this to the project's private `scopeloop.yaml`:

```yaml
zero_pi_hat:
  revision: "0.1.0"
  gpiochip: /dev/gpiochip0
  expected_pi_serial: null  # Fill from read-only inspect before starting owner.
  socket_path: ~/.scopeloop/zero-pi-hat/control.sock
  loads: []                # Name ALL loads on the shared output, not just J9.
  wiring_confirmed: false  # Set true after checking that mapping/load list.
```

`scopeloop hat inspect --config scopeloop.yaml` reads identity and current
consumers without requesting GPIO. Fill the serial/load fields from the actual
fixture. Revision is operator supplied; neither the Pi CPU serial nor GPIO
inspection detects the HAT revision automatically.

## Persistent ownership

After an explicit bench handoff, start a **persistent foreground owner** in a
dedicated terminal or an explicitly configured supervisor:

```sh
scopeloop hat serve --start-off --config scopeloop.yaml
```

Starting the owner changes the shared output to off. It atomically claims GPIO7
low and GPIO8 as an input, holds a ScopeLoop HostLease and the kernel GPIO
request for its entire lifetime, and serves a mode-0600 Unix socket. A competing
ScopeLoop owner or existing GPIO consumer is refused. It never kills or adopts
an existing `gpioset` process and never changes an alternate pin function.

From another terminal on the same Pi (or via SSH), using the same configuration:

```sh
scopeloop hat status --config scopeloop.yaml
scopeloop hat output on --config scopeloop.yaml
scopeloop hat cycle --off-seconds 3 --settle-seconds 1 --config scopeloop.yaml
scopeloop hat output off --config scopeloop.yaml
```

The on command can energize every shared load. Client exit does not release the
GPIO. Cycle requires an already-enabled output; an interrupted/failed cycle
commands off while the owner remains alive. Concurrent commands fail busy
rather than queue a later power change. Fault/readback failure after enabling
commands off. An off command is issued before attempting fault/readback queries.

The socket's adjacent JSONL journal records request IDs, results/errors and UTC
times. A lost response means outcome unknown: read status before retrying a
power-changing command. Do not automatically retry cycles. CLI connection does
not launch an owner or acquire GPIO. MCP exposes `scopeloop_hat_status`,
`scopeloop_hat_output` and `scopeloop_hat_cycle` through the same owner, with
normal session event logging. Run that MCP instance on the Pi; remote runtime
routing is not added by this driver.

**Owner exit, crash, reboot or releasing GPIO can re-enable the output.**
There is intentionally no misleading `shutdown` command that releases GPIO
while claiming the rail stays off. Use `output off` and keep the owner alive;
remove upstream power for unconditional isolation. Do not install an automatic
owner startup policy without deciding the desired boot behavior for every load.
After a crash, investigate any leftover socket before removing it; startup does
not unlink an existing owner's socket.

## Migrating the antenna-box helper

The earlier detached `antenna-vtx-j9-off` gpioset holder must remain in place
while that bench is off. This driver does not take it over. At a separately
scheduled power session, remove upstream supply before releasing the old holder,
then start the ScopeLoop owner off and verify the load rail before restoring
upstream power. Never treat restarting the owner as a seamless handover.

The implementation is covered by fake-GPIO state, fault, ownership,
cancellation and Linux socket tests. The new owner has not yet been qualified
by switching the live bench; prior physical measurements validate the mapping
and electrical behavior, not every path in this new service.
