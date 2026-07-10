import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import bridge  # noqa: E402


# ── _patch_flac_header ────────────────────────────────────────────────────────

def _fake_streaminfo(total_samples=0):
    """Build a minimal 42-byte FLAC header: fLaC magic, STREAMINFO block
    header, and a 34-byte STREAMINFO body with the given total_samples."""
    body = bytearray(34)
    # sample rate (20 bits), channels (3), bits-per-sample (5) occupy
    # bytes 10-13; total_samples is the low 4 bits of byte 13 + bytes 14-17
    # of the body — i.e. bytes 21-25 of the whole file.
    body[13] = (body[13] & 0xF0) | ((total_samples >> 32) & 0x0F)
    body[14] = (total_samples >> 24) & 0xFF
    body[15] = (total_samples >> 16) & 0xFF
    body[16] = (total_samples >> 8) & 0xFF
    body[17] = total_samples & 0xFF
    return b"fLaC" + bytes([0x00, 0x00, 0x00, 0x22]) + bytes(body)


def test_patch_sets_max_total_samples():
    patched = bridge._patch_flac_header(_fake_streaminfo(0))
    assert patched[21] & 0x0F == 0x0F
    assert patched[22:26] == b"\xff\xff\xff\xff"


def test_patch_preserves_other_fields():
    header = bytearray(_fake_streaminfo(0))
    header[21] = 0xA0  # upper nibble holds bits-per-sample bits — must survive
    patched = bridge._patch_flac_header(bytes(header))
    assert patched[21] & 0xF0 == 0xA0
    assert patched[:21] == bytes(header[:21])
    assert patched[26:] == bytes(header[26:])


def test_patch_passthrough_short_buffer():
    assert bridge._patch_flac_header(b"fLaC") == b"fLaC"


def test_patch_passthrough_not_flac():
    buf = b"\x00" * bridge._FLAC_HEADER_LEN
    assert bridge._patch_flac_header(buf) == buf


# ── _read_config ──────────────────────────────────────────────────────────────

CONFIG_KEYS = ("SONOS_RINCON", "MY_IP", "SONOS_PORT", "STREAM_PORT", "PIPE_PATH")


@pytest.fixture
def clean_env(monkeypatch):
    for key in CONFIG_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_read_config_parses_values(tmp_path, monkeypatch, clean_env):
    cfg_file = tmp_path / "sonos-bridge.conf"
    cfg_file.write_text(
        "# a comment\n"
        "\n"
        'SONOS_RINCON="RINCON_ABC123"\n'
        "STREAM_PORT = 9000\n"
        "MY_IP='10.0.0.5'\n"
        "not a key value line\n"
    )
    monkeypatch.setattr(bridge, "_CONFIG_PATHS", [str(cfg_file)])
    cfg = bridge._read_config()
    assert cfg == {
        "SONOS_RINCON": "RINCON_ABC123",
        "STREAM_PORT": "9000",
        "MY_IP": "10.0.0.5",
    }


def test_read_config_env_overrides_file(tmp_path, monkeypatch, clean_env):
    cfg_file = tmp_path / "sonos-bridge.conf"
    cfg_file.write_text('STREAM_PORT="9000"\n')
    monkeypatch.setattr(bridge, "_CONFIG_PATHS", [str(cfg_file)])
    monkeypatch.setenv("STREAM_PORT", "7777")
    assert bridge._read_config()["STREAM_PORT"] == "7777"


def test_read_config_missing_files(monkeypatch, clean_env):
    monkeypatch.setattr(bridge, "_CONFIG_PATHS", ["/nonexistent/nope.conf"])
    assert bridge._read_config() == {}


# ── airplay-volume hook script ───────────────────────────────────────────────

@pytest.mark.parametrize("db,expected", [
    ("0.0", 100),
    ("-15.0", 50),
    ("-30.0", 0),
    ("-144.0", 0),   # AirPlay mute sentinel
    ("-0.5", 98),
])
def test_airplay_volume_mapping(tmp_path, db, expected):
    """Run the real hook script with a stubbed curl and check the URL."""
    capture = tmp_path / "curl-args"
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(f'#!/bin/sh\necho "$@" >> "{capture}"\n')
    fake_curl.chmod(fake_curl.stat().st_mode | stat.S_IXUSR)

    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}")
    subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "airplay-volume"), db],
        env=env, check=True, timeout=10,
    )
    assert f"http://localhost:8080/volume/{expected}" in capture.read_text()


def test_airplay_volume_no_arg_is_noop(tmp_path):
    capture = tmp_path / "curl-args"
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(f'#!/bin/sh\necho "$@" >> "{capture}"\n')
    fake_curl.chmod(fake_curl.stat().st_mode | stat.S_IXUSR)

    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}")
    subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "airplay-volume")],
        env=env, check=True, timeout=10,
    )
    assert not capture.exists()
