# MSI PRO H610M-G WIFI (MS-7D46) on Linux: everything software-controllable

Machine: MSI Codex R2 prebuilt, BIOS A.F6, i7-14700F, RTX 5060, DDR5 (SPD5118 sensors), Fedora 44, kernel 7.2.9.
Checked 2026-11-10. Nothing has been written to the hardware yet.

## Summary: what MSI Center does on Windows vs. Linux

| MSI Center module (Windows) | What it actually controls | Linux status on this machine |
|---|---|---|
| **Mystic Light** | USB HID controller `1462:7d46` | Fully possible. Protocol known (OpenRGB), and the device is user-accessible now |
| **Frozr AI Cooling** / fan control | Nuvoton Super I/O fan controller | Possible with the out-of-tree `nct6687d` driver. Mainline driver is read-only on MSI. **No driver loaded yet** |
| **Hardware Monitoring** | Super I/O voltages, temps, fan RPM, plus CPU/RAM/NVMe sensors | CPU, RAM and NVMe already visible. Board voltages, VRM/board temps and fans need the Super I/O driver |
| **User Scenario** (performance presets) | CPU power limits, Windows power plan, fan profile | Possible: RAPL power limits (root), intel_pstate EPP, fan profile via the driver |
| Smart Priority, Game Highlights, True Color | Windows app scheduling, overlays, display colour | Not applicable to the motherboard. Linux has its own equivalents (e.g. `gamemode`) |
| Live Update (BIOS/driver updates) | MSI servers + flasher | BIOS can only be flashed via M-Flash (BIOS menu, USB stick). fwupd only handles the UEFI dbx |
| Hardware Diagnosis | Reads sensors and logs | Can be reproduced with sensors + SMART + `journalctl` |

## 1. RGB lighting: Mystic Light (see the OpenRGB code in `reference-openrgb-mystic-light/`)
- USB HID `1462:7d46`, `/dev/hidraw4`, already accessible without root via `60-openrgb.rules`
- Headers: **JRGB1** (4-pin 12 V RGB), **JRAINBOW1** and **JRAINBOW2** (3-pin 5 V ARGB)
- Feature report `0x52` (185 B): effect, colours, speed, brightness for each header, plus "save to board".
  A read works now. Feature report `0x53` (725 B) holds per-LED colours (direct mode).
- 22 hardware effects plus per-LED direct mode. OpenRGB board table:
  `{ 0x7D46, 0, 1, 1, 0, &zones_set11, DIRECT_MODE_PER_LED, 0 }  // PRO H610M-G DDR4`
- Licence: OpenRGB is GPL-2.0-or-later, so a port of it must be GPL as well.

## 2. Fans: Nuvoton Super I/O
### Hardware
- Only **two 4-pin fan headers**: `CPU_FAN1` and `SYS_FAN1` (2 A each, PWM or DC).
  In the Codex R2 the case fans are most likely on a splitter or hub off `SYS_FAN1`,
  so **all case fans share one speed**. That is a hardware limit, and Windows has it too.
- Chip: MSI's 600-series boards use the **Nuvoton NCT6687D-R** (unconfirmed on this board until a driver probes it).
  A `PNP0501` serial port, which lives on the Super I/O, is present.
- Currently **no hwmon driver is bound**, so `sensors` shows no fans or board voltages.

### Driver options
| Driver | Source | Read RPM/temp/volt | Set fan speed | Notes |
|---|---|---|---|---|
| `nct6683` | mainline, shipped in Fedora | yes | **no**. MSI customer IDs 0x200/0x201/0x207/0x20d are read-only; only MITAC gets writable PWM | safe first step: `sudo modprobe nct6683 && sensors` |
| `nct6687` (`nct6687d`) | https://github.com/Fred78290/nct6687d (out of tree, DKMS/akmod) | yes | **yes**: `pwmN` 0-255, `pwmN_enable` 1=manual / 2=firmware auto, `fan_control_watchdog` lease | MSI B660/Z690/Z790 described as auto-detected; H610 not explicitly listed. Has `make akmod` for Fedora |

