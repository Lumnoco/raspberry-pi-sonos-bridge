#!/usr/bin/env python3
"""
Sonos AirPlay Bridge

Receives raw PCM from shairport-sync via a named FIFO, converts to FLAC via
ffmpeg, serves it over HTTP, and controls a Sonos speaker via UPnP/SOAP.

Sonos IP is discovered automatically via SSDP using the device RINCON ID so
it survives DHCP address changes.

Physical Sonos button presses are detected via UPnP GENA subscriptions and
relayed to Apple Music through shairport-sync's MPRIS D-Bus interface.

Configuration: /etc/sonos-bridge.conf (KEY=VALUE, one per line)
  SONOS_RINCON  – required; run --discover to find yours
  MY_IP         – optional; auto-detected from the default network interface
  SONOS_PORT    – optional; default 1400
  STREAM_PORT   – optional; default 8080
  PIPE_PATH     – optional; must match shairport-sync.conf
"""

import http.client
import http.server
import logging
import os
import re
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import threading
import time
import urllib.request

# ── Config ────────────────────────────────────────────────────────────────────

_CONFIG_PATHS = [
    "/etc/sonos-bridge.conf",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
]


def _read_config():
    cfg = {}
    for path in _CONFIG_PATHS:
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        cfg[k.strip()] = v.strip().strip("'\"")
        except FileNotFoundError:
            pass
    for key in ("SONOS_RINCON", "MY_IP", "SONOS_PORT", "STREAM_PORT", "PIPE_PATH"):
        if key in os.environ:
            cfg[key] = os.environ[key]
    return cfg


