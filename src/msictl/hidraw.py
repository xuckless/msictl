"""Minimal Linux hidraw access: find a device by USB ID, exchange feature/output reports.

Dependency-free: uses /sys for discovery and ioctl() for feature reports.
"""
from __future__ import annotations

import fcntl
import glob
import os
import select


class DeviceError(RuntimeError):
    pass


def _ioc(direction: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord("H") << 8) | nr


def _hidiocsfeature(n: int) -> int:  # _IOC(_IOC_WRITE|_IOC_READ, 'H', 0x06, len)
    return _ioc(3, 0x06, n)


def _hidiocgfeature(n: int) -> int:  # _IOC(_IOC_WRITE|_IOC_READ, 'H', 0x07, len)
    return _ioc(3, 0x07, n)


def find(vid: int, pid: int, sysfs: str = "/sys/class/hidraw") -> list[str]:
    """Return /dev/hidrawN paths whose HID_ID matches vid:pid."""
    out = []
    for node in sorted(glob.glob(os.path.join(sysfs, "hidraw*"))):
        try:
            uevent = open(os.path.join(node, "device/uevent")).read()
        except OSError:
            continue
        for line in uevent.splitlines():
            if line.startswith("HID_ID="):
                try:
                    _bus, v, p = (int(x, 16) for x in line[7:].split(":"))
                except ValueError:
                    break
                if v == vid and p == pid:
                    out.append("/dev/" + os.path.basename(node))
    return out


class Hidraw:
    def __init__(self, path: str):
        self.path = path
        self.fd: int | None = None

    def open(self) -> "Hidraw":
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError:
            raise DeviceError(f"No permission to open {self.path}. Run the installer "
                              "(it adds a udev rule), then replug or reboot.") from None
        except FileNotFoundError:
            raise DeviceError(f"{self.path} disappeared") from None
        return self

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *a):
        self.close()

    def get_feature(self, report_id: int, length: int) -> bytes:
        """Read a feature report; `length` includes the report-id byte."""
        buf = bytearray(length)
        buf[0] = report_id
        n = fcntl.ioctl(self.fd, _hidiocgfeature(length), buf)
        if n != length:
            raise DeviceError(f"short feature read: {n}/{length} bytes")
        return bytes(buf)

    def set_feature(self, data: bytes) -> None:
        """Send a feature report; data[0] is the report id."""
        buf = bytearray(data)
        n = fcntl.ioctl(self.fd, _hidiocsfeature(len(buf)), buf)
        if n != len(buf):
            raise DeviceError(f"short feature write: {n}/{len(buf)} bytes")

    def write(self, data: bytes) -> None:
        os.write(self.fd, data)

    def read(self, timeout: float) -> bytes | None:
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        return os.read(self.fd, 64)
