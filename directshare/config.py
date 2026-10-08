"""Paths, ports and protocol constants."""

import getpass
import os
import socket
from pathlib import Path

APP_NAME = "DirectShare"
APP_ID = "directshare"
PROTOCOL_VERSION = 1

# UDP beacons go to a link-local multicast group, so they never leave the cable.
DISCOVERY_GROUP = "ff02::d5"
DISCOVERY_PORT = 47820
CONTROL_PORT = 47821
SSH_PORT = 47822
PORT_SCAN_RANGE = 10  # try PORT .. PORT+9 if a port is taken

BEACON_INTERVAL = 2.0
PEER_TIMEOUT = 7.0
REQUEST_TIMEOUT = 120.0  # how long the other side has to click "Accept"


def _home_override() -> Path | None:
    # DIRECTSHARE_HOME relocates all state; used to run two instances on one machine (tests).
    value = os.environ.get("DIRECTSHARE_HOME")
    return Path(value).expanduser() if value else None


def data_dir() -> Path:
    base = _home_override()
    if base:
        return base / "data"
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(xdg) / APP_ID


def runtime_dir() -> Path:
    base = _home_override()
    if base:
        return base / "run"
    xdg = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/{APP_ID}-{os.getuid()}"
    return Path(xdg) / APP_ID


def mount_base() -> Path:
    base = _home_override()
    if base:
        return base / "mnt"
    return Path.home() / APP_NAME


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def local_user() -> str:
    return getpass.getuser()


def local_host() -> str:
    return socket.gethostname()
