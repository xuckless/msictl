"""CPU power: RAPL package limits (root, via the daemon) and power profiles (user, via D-Bus)."""
from __future__ import annotations

import os
import re
import subprocess
import time

RAPL = os.environ.get("MSICTL_RAPL", "/sys/class/powercap/intel-rapl:0")

# Intel's specified limits for the i7-14700F are PL1 65 W / PL2 219 W. Never allow more than that,
# and never so little that the machine becomes unusable.
PL1_RANGE = (25, 125)
PL2_RANGE = (25, 219)

PPD = ["--system", "--dest", "org.freedesktop.UPower.PowerProfiles",
       "--object-path", "/org/freedesktop/UPower/PowerProfiles"]
PROFILES = ["power-saver", "balanced", "performance"]


def _r(name: str) -> int | None:
    try:
        with open(os.path.join(RAPL, name)) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _w(name: str, value: int) -> None:
    with open(os.path.join(RAPL, name), "w") as f:
        f.write(str(int(value)))


def _constraint(label: str) -> int | None:
    for i in range(3):
        try:
            with open(os.path.join(RAPL, f"constraint_{i}_name")) as f:
                if f.read().strip() == label:
                    return i
        except OSError:
            break
    return None


def limits() -> dict:
    out = {}
    for key, label in (("pl1", "long_term"), ("pl2", "short_term")):
        i = _constraint(label)
        uw = _r(f"constraint_{i}_power_limit_uw") if i is not None else None
        out[key] = round(uw / 1e6, 1) if uw is not None else None
    out["enabled"] = _r("enabled") == 1
    return out


def set_limits(pl1: float, pl2: float) -> dict:
    """Root only. Returns the values read back (the BIOS may clamp or lock them)."""
    if not PL1_RANGE[0] <= pl1 <= PL1_RANGE[1]:
        raise ValueError(f"PL1 must be {PL1_RANGE[0]}..{PL1_RANGE[1]} W")
    if not PL2_RANGE[0] <= pl2 <= PL2_RANGE[1]:
        raise ValueError(f"PL2 must be {PL2_RANGE[0]}..{PL2_RANGE[1]} W")
    if pl2 < pl1:
        raise ValueError("PL2 (burst) can't be lower than PL1 (sustained)")
    i1, i2 = _constraint("long_term"), _constraint("short_term")
    if i1 is None or i2 is None:
        raise ValueError("RAPL package limits not available")
    # order matters when lowering vs raising: keep PL2 >= PL1 at every step
    seq = [(i2, pl2), (i1, pl1)] if pl2 >= (limits()["pl2"] or 0) else [(i1, pl1), (i2, pl2)]
    for i, w in seq:
        _w(f"constraint_{i}_power_limit_uw", int(w * 1e6))
    return limits()


class PowerMeter:
    """Package power from energy_uj deltas (root-readable only)."""

    def __init__(self):
        self.last: tuple[float, int] | None = None

    def sample(self) -> float | None:
        e = _r("energy_uj")
        if e is None:
            return None
        now = time.monotonic()
        prev, self.last = self.last, (now, e)
        if not prev or now <= prev[0]:
            return None
        de = e - prev[1]
        if de < 0:  # counter wrapped
            de += _r("max_energy_range_uj") or 0
        return round(de / 1e6 / (now - prev[0]), 1)


def profile() -> dict:
    try:
        r = subprocess.run(["gdbus", "call", *PPD, "--method", "org.freedesktop.DBus.Properties.Get",
                            "org.freedesktop.UPower.PowerProfiles", "ActiveProfile"],
                           capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False}
    m = re.search(r"'([a-z-]+)'", r.stdout)
    if r.returncode != 0 or not m:
        return {"available": False}
    return {"available": True, "active": m.group(1), "profiles": PROFILES}


def set_profile(name: str) -> dict:
    if name not in PROFILES:
        raise ValueError(f"profile must be one of {', '.join(PROFILES)}")
    r = subprocess.run(["gdbus", "call", *PPD, "--method", "org.freedesktop.DBus.Properties.Set",
                        "org.freedesktop.UPower.PowerProfiles", "ActiveProfile", f"<'{name}'>"],
                       capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        raise ValueError(r.stderr.strip() or "could not change power profile")
    return profile()
