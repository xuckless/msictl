"""Read-only system sensors from hwmon (+ NVIDIA GPU via nvidia-smi).

Works as any user. The Super I/O channel layout below is for MSI's Intel 600-series boards
running the nct6687d driver in its default mode (see config/sensors.d/msi-pro-h610m-g-7d46.conf).
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import time

HWMON = os.environ.get("MSICTL_HWMON", "/sys/class/hwmon")

# (channel, label, multiplier)
NCT_VOLTS = [
    ("in0", "+12V", 1), ("in1", "+5V", 1), ("in2", "+3.3V", 1),
    ("in3", "CPU Vcore", 1), ("in4", "DRAM", 2), ("in8", "CPU SA", 1), ("in9", "CPU AUX", 2),
    ("in12", "3VSB", 1),
]
NCT_TEMPS = [
    ("temp1", "cpu", "CPU (EC)"), ("temp2", "system", "System"), ("temp3", "vrm", "VRM MOS"),
    ("temp4", "pch", "PCH"), ("temp5", "socket", "CPU socket"), ("temp6", "pcie", "PCIe x1"),
    ("temp7", "m2", "M.2 slot"),
]
# Fan headers on MS-7D46: id -> (channel, label)
FANS = {"cpu": (1, "CPU_FAN1"), "sys": (3, "SYS_FAN1")}


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _int(path: str) -> int | None:
    v = _read(path)
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def hwmon_by_name() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for h in sorted(glob.glob(os.path.join(HWMON, "hwmon*"))):
        name = _read(os.path.join(h, "name"))
        if name:
            out.setdefault(name, []).append(h)
    return out


def nct_path() -> str | None:
    hw = hwmon_by_name().get("nct6687")
    return hw[0] if hw else None


def driver_info() -> dict:
    """Which Super I/O driver is bound, and can it set fan speeds."""
    p = nct_path()
    if not p:
        return {"loaded": False, "writable": False, "driver": None}
    drv = os.path.basename(os.path.realpath(os.path.join(p, "device/driver")))
    return {"loaded": True, "driver": drv, "path": p,
            "writable": os.path.exists(os.path.join(p, "pwm1_enable"))}


def gpu() -> dict | None:
    if not shutil.which("nvidia-smi"):
        return None
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,temperature.gpu,fan.speed,power.draw,utilization.gpu,clocks.gr",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    parts = [p.strip() for p in r.stdout.splitlines()[0].split(",")]

    def num(s):
        try:
            return float(s)
        except ValueError:
            return None
    return {"name": parts[0], "temp": num(parts[1]), "fan_pct": num(parts[2]),
            "power_w": num(parts[3]), "util_pct": num(parts[4]), "clock_mhz": num(parts[5])}


def cpu_package_temp() -> float | None:
    for h in hwmon_by_name().get("coretemp", []):
        for lab in glob.glob(os.path.join(h, "temp*_label")):
            if _read(lab) == "Package id 0":
                v = _int(lab.replace("_label", "_input"))
                return v / 1000 if v is not None else None
    return None


_last_stat: tuple[float, list[int]] | None = None


def cpu_usage() -> float | None:
    global _last_stat
    v = _read("/proc/stat")
    if not v:
        return None
    vals = [int(x) for x in v.splitlines()[0].split()[1:]]
    now = (time.monotonic(), vals)
    prev, _last_stat = _last_stat, now
    if not prev:
        return None
    d = [a - b for a, b in zip(vals, prev[1])]
    total = sum(d)
    idle = d[3] + (d[4] if len(d) > 4 else 0)
    return round(100 * (total - idle) / total, 1) if total else None


def temps_for_control(include_gpu: bool = True) -> dict[str, float]:
    """Temperatures a fan curve may follow, keyed by source id."""
    t: dict[str, float] = {}
    pkg = cpu_package_temp()
    if pkg is not None:
        t["cpu"] = pkg
    p = nct_path()
    if p:
        for ch, key, _ in NCT_TEMPS:
            v = _int(os.path.join(p, f"{ch}_input"))
            if v is None:
                continue
            if key == "cpu":
                t.setdefault("cpu", v / 1000)  # prefer coretemp's package sensor
            else:
                t[key] = v / 1000
    if include_gpu:
        g = gpu()
        if g and g["temp"] is not None:
            t["gpu"] = g["temp"]
    return t


SOURCES = {
    "cpu": "CPU package", "gpu": "GPU", "max": "Hottest of CPU / GPU",
    "system": "System (board)", "vrm": "VRM MOS", "pch": "Chipset (PCH)",
}


def source_temp(source: str, temps: dict[str, float]) -> float | None:
    if source == "max":
        vals = [temps[k] for k in ("cpu", "gpu") if k in temps]
        return max(vals) if vals else None
    return temps.get(source)


def fans() -> dict:
    p = nct_path()
    out = {}
    for fid, (ch, label) in FANS.items():
        rpm = _int(os.path.join(p, f"fan{ch}_input")) if p else None
        pwm = _int(os.path.join(p, f"pwm{ch}")) if p else None
        en = _int(os.path.join(p, f"pwm{ch}_enable")) if p else None
        out[fid] = {"label": label, "channel": ch, "rpm": rpm,
                    "duty": round(pwm * 100 / 255) if pwm is not None else None,
                    "mode": {1: "manual", 2: "bios"}.get(en, "bios" if en is None else str(en))}
    return out


def snapshot() -> dict:
    """Everything the GUI shows, readable without root."""
    groups: list[dict] = []
    p = nct_path()
    if p:
        groups.append({"name": "Motherboard voltages", "kind": "volt", "items": [
            {"label": lab, "value": round(v * mul / 1000, 3)}
            for ch, lab, mul in NCT_VOLTS
            if (v := _int(os.path.join(p, f"{ch}_input"))) is not None]})
        groups.append({"name": "Motherboard temperatures", "kind": "temp", "items": [
            {"label": lab, "value": v / 1000}
            for ch, _k, lab in NCT_TEMPS
            if (v := _int(os.path.join(p, f"{ch}_input"))) is not None]})
    hw = hwmon_by_name()
    cpu_items = []
    for h in hw.get("coretemp", []):
        for lab in sorted(glob.glob(os.path.join(h, "temp*_label")),
                          key=lambda s: int(os.path.basename(s)[4:].split("_")[0])):
            name = _read(lab) or "?"
            v = _int(lab.replace("_label", "_input"))
            if v is not None and (name.startswith("Package") or name.startswith("Core")):
                cpu_items.append({"label": "Package" if name.startswith("Package") else name,
                                  "value": v / 1000})
    if cpu_items:
        hottest = max(i["value"] for i in cpu_items if i["label"] != "Package")
        pkg = [i for i in cpu_items if i["label"] == "Package"]
        groups.append({"name": "CPU", "kind": "temp", "items": pkg + [{"label": "Hottest core", "value": hottest}],
                       "cores": [i for i in cpu_items if i["label"] != "Package"]})
    other = []
    for i, h in enumerate(hw.get("spd5118", [])):
        addr = os.path.basename(os.path.realpath(os.path.join(h, "device")))
        v = _int(os.path.join(h, "temp1_input"))
        if v is not None:
            other.append({"label": f"DIMM {addr.split('-')[-1]}", "value": v / 1000})
    for h in hw.get("nvme", []):
        v = _int(os.path.join(h, "temp1_input"))
        if v is not None:
            other.append({"label": "NVMe SSD", "value": v / 1000})
    if other:
        groups.append({"name": "Memory & storage", "kind": "temp", "items": other})
    g = gpu()
    return {"time": time.time(), "groups": groups, "fans": fans(), "gpu": g,
            "cpu_usage": cpu_usage(), "cpu_temp": cpu_package_temp(), "driver": driver_info()}
