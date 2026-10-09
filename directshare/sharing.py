"""The actual file sharing: an unprivileged SFTP server and an sshfs mount of the peer.

Two transports, both serving SFTP as the logged-in user, so the peer can do exactly what
that user can do - no more, no less:

- ssh (Ethernet): a private sshd instance that only accepts the one-time key the peer sent
  for this session, only speaks SFTP, and only listens on the cable's link-local address.
- direct (USB-C / Thunderbolt): SSH encryption caps throughput at ~0.5-0.9 GB/s, far below
  what a Thunderbolt cable carries. On that strictly point-to-point link we skip it: the
  peer connects over plain TCP, proves itself with a one-time session token, and the socket
  is handed straight to sftp-server (and on the other side straight to sshfs), so no
  process copies the data in between. Measured ~2 GB/s read, ~1.4 GB/s write.
"""

from __future__ import annotations

import os
import re
import shutil
import hmac
import socket
import subprocess
import threading
import time
from pathlib import Path

from . import config

SSHD = "/usr/bin/sshd" if os.path.exists("/usr/bin/sshd") else (shutil.which("sshd") or "/usr/sbin/sshd")


class ShareError(Exception):
    pass


SFTP_SERVER_PATHS = ["/usr/lib/ssh/sftp-server", "/usr/lib/openssh/sftp-server",
                     "/usr/libexec/openssh/sftp-server", "/usr/libexec/sftp-server", "/usr/lib/misc/sftp-server"]

# Prefer AES-GCM: with AES-NI it is ~1.7x faster than OpenSSH's default chacha20-poly1305.
SSH_CIPHERS = "aes128-gcm@openssh.com,aes256-gcm@openssh.com,chacha20-poly1305@openssh.com"


def sftp_server() -> str | None:
    return next((p for p in SFTP_SERVER_PATHS if os.access(p, os.X_OK)), None)


def missing_tools() -> list[str]:
    tools = {"sshd": SSHD, "ssh-keygen": "ssh-keygen", "sshfs": "sshfs", "fusermount3": "fusermount3"}
    return [name for name, cmd in tools.items() if not (os.path.exists(cmd) or shutil.which(cmd))]


def generate_key(path: Path, comment: str) -> str:
    """Create an ed25519 key pair at path (replacing an old one) and return the public key."""
    for p in (path, path.with_suffix(".pub")):
        p.unlink(missing_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)],
                   check=True, capture_output=True)
    return path.with_suffix(".pub").read_text().strip()


def host_key() -> tuple[Path, str]:
    """The persistent host key of this computer's file server."""
    path = config.ensure_private_dir(config.data_dir()) / "host_ed25519"
    pub = path.with_suffix(".pub")
    if not path.exists() or not pub.exists():
        generate_key(path, f"{config.APP_ID}-host@{config.local_host()}")
    return path, pub.read_text().strip()


def _valid_pubkey(key: str) -> bool:
    return re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+( [^\n]*)?", key or "") is not None


def _port_open(address: str, port: int, ifindex: int) -> bool:
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((address, port, 0, ifindex)) == 0


