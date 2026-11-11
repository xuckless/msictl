"""Fan curve model, presets and validation (shared by the daemon and the GUI)."""
from __future__ import annotations

import copy

from .sensors import FANS, SOURCES

# Hard safety limits enforced by the daemon regardless of what a client asks for.
CPU_FAN_FLOOR = 20        # % never go below on CPU_FAN1 while software-controlled
SYS_FAN_STOP = 25         # % below this the case fans (behind the RGB hub) stop spinning (measured)
EMERGENCY_TEMP = 90.0     # °C on CPU or GPU -> every controlled fan to 100 %
MAX_POINTS = 8

PRESETS = {
    "silent": {
        "cpu": [[30, 20], [50, 25], [65, 40], [75, 60], [85, 100]],
        "sys": [[30, 30], [50, 32], [65, 40], [75, 60], [85, 100]],
    },
    "balanced": {
        "cpu": [[30, 25], [50, 35], [65, 55], [75, 75], [85, 100]],
        "sys": [[30, 30], [50, 38], [65, 50], [75, 70], [85, 100]],
    },
    "performance": {
        "cpu": [[30, 40], [50, 55], [65, 75], [75, 90], [80, 100]],
        "sys": [[30, 40], [50, 50], [65, 70], [75, 90], [80, 100]],
    },
}

DEFAULT_FAN = {"mode": "bios", "duty": 50, "source": "cpu", "points": PRESETS["balanced"]["cpu"],
               "min_duty": 0}
DEFAULT_CONFIG = {
    "fans": {
        "cpu": {**DEFAULT_FAN, "source": "cpu", "points": PRESETS["balanced"]["cpu"], "min_duty": CPU_FAN_FLOOR},
        "sys": {**DEFAULT_FAN, "source": "max", "points": PRESETS["balanced"]["sys"], "min_duty": 30},
    },
}


def default_config() -> dict:
    return copy.deepcopy(DEFAULT_CONFIG)


def duty_for(points: list[list[float]], temp: float) -> float:
    """Linear interpolation along the curve; flat beyond both ends."""
    if temp <= points[0][0]:
        return points[0][1]
    for (t0, d0), (t1, d1) in zip(points, points[1:]):
        if temp <= t1:
            return d0 + (d1 - d0) * (temp - t0) / (t1 - t0) if t1 > t0 else d1
    return points[-1][1]


def validate_fan(fid: str, f: dict) -> dict:
    if fid not in FANS:
        raise ValueError(f"unknown fan {fid!r}")
    mode = f.get("mode", "bios")
    if mode not in ("bios", "fixed", "curve"):
        raise ValueError("mode must be bios, fixed or curve")
    out = {"mode": mode}
    duty = float(f.get("duty", 50))
    if not 0 <= duty <= 100:
        raise ValueError("duty must be 0..100 %")
    out["duty"] = duty
    src = f.get("source", "cpu")
    if src not in SOURCES:
        raise ValueError(f"unknown temperature source {src!r}")
    out["source"] = src
    pts = f.get("points", DEFAULT_FAN["points"])
    if not isinstance(pts, list) or not 2 <= len(pts) <= MAX_POINTS:
        raise ValueError(f"a curve needs 2..{MAX_POINTS} points")
    clean = []
    for p in pts:
        t, d = float(p[0]), float(p[1])
        if not (0 <= t <= 105 and 0 <= d <= 100):
            raise ValueError("curve points must be 0..105 °C and 0..100 %")
        clean.append([t, d])
    clean.sort(key=lambda p: p[0])
    if any(b[0] <= a[0] for a, b in zip(clean, clean[1:])):
        raise ValueError("curve temperatures must all be different")
    out["points"] = clean
    mn = float(f.get("min_duty", 0))
    if not 0 <= mn <= 100:
        raise ValueError("min_duty must be 0..100 %")
    if fid == "cpu":
        mn = max(mn, CPU_FAN_FLOOR)
    out["min_duty"] = mn
    return out


def validate_config(cfg: dict) -> dict:
    fans = cfg.get("fans", {})
    base = default_config()["fans"]
    return {"fans": {fid: validate_fan(fid, {**base[fid], **fans.get(fid, {})}) for fid in FANS}}


class Controller:
    """Turns a config + temperatures into duty cycles, with smoothing and safety overrides."""

    def __init__(self):
        self.current: dict[str, float] = {}

    def target(self, fid: str, f: dict, temps: dict[str, float], src_temp: float | None) -> tuple[float, str]:
        if "cpu" not in temps:
            return 100.0, "CPU temperature unavailable -> full speed"
        hot = max([temps.get("cpu", 0), temps.get("gpu", 0)])
        if hot >= EMERGENCY_TEMP:
            return 100.0, f"emergency: {hot:.0f} °C"
        if f["mode"] == "fixed":
            want = f["duty"]
            why = "fixed"
        else:
            if src_temp is None:
                return 100.0, "sensor unavailable -> full speed"
            want = duty_for(f["points"], src_temp)
            why = f"curve @ {src_temp:.1f} °C"
        return max(want, f["min_duty"]), why

    def step(self, fid: str, want: float, dt: float = 1.0) -> float:
        """Ramp up fast (+15 %/s), down slowly (-3 %/s) to avoid audible hunting."""
        cur = self.current.get(fid, want)
        if want > cur:
            cur = min(want, cur + 15 * dt)
        else:
            cur = max(want, cur - 3 * dt)
        self.current[fid] = cur
        return cur

    def forget(self, fid: str) -> None:
        self.current.pop(fid, None)
