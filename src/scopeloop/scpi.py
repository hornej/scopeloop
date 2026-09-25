"""Strict numeric parsing for the Siglent short, long and header-off replies."""

from __future__ import annotations

import math
import re

from scopeloop.instruments.base import InstrumentError

_NUMBER = re.compile(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([A-Za-zµμ/%]*)")
_UNITS = {"": 1, "V": 1, "A": 1, "S": 1, "s": 1, "Hz": 1, "%": 1, "Sa/s": 1, "pts": 1}
for _prefix, _factor in {
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "m": 1e-3,
    "k": 1e3,
    "K": 1e3,
    "M": 1e6,
    "G": 1e9,
}.items():
    for _unit in ("V", "A", "s", "S", "Hz", "Sa/s", "pts"):
        _UNITS[_prefix + _unit] = _factor
# SCPI input accepts uppercase time units, without confusing mV with MV.
_UNITS.update({"NS": 1e-9, "US": 1e-6, "MS": 1e-3})


def number(response: str, prefix: str | None = None) -> float:
    value = response.strip()
    aliases = {
        "VDIV": "VOLT_DIV",
        "OFST": "VOLT_OFFSET",
        "TDIV": "TIME_DIV",
        "TRDL": "TRIG_DELAY",
        "SARA": "SAMPLE_RATE",
        "ATTN": "ATTENUATION",
    }
    if prefix:
        short = prefix.split(":")[-1]
        long = prefix.removesuffix(short) + aliases.get(short, short)
        if value.upper().startswith(long.upper() + " "):
            value = prefix + value[len(long) :]
    if prefix and value.upper().startswith(prefix.upper() + " "):
        value = value[len(prefix) :].strip()
    match = _NUMBER.fullmatch(value.replace("µ", "u").replace("μ", "u"))
    if not match or match[2] not in _UNITS:
        raise InstrumentError(f"Invalid numeric SCPI response: {response!r}")
    result = float(match[1]) * _UNITS[match[2]]
    if not math.isfinite(result):
        raise InstrumentError(f"Non-finite SCPI value: {response!r}")
    return result


def integer(response: str, prefix: str | None = None) -> int:
    value = number(response, prefix)
    if value != int(value) or value < 0:
        raise InstrumentError(f"Invalid integer SCPI response: {response!r}")
    return int(value)