def _auto_ip():
    """Return the host's LAN IP by probing an outbound UDP connection."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


_cfg = _read_config()

SONOS_RINCON = _cfg.get("SONOS_RINCON", "")
SONOS_PORT   = int(_cfg.get("SONOS_PORT", 1400))
STREAM_PORT  = int(_cfg.get("STREAM_PORT", 8080))
PIPE_PATH    = _cfg.get("PIPE_PATH", "/run/sonos-bridge/audio")

_my_ip_cfg   = _cfg.get("MY_IP", "")
_my_ip_cache = None
_my_ip_lock  = threading.Lock()


def get_my_ip():
    """Return the configured or auto-detected LAN IP.

    Auto-detection is retried whenever the cached result is loopback, so a
    service that started before the network was up recovers once it is."""
    global _my_ip_cache
    if _my_ip_cfg:
        return _my_ip_cfg
    with _my_ip_lock:
        if _my_ip_cache is None or _my_ip_cache.startswith("127."):
            _my_ip_cache = _auto_ip()
        return _my_ip_cache

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sonos-bridge")

# ── Sonos SSDP discovery ──────────────────────────────────────────────────────

_sonos_ip       = None
_discovery_lock = threading.Lock()
_SSDP_ADDR      = "239.255.255.250"
_SSDP_PORT      = 1900


def _ssdp_search(timeout=5):
    """Return list of (ip, location_url) for all Sonos ZonePlayer devices found."""
    req = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {_SSDP_ADDR}:{_SSDP_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 3\r\n"
        "ST: urn:schemas-upnp-org:device:ZonePlayer:1\r\n"
        "\r\n"
    )
    results, seen = [], set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(timeout)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
    try:
        sock.sendto(req.encode(), (_SSDP_ADDR, _SSDP_PORT))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, (ip, _) = sock.recvfrom(4096)
            except socket.timeout:
                break
            if ip in seen:
                continue
            seen.add(ip)
            loc = re.search(r"LOCATION:\s*(\S+)", data.decode(errors="ignore"), re.I)
            if loc:
                results.append((ip, loc.group(1)))
    finally:
        sock.close()
    return results


def discover_all_sonos(timeout=5):
    """Return a list of dicts with ip, rincon, name for every Sonos on the LAN."""
    devices = []
    for ip, location in _ssdp_search(timeout):
        try:
            with urllib.request.urlopen(location, timeout=3) as r:
                body = r.read().decode(errors="ignore")
        except Exception:
            continue
        rincon = re.search(r"(RINCON_[A-F0-9]+)", body)
        name   = re.search(r"<roomName>(.*?)</roomName>", body, re.I)
        if rincon:
            devices.append({
                "ip":     ip,
                "rincon": rincon.group(1),
                "name":   name.group(1) if name else ip,
            })
    return devices


def get_sonos_ip(force_rediscover=False):
    global _sonos_ip
    with _discovery_lock:
        if _sonos_ip and not force_rediscover:
            return _sonos_ip
        if not SONOS_RINCON:
            log.error("SONOS_RINCON not set — run: python3 bridge.py --discover")
            return None
        log.info("Discovering Sonos %s…", SONOS_RINCON)
        for ip, location in _ssdp_search():
            try:
                with urllib.request.urlopen(location, timeout=3) as r:
                    if SONOS_RINCON in r.read().decode(errors="ignore"):
                        log.info("Found Sonos at %s", ip)
                        _sonos_ip = ip
                        return ip
            except Exception:
                pass
        log.warning("Sonos not found; using last known IP (%s)", _sonos_ip)
        return _sonos_ip


# ── Sonos UPnP / SOAP ─────────────────────────────────────────────────────────

# Track the last transport command we sent so GENA events caused by our own
# commands can be distinguished from physical button presses.
_last_transport_cmd      = (None, 0.0)  # (name, monotonic_time)
_transport_cmd_lock      = threading.Lock()
_TRANSPORT_DEBOUNCE_SECS = 2.0


def _mark_transport_cmd(name):
    global _last_transport_cmd
    with _transport_cmd_lock:
        _last_transport_cmd = (name, time.monotonic())


def _soap(path, action, body, retry=True, timeout=10):
    ip = get_sonos_ip()
    if not ip:
        return
    envelope = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f"<s:Body>{body}</s:Body>"
        "</s:Envelope>"
    )
    req = urllib.request.Request(
        f"http://{ip}:{SONOS_PORT}{path}",
        data=envelope.encode(),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION":   f'"{action}"',
        },
    )
    try:
        urllib.request.urlopen(req, timeout=timeout)
    except Exception as e:
        log.error("SOAP %s failed: %s", action.split("#")[-1], e)
        if retry:
            # Don't rediscover/retry while actively streaming. The Sonos is
            # already playing — retrying Play would make it drop the current
            # HTTP connection and re-fetch, causing a reconnect storm.
            with _ffmpeg_lock:
                streaming = _ffmpeg_proc is not None
            if not streaming:
                get_sonos_ip(force_rediscover=True)
                _soap(path, action, body, retry=False)


def sonos_set_uri(url):
    didl = (
        "&lt;DIDL-Lite xmlns=\"urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/\""
        " xmlns:dc=\"http://purl.org/dc/elements/1.1/\""
        " xmlns:upnp=\"urn:schemas-upnp-org:metadata-1-0/upnp/\"&gt;"
        "&lt;item id=\"1\" parentID=\"0\" restricted=\"1\"&gt;"
        "&lt;dc:title&gt;AirPlay&lt;/dc:title&gt;"
        "&lt;upnp:class&gt;object.item.audioItem.audioBroadcast&lt;/upnp:class&gt;"
        f"&lt;res protocolInfo=\"http-get:*:audio/flac:*\"&gt;{url}&lt;/res&gt;"
        "&lt;/item&gt;&lt;/DIDL-Lite&gt;"
    )
    _soap(
        "/MediaRenderer/AVTransport/Control",
        "urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI",
        f'<u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        f"<InstanceID>0</InstanceID><CurrentURI>{url}</CurrentURI>"
        f"<CurrentURIMetaData>{didl}</CurrentURIMetaData></u:SetAVTransportURI>",
    )


def sonos_play():
    _mark_transport_cmd("play")
    _soap(
        "/MediaRenderer/AVTransport/Control",
        "urn:schemas-upnp-org:service:AVTransport:1#Play",
        '<u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        "<InstanceID>0</InstanceID><Speed>1</Speed></u:Play>",
        timeout=20,
    )


def sonos_pause():
    _mark_transport_cmd("pause")
    _soap(
        "/MediaRenderer/AVTransport/Control",
        "urn:schemas-upnp-org:service:AVTransport:1#Pause",
        '<u:Pause xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        "<InstanceID>0</InstanceID></u:Pause>",
    )


def sonos_stop():
    _mark_transport_cmd("stop")
    _soap(
        "/MediaRenderer/AVTransport/Control",
        "urn:schemas-upnp-org:service:AVTransport:1#Stop",
        '<u:Stop xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        "<InstanceID>0</InstanceID></u:Stop>",
    )


def sonos_set_volume(vol):
    _soap(
        "/MediaRenderer/RenderingControl/Control",
        "urn:schemas-upnp-org:service:RenderingControl:1#SetVolume",
        '<u:SetVolume xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">'
        f"<InstanceID>0</InstanceID><Channel>Master</Channel>"
        f"<DesiredVolume>{vol}</DesiredVolume></u:SetVolume>",
    )


# ── UPnP GENA event subscription ─────────────────────────────────────────────

_gena_sids  = {}  # service_path → SID
_gena_lock  = threading.Lock()

# Only subscribe to AVTransport so we can detect hardware button presses.
_GENA_SERVICES = ["/MediaRenderer/AVTransport/EventSub"]


def _gena_request(method, service, extra_headers):
    ip = get_sonos_ip()
    if not ip:
        return None
    try:
        conn = http.client.HTTPConnection(ip, SONOS_PORT, timeout=5)
        conn.request(method, service, headers={
            "HOST": f"{ip}:{SONOS_PORT}",
            **extra_headers,
        })
        resp = conn.getresponse()
        resp.read()
        conn.close()
        if not 200 <= resp.status < 300:
            log.debug("GENA %s %s returned HTTP %d", method, service, resp.status)
            return None
        return resp.getheader("SID")
    except Exception as e:
        log.debug("GENA %s %s failed: %s", method, service, e)
        return None


def _gena_subscribe_all():
    """Subscribe or renew all GENA services. Returns True if all succeeded."""
    all_ok = True
    with _gena_lock:
        for svc in _GENA_SERVICES:
            existing = _gena_sids.get(svc)
            if existing:
                sid = _gena_request("SUBSCRIBE", svc, {
                    "SID":     existing,
                    "TIMEOUT": "Second-3600",
                })
                if not sid:
                    # Stale SID — Sonos likely rebooted; do a fresh subscribe now.
                    log.info("GENA renewal failed for %s — re-subscribing", svc)
                    del _gena_sids[svc]
                    sid = _gena_request("SUBSCRIBE", svc, {
                        "CALLBACK": f"<http://{get_my_ip()}:{STREAM_PORT}/events>",
                        "NT":       "upnp:event",
                        "TIMEOUT":  "Second-3600",
                    })
            else:
                sid = _gena_request("SUBSCRIBE", svc, {
                    "CALLBACK": f"<http://{get_my_ip()}:{STREAM_PORT}/events>",
                    "NT":       "upnp:event",
                    "TIMEOUT":  "Second-3600",
                })
            if sid:
                _gena_sids[svc] = sid
            else:
                all_ok = False
    return all_ok


def _gena_unsubscribe_all():
    """Best-effort UNSUBSCRIBE so the Sonos stops notifying a dead callback."""
    with _gena_lock:
        for svc, sid in list(_gena_sids.items()):
            _gena_request("UNSUBSCRIBE", svc, {"SID": sid})
            del _gena_sids[svc]


def _gena_worker():
    """Subscribe at startup, renew every 55 minutes. Retry in 30 s on failure."""
    time.sleep(5)  # let the HTTP server start first
    while True:
        ok = _gena_subscribe_all()
        time.sleep(55 * 60 if ok else 30)


# ── Apple Music remote control via shairport-sync MPRIS ──────────────────────

def _mpris(method):
    """Call an MPRIS method on shairport-sync's system-bus interface."""
    try:
        subprocess.run(
            [
                "dbus-send", "--system", "--print-reply",
                "--dest=org.mpris.MediaPlayer2.ShairportSync",
                "/org/mpris/MediaPlayer2",
                f"org.mpris.MediaPlayer2.Player.{method}",
            ],
            timeout=2,
            capture_output=True,
        )
        log.info("MPRIS %s sent", method)
    except Exception as e:
        log.debug("MPRIS %s failed: %s", method, e)


