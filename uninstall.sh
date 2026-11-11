#!/bin/sh
# Remove msictl. Stopping the service first returns fans to the BIOS curve and
# CPU power limits to the BIOS values. Lighting saved to the board stays as it is.
set -eu
sudo systemctl disable --now msictld.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/msictld.service /usr/local/bin/msictl /etc/udev/rules.d/70-msictl.rules \
           /etc/sensors.d/msi-pro-h610m-g-7d46.conf
sudo rm -rf /usr/local/lib/msictl
sudo systemctl daemon-reload
sudo udevadm control --reload
rm -f "$HOME/.local/share/applications/msictl.desktop"
echo "Removed. Settings kept in /etc/msictl and /var/lib/msictl (delete them to forget everything)."
echo "The nct6687d fan driver is separate: remove it with 'make akmod/remove' in third_party/nct6687d."
