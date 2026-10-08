"""Detection and preparation of point-to-point links (the cable between the two computers).

Everything above this module only needs a network interface with an IPv6 link-local
address. Ethernet is the only link type wired up today; other cable types (USB-C via
USB4/Thunderbolt networking or USB gadget networking) can be added as another
LinkProvider without touching discovery, the control protocol or the file sharing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config

SYS_NET = Path("/sys/class/net")
ARPHRD_ETHER = "1"


@dataclass(frozen=True)
class Link:
    name: str
    ifindex: int
    kind: str  # "ethernet" (later also e.g. "usb-c")
    carrier: bool
    address: str | None  # IPv6 link-local address without scope, None if not (yet) usable

    @property
    def usable(self) -> bool:
        return self.carrier and self.address is not None

    @property
    def label(self) -> str:
        return {"ethernet": "Ethernet", "usb-c": "USB-C"}.get(self.kind, self.kind)


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _make_link(name: str, kind: str) -> Link | None:
    """State of one interface, read via iproute2 (unlike sysfs, that follows the network namespace)."""
    try:
        out = subprocess.run(["ip", "-j", "addr", "show", "dev", name],
                             capture_output=True, text=True, timeout=3).stdout
        entry = (json.loads(out or "[]") or [None])[0]
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not entry:
        return None
    carrier = "LOWER_UP" in entry.get("flags", []) and "UP" in entry.get("flags", [])
    address = None
    for info in entry.get("addr_info", []):
        # A tentative address is still doing duplicate address detection and can't be bound yet.
        if (info.get("family") == "inet6" and info.get("scope") == "link"
                and not info.get("tentative") and not info.get("dadfailed")):
            address = info.get("local")
            break
    return Link(name, int(entry["ifindex"]), kind, carrier, address if carrier else None)


class LinkProvider:
    kind = ""

    def candidates(self) -> list[str]:
        raise NotImplementedError

    def links(self) -> list[Link]:
        return [link for name in self.candidates() if (link := _make_link(name, self.kind))]


class EthernetLinkProvider(LinkProvider):
    """Physical wired Ethernet ports (no Wi-Fi, bridges, bonds, VPNs or containers)."""

    kind = "ethernet"

    def candidates(self) -> list[str]:
        names = []
        for dev in sorted(SYS_NET.iterdir()) if SYS_NET.exists() else []:
            if _read(dev / "type") != ARPHRD_ETHER:
                continue
            if not (dev / "device").exists():  # virtual interface
                continue
            if (dev / "wireless").exists() or (dev / "phy80211").exists():
                continue
            if (dev / "bridge").exists() or (dev / "bonding").exists():
                continue
            if dev.name.startswith("thunderbolt"):
                continue  # USB4/Thunderbolt networking belongs to a future USB-C provider
            names.append(dev.name)
        return names


class OverrideLinkProvider(LinkProvider):
    """DIRECTSHARE_IFACES=if1,if2 forces specific interfaces (e.g. veth pairs in tests)."""

    kind = "ethernet"

    def __init__(self, names: list[str]):
        self.names = names

    def candidates(self) -> list[str]:
        return self.names


def providers() -> list[LinkProvider]:
    override = os.environ.get("DIRECTSHARE_IFACES")
    if override:
        return [OverrideLinkProvider([n.strip() for n in override.split(",") if n.strip()])]
    # Future: append a UsbCLinkProvider here (thunderbolt-net / cdc_ncm interfaces).
    return [EthernetLinkProvider()]


def list_links() -> list[Link]:
    return [link for provider in providers() for link in provider.links()]


def can_prepare() -> bool:
    return shutil.which("nmcli") is not None


def prepare_link(name: str) -> tuple[bool, str]:
    """Activate a private NetworkManager profile that uses link-local addressing only.

    A normal "Wired connection" waits for DHCP, gives up after a while and then drops the
    interface's addresses. This profile never waits for a DHCP server, which is what a
    direct cable needs. It's restricted to the current user, so no admin password is
    needed on a typical desktop.
    """
    if not can_prepare():
        return False, f"NetworkManager not found. Bring the interface up manually: ip link set {name} up"
    profile = f"{config.APP_NAME} {name}"
    existing = subprocess.run(["nmcli", "-g", "NAME", "connection", "show"],
                              capture_output=True, text=True).stdout.splitlines()
    if profile not in existing:
        add = subprocess.run(
            ["nmcli", "connection", "add", "type", "ethernet", "ifname", name, "con-name", profile,
             "ipv4.method", "link-local", "ipv6.method", "link-local",
             "connection.autoconnect", "no", "connection.permissions", f"user:{config.local_user()}"],
            capture_output=True, text=True,
        )
        if add.returncode != 0:
            return False, add.stderr.strip() or "nmcli connection add failed"
    up = subprocess.run(["nmcli", "connection", "up", profile], capture_output=True, text=True, timeout=60)
    if up.returncode != 0:
        return False, up.stderr.strip() or "nmcli connection up failed"
    return True, f"Link on {name} is ready"
