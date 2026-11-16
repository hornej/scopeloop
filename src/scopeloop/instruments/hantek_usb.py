"""Hantek bulk USB transport. Windows uses the built-in WinUSB driver.

No vendor DLL is loaded and discovery never installs or changes a driver.
The protocol driver owns the host lease and serializes these synchronous calls.
"""

from __future__ import annotations

import ctypes as c
import os
import struct
import uuid

from scopeloop.instruments.base import InstrumentError

VID, PID = 0x0483, 0x2D42


class USBError(InstrumentError):
    pass


def _bind(dll, name, result, *args):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = result, args
    return fn


def _check(ok, operation):
    if not ok:
        raise USBError(f"{operation}: {c.WinError(c.get_last_error())}")


class WinUSBTransport:
    def __init__(self, path: str | None = None, timeout_ms: int = 1500):
        self.path, self.timeout_ms = path, timeout_ms
        self._explicit_path = path
        self.handle = None
        self.usb_handle = c.c_void_p()

    @staticmethod
    def discover() -> list[str]:
        if os.name != "nt":
            raise USBError("WinUSB discovery requires Windows")
        from ctypes import wintypes as w

        class Interface(c.Structure):
            _fields_ = [
                ("size", w.DWORD),
                ("guid", c.c_byte * 16),
                ("flags", w.DWORD),
                ("reserved", c.c_void_p),
            ]

        api = c.WinDLL("setupapi", use_last_error=True)
        guid = (c.c_byte * 16).from_buffer_copy(
            uuid.UUID("a5dcbf10-6530-11d2-901f-00c04fb951ed").bytes_le
        )
        get = _bind(api, "SetupDiGetClassDevsW", w.HANDLE, c.c_void_p, w.LPCWSTR, w.HWND, w.DWORD)
        enum = _bind(
            api,
            "SetupDiEnumDeviceInterfaces",
            w.BOOL,
            w.HANDLE,
            c.c_void_p,
            c.c_void_p,
            w.DWORD,
            c.POINTER(Interface),
        )
        detail = _bind(
            api,
            "SetupDiGetDeviceInterfaceDetailW",
            w.BOOL,
            w.HANDLE,
            c.POINTER(Interface),
            c.c_void_p,
            w.DWORD,
            c.POINTER(w.DWORD),
            c.c_void_p,
        )
        destroy = _bind(api, "SetupDiDestroyDeviceInfoList", w.BOOL, w.HANDLE)
        devices = get(c.byref(guid), None, None, 0x12)
        _check(devices != c.c_void_p(-1).value, "enumerate USB interfaces")
        paths = []
        try:
            index = 0
            while True:
                entry = Interface()
                entry.size = c.sizeof(entry)
                if not enum(devices, None, c.byref(guid), index, c.byref(entry)):
                    if c.get_last_error() == 259:
                        break
                    _check(False, "enumerate USB interface")
                index += 1
                needed = w.DWORD()
                detail(devices, c.byref(entry), None, 0, c.byref(needed), None)
                if needed.value < 8 or needed.value > 65536:
                    raise USBError("Invalid USB interface detail size")
                buf = c.create_string_buffer(needed.value)
                c.cast(buf, c.POINTER(w.DWORD))[0] = 8 if c.sizeof(c.c_void_p) == 8 else 6
                _check(
                    detail(devices, c.byref(entry), buf, needed, None, None),
                    "read USB interface path",
                )
                path = c.wstring_at(c.addressof(buf) + 4)
                if "vid_0483&pid_2d42" in path.lower():
                    paths.append(path)
        finally:
            destroy(devices)
        return sorted(set(paths))

    def open(self) -> dict:
        if os.name != "nt":
            raise USBError("WinUSB requires Windows; select the libusb transport on Linux")
        from ctypes import wintypes as w

        if self.handle is not None:
            raise USBError("Transport already open")
        self.path = self._explicit_path
        if self.path is None:
            paths = self.discover()
            if len(paths) != 1:
                raise USBError(f"Expected one Hantek interface, found {len(paths)}; set usb_path")
            self.path = paths[0]
        if "vid_0483&pid_2d42" not in self.path.lower():
            raise USBError("USB path does not identify a Hantek 2000 device")
        k32 = c.WinDLL("kernel32", use_last_error=True)
        usb = c.WinDLL("winusb", use_last_error=True)
        void, ptr, byte = c.c_void_p, c.POINTER, c.c_ubyte
        create = _bind(
            k32,
            "CreateFileW",
            w.HANDLE,
            w.LPCWSTR,
            w.DWORD,
            w.DWORD,
            void,
            w.DWORD,
            w.DWORD,
            w.HANDLE,
        )
        self._close = _bind(k32, "CloseHandle", w.BOOL, w.HANDLE)
        initialize = _bind(usb, "WinUsb_Initialize", w.BOOL, w.HANDLE, ptr(void))
        self._free = _bind(usb, "WinUsb_Free", w.BOOL, void)
        self._read = _bind(
            usb, "WinUsb_ReadPipe", w.BOOL, void, byte, void, w.ULONG, ptr(w.ULONG), void
        )
        self._write = _bind(
            usb, "WinUsb_WritePipe", w.BOOL, void, byte, void, w.ULONG, ptr(w.ULONG), void
        )
        descriptor = _bind(
            usb,
            "WinUsb_GetDescriptor",
            w.BOOL,
            void,
            byte,
            byte,
            w.USHORT,
            void,
            w.ULONG,
            ptr(w.ULONG),
        )
        policy = _bind(usb, "WinUsb_SetPipePolicy", w.BOOL, void, byte, w.ULONG, w.ULONG, void)

        class Interface(c.Structure):
            _fields_ = [
                (name, byte)
                for name in [
                    "length",
                    "type",
                    "number",
                    "alt",
                    "endpoints",
                    "cls",
                    "subcls",
                    "protocol",
                    "string",
                ]
            ]

        class Pipe(c.Structure):
            _fields_ = [
                ("kind", c.c_int),
                ("address", byte),
                ("size", w.USHORT),
                ("interval", byte),
            ]

        interface = _bind(usb, "WinUsb_QueryInterfaceSettings", w.BOOL, void, byte, ptr(Interface))
        pipe_info = _bind(usb, "WinUsb_QueryPipe", w.BOOL, void, byte, byte, ptr(Pipe))
        self.handle = create(self.path, 0xC0000000, 3, None, 3, 0x40000000, None)
        if self.handle == void(-1).value:
            self.handle = None
            _check(False, "open Hantek WinUSB interface")
        try:
            _check(
                initialize(self.handle, c.byref(self.usb_handle)),
                "initialize WinUSB (device must use the WinUSB driver)",
            )
            data, count = (byte * 18)(), w.ULONG()
            _check(
                descriptor(self.usb_handle, 1, 0, 0, data, 18, c.byref(count)),
                "read USB descriptor",
            )
            raw = bytes(data[: count.value])
            if len(raw) != 18 or struct.unpack_from("<HH", raw, 8) != (VID, PID):
                raise USBError("Unexpected USB device identity")
            settings = Interface()
            _check(interface(self.usb_handle, 0, c.byref(settings)), "read USB interface")
            pipes = {}
            for i in range(settings.endpoints):
                p = Pipe()
                _check(pipe_info(self.usb_handle, 0, i, c.byref(p)), "read USB endpoint")
                pipes[p.address] = (p.kind, p.size)
            if pipes.get(2) != (2, 64) or pipes.get(0x81) != (2, 64):
                raise USBError("Unexpected Hantek bulk endpoints")
            timeout = w.ULONG(self.timeout_ms)
            for ep in (2, 0x81):
                _check(policy(self.usb_handle, ep, 3, 4, c.byref(timeout)), "set USB timeout")
            return {"transport": "winusb", "path": self.path, "descriptor_hex": raw.hex()}
        except BaseException:
            self.close()
            raise

    def write(self, packet: bytes) -> None:
        from ctypes import wintypes as w

        n = w.ULONG()
        _check(
            self._write(self.usb_handle, 2, packet, len(packet), c.byref(n), None),
            "write Hantek command",
        )
        if n.value != len(packet):
            raise USBError(f"Short USB write: {n.value}/{len(packet)}")

    def read(self, size: int = 64) -> bytes:
        from ctypes import wintypes as w

        buf, n = (c.c_ubyte * size)(), w.ULONG()
        _check(
            self._read(self.usb_handle, 0x81, buf, size, c.byref(n), None), "read Hantek response"
        )
        return bytes(buf[: n.value])

    def close(self) -> None:
        if self.usb_handle.value:
            self._free(self.usb_handle)
            self.usb_handle = c.c_void_p()
        if self.handle is not None:
            self._close(self.handle)
            self.handle = None


