"""MSI Mystic Light, 185-byte protocol (USB HID 1462:7Dxx), e.g. MS-7D46 PRO H610M-G.

Packet layout, effect numbers and update sequence are ported from OpenRGB's
MSIMotherboard185Controller (GPL-2.0-or-later, Adam Honse and contributors).

Feature report 0x52, 185 bytes including the id:
    offset  1  JRGB1         ZoneData (10 bytes)
    offset 11  JPIPE1        ZoneData
    offset 21  JPIPE2        ZoneData
    offset 31  JRAINBOW1     RainbowZoneData (11 bytes: ZoneData + LED count)
    offset 42  JRAINBOW2     RainbowZoneData
    offset 53  JCORSAIR      CorsairZoneData (11 bytes)
    offset 64  OUTERLL120    ZoneData
    offset 74  ONBOARD LED 0 ZoneData (also the "sync master" for JRGB on boards w/o onboard LEDs)
    offset 84..164 ONBOARD 1..9
    offset 174 JRGB2         ZoneData
    offset 184 save flag (1 = store in the controller's flash)

ZoneData: effect, R, G, B, speed|brightness<<2|sync_jrgb<<7, R2, G2, B2, color_flags, padding
color_flags bit7 = use the custom colour (clear = rainbow/random colours)
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from .hidraw import DeviceError, Hidraw, find

VID = 0x1462
REPORT_ID = 0x52
REPORT_LEN = 185
SAVE_OFFSET = 184
ONBOARD = 74

# Board table entries we know (OpenRGB board_configs); value = headers on that board.
BOARDS = {
    0x7D46: {"name": "PRO H610M-G (MS-7D46)", "zones": ["JRGB1", "JRAINBOW1", "JRAINBOW2"]},
}

ZONES = {
    # name: (offset, kind, description)
    "JRGB1": (1, "rgb", "4-pin 12 V RGB header"),
    "JRAINBOW1": (31, "argb", "3-pin 5 V addressable RGB header"),
    "JRAINBOW2": (42, "argb", "3-pin 5 V addressable RGB header"),
}

MAX_LEDS = {"JRAINBOW1": 200, "JRAINBOW2": 240}

# effect id -> (name, colour support). colour: "custom" = picked colour only,
# "rainbow" = random/rainbow colours only, "both" = either, None = no colour.
# Exactly the set OpenRGB exposes for this controller.
EFFECTS = {
    1: ("Static", "custom", False),
    2: ("Breathing", "custom", True),
    3: ("Flashing", "both", True),
    4: ("Double flashing", "both", True),
    5: ("Lightning", "custom", True),
    7: ("Meteor", "both", True),
    15: ("Color ring", "rainbow", True),
    16: ("Planetary", "rainbow", True),
    17: ("Double meteor", "rainbow", True),
    18: ("Energy", "rainbow", True),
    19: ("Blink", "both", True),
    20: ("Clock", "rainbow", True),
    21: ("Color pulse", "both", True),
    22: ("Color shift", "rainbow", True),
    23: ("Color wave", "both", True),
    24: ("Marquee", "custom", True),
    26: ("Rainbow wave", "rainbow", True),
    27: ("Visor", "both", True),
    29: ("Rainbow flashing", "rainbow", True),
    35: ("Color ring double flashing", "rainbow", True),
    36: ("Stack", "both", True),
    38: ("Fire", "rainbow", True),
}
MSI_MODE_LIGHTNING = 5
SPEEDS = ["Slow", "Medium", "Fast"]
BRIGHTNESS_MAX = 10

FLAG_CUSTOM_COLOR = 0x80
FLAG_SYNC_ONBOARD = 0x01
FLAG_SYNC_JRAINBOW1 = 0x02
FLAG_SYNC_JRAINBOW2 = 0x04
FLAG_SYNC_JCORSAIR = 0x08
SB_SYNC_JRGB = 0x80


def effects_info() -> list[dict]:
    return [{"id": k, "name": n, "color": c, "speed": s} for k, (n, c, s) in EFFECTS.items()]


@dataclass
class ZoneState:
    effect: int = 1
    color: str = "#ffffff"       # "#rrggbb"
    color2: str = "#ffffff"
    rainbow: bool = False        # True = random/rainbow colours instead of `color`
    speed: int = 1               # 0..2
    brightness: int = 10         # 0..10
    led_count: int | None = None  # JRAINBOW only

    def to_json(self) -> dict:
        d = asdict(self)
        d["effect_name"] = EFFECTS.get(self.effect, (f"Unknown ({self.effect})",))[0]
        return d


def _hex(b: bytes) -> str:
    return "#" + b.hex()


def _rgb(s: str) -> bytes:
    s = s.strip().lstrip("#")
    if len(s) != 6:
        raise ValueError(f"bad colour {s!r}, expected #rrggbb")
    return bytes.fromhex(s)


def decode_zone(pkt: bytes, name: str) -> ZoneState:
    off, kind, _ = ZONES[name]
    z = pkt[off:off + 11]
    sb = z[4]
    return ZoneState(
        effect=z[0], color=_hex(z[1:4]), color2=_hex(z[5:8]),
        rainbow=not (z[8] & FLAG_CUSTOM_COLOR),
        speed=sb & 0x03, brightness=(sb >> 2) & 0x1F,
        led_count=z[10] if kind == "argb" else None,
    )


def validate(name: str, st: ZoneState) -> None:
    if name not in ZONES:
        raise ValueError(f"unknown header {name!r}")
    if st.effect not in EFFECTS and st.effect != 0:
        raise ValueError(f"unknown effect {st.effect}")
    if not 0 <= st.speed <= 2:
        raise ValueError("speed must be 0..2")
    if not 0 <= st.brightness <= BRIGHTNESS_MAX:
        raise ValueError(f"brightness must be 0..{BRIGHTNESS_MAX}")
    _rgb(st.color), _rgb(st.color2)
    if st.led_count is not None:
        if ZONES[name][1] != "argb":
            raise ValueError(f"{name} has no LED count")
        if not 0 <= st.led_count <= MAX_LEDS[name]:
            raise ValueError(f"{name} LED count must be 0..{MAX_LEDS[name]}")
    kind = EFFECTS.get(st.effect, ("", "custom", False))[1]
    if kind == "rainbow" and not st.rainbow:
        st.rainbow = True
    if kind == "custom" and st.rainbow:
        st.rainbow = False


def encode_zone(pkt: bytearray, name: str, st: ZoneState) -> None:
    """Write one header's settings into `pkt`, mirroring OpenRGB's SetMode + SetZoneColor."""
    validate(name, st)
    off, kind, _ = ZONES[name]
    c1, c2 = _rgb(st.color), _rgb(st.color2)
    sb = ((st.brightness << 2) & 0x7C) | (st.speed & 0x03)

    pkt[off] = st.effect
    pkt[off + 1:off + 4] = c1
    pkt[off + 4] = sb
    pkt[off + 5:off + 8] = c2
    flags = pkt[off + 8]
    pkt[off + 8] = (flags & ~FLAG_CUSTOM_COLOR & 0xFF) if st.rainbow else (flags | FLAG_CUSTOM_COLOR)
    pkt[off + 9] = 0
    if kind == "argb" and st.led_count is not None:
        pkt[off + 10] = st.led_count

    if name == "JRGB1":
        # Board has no onboard LEDs: the onboard slot is JRGB's sync master and must mirror it.
        pkt[ONBOARD] = st.effect
        pkt[ONBOARD + 1:ONBOARD + 4] = c1
        pkt[ONBOARD + 5:ONBOARD + 8] = c2
        sync = SB_SYNC_JRGB if st.effect > MSI_MODE_LIGHTNING else 0
        pkt[ONBOARD + 4] = sb | sync
        of = pkt[ONBOARD + 8]
        # keep pipe sync bits as found; never let JRAINBOW/JCORSAIR follow the onboard master
        of &= ~(FLAG_SYNC_JRAINBOW1 | FLAG_SYNC_JRAINBOW2 | FLAG_SYNC_JCORSAIR | FLAG_CUSTOM_COLOR) & 0xFF
        of |= FLAG_SYNC_ONBOARD | (0 if st.rainbow else FLAG_CUSTOM_COLOR)
        pkt[ONBOARD + 8] = of
        pkt[ONBOARD + 9] = 0


class MysticLight:
    """One Mystic Light controller."""

    def __init__(self, path: str, pid: int):
        self.path, self.pid = path, pid
        self.board = BOARDS.get(pid, {"name": f"MSI board {pid:04X}", "zones": list(ZONES)})

    @classmethod
    def find(cls) -> "MysticLight":
        for pid in BOARDS:
            paths = find(VID, pid)
            if paths:
                return cls(paths[0], pid)
        raise DeviceError("No supported MSI Mystic Light controller found (expected USB 1462:7D46).")

    def read(self) -> bytearray:
        with Hidraw(self.path) as h:
            data = h.get_feature(REPORT_ID, REPORT_LEN)
        if data[0] != REPORT_ID:
            raise DeviceError("unexpected report id from controller")
        return bytearray(data)

    def state(self) -> dict:
        pkt = self.read()
        return {name: decode_zone(pkt, name).to_json() for name in self.board["zones"]}

    def firmware(self) -> str | None:
        """APROM version via output report 0x01 0xB0 (same query OpenRGB sends)."""
        try:
            with Hidraw(self.path) as h:
                req = bytes([0x01, 0xB0]) + b"\xCC" * 62
                h.write(req)
                for _ in range(5):
                    r = h.read(0.3)
                    if r and len(r) > 2 and r[0] == 0x01:
                        return f"{r[2] >> 4}.{r[2] & 0x0F}"
        except (OSError, DeviceError):
            pass
        return None

    def apply(self, zones: dict[str, ZoneState], save: bool = False) -> dict:
        """Change the given headers (others untouched). save=True stores it in the board's flash."""
        pkt = self.read()
        before = bytes(pkt)
        for name, st in zones.items():
            if name not in self.board["zones"]:
                raise ValueError(f"{name} is not on this board")
            encode_zone(pkt, name, st)
        with Hidraw(self.path) as h:
            # Same two-step sequence as OpenRGB's Update(): resend the current state with the
            # save flag, then the new state.
            old = bytearray(before)
            old[SAVE_OFFSET] = 1 if save else 0
            h.set_feature(bytes(old))
            pkt[SAVE_OFFSET] = 1 if save else 0
            h.set_feature(bytes(pkt))
        return self.state()
