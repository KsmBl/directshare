"""Session handling: discovery, the share request/accept handshake and the lifetime of a share.

Control protocol: one TCP connection per session over the cable, newline-delimited JSON.

    initiator                                responder
    request {host,user,pubkey,hostkey,code} ->
                                             (user is asked)
                                          <- accept {user,pubkey,hostkey,ssh_port} | decline
    (starts its server)
    ready {ssh_port}                       ->
    both mount each other; mounted {}     <->
    bye {}                                <->   (or the connection drops)

The share lives exactly as long as the control connection. When it ends - "Stop sharing",
the app quits, the cable is pulled - both sides unmount and stop their servers.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import secrets
import shutil
import socket
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import config, sharing
from .discovery import Discovery, Peer
from .links import Link


class Listener(Protocol):
    def links_changed(self, links: list[Link]) -> None: ...
    def peers_changed(self, peers: list[Peer]) -> None: ...
    def incoming_request(self, request: "IncomingRequest") -> None: ...
    def request_cancelled(self, request_id: str) -> None: ...
    def session_changed(self, session: "SessionInfo") -> None: ...
    def notice(self, text: str) -> None: ...


# Session states
WAITING = "waiting"          # we asked, the other side hasn't answered yet
CONNECTING = "connecting"    # accepted, servers starting / mounting
CONNECTED = "connected"      # our mount of the peer is up
ENDED = "ended"


@dataclass
class SessionInfo:
    peer_id: str
    peer_title: str
    peer_host: str
    state: str
    code: str = ""
    mountpoint: Path | None = None
    peer_mounted: bool = False  # the other side has mounted us
    message: str = ""


@dataclass
class IncomingRequest:
    id: str
    peer_id: str
    peer_host: str
    peer_user: str
    ifname: str
    code: str

    @property
    def peer_title(self) -> str:
        return f"{self.peer_user}@{self.peer_host}"


def verification_code(pubkey: str, nonce: str) -> str:
    digest = int.from_bytes(hashlib.sha256(f"{pubkey}|{nonce}".encode()).digest()[:4], "big")
    code = f"{digest % 1_000_000:06d}"
    return f"{code[:3]} {code[3:]}"


class Channel:
    """Line-based JSON messages over a TCP socket."""

    MAX_LINE = 64 * 1024

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._buf = b""
        self._send_lock = threading.Lock()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        # Notice a pulled cable within ~15 seconds instead of hours.
        for opt, val in (("TCP_KEEPIDLE", 5), ("TCP_KEEPINTVL", 2), ("TCP_KEEPCNT", 5)):
            if hasattr(socket, opt):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)

    def send(self, msg: dict) -> None:
        data = (json.dumps(msg) + "\n").encode()
        with self._send_lock:
            self.sock.sendall(data)

    def recv(self, timeout: float | None = None) -> dict | None:
        """Next message; None on EOF. Raises TimeoutError/OSError."""
        self.sock.settimeout(timeout)
        while b"\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                return None
            self._buf += chunk
            if len(self._buf) > self.MAX_LINE:
                raise OSError("message too long")
        line, self._buf = self._buf.split(b"\n", 1)
        try:
            msg = json.loads(line)
        except ValueError as exc:
            raise OSError("malformed message") from exc
        if not isinstance(msg, dict):
            raise OSError("malformed message")
        return msg

    def peer_closed(self) -> bool:
        """Non-blocking check whether the other side hung up (used while the user decides)."""
        try:
            self.sock.settimeout(0)
            data = self.sock.recv(1, socket.MSG_PEEK)
            return data == b""
        except (BlockingIOError, InterruptedError):
            return False
        except OSError:
            return True

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


@dataclass
class _Session:
    info: SessionInfo
    peer: Peer
    dir: Path
    channel: Channel | None = None
    server: sharing.FileServer | None = None
    mount: sharing.PeerMount | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    ended: bool = False


class Engine:
    def __init__(self, listener: Listener):
        self.listener = listener
        self.id = uuid.uuid4().hex
        self.host = config.local_host()
        self.user = config.local_user()
        self._sessions: dict[str, _Session] = {}
        self._pending: dict[str, tuple[IncomingRequest, threading.Event, list[bool]]] = {}
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._links: list[Link] = []
        self._server_sock: socket.socket | None = None
        self.control_port = 0
        self.discovery: Discovery | None = None

    # --- lifecycle -------------------------------------------------------------------------

    def start(self) -> None:
        lock_path = config.ensure_private_dir(config.data_dir()) / "instance.lock"
        self._lock_file = open(lock_path, "w")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_file.close()
            raise sharing.ShareError(f"{config.APP_NAME} is already running")
        sharing.cleanup_stale_mounts()
        shutil.rmtree(config.runtime_dir(), ignore_errors=True)
        config.ensure_private_dir(config.runtime_dir())
        sharing.host_key()
        self._server_sock = self._listen()
        threading.Thread(target=self._accept_loop, name="control-accept", daemon=True).start()
        identity = {"id": self.id, "host": self.host, "user": self.user, "control_port": self.control_port}
        self.discovery = Discovery(identity, self._on_links, self.listener.peers_changed)
        self.discovery.start()

    def shutdown(self) -> None:
        self._stopping.set()
        with self._lock:
            sessions = list(self._sessions.values())
            pending = list(self._pending.values())
        for _, event, answer in pending:
            answer.append(False)
            event.set()
        for session in sessions:
            self._end(session, "This computer closed the share", notify_peer=True)
        if self.discovery:
            self.discovery.stop()
        if self._server_sock:
            self._server_sock.close()
        shutil.rmtree(config.runtime_dir(), ignore_errors=True)

    def _listen(self) -> socket.socket:
        for port in range(config.CONTROL_PORT, config.CONTROL_PORT + config.PORT_SCAN_RANGE):
            sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("::", port))
            except OSError:
                sock.close()
                continue
            sock.listen(4)
            self.control_port = port
            return sock
        raise sharing.ShareError("No free control port")

    def _on_links(self, links: list[Link]) -> None:
        self._links = links
        self.listener.links_changed(links)
        # A pulled cable ends the share right away instead of waiting for TCP keepalive.
        usable = {link.ifindex for link in links if link.carrier}
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            if session.peer.ifindex not in usable:
                self._end(session, "The cable was disconnected", notify_peer=False)

    def _local_address(self, ifindex: int) -> str:
        link = next((l for l in self._links if l.ifindex == ifindex and l.usable), None)
        if link is None:
            raise sharing.ShareError("The cable is not connected")
        return link.address  # type: ignore[return-value]

    def _new_session(self, peer: Peer, state: str, code: str) -> _Session:
        sdir = config.ensure_private_dir(config.runtime_dir() / f"session-{secrets.token_hex(4)}")
        info = SessionInfo(peer.id, peer.title, peer.host, state, code=code)
        session = _Session(info, peer, sdir)
        with self._lock:
            self._sessions[peer.id] = session
        self._publish(session)
        return session

    def _publish(self, session: _Session) -> None:
        self.listener.session_changed(SessionInfo(**vars(session.info)))

    def session(self, peer_id: str) -> SessionInfo | None:
        with self._lock:
            s = self._sessions.get(peer_id)
        return SessionInfo(**vars(s.info)) if s else None

    # --- outgoing ----------------------------------------------------------------------------

    def request_share(self, peer_id: str) -> None:
        peer = self.discovery.peer(peer_id) if self.discovery else None
        if peer is None:
            self.listener.notice("That computer is no longer reachable")
            return
        with self._lock:
            if peer_id in self._sessions:
                return
        threading.Thread(target=self._initiate, args=(peer,), name="session-out", daemon=True).start()

    def _initiate(self, peer: Peer) -> None:
        nonce = secrets.token_hex(16)
        session = self._new_session(peer, WAITING, "")
        try:
            pubkey = sharing.generate_key(session.dir / "id_ed25519", f"{self.user}@{self.host}")
            session.info.code = verification_code(pubkey, nonce)
            self._publish(session)
            session.channel = Channel(self._connect(peer))
            if session.ended:  # cancelled while connecting
                session.channel.close()
                return
            session.channel.send({"t": "request", "v": config.PROTOCOL_VERSION, "id": self.id,
                                  "host": self.host, "user": self.user, "pubkey": pubkey,
                                  "hostkey": sharing.host_key()[1], "nonce": nonce})
            reply = session.channel.recv(timeout=config.REQUEST_TIMEOUT + 10)
            if reply is None or reply.get("t") == "decline":
                reason = "declined" if reply else "did not answer"
                self._end(session, f"{peer.title} {reason}", notify_peer=False)
                return
            if reply.get("t") != "accept":
                raise sharing.ShareError("Unexpected answer from the other computer")
            self._set_state(session, CONNECTING, "Starting file server…")
            session.server = sharing.FileServer(session.dir)
            port = session.server.start(self._local_address(peer.ifindex), peer.ifname, peer.ifindex,
                                        str(reply.get("pubkey", "")))
            if session.ended:
                session.server.stop()
                return
            session.channel.send({"t": "ready", "ssh_port": port})
            self._mount_peer(session, reply)
            self._serve(session)
        except TimeoutError:
            self._end(session, f"{peer.title} did not answer", notify_peer=True)
        except (OSError, sharing.ShareError, ValueError, TypeError) as exc:
            self._end(session, self._describe(exc), notify_peer=True)

    def _connect(self, peer: Peer) -> socket.socket:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.settimeout(5)
        try:
            sock.connect(peer.sockaddr)
        except OSError:
            sock.close()
            raise
        return sock

    def cancel(self, peer_id: str) -> None:
        self.stop_share(peer_id)

    def stop_share(self, peer_id: str) -> None:
        with self._lock:
            session = self._sessions.get(peer_id)
        if session:
            threading.Thread(target=self._end, args=(session, "Sharing stopped", True), daemon=True).start()

    # --- incoming ----------------------------------------------------------------------------

    def _accept_loop(self) -> None:
        assert self._server_sock is not None
        while not self._stopping.is_set():
            try:
                sock, addr = self._server_sock.accept()
            except OSError:
                return
            address, _, _, ifindex = addr
            # Only talk to computers on the cable, never to the rest of the network.
            link = next((l for l in self._links if l.ifindex == ifindex), None)
            if link is None or not address.lower().startswith("fe80:"):
                sock.close()
                continue
            threading.Thread(target=self._respond, args=(sock, address.split("%")[0], link),
                             name="session-in", daemon=True).start()

    def _respond(self, sock: socket.socket, address: str, link: Link) -> None:
        channel = Channel(sock)
        try:
            msg = channel.recv(timeout=10)
            if not msg or msg.get("t") != "request":
                channel.close()
                return
            peer_id = str(msg["id"])
            known = self.discovery.peer(peer_id) if self.discovery else None
            peer = Peer(peer_id, str(msg["host"])[:64], str(msg["user"])[:64], address, link.name,
                        link.ifindex, known.control_port if known else 0)
            with self._lock:
                busy = peer_id in self._sessions
            if busy:
                channel.send({"t": "decline", "reason": "busy"})
                channel.close()
                return
            request = IncomingRequest(secrets.token_hex(8), peer.id, peer.host, peer.user, link.name,
                                      verification_code(str(msg["pubkey"]), str(msg["nonce"])))
            if not self._ask_user(request, channel):
                try:
                    channel.send({"t": "decline"})
                except OSError:
                    pass
                channel.close()
                return
        except (OSError, KeyError, ValueError, TypeError):
            channel.close()
            return

        session = self._new_session(peer, CONNECTING, request.code)
        session.channel = channel
        try:
            session.info.message = "Starting file server…"
            self._publish(session)
            session.server = sharing.FileServer(session.dir)
            port = session.server.start(self._local_address(link.ifindex), link.name, link.ifindex,
                                        str(msg["pubkey"]))
            if session.ended:
                session.server.stop()
                return
            pubkey = sharing.generate_key(session.dir / "id_ed25519", f"{self.user}@{self.host}")
            channel.send({"t": "accept", "user": self.user, "pubkey": pubkey,
                          "hostkey": sharing.host_key()[1], "ssh_port": port})
            ready = channel.recv(timeout=30)
            if not ready or ready.get("t") != "ready":
                raise sharing.ShareError(f"{peer.title} cancelled")
            self._mount_peer(session, {"user": peer.user, "hostkey": msg["hostkey"],
                                       "ssh_port": ready["ssh_port"]})
            self._serve(session)
        except TimeoutError:
            self._end(session, f"{peer.title} stopped responding", notify_peer=True)
        except (OSError, sharing.ShareError, KeyError, ValueError, TypeError) as exc:
            self._end(session, self._describe(exc), notify_peer=True)

    def _ask_user(self, request: IncomingRequest, channel: Channel) -> bool:
        event, answer = threading.Event(), []
        with self._lock:
            self._pending[request.id] = (request, event, answer)
        self.listener.incoming_request(request)
        try:
            waited = 0.0
            while not event.wait(0.5):
                waited += 0.5
                if channel.peer_closed() or waited > config.REQUEST_TIMEOUT:
                    self.listener.request_cancelled(request.id)
                    return False
            return bool(answer and answer[0])
        finally:
            with self._lock:
                self._pending.pop(request.id, None)

    def answer_request(self, request_id: str, accept: bool) -> None:
        with self._lock:
            entry = self._pending.get(request_id)
        if entry:
            entry[2].append(accept)
            entry[1].set()

    # --- shared by both sides ------------------------------------------------------------------

    def _mount_peer(self, session: _Session, info: dict) -> None:
        session.info.message = "Mounting the other computer…"
        self._publish(session)
        mount = sharing.PeerMount(session.dir, session.peer.host)
        session.mount = mount
        mount.mount(str(info["user"]), session.peer.address, session.peer.ifname, int(info["ssh_port"]),
                    str(info["hostkey"]), session.dir / "id_ed25519")
        if session.ended:  # stopped while mounting
            mount.unmount()
            return
        sharing.add_bookmark(mount.mountpoint, f"{session.peer.host} ({config.APP_NAME})")
        session.info.mountpoint = mount.mountpoint
        self._set_state(session, CONNECTED, "")
        assert session.channel is not None
        session.channel.send({"t": "mounted"})

    def _serve(self, session: _Session) -> None:
        assert session.channel is not None
        while not session.ended:
            try:
                msg = session.channel.recv(timeout=None)
            except OSError:
                msg = None
            if session.ended:
                return
            if msg is None:
                self._end(session, f"Connection to {session.peer.title} lost", notify_peer=False)
                return
            if msg.get("t") == "bye":
                self._end(session, f"{session.peer.title} stopped sharing", notify_peer=False)
                return
            if msg.get("t") == "mounted":
                session.info.peer_mounted = True
                self._publish(session)

    def _set_state(self, session: _Session, state: str, message: str) -> None:
        session.info.state = state
        session.info.message = message
        self._publish(session)

    def _end(self, session: _Session, message: str, notify_peer: bool) -> None:
        with session.lock:
            if session.ended:
                return
            session.ended = True
        if session.channel:
            if notify_peer:
                try:
                    session.channel.send({"t": "bye"})
                except OSError:
                    pass
            session.channel.close()
        if session.mount:
            sharing.remove_bookmark(session.mount.mountpoint)
            session.mount.unmount()
        if session.server:
            session.server.stop()
        shutil.rmtree(session.dir, ignore_errors=True)
        with self._lock:
            if self._sessions.get(session.peer.id) is session:
                del self._sessions[session.peer.id]
        session.info.state = ENDED
        session.info.mountpoint = None
        session.info.message = message
        self._publish(session)

    @staticmethod
    def _describe(exc: Exception) -> str:
        if isinstance(exc, sharing.ShareError):
            return str(exc)
        if isinstance(exc, ConnectionRefusedError):
            return "The other computer refused the connection"
        return f"Sharing failed: {exc}"
