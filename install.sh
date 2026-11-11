#!/bin/sh
# Install msictl system-wide (asks for sudo). Re-run to update.
#   /usr/local/lib/msictl      the Python package (stdlib only, no pip)
#   /usr/local/bin/msictl      command
#   msictld.service            root service for fan curves + CPU power limits
#   70-msictl.rules            lets your user control lighting without root
#   ~/.local/share/applications/msictl.desktop   app launcher
set -eu
cd "$(dirname "$0")"

[ "$(id -u)" -eq 0 ] && { echo "Run this as your normal user; it asks for sudo when needed."; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }

echo "==> Installing files (sudo)"
sudo rm -rf /usr/local/lib/msictl.new
sudo install -d -m755 /usr/local/lib/msictl.new
sudo cp -r src/msictl /usr/local/lib/msictl.new/
sudo cp README.md LICENSE /usr/local/lib/msictl.new/
sudo find /usr/local/lib/msictl.new -name __pycache__ -prune -exec rm -rf {} +
sudo rm -rf /usr/local/lib/msictl && sudo mv /usr/local/lib/msictl.new /usr/local/lib/msictl
sudo chmod -R u=rwX,go=rX /usr/local/lib/msictl

sudo tee /usr/local/bin/msictl >/dev/null <<'EOF'
#!/bin/sh
exec /usr/bin/python3 -I -c 'import sys; sys.path.insert(0, "/usr/local/lib/msictl"); from msictl.cli import main; main()' "$@"
EOF
sudo chmod 755 /usr/local/bin/msictl

sudo install -m644 packaging/70-msictl.rules /etc/udev/rules.d/70-msictl.rules
sudo install -d -m755 /etc/sensors.d
sudo install -m644 config/sensors.d/msi-pro-h610m-g-7d46.conf /etc/sensors.d/msi-pro-h610m-g-7d46.conf
sudo install -m644 packaging/msictld.service /etc/systemd/system/msictld.service
command -v restorecon >/dev/null && sudo restorecon -R /usr/local/lib/msictl /usr/local/bin/msictl /etc/systemd/system/msictld.service /etc/udev/rules.d/70-msictl.rules 2>/dev/null || true

echo "==> Reloading udev and systemd"
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=hidraw --action=change
sudo systemctl daemon-reload
sudo systemctl enable --now msictld.service
sudo systemctl restart msictld.service

mkdir -p "$HOME/.local/share/applications"
install -m644 packaging/msictl.desktop "$HOME/.local/share/applications/msictl.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

echo
sleep 1
if systemctl is-active --quiet msictld; then
  echo "✓ msictld is running (fans stay on the BIOS curve until you choose otherwise)"
else
  echo "! msictld failed to start: sudo journalctl -u msictld -n 30"
fi
if ! id -nG | tr ' ' '\n' | grep -qx wheel; then
  echo "! Your user isn't in the 'wheel' group, so the GUI can't change fans/power."
fi
echo "✓ Done. Open 'MSI Board Control' from your app menu, or run:  msictl gui"
