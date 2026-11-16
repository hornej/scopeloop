# Electronic-load fault recovery

## Rigol DL3021A / DL3000 reverse-connection alarm

Status: rechecked against the manufacturer manuals on 2026-11-15. The remote
acknowledgment commands are documented, but recovery from this alarm has not yet
been validated on the bench instrument. This is a manual SCPI procedure, not a
ScopeLoop CLI or MCP recovery command.

The DL3000 user guide says reverse-voltage protection turns the load input off
and displays `Reverse connection!`. Pressing **OK** dismisses the message. The
programming guide provides remote front-panel key simulation, with key value
`41` representing **OK**.

### Before acknowledging

- Coordinate with the current bench owner and retain the instrument's existing
  resource/session ownership for the whole recovery attempt. Do not open a
  competing raw SCPI connection or clear another test's fault.
- Pause the test sequence and preserve the fault details, instrument identity
  and firmware (`*IDN?`), load settings, and observed voltage/current in the
  session record. Keep the endpoint and serial number in private configuration.
- Confirm that the reverse-voltage condition has ended. If polarity remains
  wrong, turn off the DUT and load and correct the connection before resuming.
  Acknowledging the message does not correct reversed wiring.

### Remote acknowledgment

Send these commands separately over the instrument's existing SCPI connection:

```text
:SOUR:INP:STAT OFF
:DEBUG:KEY ON
:SYST:KEY 41
:DEBUG:KEY OFF
```

The first command explicitly disables the load input. The next commands enable
virtual front-panel control, press **OK**, then disable virtual front-panel
control. In a scripted implementation, put `:DEBUG:KEY OFF` in cleanup so it is
attempted even if acknowledgment or a subsequent check fails. Do not substitute
the ON/OFF toggle key or assume acknowledgment re-enables the input.

Afterward, record the input state, measured voltage/current, protection status,
and command errors:

```text
:SOUR:INP:STAT?
:MEAS:VOLT?
:MEAS:CURR?
:STAT:QUES:COND?
:SYST:ERR?
```

The input-state query should return `0`. Decode the condition register using the
programming guide; a retained event or voltage-fault latch is distinct from an
active reverse-voltage condition. The error query consumes an error-queue entry,
so preserve its response. If the alarm remains or returns, or command support is
uncertain, leave the input off and investigate instead of repeatedly clearing it.

Re-enable the input with `:SOUR:INP:STAT ON` only when polarity, protection state,
load settings, and the intended test conditions have been checked. Confirm the
input state and measured current afterward. For relay-selected load paths,
disable the load input and verify current has stopped before switching; allow
the relay and source voltage to settle before enabling the input again.

### Status clearing and validation limits

- `*CLS` clears event registers. It is not documented as a substitute for
  acknowledging this dialog, and clearing an event does not prove the electrical
  fault has ended. Preserve the event evidence before clearing it.
- `*RST` resets instrument settings; it is not the first recovery step.
- Before relying on unattended recovery, validate acknowledgment and the
  subsequent input/protection checks on the actual model and firmware during a
  coordinated bench session. Do not deliberately reverse a powered connection
  merely to generate the alarm. Record the observed result separately from this
  manufacturer-documented procedure.

### References

- [Rigol DL3000 User Guide, reverse-voltage protection, printed page 2-84](https://www.rigol.com/dam/global/downloads/brochures/en/user-manual/dc-load/DL3000_UserGuide_EN.pdf#page=114).
- [Rigol DL3000 Programming Guide, front-panel key simulation, printed page 2-40](https://www.batronix.com/files/Rigol/Elektronische-Lasten/DL3000/DL3000_ProgrammingManual_EN.pdf#page=58).
  This manufacturer-authored copy also documents `*CLS` (2-2), status queries
  (2-9), input state (2-19), and the error queue (2-41).
