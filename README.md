# Raspberry Pi Sonos AirPlay Bridge

Stream Apple Music (and any AirPlay source) to a Sonos speaker using a Raspberry Pi as a bridge. The Pi appears as an AirPlay device in your Apple ecosystem, receives the audio via shairport-sync, and forwards it to Sonos over its native UPnP/SOAP protocol.

## How it works

```
Apple Music → AirPlay → shairport-sync → FIFO → ffmpeg → FLAC HTTP stream → Sonos
```

- **shairport-sync** receives the AirPlay stream and writes raw PCM to a named pipe
- **bridge.py** encodes that PCM to FLAC via ffmpeg and serves it over HTTP
- The bridge controls the Sonos via UPnP/SOAP (play, pause, stop, volume, URI)
- Physical Sonos button presses are relayed back to Apple Music via shairport-sync's MPRIS D-Bus interface

## Requirements

- Raspberry Pi (any model with network connectivity; ethernet strongly recommended)
- Raspberry Pi OS (Bookworm or later)
- A Sonos speaker on the same LAN
- **shairport-sync** built with pipe output and D-Bus support
- `ffmpeg`, `python3`, `curl`

### Install shairport-sync

shairport-sync must be built from source with the required options. Follow the [official build guide](https://github.com/mikebrady/shairport-sync/blob/master/BUILD.md), adding these configure flags:

```bash
./configure --with-pipe --with-dbus-interface --with-mpris-interface --with-systemd
```

## Installation

```bash
git clone https://github.com/Lumnoco/raspberry-pi-sonos-bridge.git
cd raspberry-pi-sonos-bridge
sudo bash install.sh
```

The installer will:
1. Install `ffmpeg`, `python3`, and `curl`
2. Copy the bridge script to `/opt/sonos-bridge/`
3. Install shairport-sync config and hook scripts
4. Register and enable the `sonos-bridge` systemd service

## Configuration

After install, discover your Sonos RINCON ID:

```bash
python3 /opt/sonos-bridge/bridge.py --discover
```

This scans the network and writes the selected device's RINCON to `/etc/sonos-bridge.conf`.

Then start the bridge:

```bash
sudo systemctl start sonos-bridge
```

Your Pi will appear as **"Playbar"** (or whatever name you set in `shairport-sync.conf`) in the AirPlay picker on your Apple devices.

### Config file: `/etc/sonos-bridge.conf`

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `SONOS_RINCON` | Yes | — | Device RINCON ID (run `--discover`) |
| `MY_IP` | No | auto-detected | LAN IP of the Pi |
| `STREAM_PORT` | No | `8080` | Port the bridge HTTP server listens on |
| `PIPE_PATH` | No | `/run/sonos-bridge/audio` | Must match `shairport-sync.conf` |

### Rename the AirPlay device

Edit the `name` field in `shairport-sync.conf` before installing, or edit `/etc/shairport-sync.conf` directly and restart shairport-sync:

```bash
sudo systemctl restart shairport-sync
```

## Logs

```bash
# Bridge logs
journalctl -u sonos-bridge -f

# shairport-sync logs
journalctl -u shairport-sync -f
```

## Troubleshooting

**Sonos doesn't appear / connect**
- Make sure the Pi and Sonos are on the same subnet
- Check `journalctl -u sonos-bridge` for SOAP errors
- Re-run `--discover` to refresh the RINCON if the Sonos IP changed

**Audio cuts out**
- Use a wired ethernet connection — WiFi causes intermittent stream drops
- Check for OOM kills: `dmesg | grep -i oom`

**Pi-hole DNS conflicts**
- Set a fallback DNS on your router (e.g. `1.1.1.1`) so DNS survives if the Pi restarts

## Architecture notes

- Sonos IP is discovered via SSDP and cached; IP changes are handled automatically
- UPnP GENA subscriptions relay hardware button presses (play/pause) back to Apple Music
- The FLAC `total_samples` field is patched to `2^36-1` so Sonos treats the live stream as a long file rather than dropping it after one buffer
- A health watchdog polls Sonos transport state every 15 seconds and reconnects if it silently goes idle mid-session

## License

MIT
