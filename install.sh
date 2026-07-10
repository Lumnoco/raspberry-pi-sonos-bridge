#!/bin/bash
set -e

# Sonos AirPlay Bridge installer
# Run as root: sudo bash install.sh

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root: sudo bash install.sh"
  exit 1
fi

echo "Installing dependencies..."
apt-get update -qq
apt-get install -y ffmpeg python3 curl

echo "Installing bridge script..."
mkdir -p /opt/sonos-bridge
cp bridge.py /opt/sonos-bridge/bridge.py

echo "Installing shairport-sync config..."
cp shairport-sync.conf /etc/shairport-sync.conf

echo "Installing hook scripts..."
HOOKS="airplay-session-start airplay-session-end airplay-active-start airplay-active-end airplay-volume"
for hook in $HOOKS; do
  cp "scripts/$hook" "/usr/local/bin/$hook"
  chmod +x "/usr/local/bin/$hook"
done

echo "Installing systemd service..."
cp sonos-bridge.service /etc/systemd/system/sonos-bridge.service
systemctl daemon-reload
systemctl enable sonos-bridge

# Apply the new config and hook scripts if shairport-sync is already running
if systemctl is-active --quiet shairport-sync; then
  echo "Restarting shairport-sync to apply config..."
  systemctl restart shairport-sync
fi

if [ ! -f /etc/sonos-bridge.conf ]; then
  cp sonos-bridge.conf.example /etc/sonos-bridge.conf
  echo ""
  echo "--------------------------------------------------------------"
  echo "Next step: find your Sonos RINCON ID and add it to the config."
  echo ""
  echo "  python3 /opt/sonos-bridge/bridge.py --discover"
  echo ""
  echo "Then start the bridge:"
  echo "  sudo systemctl start sonos-bridge"
  echo "--------------------------------------------------------------"
else
  systemctl restart sonos-bridge
  echo "Done. Bridge restarted."
fi
