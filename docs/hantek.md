# Hantek 2000 scope, DMM and generator

The Hantek is one combined USB instrument. Configure `instruments.hantek` alongside
any existing Siglent, Saleae or HMP4040 entries. All three functions share one
ResourceManager owner and the host lease `scope:usb:0483:2d42`. Obtain the existing
bench owner's explicit idle handoff before access. A CLI/MCP command opens and
closes the connection; Python bench workflows retain `HantekService.session()`
across the entire test. A higher-priority client cannot preempt an owner.

## Host setup

```yaml
project:
  name: Hantek bench
hardware:
  mcu: bench
build:
  system: custom
instruments:
  hantek:
    type: hantek-2000
    transport: auto
    expected_serial: YOUR_INSTRUMENT_SERIAL
    max_output_peak_v: 2.5
    # Optional zero reference from a recent loopback with identical scope settings:
    # scope_zero_reference: /private/evidence/loopback/scope-zero
```

Keep serials, USB paths and evidence paths in private host configuration. Windows
uses native SetupAPI/WinUSB through ctypes, with no vendor DLL or Python USB
dependency. The exact VID/PID `0483:2d42` interface must already use Microsoft's
WinUSB driver. Discovery does not install drivers or change Windows security.
Multiple matches require `usb_path`; `expected_serial` is checked before setup
writes. The legacy vendor kernel driver can be blocked by Memory Integrity;
do not disable it to run this driver. Replacing a driver requires a separate
idle handoff and Windows administrator approval.

Linux has an optional `pip install 'scopeloop[hantek]'` PyUSB transport requiring
host-provided libusb and USB permissions. It does not detach kernel drivers,
reset USB or update firmware. This transport has not been qualified on hardware.
The live qualification is Windows x64, Hantek 2D72 firmware `2021090901`; family
identity is readable, but exact model readback is unavailable. Other 2000-series
models and firmware need separate qualification.

## Commands and MCP

Use `--config /private/bench.yaml` on each command, or run from that config's
directory. MCP uses the same services, with the prefix `scopeloop_hantek_` and
underscores in operation names. Start stdio MCP with the installed Python:
`python -m scopeloop.mcp_server` from the private configuration directory.

| CLI operation | Behavior |
| --- | --- |
| `hantek status` | Identity, scope settings, DMM display and AWG setpoints |
| `hantek dmm-read` | Signed SI value, mode, displayed digits/unit and raw frame |
| `hantek dmm-configure ac_v` | Select mode and poll until its readback matches |
| `hantek screen dmm` | Select scope, DMM or AWG front-panel screen |
| `hantek awg-configure awg.json` | Disable output, apply and verify setpoints |
| `hantek awg-run awg.json new-bundle --duration 5 --dmm-mode ac_v` | Bounded output and optional voltage logging; send OFF on completion/failure |
| `hantek awg-off` | Send OFF under ownership |
| `hantek scope-configure scope.json` | Validate settings before writes and verify returned setup |
| `hantek scope-capture new-bundle` | Drain one pending frame, save AUTO snapshot, then STOP |
| `hantek loopback new-bundle --with-scope` | Near-zero reference, five AC/DC comparisons, then output OFF |

Example AWG settings, into a high-impedance load:

```json
{"waveform":"sine","frequency_hz":100,"amplitude_vpp":2,"offset_v":0}
```

Supported waveforms are sine, square, ramp and trapezoid. Square duty and ramp
symmetry use `duty_percent`; trapezoid timing and arbitrary-wave upload are not
implemented. Frequency is integer Hz. Voltage resolution is 2 mVpp and offset
resolution 1 mV. Requests are bounded to 5 Vpp and the configured peak limit:
`amplitude_vpp / 2 + abs(offset_v) <= max_output_peak_v <= 2.5 V`. Rounded wire
values are checked too. Into 50 ohms, amplitude is approximately half the stated
high-impedance amplitude. The current live electrical validation covers 100 Hz
sine and low DC offsets, not the instrument's maximum frequency or other shapes.

AWG reads return **setpoints**. The firmware's output-enable query returns a
stale unrelated reply, so `output_enabled` remains null. An OFF result means the
command was sent successfully, not that an independent output-state sensor was
read. Output enabled through this connection is disabled on disconnect. CLI/MCP
output runs are bounded to 0.2–300 seconds; abrupt process termination or USB
loss cannot guarantee that hardware received OFF. Inspect the physical output
after such a failure. A cleanup failure is reported as failure, not success.

DMM modes: `ac_a`, `dc_a`, `ac_ma`, `dc_ma`, `dc_mv`, `dc_v`, `ac_v`,
`capacitance`, `resistance`, `continuity`, `diode`. Input wiring must suit the
selected function. Generator loopback permits voltage modes only. Numeric
readbacks use base units V/A/F/ohm and retain original digits and prefix.
Overload/symbol digits return null, never a fabricated number. The firmware's
range/status bytes are retained without assuming their undocumented meanings.
Hold state and instrument sample timestamps are unavailable; this is polled
display data. Only DC V and AC V were electrically exercised in commissioning.

Example scope setup for GEN OUT connected to CH1, probe physically at 1x:

```json
{"channel":"CH1","enabled":true,"coupling":"DC","probe":1,
 "volts_per_div":1,"position_code":100,"seconds_per_div":0.002,
 "trigger_mode":"auto","trigger_source":"CH1","trigger_slope":"rising"}
```