def _handle_gena_event(body):
    """
    Parse an AVTransport GENA event and relay hardware button presses to Apple
    Music.  Events triggered by our own SOAP commands are suppressed via a
    debounce window.
    """
    if "TransportState" not in body:
        return

    # Ignore if we just sent the command ourselves
    with _transport_cmd_lock:
        _, t = _last_transport_cmd
    if time.monotonic() - t < _TRANSPORT_DEBOUNCE_SECS:
        return

    # Only relay when an AirPlay session is active
    with _state_lock:
        if not _in_session:
            return

    if "PAUSED_PLAYBACK" in body:
        log.info("Sonos hardware pause — relaying to Apple Music")
        threading.Thread(target=_mpris, args=("Pause",), daemon=True).start()
    elif ">PLAYING<" in body or 'val="PLAYING"' in body:
        log.info("Sonos hardware play — relaying to Apple Music")
        threading.Thread(target=_mpris, args=("Play",), daemon=True).start()


# ── FIFO helpers ──────────────────────────────────────────────────────────────

_keepalive_fd = None


def _setup_pipe():
    """Ensure the FIFO exists and hold it open for both read and write so that
    ffmpeg's open() never blocks waiting for a writer, and shairport-sync's
    open() never blocks waiting for a reader.

    Idempotent: if a FIFO already exists at the path (e.g. created by
    ExecStartPre or surviving from a previous run while shairport-sync still
    holds it open for writing), reuse it instead of unlinking and recreating."""
    global _keepalive_fd
    if _keepalive_fd is not None:
        try:
            os.close(_keepalive_fd)
        except OSError:
            pass
        _keepalive_fd = None
    try:
        st = os.stat(PIPE_PATH)
        is_fifo = stat.S_ISFIFO(st.st_mode)
    except FileNotFoundError:
        is_fifo = False
    if not is_fifo:
        try:
            os.remove(PIPE_PATH)
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(PIPE_PATH) or ".", exist_ok=True)
        os.mkfifo(PIPE_PATH)
    os.chmod(PIPE_PATH, 0o666)
    _keepalive_fd = os.open(PIPE_PATH, os.O_RDWR)


