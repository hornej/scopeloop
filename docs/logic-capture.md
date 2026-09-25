# Logic capture recipes and evidence bundles

ScopeLoop uses a capture recipe to keep the channel set, sampling, trigger,
decoder configuration, and run metadata together. The recipe is generic; board
pinouts and repair hypotheses belong in a project configuration or fixture
profile, not in ScopeLoop core.

## Install and connect

Install Saleae Logic 2 on the instrument host, enable its automation server,
and install ScopeLoop's Saleae extra in the same Python environment:

```bash
python -m pip install -e ".[saleae]"
scopeloop logic connect --config scopeloop.yaml
```

`logic connect` reports the selected device identity plus the Logic application,
automation package, and API versions when the official API exposes them.

## Recipe schema

This example is an ESP32 3.3 V capture profile. The names and required metadata
are project choices, not built-in concepts:

```yaml
instruments:
  logic_analyzer:
    type: saleae
    port: 10430
    device_id: null
    channels:
      2:
        signal: RESETn
        label: reset
        signal_type: reset
        digital: true
        analog: true
        electrical:
          input_low_max_v: 0.825
          input_high_min_v: 2.475
          output_low_max_v: 0.33
          output_high_min_v: 2.64
      3:
        signal: UART_TX
        label: boot_uart
        signal_type: uart
        digital: true
    recipes:
      boot:
        description: Power and reset capture with boot UART
        digital_channels: [2, 3]
        analog_channels: [2]
        digital_sample_rate: 6250000
        analog_sample_rate: 781250
        logic_family_volts: 3.3
        trigger:
          channel: 2
          edge: rising
          pre_trigger_seconds: 0.25
          post_trigger_seconds: 0.75
          timeout_seconds: 30
        uart:
          - name: boot_uart
            channel: 3
            baud_rate: 115200
            bits_per_frame: 8
            stop_bits: 1.0
            parity: "None"
            bit_order: least_significant_first
            inverted: false
            radix: hexadecimal
        required_metadata:
          - serial
          - board_revision
          - fixture_state
        comparison:
          align_channel: 2
          edge: rising
          threshold_v: 1.65
          signals: [2, 3]
```

For an ESP32-PICO-V3 input, values at or below 0.825 V are guaranteed low and
values at or above 2.475 V are guaranteed high. The region between them is
indeterminate. A driven output should satisfy `VOL <= 0.33 V` or `VOH >= 2.64
V` under the applicable data-sheet conditions. Saleae's `3.3` setting names
the Logic Pro 3.3 V logic-family mode; it uses a single comparator threshold
near 1.65 V. A digital transition in the indeterminate electrical region does
not prove a valid ESP32 logic level.

The metadata object is deliberately open-ended. Require the fields that make a
run reproducible, then use any additional key/value fields needed to describe
the DUT, fixture, probe state, temporary modifications, or custody.

Sample-rate combinations depend on the Logic model and the enabled digital and
analog channel counts. The two-digital/one-analog Logic Pro 16 example above
uses a pair accepted by Logic 2.4.46. ScopeLoop passes the requested pair to
Logic 2 and reports the device's advertised alternatives if the request is not
supported; it does not silently substitute different rates.

## Capture and decode

Run the recipe and supply required metadata:

```bash
scopeloop logic capture --recipe boot \
  --metadata serial=unit-001 \
  --metadata board_revision=5.10 \
  --metadata 'fixture_state={"dongle":"powered","switch":"development"}' \
  --output-root /srv/scopeloop/captures
```

Every successful capture automatically contains:

- `capture.sal`, saved directly from Logic 2 and left unmodified;
- `raw/digital.csv` and/or `raw/analog.csv` from Logic 2;
- one `decoded/*.csv` for each configured UART analyzer;
- `channel-map.json`, the authoritative digital and analog channel map;
- `manifest.json`, containing recipe, metadata, timestamps, device/software
  identity, sample rates, logic-family selection, trigger window, progress, and
  per-artifact SHA-256 hashes; and
