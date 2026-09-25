# ScopeLoop Host Setup

ScopeLoop should run directly on the machine that owns the hardware. For now
that can be a macOS or Windows workstation. When the LattePanda Mu arrives, use
it as a dedicated Linux instrument host.

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

When the Mu arrives, set it up as a single-purpose Linux host.

Recommended baseline:

- Ubuntu 24.04 LTS
- Active cooler installed
- 500 GB or 1 TB NVMe mounted for capture storage
- eMMC reserved for OS and packages
- Wired Ethernet if available
- Static DHCP reservation or stable hostname

Suggested layout:

```text
/opt/scopeloop        repo checkout and venv
/srv/scopeloop        capture artifacts on NVMe
/etc/systemd/system   optional ScopeLoop service units
```

Initial install sketch:

```bash
sudo apt update
sudo apt install -y git python3-venv python3-pip
sudo mkdir -p /opt/scopeloop /srv/scopeloop
sudo chown -R "$USER:$USER" /opt/scopeloop /srv/scopeloop
git clone <repo-url> /opt/scopeloop
cd /opt/scopeloop
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev,all-instruments]"
.venv/bin/scopeloop host status --data-dir /srv/scopeloop
```

Install PicoScope 7 and Saleae Logic 2 on the host OS, then apply the Linux USB
permissions they require. Keep the capture directory on the NVMe.

## Updating an existing bench

Follow [bench operations](bench-operations.md#release-and-rollback) for versioned,
rollback-capable runtime updates. Discover the authoritative installed environment
instead of assuming that either a checkout or a similarly named directory is live.
Keep large Linux build/capture data on the designated NVMe volume. See
[scope evidence](scope-capture.md) for the CLI/MCP acquisition workflow.