class LibUSBTransport:
    """Optional PyUSB transport; requires a host-provided libusb backend."""

    def __init__(self, serial: str | None = None, timeout_ms: int = 1500):
        self.serial, self.timeout_ms, self.device = serial, timeout_ms, None

    def open(self) -> dict:
        try:
            import usb.core
            import usb.util
        except ImportError as exc:
            raise USBError("Install scopeloop[hantek] and libusb for non-Windows USB") from exc
        devices = list(usb.core.find(find_all=True, idVendor=VID, idProduct=PID))
        if self.serial:
            devices = [d for d in devices if d.serial_number == self.serial]
        if len(devices) != 1:
            raise USBError(f"Expected one matching Hantek, found {len(devices)}")
        self.device = devices[0]
        try:
            cfg = self.device.get_active_configuration()
            intf = cfg[(0, 0)]
            pipes = {p.bEndpointAddress: (p.bmAttributes & 3, p.wMaxPacketSize) for p in intf}
            if pipes.get(2) != (2, 64) or pipes.get(0x81) != (2, 64):
                raise USBError("Unexpected Hantek bulk endpoints")
            usb.util.claim_interface(self.device, 0)
            return {
                "transport": "libusb",
                "bus": self.device.bus,
                "address": self.device.address,
                "usb_serial": self.device.serial_number,
            }
        except BaseException:
            self.close()
            raise

    def write(self, packet: bytes) -> None:
        if self.device.write(2, packet, self.timeout_ms) != len(packet):
            raise USBError("Short Hantek USB write")

    def read(self, size: int = 64) -> bytes:
        return bytes(self.device.read(0x81, size, self.timeout_ms))

    def close(self) -> None:
        if self.device is not None:
            import usb.util

            usb.util.dispose_resources(self.device)
            self.device = None
