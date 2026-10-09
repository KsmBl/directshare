"""Detection and preparation of point-to-point links (the cable between the two computers).

Everything above this module only needs a network interface with an IPv6 link-local
address, so each cable type is just a LinkProvider:

- Ethernet: the built-in or USB Ethernet ports.
- USB-C: USB4 / Thunderbolt 3+ host-to-host networking. When two such computers are
  connected with a USB-C cable, the kernel's thunderbolt-net driver creates an Ethernet-like
  interface (thunderbolt0) on both sides. That is all we need.
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
SYS_THUNDERBOLT = Path("/sys/bus/thunderbolt/devices")
ARPHRD_ETHER = "1"


@dataclass(frozen=True)
class Link:
    name: str
    ifindex: int
    kind: str  # "ethernet" or "usb-c"
    carrier: bool
    address: str | None  # IPv6 link-local address without scope, None if not (yet) usable
    note: str = ""  # status for ports that have no interface yet (USB-C with nothing plugged in)

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


def _driver(dev: Path) -> str:
    try:
        return os.path.basename(os.path.realpath(dev / "device" / "driver"))
    except OSError:
        return ""


def is_thunderbolt_net(dev: Path) -> bool:
    return dev.name.startswith("thunderbolt") or _driver(dev) == "thunderbolt-net"


class LinkProvider:
    kind = ""

    def __init__(self, sys_net: Path = SYS_NET):
        self.sys_net = sys_net

    def _devices(self) -> list[Path]:
        return sorted(self.sys_net.iterdir()) if self.sys_net.exists() else []

    def candidates(self) -> list[str]:
        raise NotImplementedError

    def links(self) -> list[Link]:
        return [link for name in self.candidates() if (link := _make_link(name, self.kind))]


class EthernetLinkProvider(LinkProvider):
    """Physical wired Ethernet ports (no Wi-Fi, bridges, bonds, VPNs or containers)."""

    kind = "ethernet"

    def candidates(self) -> list[str]:
        names = []
        for dev in self._devices():
            if _read(dev / "type") != ARPHRD_ETHER:
                continue
            if not (dev / "device").exists():  # virtual interface
                continue
            if (dev / "wireless").exists() or (dev / "phy80211").exists():
                continue
            if (dev / "bridge").exists() or (dev / "bonding").exists():
                continue
            if is_thunderbolt_net(dev):
                continue  # handled by UsbCLinkProvider
            names.append(dev.name)
        return names


class UsbCLinkProvider(LinkProvider):
    """USB4 / Thunderbolt host-to-host networking over a USB-C cable."""

    kind = "usb-c"

    def __init__(self, sys_net: Path = SYS_NET, sys_thunderbolt: Path = SYS_THUNDERBOLT):
        super().__init__(sys_net)
        self.sys_thunderbolt = sys_thunderbolt

    def candidates(self) -> list[str]:
        return [dev.name for dev in self._devices() if is_thunderbolt_net(dev)]

    def has_controller(self) -> bool:
        return self.sys_thunderbolt.exists() and any(
            d.name.startswith("domain") for d in self.sys_thunderbolt.iterdir())

    def links(self) -> list[Link]:
        links = super().links()
        if not links and self.has_controller():
            # The interface only appears once another computer is connected, so show the port anyway.
            note = ("No computer connected" if driver_available()
                    else "Kernel module thunderbolt_net is missing")
            links = [Link("", -1, self.kind, False, None, note)]
        return links


_driver_available: bool | None = None


def driver_available() -> bool:
    global _driver_available
    if _driver_available is None:
        if Path("/sys/module/thunderbolt_net").exists():
            _driver_available = True
        else:
            try:
                _driver_available = subprocess.run(["modinfo", "-F", "name", "thunderbolt_net"],
                                                   capture_output=True, timeout=3).returncode == 0
            except (OSError, subprocess.SubprocessError):
                _driver_available = True  # can't tell, don't show a false alarm
    return _driver_available


class OverrideLinkProvider(LinkProvider):
    """DIRECTSHARE_IFACES=if1,if2:usb-c forces specific interfaces and kinds (e.g. veth pairs in tests)."""

    def __init__(self, spec: str):
        super().__init__()
        self.kinds = {}
        for item in filter(None, (part.strip() for part in spec.split(","))):
            name, _, kind = item.partition(":")
            self.kinds[name] = kind or "ethernet"

    def candidates(self) -> list[str]:
        return list(self.kinds)

    def links(self) -> list[Link]:
        return [link for name, kind in self.kinds.items() if (link := _make_link(name, kind))]


def providers() -> list[LinkProvider]:
    override = os.environ.get("DIRECTSHARE_IFACES")
    if override:
        return [OverrideLinkProvider(override)]
    return [EthernetLinkProvider(), UsbCLinkProvider()]


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
    # The cable must never compete with Wi-Fi: no default route, no DNS, lowest priority.
    settings = ["ipv4.method", "link-local", "ipv6.method", "link-local",
                "ipv4.never-default", "yes", "ipv6.never-default", "yes",
                "ipv4.ignore-auto-dns", "yes", "ipv6.ignore-auto-dns", "yes",
                "ipv4.route-metric", "20000", "ipv6.route-metric", "20000",
                "connection.autoconnect", "no", "connection.permissions", f"user:{config.local_user()}"]
    existing = subprocess.run(["nmcli", "-g", "NAME", "connection", "show"],
                              capture_output=True, text=True).stdout.splitlines()
    if profile in existing:
        cmd = ["nmcli", "connection", "modify", profile, *settings]  # upgrade profiles from older versions
    else:
        cmd = ["nmcli", "connection", "add", "type", "ethernet", "ifname", name, "con-name", profile, *settings]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return False, result.stderr.strip() or "nmcli failed"
    up = subprocess.run(["nmcli", "connection", "up", profile], capture_output=True, text=True, timeout=60)
    if up.returncode != 0:
        return False, up.stderr.strip() or "nmcli connection up failed"
    return True, f"Link on {name} is ready"
