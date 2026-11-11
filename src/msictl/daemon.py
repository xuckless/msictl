"""msictld: root service that owns fan speeds and CPU power limits.

Safety model
  * Fans start (and return to) BIOS control unless a curve/fixed mode is configured.
  * While any fan is software-controlled the nct6687 watchdog lease is refreshed every second;
    if this process dies or hangs, the driver hands the fans back to the BIOS after LEASE seconds.
  * CPU_FAN1 never goes below CPU_FAN_FLOOR; CPU or GPU >= EMERGENCY_TEMP forces 100 %;
    a missing temperature sensor forces 100 % on the fans that follow it.
  * On stop, fans go back to BIOS control and power limits back to what the BIOS set at first start.

Clients talk JSON lines over a Unix socket that only root and the admin group can open.
"""
from __future__ import annotations

import grp
import json
import logging
import os
import signal
import socket
import socketserver
import threading
import time

from . import fans as fanlib
from . import power, sensors

CONF_DIR = os.environ.get("MSICTL_CONF", "/etc/msictl")
STATE_DIR = os.environ.get("MSICTL_STATE", "/var/lib/msictl")
SOCK = os.environ.get("MSICTL_SOCK", "/run/msictl/msictld.sock")
GROUP = os.environ.get("MSICTL_GROUP", "wheel")
LEASE = 15
INTERVAL = 1.0

log = logging.getLogger("msictld")


def _write(path: str, value) -> None:
    with open(path, "w") as f:
        f.write(str(value))


def _load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


