# msictl

**RGB lighting, fan curves, sensors and CPU power for MSI motherboards on Linux.**

MSI Center only runs on Windows. msictl gives you the same controls in a browser-based GUI (`msictl gui`)
and on the command line. Built for and tested on the **MSI PRO H610M-G WIFI (MS-7D46)** in an MSI Codex R2.

> Unofficial, not affiliated with or endorsed by MSI. GPL-2.0-or-later. The Mystic Light packet layout
> comes from [OpenRGB](https://gitlab.com/CalcProgrammer1/OpenRGB); fan control relies on the
> [nct6687d](https://github.com/Fred78290/nct6687d) kernel driver.

## Features

| Area | What you get | Needs |
|---|---|---|
| **Lighting** | All 22 Mystic Light effects on JRGB1, JRAINBOW1, JRAINBOW2; any colour or rainbow; speed; brightness; LED count; live preview; **save to the board** (survives power-off, works with nothing running) | nothing (udev rule) |
| **Fans** | CPU_FAN1 and SYS_FAN1: BIOS curve, fixed speed, or a **custom curve** following CPU, GPU, hottest-of-both, VRM, chipset or board temperature; Silent / Balanced / Performance presets; drag-to-edit curve | `nct6687d` driver + `msictld` service |
| **Sensors** | Voltages (Vcore, DRAM, SA, AUX, 12/5/3.3 V), board temps (VRM, PCH, socket, M.2…), CPU cores, DDR5, NVMe, GPU (temp, power, load, fan), fan RPM, CPU package power | CPU power needs `msictld` |
| **Power** | Power profile (power-saver / balanced / performance, via tuned-ppd); CPU PL1/PL2 limits within Intel's spec | limits need `msictld` |

Not possible from Linux (or from MSI Center): changing BIOS settings, undervolting 14th-gen Intel, overclocking on H610.

## Install

```sh
./install.sh        # asks for sudo; re-run to update
msictl gui          # or "MSI Board Control" in the app menu
```

Fan control also needs the out-of-tree [`nct6687d`](https://github.com/Fred78290/nct6687d) driver (the in-kernel
`nct6683` driver can only read fan speeds on MSI boards). On Fedora:

```sh
git clone https://github.com/Fred78290/nct6687d third_party/nct6687d
cd third_party/nct6687d && make akmod && sudo akmods --force --kernels "$(uname -r)"
echo 'blacklist nct6683' | sudo tee /etc/modprobe.d/nct6683-blacklist.conf
sudo modprobe -r nct6683; sudo modprobe nct6687
```
With Secure Boot on, akmods signs the module with its own key, which you need to have enrolled with `mokutil` (it is already enrolled if you use akmod-nvidia).

## Command line

```sh
msictl status                                   # sensors, fans, power
msictl light                                    # show lighting
msictl light static --color '#00aaff' --save    # all headers, stored in the board
msictl light 'rainbow wave' --header JRAINBOW1 --leds 24 --speed 2
msictl light off
msictl fans balanced                            # curve preset on both fans
msictl fans fixed --fan sys --duty 40
msictl fans bios                                # hand back to the BIOS
msictl power --profile balanced
msictl power --pl1 65 --pl2 150                 # or --reset
```

## How it's put together

```
browser ──HTTP(127.0.0.1, token)──▶ msictl gui (your user)
                                      ├─ hidraw ioctl ──▶ Mystic Light USB controller (1462:7d46)
                                      ├─ /sys/class/hwmon (read) ──▶ sensors
                                      ├─ D-Bus ──▶ tuned-ppd (power profile)
                                      └─ /run/msictl/msictld.sock (wheel only) ──▶ msictld (root)
                                                                                    ├─ pwmN / pwmN_enable / fan_control_watchdog
                                                                                    └─ intel-rapl power limits
```

### Safety
* Fans stay on the BIOS curve until you pick another mode.
* While msictld controls a fan it refreshes the driver's 15 s watchdog lease every second. If the service
  crashes or hangs, the **driver** returns the fans to the BIOS curve on its own.
* Stopping the service (or uninstalling) returns fans to the BIOS and power limits to the BIOS values.
* CPU_FAN1 never goes below 20 %; CPU or GPU at 90 °C forces every controlled fan to 100 %; a missing
  temperature sensor forces 100 %.
* Power limits are clamped to PL1 25–125 W, PL2 25–219 W (Intel's spec for the i7-14700F is 65/219 W).
* The GUI only listens on 127.0.0.1 and requires a per-session token; the service socket is root + `wheel` only
  and every request is validated.

## Files

| Path | What |
|---|---|
| `src/msictl/` | the package (Python stdlib only) |
| `config/sensors.d/` | `lm_sensors` labels for this board |
| `packaging/` | systemd unit, udev rule, desktop entry |
| `RESEARCH.md` | what the board offers and what was verified |
| `/etc/msictl/config.json` | fan and power settings (written by msictld) |
| `/var/lib/msictl/power-baseline.json` | the BIOS power limits recorded on first start |

## Uninstall

```sh
./uninstall.sh
```

## Credits
* Mystic Light 185-byte protocol: [OpenRGB](https://gitlab.com/CalcProgrammer1/OpenRGB) `MSIMotherboard185Controller` (GPL-2.0-or-later).
* Fan control: [nct6687d](https://github.com/Fred78290/nct6687d) by Fred78290 and contributors.
