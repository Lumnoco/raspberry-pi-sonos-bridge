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
| `SONOS_PORT` | No | `1400` | Port of the Sonos UPnP/SOAP interface |
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

## HTTP API

The bridge runs one HTTP server on `STREAM_PORT` that serves three kinds of traffic:

| Method | Path | Called by | Access | Purpose |
|--------|------|-----------|--------|---------|
| `GET` | `/stream.flac` | Sonos | LAN | The live FLAC stream. Returns `503` when no AirPlay session is active or another client already holds the stream |
| `GET` | `/health` | you | LAN | JSON snapshot of bridge state (session, stream, Sonos IP, GENA) — first stop when debugging |
| `POST` | `/session/start` | hook script | localhost only | AirPlay session began: point the Sonos at the stream URL and play |
| `POST` | `/session/stop` | hook script | localhost only | AirPlay session ended: stop ffmpeg and the Sonos |
| `POST` | `/playback/pause` | hook script | localhost only | Source went silent: pause the Sonos |
| `POST` | `/playback/resume` | hook script | localhost only | Source resumed: resume or re-establish the stream |
| `POST` | `/volume/<0-100>` | hook script | localhost only | Set Sonos volume |
| `NOTIFY` | `/events` | Sonos | LAN | UPnP GENA event callback (hardware button presses) |

All `POST` endpoints reject non-localhost callers with `403` — they exist for the shairport-sync hook scripts in `scripts/`, not for remote control.

## Troubleshooting

**Sonos doesn't appear / connect**
- Make sure the Pi and Sonos are on the same subnet
- Check `journalctl -u sonos-bridge` for SOAP errors
- Re-run `--discover` to refresh the RINCON if the Sonos IP changed

**Audio cuts out**
- Use a wired ethernet connection — WiFi causes intermittent stream drops
- Check for OOM kills: `dmesg | grep -i oom`

**Stream connects but the next session is silent**
- Look for a leftover encoder from the previous session: `pgrep -a ffmpeg`. ffmpeg blocked on a silent pipe ignores SIGTERM; the bridge escalates to SIGKILL after 2 s, so a stuck one older than a session is a bug worth reporting
- `Stream busy` / repeated `Sonos did not connect within 15 s` in the logs means something is holding the single stream slot — check `ss -tn state established '( sport = :8080 )'` to see who is connected

**What the log lines mean**
- `Sonos connected — streaming` — the Sonos fetched the stream; audio should be audible within ~2 s
- `Sonos did not connect within 15 s — retrying` — SOAP commands worked but the Sonos never fetched the stream; usually a stale Sonos IP (rediscovery follows automatically)
- `Stream dropped mid-session — requesting Sonos reconnect` — normal recovery after a network blip
- `503` on `/stream.flac` with no active session is expected, not an error (the Sonos re-fetches the URI after being stopped)

**Pi-hole DNS conflicts**
- Set a fallback DNS on your router (e.g. `1.1.1.1`) so DNS survives if the Pi restarts

## Development

You can run the bridge on any machine (including a Mac) without touching `/etc`:

- Config is read from `/etc/sonos-bridge.conf` first, then a `.env` file next to `bridge.py` — put `SONOS_RINCON="RINCON_…"` in `.env` for local runs
- Environment variables (`SONOS_RINCON`, `MY_IP`, `SONOS_PORT`, `STREAM_PORT`, `PIPE_PATH`) override both files
- `python3 bridge.py --discover` scans the LAN and needs no config
- Without shairport-sync you can still exercise the Sonos control path: create the FIFO (`mkfifo /tmp/audio`, set `PIPE_PATH=/tmp/audio`), start the bridge, and `curl -X POST localhost:8080/session/start` — the Sonos will connect and play silence

Run the tests and linter:

```bash
pip install pytest ruff
pytest
ruff check .
```

## Architecture notes

- Sonos IP is discovered via SSDP and cached; IP changes are handled automatically
- UPnP GENA subscriptions relay hardware button presses (play/pause) back to Apple Music
- The FLAC `total_samples` field is patched to `2^36-1` so Sonos treats the live stream as a long file rather than dropping it after one buffer
- A health watchdog polls Sonos transport state every 15 seconds and reconnects if it silently goes idle mid-session

## License

[MIT](LICENSE)
