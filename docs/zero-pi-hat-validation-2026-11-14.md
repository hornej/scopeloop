# Zero Pi HAT implementation verification

Date: November 14, 2026 (America/Denver).

Implementation commit: `c5a5850`, based on upstream `f91f732`. Work was isolated
from the older, dirty main checkout. No active ScopeLoop launcher, service,
firmware, GPIO owner or DUT power setting was replaced.

| Check | Result |
| --- | --- |
| Windows installed-wheel full suite, Python 3.12.10 | 211 passed, 1 Linux-only socket test skipped |
| Linux installed-wheel HAT suite, Python 3.13.5 | 25 passed, including real Unix socket transport with fake GPIO |
| CLI discovery | `hat inspect`, `serve`, `status`, `output`, `cycle` present |
| Actual stdio MCP initialize/tools-list, both hosts | 37 tools, including all three HAT tools |
| Dependency checks, both staging environments | `pip check` passed |
| Changed-file Ruff and final whitespace/link checks | Passed |
| Read-only physical Pi inspection | Pi 4 identified; GPIO7 still owned by the existing off holder, output low before and after; GPIO8 input high |
| New service rail switching or takeover | Not run; the bench was deliberately left off |
| New analog-only capture against physical Saleae | Not run; regression uses replayed API shape |

The staged Pi environment used libgpiod 2.6.0. The existing bench helper and
off holder retain their original system libgpiod 2.2.0 environment. Inspection
requested no GPIO lines. The module import paths were verified in each staged
environment, not inferred from a source checkout. Host identities and full
environment records stay in private staging/validation files.

Wheel: `scopeloop-0.3.0-py3-none-any.whl`, SHA-256
`66565355c73c2245c7c455614eb041fc1ddc8715ce9733907201f0da8b65bdd8`.
Rebuilding after the implementation commit produced the same tested wheel hash.
The wheel transferred to the Pi matched that hash. The installed HAT module on
both systems matched source SHA-256
`6979c723da053df500852363b0830b8e8e82e9d69285fa97fff930f902697741`.

Tests cover unknown revisions, missing fixture identity/load configuration,
busy GPIO and alternate pin functions, atomic initial-low requests, persistent
ownership across calls, fault/readback failures, off-before-read behavior,
cancelled cycles, competing clients, malformed requests, config mismatch,
socket preservation when a second owner attempts startup, and disconnected
clients. The full suite also covers the analog-only logging fix and existing
ScopeLoop paths.

Remaining physical qualification: migrate from the old holder with upstream
power removed, exercise the new owner's off/on/cycle paths on an agreed load,
measure the switched rail, and exercise a real fault indication. v0.0.1 remains
unsupported until its mapping is established. See [operation and migration](zero-pi-hat.md).

Other reusable follow-ups from the antenna-box bench are a native PPK2
source/capture driver with final-state publication and retained calibration/raw
samples, and first-class lossless compression of large CSV evidence with hash
verification. Those need their own driver/evidence work; they were not silently
folded into this HAT implementation. Product-specific SPI/TRAMP and RF-tuning
findings remain in the antenna-box repository.
