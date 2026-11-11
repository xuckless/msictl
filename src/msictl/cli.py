"""msictl command line."""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__, fans, mysticlight as ml, power, sensors
from .hidraw import DeviceError


def _effect_id(name: str) -> int:
    if name.isdigit():
        return int(name)
    key = name.lower().replace("-", " ").replace("_", " ")
    for k, (n, _c, _s) in ml.EFFECTS.items():
        if n.lower() == key:
            return k
    if key == "off":
        return 1
    raise SystemExit(f"unknown effect {name!r}. Try: {', '.join(n for n, _c, _s in ml.EFFECTS.values())}, off")


def cmd_status(a):
    snap = sensors.snapshot()
    for g in snap["groups"]:
        unit = "V" if g["kind"] == "volt" else "°C"
        print(f"{g['name']}:")
        for i in g["items"]:
            print(f"  {i['label']:<16} {i['value']:>7.2f} {unit}")
    print("Fans:")
    for f in snap["fans"].values():
        print(f"  {f['label']:<16} {f['rpm'] or 0:>5} RPM  {f['duty'] or 0:>3} %  ({f['mode']})")
    if snap["gpu"]:
        g = snap["gpu"]
        print(f"GPU: {g['name']}  {g['temp']} °C  {g['power_w']} W  fan {g['fan_pct']} %")
    lim, prof = power.limits(), power.profile()
    print(f"CPU power limits: PL1 {lim['pl1']} W, PL2 {lim['pl2']} W;  profile: {prof.get('active', '?')}")
    if not snap["driver"]["writable"]:
        print("\n! Fan driver nct6687 isn't loaded: fans are read-only.")


def cmd_light(a):
    dev = ml.MysticLight.find()
    if a.effect is None:
        for z, st in dev.state().items():
            extra = f", {st['led_count']} LEDs" if st["led_count"] is not None else ""
            col = "rainbow" if st["rainbow"] else st["color"]
            print(f"{z:<10} {st['effect_name']:<14} {col:<8} speed {ml.SPEEDS[st['speed']] if st['speed'] < 3 else st['speed']}, "
                  f"brightness {st['brightness']}/10{extra}")
        return
    eff = _effect_id(a.effect)
    off = a.effect.lower() == "off"
    zones = a.header or dev.board["zones"]
    cur = dev.state()
    new = {}
    for z in zones:
        if z not in dev.board["zones"]:
            raise SystemExit(f"{z} isn't on this board ({', '.join(dev.board['zones'])})")
        c = cur[z]
        new[z] = ml.ZoneState(effect=eff, color="#000000" if off else (a.color or c["color"]),
                              color2=a.color or c["color2"], rainbow=a.rainbow,
                              speed=a.speed if a.speed is not None else c["speed"],
                              brightness=a.brightness if a.brightness is not None else c["brightness"],
                              led_count=a.leds if (a.leds is not None and ml.ZONES[z][1] == "argb") else None)
    dev.apply(new, save=a.save)
    print("Saved to the board." if a.save else "Applied (add --save to keep it after power-off).")


def cmd_fans(a):
    from .daemon import request
    st = request({"cmd": "status"})
    cfg = st["config"]["fans"]
    if a.mode is None:
        print(json.dumps({"config": cfg, "status": st["fan_status"]}, indent=2))
        return
    targets = [a.fan] if a.fan else list(cfg)
    for fid in targets:
        if a.mode in fans.PRESETS:
            cfg[fid].update(mode="curve", points=fans.PRESETS[a.mode][fid])
        elif a.mode == "bios":
            cfg[fid]["mode"] = "bios"
        elif a.mode == "fixed":
            cfg[fid].update(mode="fixed", duty=a.duty)
    r = request({"cmd": "set_fans", "fans": cfg})
    print("OK" if r.get("ok") else r.get("error"))


def cmd_power(a):
    if a.profile:
        print(power.set_profile(a.profile))
    if a.reset or a.pl1 is not None:
        from .daemon import request
        req = {"cmd": "set_power", "reset": True} if a.reset else {"cmd": "set_power", "pl1": a.pl1, "pl2": a.pl2 or a.pl1}
        print(request(req))
    if not (a.profile or a.reset or a.pl1 is not None):
        print(power.limits(), power.profile())


def main(argv=None):
    p = argparse.ArgumentParser(prog="msictl", description="MSI motherboard lighting, fans and power on Linux")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")
    g = sub.add_parser("gui", help="open the graphical editor in your browser")
    g.add_argument("--port", type=int, default=0)
    g.add_argument("--no-browser", action="store_true")
    sub.add_parser("status", help="sensors, fans, power")
    l = sub.add_parser("light", help="show or set RGB lighting")
    l.add_argument("effect", nargs="?", help="effect name (e.g. static, breathing, 'rainbow wave', off)")
    l.add_argument("--header", action="append", choices=list(ml.ZONES), help="only this header (repeatable)")
    l.add_argument("--color", help="#rrggbb")
    l.add_argument("--rainbow", action="store_true", help="rainbow/random colours")
    l.add_argument("--speed", type=int, choices=[0, 1, 2])
    l.add_argument("--brightness", type=int, choices=range(0, 11), metavar="0-10")
    l.add_argument("--leds", type=int, help="LED count on JRAINBOW headers")
    l.add_argument("--save", action="store_true", help="store in the board (survives power-off)")
    f = sub.add_parser("fans", help="show or set fan control (needs msictld)")
    f.add_argument("mode", nargs="?", choices=["bios", "fixed", *fans.PRESETS])
    f.add_argument("--fan", choices=list(sensors.FANS))
    f.add_argument("--duty", type=float, default=50)
    w = sub.add_parser("power", help="CPU power profile and limits")
    w.add_argument("--profile", choices=power.PROFILES)
    w.add_argument("--pl1", type=float, help="sustained limit, W (needs msictld)")
    w.add_argument("--pl2", type=float, help="burst limit, W")
    w.add_argument("--reset", action="store_true", help="back to the BIOS limits")
    sub.add_parser("daemon", help="run the root fan/power service (used by systemd)")
    a = p.parse_args(argv)
    try:
        if a.cmd == "gui":
            from .gui import serve
            serve(a.port, not a.no_browser)
        elif a.cmd == "status" or a.cmd is None:
            cmd_status(a)
        elif a.cmd == "light":
            cmd_light(a)
        elif a.cmd == "fans":
            cmd_fans(a)
        elif a.cmd == "power":
            cmd_power(a)
        elif a.cmd == "daemon":
            from .daemon import main as dmain
            dmain()
    except (DeviceError, ValueError) as e:
        sys.exit(f"error: {e}")
    except (FileNotFoundError, ConnectionRefusedError) as e:
        sys.exit(f"error: fan service not reachable ({e}). Is msictld installed and running?")


if __name__ == "__main__":
    main()