def _teardown_pipe():
    global _keepalive_fd
    if _keepalive_fd is not None:
        try:
            os.close(_keepalive_fd)
        except OSError:
            pass
        _keepalive_fd = None
    try:
        os.remove(PIPE_PATH)
    except FileNotFoundError:
        pass


# ── HTTP handler ──────────────────────────────────────────────────────────────

_stream_lock       = threading.Lock()
_ffmpeg_proc       = None
_ffmpeg_lock       = threading.Lock()
_client_ready      = threading.Event()
_in_session        = False
_sonos_paused      = False
_session_start_time = 0.0
_state_lock        = threading.Lock()

# FLAC STREAMINFO is always the first metadata block. ffmpeg writes total_samples=0
# for a live stream, which Sonos interprets as an empty file and drops the connection
# after the first play. Patching it to the 36-bit maximum (~399 days at 48 kHz) makes
# the Sonos treat the stream as a very long file and keep playing.
_FLAC_HEADER_LEN  = 42          # fLaC(4) + block_header(4) + STREAMINFO(34)
_FLAC_MAX_SAMPLES = (1 << 36) - 1


def _patch_flac_header(buf: bytes) -> bytes:
    if len(buf) < _FLAC_HEADER_LEN or buf[:4] != b'fLaC':
        return buf
    b = bytearray(buf)
    s = _FLAC_MAX_SAMPLES
    # total_samples is 36 bits starting at bit 172 of the file:
    #   byte 21 lower nibble = bits 35-32, bytes 22-25 = bits 31-0
    b[21] = (b[21] & 0xF0) | ((s >> 32) & 0x0F)
    b[22] = (s >> 24) & 0xFF
    b[23] = (s >> 16) & 0xFF
    b[24] = (s >>  8) & 0xFF
    b[25] =  s        & 0xFF
    return bytes(b)


