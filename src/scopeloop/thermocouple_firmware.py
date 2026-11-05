"""Copy to CircuitPython as code.py; private wiring/limits live in JSON.

No output actuation, automatic fault clearing, hub reset, or DUT restart.
The pure state machine is also importable on CPython for failure replay.
"""

import json
import math
import time

PROTOCOL = "scopeloop-max31856-v1"
FIRMWARE = "scopeloop-max31856-3"


class SensorError(Exception):
    def __init__(self, code, detail):
        self.code = code
        self.detail = detail
        super().__init__(code)


class Thermocouple:
    def __init__(self, factory, device, boot, low_c, high_c, noise_hz=60, clock=time):
        if not all(
            not isinstance(x, bool) and math.isfinite(x) and int(x) == x for x in (low_c, high_c)
        ):
            raise ValueError("Thresholds must be finite integer Celsius values")
        if not -2048 <= low_c < high_c <= 2047 or noise_hz not in (50, 60):
            raise ValueError("Invalid MAX31856 thresholds/filter")
        self.factory, self.device, self.boot = factory, device, boot
        self.low_c, self.high_c, self.noise_hz = low_c, high_c, noise_hz
        self.clock = clock
        self.sensor = None
        self.seq = 0
        self.latched = False
        self.fault_id = None
        self.first_fault = None
        self.recovery = None
        self.expected = {0: 0x10 | int(noise_hz == 50), 1: 0x03, 2: 0, 9: 0}
        try:
            self.configure()
        except Exception as error:
            self.latch(error)

    def registers(self):
        # Adafruit's shared SPI buffer is small: read each register separately.
        result = {}
        for address in range(16):
            try:
                result[str(address)] = self.sensor._read_register(address, 1)[0]
            except Exception as error:
                result[str(address)] = {"error": repr(error)}
        return result

    def check_config(self):
        for address, expected in self.expected.items():
            actual = self.sensor._read_register(address, 1)[0]
            if actual != expected:
                raise SensorError(
                    "configuration_mismatch",
                    {
                        "register": address,
                        "expected": expected,
                        "actual": actual,
                    },
                )
        actual = self.sensor.temperature_thresholds
        if tuple(actual) != (self.low_c, self.high_c):
            raise SensorError(
                "threshold_mismatch",
                {
                    "expected": [self.low_c, self.high_c],
                    "actual": list(actual),
                },
            )

    def configure(self):
        self.sensor = self.factory()
        self.sensor._write_u8(0, self.expected[0])
        self.sensor._write_u8(1, 0x03)  # Type K, averaging 1.
        self.sensor._write_u8(2, 0)  # Do not mask hardware faults.
        self.sensor._write_u8(9, 0)  # No inferred calibration offset.
        self.sensor.temperature_thresholds = (self.low_c, self.high_c)
        self.check_config()

    def latch(self, error):
        detail = {
            "code": getattr(error, "code", "sensor_io_error"),
            "message": repr(error),
            "detail": getattr(error, "detail", {}),
            "registers": self.registers(),
        }
        if not self.latched:
            self.fault_id = self.boot + ":" + str(self.seq)
            self.first_fault = detail
        self.latched = True
        if self.recovery and self.recovery["state"] == "verifying":
            self.recovery["state"] = "failed"
        return detail

    def sample(self):
        self.seq += 1
        started = self.clock.monotonic()
        row = {
            "kind": "temperature",
            "protocol": PROTOCOL,
            "firmware": FIRMWARE,
            "device": self.device,
            "boot": self.boot,
            "seq": self.seq,
            "uptime_s": started,
            "type": "K",
            "low_limit_c": self.low_c,
            "high_limit_c": self.high_c,
            "valid": False,
            "latched": self.latched,
        }
        try:
            self.check_config()
            self.sensor.initiate_one_shot_measurement()
            if not self.sensor.oneshot_pending:
                raise SensorError("conversion_not_started", {})
            while self.sensor.oneshot_pending:
                if self.clock.monotonic() - started >= 0.8:
                    raise SensorError("conversion_timeout", {})
                self.clock.sleep(0.01)
            # Also detect changes occurring during conversion.
            self.check_config()
            tc = self.sensor.unpack_temperature()
            cj = self.sensor.unpack_reference_temperature()
            if not math.isfinite(tc) or not math.isfinite(cj):
                raise SensorError("nonfinite_temperature", {})
            row.update(
                tc_c=tc,
                cj_c=cj,
                conversion_s=self.clock.monotonic() - started,
                fault_mask=self.sensor._read_register(15, 1)[0],
                faults=self.sensor.fault,
            )
            if row["fault_mask"] or any(row["faults"].values()):
                raise SensorError(
                    "hardware_fault", {"mask": row["fault_mask"], "flags": row["faults"]}
                )
            if tc >= self.high_c or tc <= self.low_c:
                raise SensorError("temperature_limit", {"temperature_c": tc})
            if self.recovery and self.recovery["state"] == "verifying":
                self.recovery["good_samples"] += 1
                if self.recovery["good_samples"] >= 3:
                    self.latched = False
                    self.fault_id = None
                    self.recovery["state"] = "verified"
            row["valid"] = not self.latched
        except Exception as error:
            row["error"] = self.latch(error)
        row.update(latched=self.latched, fault_id=self.fault_id)
        if self.first_fault:
            row["fault_detail"] = self.first_fault
        if self.recovery:
            row["recovery"] = dict(self.recovery)
        return row

    def command(self, request):
        reply = {
            "kind": "thermocouple",
            "protocol": PROTOCOL,
            "firmware": FIRMWARE,
            "device": self.device,
            "boot": self.boot,
            "id": request.get("id"),
            "ok": False,
        }
        action = request.get("thermocouple")
        if action == "status":
            reply.update(
                ok=True,
                registers=self.registers(),
                expected=self.expected,
                latched=self.latched,
                fault_id=self.fault_id,
                fault_detail=self.first_fault,
                recovery=self.recovery,
            )
        elif action == "recover":
            if not self.latched or request.get("fault_id") != self.fault_id:
                reply["error"] = "A matching current fault_id is required"
            elif request.get("boot") != self.boot or request.get("dut_idle") is not True:
                reply["error"] = "Matching boot and explicit dut_idle=true are required"
            elif self.recovery and self.recovery["state"] == "verifying":
                reply["error"] = "Recovery already in progress"
            else:
                self.recovery = {
                    "id": request.get("id"),
                    "state": "verifying",
                    "good_samples": 0,
                }
                try:
                    self.configure()
                    reply.update(ok=True, recovery=dict(self.recovery))
                except Exception as error:
                    reply["error"] = self.latch(error)
        else:
            reply["error"] = "Unknown command; supported commands: status, recover"
        return reply