class Daemon:
    def __init__(self):
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.ctl = fanlib.Controller()
        self.meter = power.PowerMeter()
        self.watts: float | None = None
        self.temps: dict[str, float] = {}
        self.status: dict[str, dict] = {}
        self.manual: set[str] = set()
        self.conf_path = os.path.join(CONF_DIR, "config.json")
        raw = _load_json(self.conf_path, {})
        if not isinstance(raw, dict):
            raw = {}
        try:
            self.config = fanlib.validate_config(raw)
        except (ValueError, TypeError, KeyError, IndexError) as e:
            log.error("bad config %s (%s); using BIOS fan control", self.conf_path, e)
            self.config = fanlib.default_config()
        self.config["power"] = raw.get("power") if isinstance(raw.get("power"), dict) else None
        self.baseline_path = os.path.join(STATE_DIR, "power-baseline.json")
        self.baseline = _load_json(self.baseline_path, None)
        if not self.baseline:
            self.baseline = power.limits()
            if self.baseline.get("pl1"):
                _save_json(self.baseline_path, self.baseline)
                log.info("recorded BIOS power limits %s", self.baseline)
        self.power_changed = False
        if self.config["power"]:
            try:
                power.set_limits(self.config["power"]["pl1"], self.config["power"]["pl2"])
                self.power_changed = True
            except (ValueError, OSError, KeyError) as e:
                log.error("could not apply saved power limits: %s", e)

    # ------------------------------------------------------------------ fans
    def _hw(self) -> str | None:
        info = sensors.driver_info()
        return info["path"] if info["writable"] else None

    def _to_bios(self, hw: str, fid: str) -> None:
        ch = sensors.FANS[fid][0]
        try:
            _write(os.path.join(hw, f"pwm{ch}_enable"), 2)
            log.info("%s -> BIOS control", fid)
        except OSError as e:
            log.error("could not return %s to BIOS control: %s", fid, e)
        self.manual.discard(fid)
        self.ctl.forget(fid)

    def tick(self, dt: float) -> None:
        hw = self._hw()
        temps = sensors.temps_for_control()
        self.watts = self.meter.sample()
        with self.lock:
            self.temps = temps
            cfg = self.config["fans"]
            if not hw:
                self.status = {fid: {"why": "fan driver (nct6687) not loaded"} for fid in cfg}
                self.manual.clear()
                return
            for fid, f in cfg.items():
                ch = sensors.FANS[fid][0]
                if f["mode"] == "bios":
                    if fid in self.manual:
                        self._to_bios(hw, fid)
                    self.status[fid] = {"why": "BIOS curve"}
                    continue
                src = sensors.source_temp(f["source"], temps)
                want, why = self.ctl.target(fid, f, temps, src)
                if want >= 100 and not why.startswith(("fixed", "curve")):
                    self.ctl.current[fid] = want     # no slow ramp in an emergency
                duty = self.ctl.step(fid, want, dt)
                try:
                    if fid not in self.manual:
                        _write(os.path.join(hw, f"pwm{ch}_enable"), 1)
                        self.manual.add(fid)
                        log.info("%s -> software control (%s)", fid, f["mode"])
                    _write(os.path.join(hw, f"pwm{ch}"), round(duty * 255 / 100))
                except OSError as e:
                    log.error("writing %s failed: %s", fid, e)
                    why = f"write failed: {e}"
                self.status[fid] = {"why": why, "target": round(want, 1), "set": round(duty, 1),
                                    "source_temp": src}
            try:
                _write(os.path.join(hw, "fan_control_watchdog"), LEASE if self.manual else 0)
            except OSError:
                pass

    def release_all(self) -> None:
        hw = self._hw()
        if hw:
            for fid in list(self.manual):
                self._to_bios(hw, fid)
            try:
                _write(os.path.join(hw, "fan_control_watchdog"), 0)
            except OSError:
                pass
        if self.power_changed and self.baseline and self.baseline.get("pl1"):
            try:
                power.set_limits(self.baseline["pl1"], self.baseline["pl2"])
                log.info("power limits restored to BIOS values %s", self.baseline)
            except (ValueError, OSError) as e:
                log.error("could not restore power limits: %s", e)

    def run(self) -> None:
        last = time.monotonic()
        while not self.stop.is_set():
            now = time.monotonic()
            try:
                self.tick(min(now - last, 3.0))
            except Exception:  # never let one bad read kill the loop; the lease covers a hang
                log.exception("control loop error")
            last = now
            self.stop.wait(INTERVAL)

    # ------------------------------------------------------------------ API
    def handle(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if cmd == "ping":
            return {"ok": True}
        if cmd == "status":
            with self.lock:
                return {"ok": True, "config": self.config, "fan_status": self.status,
                        "temps": self.temps, "power": {**power.limits(), "watts": self.watts,
                                                       "baseline": self.baseline,
                                                       "range": {"pl1": power.PL1_RANGE, "pl2": power.PL2_RANGE}}}
        if cmd == "set_fans":
            new = fanlib.validate_config({"fans": req.get("fans", {})})
            with self.lock:
                self.config["fans"] = new["fans"]
                _save_json(self.conf_path, self.config)
            log.info("fan config updated: %s", {k: v["mode"] for k, v in new["fans"].items()})
            return {"ok": True, "config": self.config}
        if cmd == "set_power":
            with self.lock:
                if req.get("reset"):
                    if not (self.baseline and self.baseline.get("pl1")):
                        raise ValueError("no recorded BIOS limits")
                    got = power.set_limits(self.baseline["pl1"], self.baseline["pl2"])
                    self.config["power"] = None
                    self.power_changed = False
                else:
                    pl1, pl2 = float(req["pl1"]), float(req["pl2"])
                    got = power.set_limits(pl1, pl2)
                    self.config["power"] = {"pl1": pl1, "pl2": pl2}
                    self.power_changed = True
                _save_json(self.conf_path, self.config)
            log.info("power limits now %s", got)
            return {"ok": True, "power": got}
        raise ValueError(f"unknown command {cmd!r}")


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline(65536)
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError("request must be an object")
            resp = self.server.daemon.handle(req)
        except (ValueError, KeyError, TypeError, IndexError) as e:
            resp = {"ok": False, "error": str(e)}
        except Exception as e:
            log.exception("request failed")
            resp = {"ok": False, "error": f"internal error: {e}"}
        self.wfile.write(json.dumps(resp).encode() + b"\n")


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if os.geteuid() != 0:
        raise SystemExit("msictld must run as root (it's normally started by systemd)")
    d = Daemon()
    os.makedirs(os.path.dirname(SOCK), exist_ok=True)
    try:
        os.unlink(SOCK)
    except FileNotFoundError:
        pass
    old = os.umask(0o117)
    srv = _Server(SOCK, _Handler)
    os.umask(old)
    try:
        gid = grp.getgrnam(GROUP).gr_gid
        os.chown(SOCK, 0, gid)
        os.chmod(SOCK, 0o660)
    except KeyError:
        log.warning("group %s missing; socket is root-only", GROUP)
        os.chmod(SOCK, 0o600)
    srv.daemon = d
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def _stop(*_):
        d.stop.set()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info("msictld running; socket %s (group %s)", SOCK, GROUP)
    try:
        d.run()
    finally:
        d.release_all()
        srv.shutdown()
        try:
            os.unlink(SOCK)
        except OSError:
            pass
        log.info("stopped; fans back on BIOS control")


def request(req: dict, timeout: float = 3.0) -> dict:
    """Client side: send one request to msictld."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(SOCK)
        s.sendall(json.dumps(req).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    return json.loads(buf)


if __name__ == "__main__":
    main()