def _stop_ffmpeg(proc):
    """Terminate ffmpeg, escalating to SIGKILL if it doesn't exit promptly.

    ffmpeg's SIGTERM handler only sets a flag that its processing loop
    checks; while blocked reading a FIFO that has never produced data, that
    loop never runs and SIGTERM is effectively ignored. The stream handler
    is itself blocked on ffmpeg's stdout at that point, so it can't clean
    up either — without the SIGKILL the handler holds _stream_lock forever
    and every new Sonos connection gets a 503."""
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()


class BridgeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        global _ffmpeg_proc

        if self.path != "/stream.flac":
            self.send_error(404)
            return

        # Serialise connections: Sonos sometimes opens multiple simultaneous
        # connections; only the first gets the stream, others get 503.
        if not _stream_lock.acquire(timeout=8):
            self.send_error(503, "Stream busy")
            return

        try:
            self.send_response(200)
            self.send_header("Content-Type", "audio/flac")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            # Only count this as "the Sonos connected" if it actually is the
            # Sonos — a stray LAN client fetching the URL must not satisfy
            # session/start's wait.
            if _sonos_ip is None or self.client_address[0] == _sonos_ip:
                _client_ready.set()
            stream_start = time.monotonic()

            ffmpeg = subprocess.Popen(
                [
                    "ffmpeg", "-loglevel", "quiet",
                    "-fflags", "nobuffer",
                    "-probesize", "32",
                    "-analyzeduration", "0",
                    "-f", "s32le", "-ar", "48000", "-ac", "2",
                    "-i", PIPE_PATH,
                    "-c:a", "flac",
                    "-compression_level", "0",
                    "-f", "flac", "-",
                ],
                stdout=subprocess.PIPE,
                bufsize=0,
            )
            with _ffmpeg_lock:
                _ffmpeg_proc = ffmpeg

            log.info("Sonos connected — streaming")
            try:
                # Read and patch the FLAC STREAMINFO before forwarding so
                # the Sonos doesn't treat total_samples=0 as an empty file.
                header = b''
                while len(header) < _FLAC_HEADER_LEN:
                    chunk = ffmpeg.stdout.read(_FLAC_HEADER_LEN - len(header))
                    if not chunk:
                        break
                    header += chunk
                self.wfile.write(_patch_flac_header(header))
                self.wfile.flush()

                while True:
                    chunk = ffmpeg.stdout.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                log.info("Sonos disconnected")
            finally:
                ffmpeg.terminate()
                try:
                    ffmpeg.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    ffmpeg.kill()
                    ffmpeg.wait()
                with _ffmpeg_lock:
                    _ffmpeg_proc = None
                # If the session is still active and the stream ran long enough
                # to have real audio, the Sonos dropped unexpectedly — reconnect.
                # Short-lived connections (< 5 s) are probe/negotiation drops;
                # reconnecting immediately causes a storm, so skip them and let
                # the Sonos re-fetch on its own or wait for the next session/start.
                stream_duration = time.monotonic() - stream_start
                with _state_lock:
                    should_reconnect = _in_session and not _sonos_paused
                if should_reconnect and stream_duration < 5:
                    log.info("Stream lasted %.1f s — skipping reconnect (probe drop)", stream_duration)
                    should_reconnect = False
                if should_reconnect:
                    log.info("Stream dropped mid-session — requesting Sonos reconnect")
                    def _reconnect():
                        time.sleep(0.5)
                        with _state_lock:
                            if not _in_session or _sonos_paused:
                                return
                        _client_ready.clear()
                        stream_url = f"http://{get_my_ip()}:{STREAM_PORT}/stream.flac"
                        sonos_set_uri(stream_url)
                        sonos_play()
                        if not _client_ready.wait(timeout=10):
                            log.warning("Sonos did not reconnect after stream drop")
                    threading.Thread(target=_reconnect, daemon=True).start()
        finally:
            _stream_lock.release()

    def do_POST(self):
        global _in_session, _sonos_paused

        # Control endpoints are only ever called by the shairport-sync hook
        # scripts on this host; don't let other LAN devices drive the session.
        if self.client_address[0] != "127.0.0.1":
            self.send_error(403)
            return

        if self.path == "/session/start":
            global _session_start_time
            log.info("AirPlay session starting")

            # If a previous stream is still active, tear it down cleanly before
            # setting up the new one. Set _sonos_paused=True to suppress do_GET's
            # reconnect logic while we drain the old stream.
            with _state_lock:
                _in_session   = True
                _sonos_paused = True
            with _ffmpeg_lock:
                old_proc = _ffmpeg_proc
            if old_proc:
                log.info("Terminating previous stream for new session")
                _stop_ffmpeg(old_proc)
            if _stream_lock.acquire(timeout=8):
                _stream_lock.release()

            with _state_lock:
                _sonos_paused = False
                _session_start_time = time.monotonic()
            _client_ready.clear()

            # Defensive: if the FIFO has gone missing since startup (e.g. someone
            # cleaned /run, or the path got unlinked), recreate it before
            # shairport-sync tries to write into nothing.
            try:
                if not stat.S_ISFIFO(os.stat(PIPE_PATH).st_mode):
                    raise FileNotFoundError
            except FileNotFoundError:
                log.warning("FIFO %s missing — recreating", PIPE_PATH)
                _setup_pipe()

            stream_url = f"http://{get_my_ip()}:{STREAM_PORT}/stream.flac"
            log.info("Sending stream URL to Sonos: %s", stream_url)
            sonos_set_uri(stream_url)
            sonos_play()

            if not _client_ready.wait(timeout=15):
                log.warning("Sonos did not connect within 15 s — retrying")
                _client_ready.clear()
                sonos_set_uri(stream_url)
                sonos_play()
                if not _client_ready.wait(timeout=15):
                    log.warning("Sonos did not connect on retry — forcing rediscovery")
                    _client_ready.clear()
                    get_sonos_ip(force_rediscover=True)
                    sonos_set_uri(stream_url)
                    sonos_play()
                    if not _client_ready.wait(timeout=15):
                        log.warning("Sonos did not connect after rediscovery")

            self.send_response(200)
            self.end_headers()

        elif self.path == "/session/stop":
            log.info("AirPlay session ended")
            with _state_lock:
                _in_session   = False
                _sonos_paused = False
            with _ffmpeg_lock:
                proc = _ffmpeg_proc
            if proc:
                _stop_ffmpeg(proc)
            _client_ready.clear()
            if _stream_lock.acquire(timeout=5):
                _stream_lock.release()
            # Synchronous — must complete before this handler returns 200.
            # shairport-sync's wait_for_completion=yes means session/start
            # cannot fire until we respond, so keeping this in-line eliminates
            # the race where a stale sonos_stop() fires after the new session's
            # sonos_play().
            sonos_stop()
            self.send_response(200)
            self.end_headers()

        elif self.path == "/playback/pause":
            with _state_lock:
                if not _in_session or _sonos_paused:
                    self.send_response(200)
                    self.end_headers()
                    return
                _sonos_paused = True
            log.info("Playback paused — halting Sonos output")
            threading.Thread(target=sonos_pause, daemon=True).start()
            self.send_response(200)
            self.end_headers()

        elif self.path == "/playback/resume":
            with _state_lock:
                if not _in_session or not _sonos_paused:
                    self.send_response(200)
                    self.end_headers()
                    return
                _sonos_paused = False
            with _ffmpeg_lock:
                ffmpeg_alive = _ffmpeg_proc is not None
            if ffmpeg_alive:
                log.info("Playback resumed — resuming Sonos output")
                threading.Thread(target=sonos_play, daemon=True).start()
            else:
                # Stream died while paused (e.g. Sonos dropped the connection);
                # re-establish from scratch.
                log.info("Playback resumed — stream gone, re-establishing")
                def _reestablish():
                    _client_ready.clear()
                    stream_url = f"http://{get_my_ip()}:{STREAM_PORT}/stream.flac"
                    sonos_set_uri(stream_url)
                    sonos_play()
                    if not _client_ready.wait(timeout=10):
                        log.warning("Sonos did not reconnect within 10 s")
                threading.Thread(target=_reestablish, daemon=True).start()
            self.send_response(200)
            self.end_headers()

        elif self.path.startswith("/volume/"):
            try:
                vol = max(0, min(100, int(self.path.split("/")[-1])))
                log.info("Setting Sonos volume to %d", vol)
                threading.Thread(target=sonos_set_volume, args=(vol,), daemon=True).start()
            except (ValueError, IndexError):
                pass
            self.send_response(200)
            self.end_headers()

        else:
            self.send_error(404)

    def do_NOTIFY(self):
        """Receive UPnP GENA event from Sonos (hardware button presses, etc.)."""
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(min(length, 65536)).decode(errors="ignore")
        self.send_response(200)
        self.end_headers()
        threading.Thread(target=_handle_gena_event, args=(body,), daemon=True).start()


