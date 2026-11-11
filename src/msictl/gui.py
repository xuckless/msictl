"""Local web UI: `msictl gui`. Serves one page on 127.0.0.1 and proxies to hardware/daemon."""
from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from . import daemon, fans, mysticlight as ml, power, sensors
from .hidraw import DeviceError

TOKEN = secrets.token_urlsafe(24)
_hw_lock = threading.Lock()


def _daemon(req: dict) -> dict:
    try:
        return daemon.request(req)
    except FileNotFoundError:
        return {"ok": False, "missing": True, "error": "The fan service (msictld) isn't running. "
                "Install it with ./install.sh, or start it: sudo systemctl start msictld"}
    except PermissionError:
        return {"ok": False, "error": "No permission to talk to msictld (your user must be in the 'wheel' group)."}
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"msictld: {e}"}


def lighting_state() -> dict:
    try:
        dev = ml.MysticLight.find()
        with _hw_lock:
            zones = dev.state()
        return {"ok": True, "board": dev.board["name"], "path": dev.path,
                "zones": zones, "zone_info": {z: {"kind": ml.ZONES[z][1], "desc": ml.ZONES[z][2],
                                                  "max_leds": ml.MAX_LEDS.get(z)} for z in zones},
                "effects": ml.effects_info(), "speeds": ml.SPEEDS, "brightness_max": ml.BRIGHTNESS_MAX}
    except (DeviceError, OSError) as e:
        return {"ok": False, "error": str(e)}


def live() -> dict:
    snap = sensors.snapshot()
    d = _daemon({"cmd": "status"})
    return {"sensors": snap, "daemon": d, "power": {"limits": power.limits(), "profile": power.profile()}}


def full_state() -> dict:
    return {"lighting": lighting_state(), "live": live(),
            "fan_meta": {"presets": fans.PRESETS, "sources": sensors.SOURCES, "fans": sensors.FANS,
                         "cpu_floor": fans.CPU_FAN_FLOOR, "sys_stop": fans.SYS_FAN_STOP, "emergency": fans.EMERGENCY_TEMP,
                         "max_points": fans.MAX_POINTS,
                         "pl1_range": power.PL1_RANGE, "pl2_range": power.PL2_RANGE}}


def set_lighting(body: dict) -> dict:
    zones = {}
    for name, z in (body.get("zones") or {}).items():
        zones[name] = ml.ZoneState(
            effect=int(z["effect"]), color=str(z.get("color", "#ffffff")),
            color2=str(z.get("color2", z.get("color", "#ffffff"))), rainbow=bool(z.get("rainbow")),
            speed=int(z.get("speed", 1)), brightness=int(z.get("brightness", 10)),
            led_count=None if z.get("led_count") is None else int(z["led_count"]))
    if not zones:
        raise ValueError("nothing to change")
    dev = ml.MysticLight.find()
    with _hw_lock:
        return {"ok": True, "zones": dev.apply(zones, save=bool(body.get("save")))}


class Handler(BaseHTTPRequestHandler):
    server_version = "msictl"

    def log_message(self, *a):
        pass

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _send(self, code: int, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain")
        if self.path in ("/", "/index.html"):
            html = resources.files(__package__).joinpath("static/index.html").read_text("utf-8")
            return self._send(200, html.replace("__TOKEN__", TOKEN).encode(), "text/html; charset=utf-8")
        if self.headers.get("X-Token") != TOKEN:
            return self._send(403, b"forbidden", "text/plain")
        if self.path == "/api/state":
            return self._json(200, full_state())
        if self.path == "/api/live":
            return self._json(200, live())
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-Token") != TOKEN:
            return self._send(403, b"forbidden", "text/plain")
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > 1 << 20:
            return self._send(413, b"too large", "text/plain")
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/lighting":
                return self._json(200, set_lighting(body))
            if self.path == "/api/fans":
                fans.validate_config({"fans": body.get("fans", {})})  # fail fast with a clear message
                return self._json(200, _daemon({"cmd": "set_fans", "fans": body.get("fans", {})}))
            if self.path == "/api/power/limits":
                req = {"cmd": "set_power", "reset": True} if body.get("reset") else \
                      {"cmd": "set_power", "pl1": float(body["pl1"]), "pl2": float(body["pl2"])}
                return self._json(200, _daemon(req))
            if self.path == "/api/power/profile":
                return self._json(200, {"ok": True, "profile": power.set_profile(str(body.get("profile")))})
            self._send(404, b"not found", "text/plain")
        except (DeviceError, KeyError, ValueError, TypeError, OSError) as e:
            msg = f"missing field {e}" if isinstance(e, KeyError) else str(e)
            self._json(400, {"ok": False, "error": msg})


def serve(port: int = 0, open_browser: bool = True):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"msictl GUI running at {url}  (Ctrl+C to quit)")
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