`nct6687d` known issues:
- conflicts with `nct6683`, so blacklist that driver
- "EC base I/O port unconfigured" on boot; fix with `softdep nct6687 pre: i2c_i801`
- on some boards only CPU/pump RPM shows; check `fan_config` (`msi_alt1` is for 800-series boards, not this one)
- Secure Boot: an akmod must be signed (Fedora's akmods can sign with an enrolled MOK)

### What a fan GUI could offer (matching or beating Frozr AI Cooling)
- Live RPM and duty for CPU_FAN1 and SYS_FAN1
- Hand control back to the BIOS curve (`pwm_enable=2`), or take manual control (`pwm_enable=1`)
- **Software fan curves** driven by any sensor: CPU package temp, **GPU temp** (via `nvidia-smi`/NVML;
  this is what "AI Cooling" does), NVMe, RAM. Needs a small root daemon (systemd service).
- Presets: Silent / Balanced / Performance / Full speed
- Safety: hysteresis, a minimum duty, full speed if the daemon dies (the driver's watchdog does this)
- Existing alternative: **CoolerControl** (GUI + daemon) already does curves on top of `nct6687d`.
  Worth using instead of writing a curve engine from scratch, or integrating with it.

## 3. Hardware monitoring (already working)
- `coretemp`: per-core and package temperatures
- `spd5118` ×2: DDR5 module temperatures
- `nvme`: SSD temperature
- `intel-rapl`: CPU package power draw (energy counters)
- NVIDIA GPU: temps, power, fan via `nvidia-smi`
- After the Super I/O driver loads: Vcore, +12 V, +5 V, +3.3 V, VCCSA, DRAM V, board/PCH/VRM temps, fan RPM

## 4. CPU power / performance ("User Scenario")
- RAPL limits currently: **PL1 = 65 W, PL2 = 115 W** (Intel's default for the i7-14700F is 65 W / 219 W,
  so the Codex BIOS caps PL2). Writable as root at `/sys/class/powercap/intel-rapl:0/constraint_*_power_limit_uw`.
  It can't exceed what the BIOS allows if the MSR lock bit is set; that needs a root MSR read to check.
- `intel_pstate` driver, currently governor `performance`, EPP `performance`; power-profiles-daemon can switch these.
- Undervolting is locked on 14th-gen Intel (Plundervolt mitigation), so that isn't possible (and isn't possible on Windows either).
- Overclocking/BCLK/memory timings: BIOS only on H610, the same as on Windows.

## 5. Other onboard devices (Linux-native, no MSI tool needed)
- Audio: Realtek **ALC897**. Jack retasking via `hdajackretask` (alsa-tools). Nahimic is Windows-only.
- LAN: Intel I219-V (`eno1`), Wake-on-LAN via `ethtool -s eno1 wol g`
- Wi-Fi: Realtek RTL8852CE (Wi-Fi 6E), Bluetooth 13d3:3586
- EZ Debug LEDs: hardware POST indicators, not software-controllable

## 6. Not possible from Linux (or anywhere outside the BIOS)
- Changing BIOS settings from the OS. There is no `firmware-attributes` interface, and MSI's WMI interface
  (`ABBC0F6E-...`, used by `msi_wmi_platform`) is absent on this desktop.
- BIOS flashing from the OS (use M-Flash)

## Confirmed 2026-11-10 (mainline nct6683 loaded by hand)
- Kernel log: `nct6683: Found NCT6687D or compatible chip at 0x4e:0xa20`, EC firmware 1.0 build 11/01/22 → hwmon `nct6687`
- fan1 = 1061 RPM, pwm1 = 89 (35 %): **CPU_FAN1**
- fan3 = 793 RPM, pwm3 = 141 (55 %): **SYS_FAN1** (case fans)
- Temps: PECI 44.5 °C, Diode 41.5, PCH 56, thermistors 57 / 41.5 / 37.5 / 32 °C
- Voltages are raw (VIN0..7 unscaled); the mainline driver doesn't know MSI's multipliers
- `pwm*` is read-only, as expected
- `sensors` printed nothing only because **lm_sensors isn't installed**
- Secure Boot is on, and the akmods MOK key (from akmod-nvidia) is already enrolled, so an akmod build is signed automatically
- `third_party/nct6687d` is cloned at 49ac9bc (2026-11-07). This board isn't in the `msi_alt1` list, so it uses the default mapping (same as B660/Z690).
  Its Fedora package autoloads `nct6687` but does **not** blacklist `nct6683`.

## Confirmed 2026-11-10: nct6687d installed and working
- akmod `nct6687d-1.0.263` (git 49ac9bc) is installed and signed, with kmods built for 7.2.9 and 7.3.3. `nct6683` is blacklisted in
  `/etc/modprobe.d/nct6683-blacklist.conf`; the package's modules-load.d file autoloads `nct6687`.
- First load failed with `-16` (EBUSY) because nct6683 still held the ports; a reload fixed it. A clean boot won't hit this.
- Kernel log: `active fan config=default`
- `pwmN`, `pwmN_enable` and `fan_control_watchdog` are root-writable. Both fans start at `pwm_enable=2` (BIOS curve).
- Fan channels: fan1/pwm1 = **CPU_FAN1**, fan3/pwm3 = **SYS_FAN1**. All other channels read 0 RPM (no header).
- The default voltage labels are the AMD layout. The correct Intel mapping is in `config/sensors.d/msi-pro-h610m-g-7d46.conf`,
  tested with `sensors -c`: Vcore 1.32 V, DRAM 1.12 V, SA 0.79 V, AUX 1.80 V, +12 V 12.14, +5 V 4.98, +3.3 V 3.34.
  The VBat scale (raw 1.05) is unverified.

## Confirmed 2026-11-10: lighting writes and how the fans are wired
- msictl writes to report 0x52 work, and readback matches.
- The case fans are **not** wired straight to the board. They hang off an **RGB hub** controlled by the case's LED button
  (a short press cycles the hub's own colours). **Holding the button ~5 s puts the hub in motherboard-sync mode.**
- The hub's sync input is on **JRAINBOW2** (5 V ARGB). Test: JRGB1 blue / JRAINBOW1 green / JRAINBOW2 red → fans red;
  then changing only JRAINBOW2 to green turned the fans green.
- Gotcha: an ARGB header with LED count 0 (the factory state here) lights nothing. The GUI now bumps 0 → 40 when an effect is picked.
- Before this, JRGB1 held "Blink #E80A0A" (from MSI Center/factory), with nothing visibly connected.

## Next steps
1. Run `sudo modprobe nct6683 && sensors`. This is read-only and safe, and it confirms the chip (NCT6687D?) and that the fans are readable.
2. If you want fan control: build `nct6687d` as an akmod, blacklist `nct6683`, and check that PWM writes take effect.
3. Build the GUI: lighting (Mystic Light) + fans (curves) + monitoring + power profile on one page,
   deerctl-style (Python stdlib + browser UI), with a small root systemd service for fans and power limits.

## Confirmed 2026-11-10: fan control on real hardware
- CPU_FAN1: 33 % → 60 % took it from 1012 to ~1480 RPM. Handover to software and back to the BIOS is clean.
- SYS_FAN1 (case fans behind the RGB hub): **20 % = 0 RPM (they stop)**, 30 % ≈ 880, 70 % ≈ 960 RPM.
  The fans have a narrow range and stop below ~25 %. Presets and the default floor for SYS are now 30 %.
