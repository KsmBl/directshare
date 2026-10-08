"""Render the README screenshots with a fake engine (no network, no display needed).

    QT_QPA_PLATFORM=offscreen python3 tools/screenshots.py [--dark]
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PyQt6.QtGui import QColor, QFont, QIcon, QPalette  # noqa: E402
from PyQt6.QtWidgets import QApplication, QStyleFactory  # noqa: E402

from directshare import gui  # noqa: E402
from directshare.discovery import Peer  # noqa: E402
from directshare.engine import CONNECTED, CONNECTING, WAITING, IncomingRequest, SessionInfo  # noqa: E402
from directshare.links import Link  # noqa: E402

OUT = ROOT / "docs" / "screenshots"


class FakeEngine:
    host, user = "workstation", "alice"

    def __getattr__(self, name):
        return lambda *a, **k: None


def palette(dark: bool) -> QPalette:
    p = QPalette()
    colors = {
        QPalette.ColorRole.Window: "#1f2125" if dark else "#f4f5f7",
        QPalette.ColorRole.WindowText: "#e8e9ec" if dark else "#1d1f23",
        QPalette.ColorRole.Base: "#2a2d33" if dark else "#ffffff",
        QPalette.ColorRole.Text: "#e8e9ec" if dark else "#1d1f23",
        QPalette.ColorRole.Button: "#32353c" if dark else "#ffffff",
        QPalette.ColorRole.ButtonText: "#e8e9ec" if dark else "#1d1f23",
        QPalette.ColorRole.Highlight: "#3b82f6" if dark else "#2563eb",
        QPalette.ColorRole.HighlightedText: "#ffffff",
    }
    for role, value in colors.items():
        p.setColor(role, QColor(value))
    return p


def shoot(widget, name):
    QApplication.processEvents()
    if isinstance(widget, gui.MainWindow):
        widget.resize(540, 520)
    else:
        widget.adjustSize()
    QApplication.processEvents()
    path = OUT / f"{name}.png"
    widget.grab().save(str(path))
    print("wrote", path)


def main():
    dark = "--dark" in sys.argv
    suffix = "-dark" if dark else ""
    OUT.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)
    app.setStyle(QStyleFactory.create("Fusion"))
    app.setPalette(palette(dark))
    app.setFont(QFont("Adwaita Sans", 10))
    QIcon.setThemeName("Adwaita")
    app.setStyleSheet(gui.stylesheet(app.palette()))

    bridge = gui.Bridge()
    window = gui.MainWindow(FakeEngine(), bridge)
    window.statusBar().hide()
    link = Link("enp0s31f6", 2, "ethernet", True, "fe80::1")
    peer = Peer("p1", "laptop", "bob", "fe80::2", "enp0s31f6", 2, 47821)

    window.on_links([link])
    window.on_peers([])
    window.show()
    window.resize(540, 520)
    shoot(window, "01-searching" + suffix)

    window.on_peers([peer])
    shoot(window, "02-peer-found" + suffix)

    window.on_session(SessionInfo("p1", peer.title, "laptop", WAITING, code="482 917"))
    shoot(window, "03-waiting" + suffix)

    window.on_session(SessionInfo("p1", peer.title, "laptop", CONNECTING, code="482 917",
                                  message="Mounting the other computer…"))
    window.on_session(SessionInfo("p1", peer.title, "laptop", CONNECTED, code="482 917",
                                  mountpoint=Path.home() / "DirectShare" / "laptop", peer_mounted=True))
    shoot(window, "05-connected" + suffix)

    request = IncomingRequest("r1", "p0", "workstation", "alice", "enp3s0", "482 917")
    dialog = gui.RequestDialog(window, request, "bob")
    dialog.show()
    shoot(dialog, "04-request" + suffix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
