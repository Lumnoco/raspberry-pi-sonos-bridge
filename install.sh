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
cp scripts/airplay-session-start  /usr/local/bin/airplay-session-start
cp scripts/airplay-session-end    /usr/local/bin/airplay-session-end
cp scripts/airplay-active-start   /usr/local/bin/airplay-active-start
cp scripts/airplay-active-end     /usr/local/bin/airplay-active-end
cp scripts/airplay-volume         /usr/local/bin/airplay-volume
chmod +x /usr/local/bin/airplay-*

echo "Installing systemd service..."
cp sonos-bridge.service /etc/systemd/system/sonos-bridge.service
systemctl daemon-reload
systemctl enable sonos-bridge

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
