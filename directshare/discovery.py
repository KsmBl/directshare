"""Finds other DirectShare instances on the cable via IPv6 link-local multicast beacons.

Link-local addresses exist as soon as the cable is plugged in, without DHCP or any
manual configuration, which is what makes the plug-and-click workflow possible.
"""

from __future__ import annotations

import json
import select
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import config
from .links import Link, list_links


@dataclass(frozen=True)
class Peer:
    id: str
    host: str
    user: str
    address: str  # IPv6 link-local, without scope
    ifname: str
    ifindex: int
    control_port: int

    @property
    def sockaddr(self) -> tuple:
        return (self.address, self.control_port, 0, self.ifindex)

    @property
    def title(self) -> str:
        return f"{self.user}@{self.host}"


class Discovery:
    def __init__(self, identity: dict, on_links: Callable[[list[Link]], None],
                 on_peers: Callable[[list[Peer]], None]):
        self.identity = identity  # id, host, user, control_port
        self.on_links = on_links
        self.on_peers = on_peers
        self._links: list[Link] = []
        self._joined: set[int] = set()
        self._peers: dict[str, tuple[Peer, float]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock = self._open_socket()

    @staticmethod
    def _open_socket() -> socket.socket:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_LOOP, 0)
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 1)
        sock.bind(("::", config.DISCOVERY_PORT))
        return sock

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="discovery", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._sock.close()

    def links(self) -> list[Link]:
        return list(self._links)

    def peers(self) -> list[Peer]:
        with self._lock:
            return [peer for peer, _ in self._peers.values()]

    def peer(self, peer_id: str) -> Peer | None:
        with self._lock:
            entry = self._peers.get(peer_id)
        return entry[0] if entry else None

    def refresh_now(self) -> None:
        self._next_beacon = 0.0

    def _run(self) -> None:
        self._next_beacon = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= self._next_beacon:
                self._next_beacon = now + config.BEACON_INTERVAL
                self._update_links()
                self._send_beacons()
                self._expire_peers(now)
            readable, _, _ = select.select([self._sock], [], [], 0.5)
            if readable:
                self._receive()

    def _update_links(self) -> None:
        links = list_links()
        for link in links:
            if link.carrier and link.ifindex not in self._joined:
                mreq = socket.inet_pton(socket.AF_INET6, config.DISCOVERY_GROUP) + struct.pack("@I", link.ifindex)
                try:
                    self._sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_JOIN_GROUP, mreq)
                    self._joined.add(link.ifindex)
                except OSError:
                    pass  # retried on the next tick
        # Membership is dropped by the kernel when an interface goes down; re-join later.
        self._joined &= {link.ifindex for link in links if link.carrier}
        if links != self._links:
            self._links = links
            self.on_links(list(links))

    def _send_beacons(self) -> None:
        payload = json.dumps({"app": config.APP_ID, "v": config.PROTOCOL_VERSION, **self.identity}).encode()
        for link in self._links:
            if not link.usable:
                continue
            try:
                self._sock.sendto(payload, (config.DISCOVERY_GROUP, config.DISCOVERY_PORT, 0, link.ifindex))
            except OSError:
                pass

    def _receive(self) -> None:
        try:
            data, addr = self._sock.recvfrom(4096)
        except OSError:
            return
        try:
            msg = json.loads(data)
            if msg.get("app") != config.APP_ID or msg.get("id") == self.identity["id"]:
                return
            address, _, _, ifindex = addr
            address = address.split("%")[0]
            link = next((l for l in self._links if l.ifindex == ifindex), None)
            if link is None or not address.startswith("fe80:"):
                return
            peer = Peer(str(msg["id"]), str(msg["host"])[:64], str(msg["user"])[:64], address,
                        link.name, ifindex, int(msg["control_port"]))
        except (ValueError, KeyError, TypeError):
            return
        with self._lock:
            known = self._peers.get(peer.id)
            self._peers[peer.id] = (peer, time.monotonic())
        if known is None or known[0] != peer:
            self.on_peers(self.peers())

    def _expire_peers(self, now: float) -> None:
        with self._lock:
            stale = [pid for pid, (_, seen) in self._peers.items() if now - seen > config.PEER_TIMEOUT]
            for pid in stale:
                del self._peers[pid]
        if stale:
            self.on_peers(self.peers())
