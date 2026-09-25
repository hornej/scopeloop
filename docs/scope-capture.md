# Scope acquisition and evidence

`scopeloop scope capture recipe.json new-evidence-directory --config bench.yaml`
and MCP `scopeloop_scope_capture` use the same evidence routine. The MCP inputs
are `recipe` (the object below) and `output` (a new directory on the instrument host).
Run the CLI/MCP on the machine that owns the instrument; desktop clients should
invoke the host runtime over SSH instead of independently opening the scope.

```json
{
  "kind": "ripple",
  "channel": "CH1",
  "signal": "output rail at load connector",
  "probe": "compensated 10x passive probe",
  "connection": "tip at output capacitor positive",
  "ground": "spring at adjacent return",
  "limitations": ["Probe pickup comparison remains unverified"],
  "timeout": 10,
  "dc_telemetry": {"source": "separate DUT telemetry", "volts": 24.1},
  "run_metadata": {"firmware": "record exact version and artifact hash", "load": "record actual load"}
}
```

Configure coupling, attenuation, bandwidth, scale, offset and trigger deliberately
before capture. Only SAMPLING acquisition is currently supported; averaging,
peak detect, interpolation, math traces and other models need separate validation.
The driver does not operate the DUT or electronic load. `points` requests at most
that many points from the start of the record; omit it for a full transfer.

## Freshness and transport

AUTO starts from STOP, clears sweeps on supported firmware, clears the INR latch,
selects AUTO, and requires two new-acquisition events separated by 300 ms. SINGLE
waits for one new event. `single()` means armed; `capture_waveform(acquisition="armed")`
finishes that connection's shot. A trigger-ready bit alone is insufficient.
Timeouts return no waveform; successful captures leave STOP. A framing failure
closes the socket, so final instrument state must be inspected after reconnecting.

SDS1104X-E firmware 8.2.6.1.37R9 was observed switching to SINGLE after ARM even
when AUTO had just been selected. Therefore the AUTO path does not send ARM.
`SANU?` timed out on that unit and is not used. Data comes from the declared
definite block length; empty, truncated, malformed and oversized blocks fail.
The rate comes from SARA and the trigger-relative origin from TRDL and TDIV;
partial transfers do not change the sample interval. A partial request leaves
the full acquisition size unknown. Configured attenuation is already reflected
in VDIV/OFST and is not applied twice.

The implementation follows the [Siglent programming guide EN02E](https://siglentna.com/wp-content/uploads/dlm_uploads/2025/11/SDS1000-SeriesSDS2000XSDS2000X-E_ProgrammingGuide_EN02E.pdf),
particularly ARM/clear sweeps, INR, TRDL and waveform transfer. Replay tests include
prefixed replies, engineering units, byte-split TCP frames, embedded LF/CR sample
bytes, two-byte trailers, stale data and concurrent transactions. Model/firmware
quirks above come from observed bench evidence, not a promise for all Siglent models.

## Noise floor and load steps

Capture `kind: "noise_floor"` with the same setup and a documented grounding
method, then set `noise_floor_manifest` to that bundle's manifest for a ripple
capture. Setup and artifact hashes must match; a clipped baseline is refused.
The baseline values and manifest hash are recorded. Without one, the report
explicitly says the noise floor is unmeasured. Never silently subtract a noise
floor or call ground-coupled instrument noise a complete probe-pickup calibration.

For `kind: "load_step"`, also provide `before_window: [-0.002, -0.001]`,
`after_window: [0.003, 0.004]`, and `settling_band_v: 0.1` in SI units. This uses
SINGLE; a separately authorized external event must trigger it. Align the scope
trigger with the actual load-control edge. The report records window means,
overshoot, undershoot, and settling within the captured observation interval.
It rejects windows outside the trace. It does not start or stop a load.

Each bundle preserves raw ADC bytes, the complete binary response, scaled samples
with timestamps, identity/firmware/setup readbacks, channel map, SCPI transcript,
software versions, computed measurements and SHA-256 checksums. Failed attempts
retain a failure manifest and transaction log. ADC codes near the endpoints flag
possible clipping; the capture is retained but amplitude is not qualified.

DC telemetry is kept separate from measured AC peak-to-peak and AC RMS values.
AC coupling removes DC and suppresses low frequencies. Bandwidth, ground-lead
pickup, probe attenuation, clipping, record duration and noise floor all affect
what a ripple number means. Built-in `scope measure` readouts are snapshots whose
freshness is unverified; use evidence capture for test conclusions.