class FileServer:
    def __init__(self, session_dir: Path):
        self.dir = session_dir
        self.proc: subprocess.Popen | None = None
        self.port = 0

    def start(self, address: str, ifname: str, ifindex: int, authorized_key: str) -> int:
        if not _valid_pubkey(authorized_key):
            raise ShareError("Peer sent an invalid key")
        hk_path, _ = host_key()
        (self.dir / "authorized_keys").write_text(authorized_key + "\n")
        log = open(self.dir / "sshd.log", "ab")
        for port in range(config.SSH_PORT, config.SSH_PORT + config.PORT_SCAN_RANGE):
            if _port_open(address, port, ifindex):
                continue  # used by another instance
            conf = self.dir / "sshd_config"
            conf.write_text("\n".join([
                f"Port {port}",
                f"ListenAddress {address}%{ifname}",
                f"HostKey {hk_path}",
                f"AuthorizedKeysFile {self.dir / 'authorized_keys'}",
                f"PidFile {self.dir / 'sshd.pid'}",
                "PubkeyAuthentication yes",
                "PasswordAuthentication no",
                "KbdInteractiveAuthentication no",
                "UsePAM no",
                "StrictModes no",
                "PermitRootLogin no",
                f"AllowUsers {config.local_user()}",
                "Subsystem sftp internal-sftp",
                "ForceCommand internal-sftp",
                "AllowTcpForwarding no",
                "AllowAgentForwarding no",
                "AllowStreamLocalForwarding no",
                "X11Forwarding no",
                "PermitTTY no",
                "PermitTunnel no",
                "ClientAliveInterval 5",
                "ClientAliveCountMax 3",
                "LogLevel VERBOSE",
                "",
            ]))
            self.proc = subprocess.Popen([SSHD, "-D", "-e", "-f", str(conf)], stdout=log, stderr=log,
                                         stdin=subprocess.DEVNULL)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and self.proc.poll() is None:
                if _port_open(address, port, ifindex):
                    self.port = port
                    log.close()
                    return port
                time.sleep(0.1)
            self.stop()
        log.close()
        raise ShareError(f"Could not start the file server, see {self.dir / 'sshd.log'}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


class DirectFileServer:
    """Unencrypted SFTP for point-to-point USB-C links, gated by peer address and session token."""

    TOKEN_BYTES = 32

    def __init__(self, session_dir: Path):
        self.dir = session_dir
        self.port = 0
        self.token = ""
        self._sock: socket.socket | None = None
        self._procs: list[subprocess.Popen] = []
        self._stopped = threading.Event()

    def start(self, address: str, ifindex: int, peer_address: str) -> int:
        server = sftp_server()
        if server is None:
            raise ShareError("sftp-server not found (part of OpenSSH)")
        self.token = os.urandom(self.TOKEN_BYTES).hex()
        for port in range(config.SSH_PORT, config.SSH_PORT + config.PORT_SCAN_RANGE):
            sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            try:
                sock.bind((address, port, 0, ifindex))
            except OSError:
                sock.close()
                continue
            sock.listen(8)
            sock.settimeout(0.5)
            self._sock, self.port = sock, port
            threading.Thread(target=self._serve, args=(server, peer_address), name="direct-sftp",
                             daemon=True).start()
            return port
        raise ShareError("No free port for the file server")

    def _serve(self, server: str, peer_address: str) -> None:
        while not self._stopped.is_set():
            try:
                conn, addr = self._sock.accept()  # type: ignore[union-attr]
            except TimeoutError:
                continue
            except OSError:
                return
            if addr[0].split("%")[0].lower() != peer_address.lower():
                conn.close()
                continue
            threading.Thread(target=self._handle, args=(conn, server), daemon=True).start()

    def _handle(self, conn: socket.socket, server: str) -> None:
        expected = self.token.encode() + b"\n"
        try:
            conn.settimeout(5)
            received = b""
            while len(received) < len(expected):  # the token may arrive in several segments
                chunk = conn.recv(len(expected) - len(received))
                if not chunk:
                    return
                received += chunk
            if not hmac.compare_digest(received, expected) or self._stopped.is_set():
                return
            conn.settimeout(None)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._procs.append(subprocess.Popen([server], stdin=conn, stdout=conn,
                                                stderr=subprocess.DEVNULL, close_fds=True))
        except OSError:
            pass
        finally:
            conn.close()  # the child keeps its own copy of the socket

    def stop(self) -> None:
        self._stopped.set()
        if self._sock:
            self._sock.close()
        for proc in self._procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in self._procs:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._procs.clear()


def _safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return name or "peer"


def is_mounted(path: Path) -> bool:
    target = str(path)
    try:
        with open("/proc/self/mounts") as f:
            return any(line.split()[1] == target for line in f)
    except OSError:
        return False


def unmount(path: Path) -> None:
    for args in (["-u"], ["-uz"]):  # lazy unmount if a program still has files open
        if not is_mounted(path):
            break
        subprocess.run(["fusermount3", *args, str(path)], capture_output=True)


def cleanup_stale_mounts() -> None:
    """Unmount leftovers of a crashed run (dead sshfs leaves "Transport endpoint is not connected")."""
    base = config.mount_base()
    if not base.is_dir():
        return
    for entry in base.iterdir():
        unmount(entry)
        try:
            entry.rmdir()
        except OSError:
            pass


class PeerMount:
    def __init__(self, session_dir: Path, peer_host: str):
        self.dir = session_dir
        self.mountpoint = self._pick_mountpoint(peer_host)
        self.proc: subprocess.Popen | None = None

    @staticmethod
    def _pick_mountpoint(host: str) -> Path:
        base = config.mount_base()
        base.mkdir(parents=True, exist_ok=True)
        name = _safe_name(host)
        for i in range(1, 100):
            candidate = base / (name if i == 1 else f"{name}-{i}")
            if is_mounted(candidate):
                continue
            if candidate.exists() and (not candidate.is_dir() or any(candidate.iterdir())):
                continue
            return candidate
        raise ShareError("No free mount point")

    def mount(self, user: str, address: str, ifname: str, port: int, peer_host_key: str,
              identity: Path) -> None:
        if not _valid_pubkey(peer_host_key):
            raise ShareError("Peer sent an invalid host key")
        if not re.fullmatch(r"[A-Za-z0-9._][A-Za-z0-9._-]*\$?", user):
            raise ShareError("Peer sent an invalid user name")
        alias = f"{config.APP_ID}-peer"
        known_hosts = self.dir / "known_hosts"
        known_hosts.write_text(f"{alias} {peer_host_key}\n")
        self.mountpoint.mkdir(parents=True, exist_ok=True)
        opts = ",".join([
            f"IdentityFile={identity}", "IdentitiesOnly=yes", "BatchMode=yes",
            f"UserKnownHostsFile={known_hosts}", "GlobalKnownHostsFile=/dev/null",
            f"HostKeyAlias={alias}", "StrictHostKeyChecking=yes",
            "ConnectTimeout=10", "ServerAliveInterval=5", "ServerAliveCountMax=3", "Ciphers=" + SSH_CIPHERS.replace(",", "\\,"),  # FUSE splits -o on unescaped commas
            "idmap=user", "max_conns=4", f"fsname={config.APP_ID}:{_safe_name(user)}@{alias}",
        ])
        result = subprocess.run(
            ["sshfs", "-p", str(port), "-F", "/dev/null", "-o", opts,
             f"{user}@[{address}%{ifname}]:/", str(self.mountpoint)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0 or not is_mounted(self.mountpoint):
            self._remove_dir()
            raise ShareError("Mounting the other computer failed: " + (result.stderr.strip() or "unknown error"))

    def mount_direct(self, address: str, ifindex: int, port: int, token: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", token or ""):
            raise ShareError("Peer sent an invalid session token")
        self.mountpoint.mkdir(parents=True, exist_ok=True)
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            sock.settimeout(10)
            sock.connect((address, port, 0, ifindex))
            sock.settimeout(None)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.sendall(token.encode() + b"\n")
            # sshfs talks SFTP over its stdin/stdout, which is this socket: no relay, no copies.
            self.proc = subprocess.Popen(
                ["sshfs", "-f", "-o", f"passive,idmap=user,fsname={config.APP_ID}:direct", ":/",
                 str(self.mountpoint)],
                stdin=sock, stdout=sock, stderr=subprocess.PIPE, close_fds=True,
            )
        except OSError as exc:
            self._remove_dir()
            raise ShareError(f"Connecting to the other computer failed: {exc}") from exc
        finally:
            sock.close()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and self.proc.poll() is None:
            if is_mounted(self.mountpoint):
                return
            time.sleep(0.1)
        error = ""
        if self.proc.poll() is not None and self.proc.stderr:
            error = self.proc.stderr.read().decode(errors="replace").strip()
        self.unmount()
        raise ShareError("Mounting the other computer failed: " + (error or "timeout"))

    def unmount(self) -> None:
        unmount(self.mountpoint)
        if self.proc:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None
        self._remove_dir()

    def _remove_dir(self) -> None:
        try:
            self.mountpoint.rmdir()
        except OSError:
            pass


# --- file manager sidebar entries -------------------------------------------------------

GTK_BOOKMARKS = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "gtk-3.0" / "bookmarks"


def add_bookmark(path: Path, label: str) -> None:
    """Show the share in the sidebar of GTK file managers (Nautilus, Nemo, Thunar, Caja, ...)."""
    if os.environ.get("DIRECTSHARE_HOME"):
        return  # test instance, leave the user's bookmarks alone
    line = f"{path.as_uri()} {label}"
    try:
        GTK_BOOKMARKS.parent.mkdir(parents=True, exist_ok=True)
        lines = GTK_BOOKMARKS.read_text().splitlines() if GTK_BOOKMARKS.exists() else []
        if not any(l.split(" ")[0] == path.as_uri() for l in lines):
            GTK_BOOKMARKS.write_text("\n".join(lines + [line]) + "\n")
    except OSError:
        pass


def remove_bookmark(path: Path) -> None:
    try:
        if not GTK_BOOKMARKS.exists():
            return
        lines = GTK_BOOKMARKS.read_text().splitlines()
        kept = [l for l in lines if l.split(" ")[0] != path.as_uri()]
        if kept != lines:
            GTK_BOOKMARKS.write_text("\n".join(kept) + ("\n" if kept else ""))
    except OSError:
        pass
