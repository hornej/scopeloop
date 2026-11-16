# ScopeLoop Host Setup

ScopeLoop should run directly on the machine that owns the hardware: a macOS or
Windows workstation, or a dedicated Linux instrument host such as the LattePanda Mu.

The hardware host runs ScopeLoop in a native Python virtualenv. USB access and
vendor GUI apps stay on the host OS.

The host owns:

- USB instruments and serial devices
- Saleae Logic 2 and its automation server
- PicoScope 7 and PicoSDK drivers
- Capture storage
- The ScopeLoop Python environment

## Common Flow

From the repo checkout:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev,all-instruments]"
.venv/bin/scopeloop host status
.venv/bin/scopeloop devices --verbose
```

On Windows, use:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev,all-instruments]"
.\.venv\Scripts\scopeloop host status
.\.venv\Scripts\scopeloop devices --verbose
```

## macOS Workstation

Use macOS as a direct host while the dedicated hardware is unavailable.

1. Install Saleae Logic 2 natively.
2. Install PicoScope 7 natively.
3. Install ScopeLoop in `.venv` with `.[dev,all-instruments]`.
4. Enable the Logic 2 automation server in the Logic 2 UI, or launch Logic 2
   with automation enabled.
5. Use `~/ScopeLoop` or an external SSD for captures if large Saleae/PicoScope
   files become common.

Check readiness:

```bash
.venv/bin/scopeloop host status
```

## Windows Workstation

Windows is a reasonable direct host when the vendor apps or drivers are easier
there.

1. Install Saleae Logic 2 natively. Saleae's Windows driver files normally live
   under `C:\Program Files\Logic\Drivers`.
2. Install PicoScope 7 T&M Stable natively.
3. Install ScopeLoop in `.venv` with `.[dev,all-instruments]`.
4. Start Logic 2 with automation enabled:

```powershell
Logic.exe --automation
```

If `Logic.exe` is not on `PATH`, run it from its install directory or enable the
automation server in the Logic 2 UI.

Check readiness:

```powershell
.\.venv\Scripts\scopeloop host status
```

## LattePanda Mu Dedicated Host

For a new Mu, set it up as a dedicated instrument host. For an existing bench,
use the [update procedure](#updating-an-existing-bench) instead of reinstalling
its active environment.

Recommended baseline:

- Ubuntu 24.04 LTS
- Active cooler installed
- 500 GB or 1 TB NVMe mounted for capture storage
- eMMC reserved for OS and packages
- Wired Ethernet if available
- Static DHCP reservation or stable hostname

Suggested layout:

```text
/opt/scopeloop-control  initial source checkout and development venv
/srv/scopeloop          releases and capture artifacts on NVMe
/etc/systemd/system     optional ScopeLoop service units
```

Initial install sketch:

```bash
sudo apt update
sudo apt install -y git openssh-server python3-venv python3-pip
sudo mkdir -p /opt/scopeloop-control /srv/scopeloop
sudo chown "$USER:$USER" /opt/scopeloop-control /srv/scopeloop
git clone <repo-url> /opt/scopeloop-control
cd /opt/scopeloop-control
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev,all-instruments]"
.venv/bin/scopeloop host status --data-dir /srv/scopeloop
```

Install PicoScope 7 and Saleae Logic 2 on the host OS, then apply the Linux USB
permissions they require. Keep the capture directory on the NVMe.

### SSH access

Check both the OpenSSH socket and service before diagnosing an inactive service
as a failure; socket activation can accept connections while the service is idle:

```bash
systemctl status ssh.socket ssh.service
ip -br addr
```

If a dedicated host should use a persistent listener instead, make the change
from its local console during setup so a connection interruption is recoverable:

```bash
sudo systemctl disable --now ssh.socket
sudo systemctl enable --now ssh.service
systemctl status ssh.service
```

After moving the Ethernet cable between ports, check the current address and
DHCP reservation rather than assuming the previous interface address still works.

### USB audio capture

When the operator needs direct ALSA capture access, install `alsa-utils` and
grant access through the host's audio-device group policy. On hosts using the
`audio` group, add the operator and start a new login session:

```bash
sudo usermod -aG audio "$USER"
id
arecord -l
```

Select the microphone by its ALSA card name from `arecord -l`, rather than a
numeric index that can change with USB enumeration. Store that name, microphone
identity and the verified mixer setting in private bench configuration. For a
device supporting 48 kHz, 16-bit mono, an example capture is:

```bash
audio_card=YOUR_CARD_NAME
amixer -c "$audio_card" scontents
arecord -q -D "hw:CARD=$audio_card,DEV=0" \
  -f S16_LE -r 48000 -c 1 -d 20 capture.wav
```

Record gain, sample format, microphone position, DUT configuration, load and
ambient conditions with every capture. Confirm the actual mixer dB setting;
numeric volume values are device-specific. Without an acoustic calibrator,
report spectral tones and relative levels in dBFS, not calibrated dBA or SPL.

## Updating an existing bench

Follow [bench operations](bench-operations.md#release-and-rollback) for versioned,
rollback-capable runtime updates. Discover the authoritative installed environment
instead of assuming that either a checkout or a similarly named directory is live.
Keep large Linux build/capture data on the designated NVMe volume. See
[scope evidence](scope-capture.md) for the CLI/MCP acquisition workflow.
