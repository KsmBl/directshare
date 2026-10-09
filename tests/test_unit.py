"""Unit tests: fast-mode server access control and cable detection.

    python3 -m unittest discover -s tests
"""

import os
import shutil
import socket
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from directshare import links, sharing  # noqa: E402

SFTP_INIT = struct.pack(">IBI", 5, 1, 3)  # length, SSH_FXP_INIT, version 3


def sftp_handshake(port: int, token: str | None) -> bool:
    """True if an SFTP server answers on the port after sending the token."""
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(("::1", port, 0, 0))
        if token is not None:
            s.sendall(token.encode() + b"\n")
        s.sendall(SFTP_INIT)
        try:
            head = s.recv(5, socket.MSG_WAITALL)
        except (ConnectionResetError, TimeoutError):
            return False
        return len(head) == 5 and head[4] == 2  # SSH_FXP_VERSION


@unittest.skipUnless(sharing.sftp_server(), "sftp-server not installed")
class DirectServerTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def start(self, peer_address: str) -> sharing.DirectFileServer:
        server = sharing.DirectFileServer(self.dir)
        server.start("::1", 0, peer_address)
        self.addCleanup(server.stop)
        return server

    def test_right_token_gets_sftp(self):
        server = self.start("::1")
        self.assertTrue(sftp_handshake(server.port, server.token))

    def test_wrong_token_is_rejected(self):
        server = self.start("::1")
        self.assertFalse(sftp_handshake(server.port, "0" * 64))

    def test_missing_token_is_rejected(self):
        server = self.start("::1")
        self.assertFalse(sftp_handshake(server.port, None))

    def test_other_address_is_rejected(self):
        server = self.start("fe80::1")  # only the peer may connect; we come from ::1
        self.assertFalse(sftp_handshake(server.port, server.token))

    def test_token_split_across_segments(self):
        server = self.start("::1")
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(5)
            s.connect(("::1", server.port, 0, 0))
            data = server.token.encode() + b"\n"
            s.sendall(data[:10])
            time.sleep(0.2)
            s.sendall(data[10:] + SFTP_INIT)
            head = s.recv(5, socket.MSG_WAITALL)
        self.assertEqual(head[4:5], b"\x02")

    def test_tokens_are_unique_per_session(self):
        self.assertNotEqual(self.start("::1").token, self.start("::1").token)


class LinkDetectionTest(unittest.TestCase):
    """Fake /sys trees for the providers."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.net = self.root / "class/net"
        self.tb = self.root / "bus/thunderbolt/devices"
        self.net.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root, True)

    def add_iface(self, name, driver="e1000e", wireless=False, physical=True, iftype="1"):
        dev = self.net / name
        dev.mkdir()
        (dev / "type").write_text(iftype + "\n")
        if physical:
            hw = self.root / "devices" / name
            (hw / "driver").parent.mkdir(parents=True, exist_ok=True)
            drv = self.root / "drivers" / driver
            drv.mkdir(parents=True, exist_ok=True)
            os.symlink(drv, hw / "driver")
            os.symlink(hw, dev / "device")
        if wireless:
            (dev / "wireless").mkdir()

    def test_ethernet_excludes_wifi_virtual_and_thunderbolt(self):
        self.add_iface("enp3s0")
        self.add_iface("enx001122", driver="r8152")  # USB Ethernet adapter counts as Ethernet
        self.add_iface("wlp2s0", driver="iwlwifi", wireless=True)
        self.add_iface("veth0", physical=False)
        self.add_iface("thunderbolt0", driver="thunderbolt-net")
        self.add_iface("tbt-renamed", driver="thunderbolt-net")
        self.assertEqual(links.EthernetLinkProvider(self.net).candidates(), ["enp3s0", "enx001122"])

    def test_usbc_finds_thunderbolt_net_by_driver(self):
        self.add_iface("enp3s0")
        self.add_iface("thunderbolt0", driver="thunderbolt-net")
        self.add_iface("tbt-renamed", driver="thunderbolt-net")
        provider = links.UsbCLinkProvider(self.net, self.tb)
        self.assertEqual(provider.candidates(), ["tbt-renamed", "thunderbolt0"])

    def test_usbc_placeholder_only_with_controller(self):
        provider = links.UsbCLinkProvider(self.net, self.tb)
        self.assertEqual(provider.links(), [])  # no USB4/Thunderbolt controller
        (self.tb / "domain0").mkdir(parents=True)
        placeholder = provider.links()
        self.assertEqual(len(placeholder), 1)
        self.assertEqual(placeholder[0].kind, "usb-c")
        self.assertFalse(placeholder[0].carrier)
        self.assertTrue(placeholder[0].note)

    def test_override_kinds(self):
        provider = links.OverrideLinkProvider("veth0, veth1:usb-c")
        self.assertEqual(provider.kinds, {"veth0": "ethernet", "veth1": "usb-c"})


if __name__ == "__main__":
    unittest.main()