def main():
    import os
    import sys

    import adafruit_max31856
    import board
    import digitalio
    import microcontroller
    import supervisor

    supervisor.runtime.autoreload = False
    with open("thermocouple_config.json") as handle:
        config = json.load(handle)
    cs = digitalio.DigitalInOut(getattr(board, config["cs_pin"]))
    spi = board.SPI()
    device = "".join(f"{n:02X}" for n in microcontroller.cpu.uid)
    boot = "".join(f"{n:02x}" for n in os.urandom(8))
    engine = Thermocouple(
        lambda: adafruit_max31856.MAX31856(spi, cs, adafruit_max31856.ThermocoupleType.K),
        device,
        boot,
        config["low_c"],
        config["high_c"],
        config.get("noise_hz", 60),
    )
    print(
        json.dumps(
            {
                "kind": "identity",
                "protocol": PROTOCOL,
                "firmware": FIRMWARE,
                "device": device,
                "boot": boot,
                "type": "K",
                "config": config,
                "runtime": sys.version,
            }
        )
    )
    line = ""
    discarding = False
    while True:
        started = time.monotonic()
        # Bounded input work cannot starve temperature acquisition.
        for _ in range(512):
            if not supervisor.runtime.serial_bytes_available:
                break
            char = sys.stdin.read(1)
            if char in ("\r", "\n"):
                if not discarding and line and not line.endswith("#"):
                    try:
                        request = json.loads(line)
                        if not isinstance(request, dict):
                            raise ValueError("JSON object required")
                        print(json.dumps(engine.command(request)))
                    except (ValueError, TypeError) as error:
                        print(
                            json.dumps(
                                {
                                    "kind": "thermocouple",
                                    "ok": False,
                                    "error": repr(error),
                                }
                            )
                        )
                line, discarding = "", False
            elif not discarding:
                line += char
                if len(line) > 512:
                    line, discarding = "", True
                    print(
                        json.dumps(
                            {
                                "kind": "thermocouple",
                                "ok": False,
                                "error": "command_too_long",
                            }
                        )
                    )
        print(json.dumps(engine.sample()))
        time.sleep(max(0, 0.5 - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