Capture supports YT/AUTO, 1 us–100 ms/div, 6000 raw bytes (3000 samples per
channel in dual-channel mode). It discards the observed pending frame while
running before reading the next frame. There is no acquisition counter or device
timestamp, so this is not a qualified single-shot/event capture. Empty, short,
malformed, perpetually busy and setup-changing transfers fail. Capture leaves
the scope stopped. Native position and trigger-level codes are intentionally
exposed as codes; their calibrated voltage interpretation is unqualified.

Without a zero reference, capture saves raw ADC CSV/NPZ, binary frames, settings,
identity, host timestamps, USB transcript and a Session timeline. With
`--zero-reference recent-loopback/scope-zero` (or private config default), it also
saves nominal voltage CSV/NPZ and mean/Vpp/AC-RMS estimates. The reference's
hashes, identity, channel, probe ratio and all channel settings must match.
Reacquire it after power, temperature, probe or setup changes. Boot count and
reference drift cannot be verified automatically. The nominal gain is 25.6
codes/div dual-channel, 25 codes/div single-channel; factory correction tables
are not applied. Single-channel gain has not been electrically qualified.
Internal GND coupling has a different ADC baseline on tested firmware and is
not used as a zero reference. Do not interpret these captures as calibrated
noise-floor or ripple measurements.

## Functional loopback and evidence

Connect GEN OUT to DMM V/COM. With `--with-scope`, also connect CH1 tip to signal,
ground to COM, at physical 1x. Configure CH1 DC/1x, 1 V/div, 2 ms/div and AUTO
first. This workflow uses the explicitly authorized wiring and does not operate
a DUT or power supply. Scope mode is kept fixed during the test.

The scope variant first applies a 100 Hz, 2 mVpp sine with zero offset and checks
that DMM DC V is within 10 mV of zero. It saves that near-zero stimulus as a
reference (not a precision calibration). Both variants then apply 100 Hz sine:
1 Vpp and 2 Vpp at zero offset for AC readings, then 1 Vpp at +1, -1 and 0 V
offset for DC readings. Three settled display polls are saved per stage. Default
functional tolerances are 10 mV + 5% for DMM and 60 mV + 10% for nominal scope
readings. The test leaves AWG off, 2 Vpp/100 Hz/zero-offset setpoints, DMM restored
to its prior mode, and scope stopped if captured. It does not restore an enabled
output. Comparisons within the same unit are not independent calibration or
product qualification.

Each scope subdirectory is independently hash-verifiable and exportable:

```sh
scopeloop evidence export loopback-bundle/scope-2 viewer-files
```

ngscopeclient CSV and PulseView SR labels retain “nominal volts”; export metadata
retains source limits. Raw-only captures cannot be exported as voltage traces.
No automated viewer import claims improve the physical measurement accuracy.

## Protocol and provenance

The Python driver is independently implemented from USB observations and static
inspection of the manufacturer PC package, with no vendor DLL execution or
redistribution. The official [Hantek product page](https://www.hantek.com/products/handheld-oscilloscope/Hantek2000)
and [PC software download](https://www.hantek.com/Products/SoftwareDL?id=41404)
provide product context. The examined software archive SHA-256 is
`951586346d4e2580ddba47f0724ff8c8ed265bf7a7ce958bc9d4a055acc3a8db`.
No vendor source or third-party driver implementation is included.

- USB bulk OUT 0x02, IN 0x81, 64-byte packets, bounded native timeouts.
- Settings packet: `00 0a group read command value_u32_le 00`.
  Groups 0/1/2/3 are scope/DMM/AWG/system. Reads set `read=1`.
- DMM read: `00 05 01 01 00`; mode write: `00 05 01 00 mode`.
  Response: `55 0b 01 mode range sign decimals d0 d1 d2 d3 prefix status 55`.
  Prefixes 0..5 are nano/micro/milli/kilo/mega/none. Range and status remain raw.
- Scope settings: group 0 command 0x15, response `55 19 00 15` plus 21 bytes.
  Two six-byte channel blocks precede run, format, timebase, horizontal position
  u16, trigger source/slope/mode/level. Write codes for trigger source/slope/mode
  are 16/17/18, while response offsets are 17/18/19.
- AWG codes: 0 waveform, 1 frequency, 2 amplitude (500 counts/Vpp high-Z),
  3 offset (1000 counts/V, negative flag at bit 16), 4 square duty, 5 ramp
  symmetry, 8 output. Readback values begin at response byte **4**, not 5.
  Tail bytes may contain stale scratch data. Group/command/length are checked.
- System commands: 0 screen selection, 1 controller UID, 9 serial, 10 firmware.
- Scope block request uses `<BBHBHHB>`: one-based block index, 10, 0x0100,
  0x16, 3000, 3000, 20. Dual-channel data interleaves CH1/CH2. A repeated
  `00 00 ff ff` prefix indicates busy; retries are bounded.

Replay tests retain real DMM and setup response shapes with synthetic identity,
and exercise wrong echoes, truncation, overload, identity mismatch, lease
contention, output failures, native-read cancellation, pipeline draining and
reference hash/setup rejection. Use the normal release workflow in
[bench operations](bench-operations.md), including the installed wheel's CLI
and MCP discovery. Keep exact host/revision/wheel hashes and live results in
the private deployment record.