# ── Health watchdog ───────────────────────────────────────────────────────────

def _get_transport_state():
    """Query Sonos GetTransportInfo; return state string or None on error."""
    ip = get_sonos_ip()
    if not ip:
        return None
    envelope = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        '<s:Body>'
        '<u:GetTransportInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        '<InstanceID>0</InstanceID>'
        '</u:GetTransportInfo>'
        '</s:Body></s:Envelope>'
    )
    req = urllib.request.Request(
        f"http://{ip}:{SONOS_PORT}/MediaRenderer/AVTransport/Control",
        data=envelope.encode(),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#GetTransportInfo"',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read().decode(errors="ignore")
        m = re.search(r"<CurrentTransportState>(.*?)</CurrentTransportState>", body)
        return m.group(1) if m else None
    except Exception as e:
        log.debug("GetTransportInfo failed: %s", e)
        return None


def _watchdog_worker():
    """Periodically verify Sonos is playing during an active session.

    If Sonos is unreachable, force rediscovery. If it silently went STOPPED
    (e.g. speaker rebooted) while we think a session is active, reconnect.
    """
    time.sleep(20)  # give session/start time to complete on first connect
    while True:
        time.sleep(15)
        with _state_lock:
            if not _in_session or _sonos_paused:
                continue
            session_start = _session_start_time
        # Stand down during the initial connection window so session/start
        # retries don't race with watchdog reconnects.
        if time.monotonic() - session_start < 45:
            continue
        state = _get_transport_state()
        if state is None:
            log.warning("Watchdog: Sonos unreachable — forcing rediscovery")
            get_sonos_ip(force_rediscover=True)
            continue
        if state == "STOPPED":
            # Check stream is also gone — if ffmpeg is still running the
            # stream handler will manage its own reconnect.
            with _ffmpeg_lock:
                ffmpeg_running = _ffmpeg_proc is not None
            if ffmpeg_running:
                continue
            log.warning("Watchdog: Sonos STOPPED with no active stream — reconnecting")
            with _state_lock:
                if not _in_session or _sonos_paused:
                    continue
            _client_ready.clear()
            stream_url = f"http://{get_my_ip()}:{STREAM_PORT}/stream.flac"
            sonos_set_uri(stream_url)
            sonos_play()
            if not _client_ready.wait(timeout=10):
                log.warning("Watchdog: Sonos did not reconnect within 10 s")


# ── Signal handling ────────────────────────────────────────────────────────────

def _on_sigterm(signum, frame):
    log.info("SIGTERM — shutting down")
    with _ffmpeg_lock:
        proc = _ffmpeg_proc
    if proc:
        _stop_ffmpeg(proc)
    _teardown_pipe()
    raise SystemExit(0)


# ── CLI: --discover ───────────────────────────────────────────────────────────

def _cmd_discover():
    print("Scanning for Sonos devices on the local network…")
    devices = discover_all_sonos()
    if not devices:
        print("No Sonos devices found. Make sure the device is powered on and"
              " on the same network.")
        return

    print(f"\nFound {len(devices)} device(s):\n")
    for i, d in enumerate(devices, 1):
        print(f"  {i}.  {d['name']:<28}  {d['rincon']}  ({d['ip']})")
    print()

    if len(devices) == 1:
        choice = 1
    else:
        try:
            choice = int(input("Select device number to configure: "))
        except (ValueError, KeyboardInterrupt):
            print("\nAborted.")
            return

    if not 1 <= choice <= len(devices):
        print("Invalid choice.")
        return

    dev     = devices[choice - 1]
    cfg_path = "/etc/sonos-bridge.conf"
    new_line = f'SONOS_RINCON="{dev["rincon"]}"\n'

    try:
        try:
            with open(cfg_path) as f:
                lines = f.readlines()
        except FileNotFoundError:
            lines = []

        lines = [l for l in lines if not l.strip().startswith("SONOS_RINCON")]
        lines.append(new_line)

        with open(cfg_path, "w") as f:
            f.writelines(lines)

        print(f'Configured {dev["name"]} ({dev["rincon"]}) in {cfg_path}')
        print("Restart the bridge to apply: sudo systemctl restart sonos-bridge")
    except PermissionError:
        print(f"Permission denied writing {cfg_path}. Run with sudo, or add manually:")
        print(f"  echo '{new_line.strip()}' | sudo tee -a {cfg_path}")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--discover" in sys.argv:
        _cmd_discover()
        sys.exit(0)

    if not SONOS_RINCON:
        log.error("SONOS_RINCON is not set.")
        log.error("Run:  sudo python3 %s --discover", __file__)
        sys.exit(1)

    signal.signal(signal.SIGTERM, _on_sigterm)

    _setup_pipe()
    threading.Thread(target=get_sonos_ip, daemon=True).start()
    threading.Thread(target=_gena_worker, daemon=True).start()
    threading.Thread(target=_watchdog_worker, daemon=True).start()

    socketserver.TCPServer.allow_reuse_address = True
    # Daemon threads so a handler blocked reading ffmpeg output can't keep
    # the process alive past SIGTERM.
    socketserver.ThreadingTCPServer.daemon_threads = True
    with socketserver.ThreadingTCPServer(("", STREAM_PORT), BridgeHandler) as srv:
        log.info("Sonos AirPlay Bridge listening on :%d  (MY_IP=%s  RINCON=%s)",
                 STREAM_PORT, get_my_ip(), SONOS_RINCON)
        try:
            srv.serve_forever()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            _gena_unsubscribe_all()
            _teardown_pipe()