- `SHA256SUMS`, including the manifest itself.

If capture fails, ScopeLoop leaves a manifest with `status: failed`, the
progress reached, and the exception. It does not present an incomplete bundle
as evidence.

The CLI can also reopen a capture for a one-off operation:

```bash
scopeloop logic decode --capture capture.sal --channel 3 --baud 115200 \
  --output uart.csv
scopeloop logic export --capture capture.sal --digital-channel 2 \
  --analog-channel 2 --output-dir raw-export
scopeloop logic save --capture capture.sal --output verified-copy.sal
```

The MCP server exposes matching `scopeloop_logic_connect`, `capture`, `decode`,
`save`, `export`, and `compare` tools. MCP capture handles remain available for
later decode/save/export calls during the server process.

## Trigger semantics and API limits

For a triggered capture, ScopeLoop configures:

- `after_trigger_seconds = post_trigger_seconds`; and
- `trim_data_seconds = pre_trigger_seconds + post_trigger_seconds`.

That is the official API's supported pre-trigger mechanism. `pre_trigger_samples`
in the low-level driver is converted to seconds for compatibility. ScopeLoop
reports `armed`, periodic `waiting_for_trigger`, `complete`, and
`trigger_timeout` states. It applies a bounded gRPC wait and attempts to stop a
capture whose deadline expires.

Logic 2 validates the actual device/channel/sample-rate combination when
`start_capture` is called. The automation API does not currently expose a
capability query for enumerating all supported combinations, so ScopeLoop
performs structural checks first and returns the complete rejected combination
if Logic 2 refuses it.

The official API labels protocol analyzers but has no native channel-label setter.
By default the SAL remains untouched and `native_labels_applied` is false. For a
verified native copy, use the [native labels workflow](#verified-native-labels).

## Known-good versus DUT comparison

Capture both units with the same recipe, fixture, probes, and control state.
Then align them on a selected edge and compare selected generic signals:

```bash
scopeloop logic compare \
  --reference /srv/scopeloop/captures/reference-bundle \
  --dut /srv/scopeloop/captures/dut-bundle \
  --align-channel 6 --edge rising --threshold 1.65 \
  --signal 6 --signal 2 --signal 3 \
  --output comparison.json
```

The comparison refuses bundles whose channel sets, sample rates, threshold
mode, duration, or trigger configuration differ. It reports edge alignment,
rail settling, transition timing, UART/activity edges, and raw min/max/mean
statistics according to each channel's generic `signal_type`. It does not
decide what a particular product should do.

## Frequency policy

Waveform storage always retains raw amplitude measurements (`vpp`, `vmin`,
`vmax`, and `vrms`). It computes frequency only when metadata explicitly
classifies the signal as periodic/clock/logic/UART and requests `frequency`.
The measurement engine then requires sufficient robust amplitude, valid logic
excursions when electrical limits apply, enough cycles, and consistent edge
spacing. Rejected results use an explicit status such as
`insufficient_amplitude`, `indeterminate_logic_levels`, or `poor_edge_quality`;
they are not reported as a zero-hertz or apparent clock measurement.

## Verified native labels

Pass `--native-labels` to `scopeloop logic capture`, or `native_labels: true`
to the MCP capture tool, to label the native archive as well as its sidecar.
The workflow preserves `capture.sal`, creates `capture-labeled.sal`, opens it in
Logic and saves `capture-labeled-verified.sal`. It checks the actual saved
`meta.json` channel rows and hashes the original before/after. Only that successful
round trip sets `native_labels_applied` and `native_reopen_verified` true.

This extends the original recipe service and ports the private PowerShell helper's
archive operation. Logic has no public native label setter; the archive schema is
version-sensitive. Mismatched/missing labels or a failed reopen make the bundle
fail, with the original still available. Digital/analog views of one physical
channel must have the same name. Without the option, the untouched SAL is not
claimed to be labeled. Sidecar entries may be simple names or objects with `name`.
