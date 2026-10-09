"""Qt user interface."""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QIcon, QPalette
from PyQt6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QProgressBar,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from . import __version__, config, links as links_mod, sharing
from .engine import CONNECTED, CONNECTING, ENDED, WAITING, IncomingRequest, SessionInfo

ASSETS = Path(__file__).resolve().parent / "assets"
SETUP_HINT_DELAY = 6.0  # seconds a cable may sit without an address before we offer to fix it


def app_icon() -> QIcon:
    icon = QIcon()
    for name in ("directshare.svg", "directshare.png"):  # PNG for systems without Qt SVG support
        if (ASSETS / name).exists():
            icon.addFile(str(ASSETS / name))
    return icon if not icon.isNull() else QIcon.fromTheme("folder-remote")


class Bridge(QObject):
    """Receives engine callbacks on worker threads and re-emits them on the GUI thread."""

    linksChanged = pyqtSignal(object)
    peersChanged = pyqtSignal(object)
    incomingRequestSig = pyqtSignal(object)
    requestCancelledSig = pyqtSignal(str)
    sessionChangedSig = pyqtSignal(object)
    noticeSig = pyqtSignal(str)
    prepareDone = pyqtSignal(bool, str)

    def links_changed(self, links):
        self.linksChanged.emit(links)

    def peers_changed(self, peers):
        self.peersChanged.emit(peers)

    def incoming_request(self, request):
        self.incomingRequestSig.emit(request)

    def request_cancelled(self, request_id):
        self.requestCancelledSig.emit(request_id)

    def session_changed(self, session):
        self.sessionChangedSig.emit(session)

    def notice(self, text):
        self.noticeSig.emit(text)


def stylesheet(palette: QPalette) -> str:
    text, window = palette.color(QPalette.ColorRole.WindowText), palette.color(QPalette.ColorRole.Window)
    dark = window.lightness() < 128

    def mix(a: QColor, b: QColor, t: float) -> str:
        return QColor(int(a.red() * (1 - t) + b.red() * t), int(a.green() * (1 - t) + b.green() * t),
                      int(a.blue() * (1 - t) + b.blue() * t)).name()

    muted = mix(text, window, 0.45)
    card = mix(window, QColor("white" if not dark else "#2a2d33"), 0.65 if not dark else 0.5)
    border = mix(text, window, 0.85)
    accent = palette.color(QPalette.ColorRole.Highlight).name()
    good = "#2e9d5b" if not dark else "#5fcf8a"
    return f"""
    QLabel#title {{ font-size: 20pt; font-weight: 700; }}
    QLabel#muted {{ color: {muted}; }}
    QLabel#section {{ color: {muted}; font-size: 8.5pt; font-weight: 700; letter-spacing: 1px; margin-top: 10px; }}
    QLabel#good {{ color: {good}; font-weight: 600; }}
    QLabel#code {{ font-family: monospace; font-size: 18pt; font-weight: 700; letter-spacing: 2px; }}
    QFrame#card QLabel#avatar {{ background: {accent}; color: white; border-radius: 20px; font-size: 14pt; font-weight: 700; }}
    QFrame#card {{ background: {card}; border: 1px solid {border}; border-radius: 12px; }}
    QFrame#card QLabel {{ background: transparent; }}
    QPushButton {{ padding: 6px 14px; border-radius: 6px; }}
    QPushButton#primary {{ background: {accent}; color: white; border: none; font-weight: 600; }}
    QPushButton#primary:hover {{ background: {mix(QColor(accent), QColor('white'), 0.12)}; }}
    QPushButton#primary:disabled {{ background: {border}; color: {muted}; }}
    QProgressBar {{ max-height: 4px; border: none; background: {border}; border-radius: 2px; }}
    QProgressBar::chunk {{ background: {accent}; border-radius: 2px; }}
    """


def _label(text: str = "", name: str | None = None, wrap: bool = False) -> QLabel:
    lbl = QLabel(text)
    if name:
        lbl.setObjectName(name)
    lbl.setWordWrap(wrap)
    return lbl


def _button(text: str, primary: bool = False, icon: str | None = None) -> QPushButton:
    btn = QPushButton(text)
    if primary:
        btn.setObjectName("primary")
    if icon:
        btn.setIcon(QIcon.fromTheme(icon))
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


def _pretty_path(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


class LinkRow(QWidget):
    def __init__(self, link: links_mod.Link, since_no_address: float | None, on_setup):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 2)
        icon = QLabel()
        theme = "thunderbolt" if link.kind == "usb-c" else "network-wired"
        icon.setPixmap(QIcon.fromTheme(theme, QIcon.fromTheme("network-wired")).pixmap(22, 22))
        row.addWidget(icon)
        row.addWidget(_label(f"<b>{link.label}</b> &nbsp;<span>{link.name}</span>"))
        if not link.carrier:
            status = _label(link.note or "No cable connected", "muted")
        elif link.address:
            status = _label("● Cable connected", "good")
        else:
            status = _label("Cable connected, waiting for address…", "muted")
        row.addStretch(1)
        row.addWidget(status)
        if link.carrier and not link.address and since_no_address is not None \
                and time.monotonic() - since_no_address > SETUP_HINT_DELAY and links_mod.can_prepare():
            btn = _button("Set up link")
            btn.setToolTip("Configure this port for a direct cable (link-local addressing, no DHCP)")
            btn.clicked.connect(lambda: on_setup(link.name, btn))
            row.addWidget(btn)


class PeerCard(QFrame):
    def __init__(self, window: "MainWindow", peer_id: str, title: str, via: str, online: bool,
                 session: SessionInfo | None):
        super().__init__()
        self.setObjectName("card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        top = QHBoxLayout()
        avatar = _label(title.split("@")[-1][:1].upper() or "?", "avatar")
        avatar.setFixedSize(40, 40)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(avatar)
        names = QVBoxLayout()
        names.setSpacing(0)
        user, _, host = title.partition("@")
        names.addWidget(_label(f"<span style='font-size:12pt;font-weight:600'>{host}</span>"))
        names.addWidget(_label(f"{user} · via {via}" + ("" if online else " · offline"), "muted"))
        top.addLayout(names, 1)
        outer.addLayout(top)

        state = session.state if session else None
        actions = QHBoxLayout()
        actions.addStretch(1)
        if state == WAITING:
            outer.addWidget(_label(f"Waiting for <b>{host}</b> to accept…", wrap=True))
            if session.code:
                code_row = QHBoxLayout()
                code_row.addWidget(_label("Verification code:", "muted"))
                code_row.addWidget(_label(session.code, "code"))
                code_row.addStretch(1)
                outer.addLayout(code_row)
                outer.addWidget(_label("The same code must be shown on the other computer.", "muted", wrap=True))
            bar = QProgressBar()
            bar.setRange(0, 0)
            bar.setTextVisible(False)
            outer.addWidget(bar)
            cancel = _button("Cancel")
            cancel.clicked.connect(lambda: window.engine.cancel(peer_id))
            actions.addWidget(cancel)
        elif state == CONNECTING:
            outer.addWidget(_label(session.message or "Connecting…", wrap=True))
            bar = QProgressBar()
            bar.setRange(0, 0)
            bar.setTextVisible(False)
            outer.addWidget(bar)
            stop = _button("Cancel")
            stop.clicked.connect(lambda: window.engine.stop_share(peer_id))
            actions.addWidget(stop)
        elif state == CONNECTED:
            status = "● Sharing"
            if session.mode == "direct":
                status += "  ·  ⚡ Fast mode (USB-C direct link)"
            outer.addWidget(_label(status, "good"))
            where = _pretty_path(session.mountpoint) if session.mountpoint else ""
            text = f"Files of <b>{host}</b> are at <b>{where}</b> and in your file manager's sidebar."
            text += (f"<br>{host} can see this computer's files too." if session.peer_mounted
                     else f"<br>{host} is still connecting to this computer…")
            outer.addWidget(_label(text, wrap=True))
            stop = _button("Stop sharing", icon="process-stop")
            stop.clicked.connect(lambda: window.engine.stop_share(peer_id))
            open_btn = _button("Open files", primary=True, icon="folder-open")
            open_btn.clicked.connect(lambda: window.open_folder(session.mountpoint))
            actions.addWidget(stop)
            actions.addWidget(open_btn)
        else:
            if session and state == ENDED and session.message:
                outer.addWidget(_label(session.message, "muted", wrap=True))
            share = _button("Share files", primary=True, icon="folder-remote")
            share.setEnabled(online)
            share.clicked.connect(lambda: window.engine.request_share(peer_id))
            actions.addWidget(share)
        outer.addLayout(actions)


class RequestDialog(QDialog):
    def __init__(self, parent: QWidget, request: IncomingRequest, local_user: str):
        super().__init__(parent)
        self.request = request
        self.setWindowTitle("Share files?")
        self.setWindowIcon(app_icon())
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)
        head = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(app_icon().pixmap(48, 48))
        head.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        title = _label(f"<span style='font-size:14pt;font-weight:700'>{request.peer_host}</span><br>"
                       f"wants to share files with you", wrap=True)
        head.addWidget(title, 1)
        layout.addLayout(head)
        layout.addWidget(_label(
            f"If you accept, both computers get access to each other's <b>entire file system</b> "
            f"over the cable on <b>{request.ifname}</b>:<br>"
            f"• you can read and write everything <b>{request.peer_user}</b> can on {request.peer_host}<br>"
            f"• {request.peer_host} can read and write everything <b>{local_user}</b> can here",
            wrap=True))
        code_row = QHBoxLayout()
        code_row.addWidget(_label("Verification code:", "muted"))
        code_row.addWidget(_label(request.code, "code"))
        code_row.addStretch(1)
        layout.addLayout(code_row)
        layout.addWidget(_label("Only accept if the other computer shows the same code.", "muted", wrap=True))
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        decline = _button("Decline")
        accept = _button("Accept", primary=True)
        decline.clicked.connect(self.reject)
        accept.clicked.connect(self.accept)
        buttons.addWidget(decline)
        buttons.addWidget(accept)
        layout.addLayout(buttons)
        decline.setFocus()  # a stray Enter key must not share the whole disk


class MainWindow(QMainWindow):
    def __init__(self, engine, bridge: Bridge):
        super().__init__()
        self.engine = engine
        self.bridge = bridge
        self.links: list[links_mod.Link] = []
        self.peers = []
        self.sessions: dict[str, SessionInfo] = {}
        self.dialogs: dict[str, RequestDialog] = {}
        self.no_address_since: dict[str, float] = {}

        self.setWindowTitle(config.APP_NAME)
        self.setWindowIcon(app_icon())
        self.resize(560, 640)

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(6)
        header = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(app_icon().pixmap(44, 44))
        header.addWidget(icon)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(_label(config.APP_NAME, "title"))
        titles.addWidget(_label(f"This computer: <b>{engine.host}</b> · signed in as <b>{engine.user}</b>", "muted"))
        header.addLayout(titles, 1)
        layout.addLayout(header)

        layout.addWidget(_label("CABLE", "section"))
        self.links_box = QVBoxLayout()
        layout.addLayout(self.links_box)

        layout.addWidget(_label("COMPUTERS ON THE CABLE", "section"))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        holder = QWidget()
        self.peers_box = QVBoxLayout(holder)
        self.peers_box.setContentsMargins(0, 4, 0, 4)
        self.peers_box.setSpacing(10)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)

        footer = _label(f"Shared computers appear in <b>{_pretty_path(config.mount_base())}</b>. "
                        f"Start {config.APP_NAME} on both computers and connect them with a cable.",
                        "muted", wrap=True)
        layout.addWidget(footer)
        self.setCentralWidget(root)
        self.statusBar().setSizeGripEnabled(False)

        bridge.linksChanged.connect(self.on_links)
        bridge.peersChanged.connect(self.on_peers)
        bridge.incomingRequestSig.connect(self.on_request)
        bridge.requestCancelledSig.connect(self.on_request_cancelled)
        bridge.sessionChangedSig.connect(self.on_session)
        bridge.noticeSig.connect(lambda text: self.statusBar().showMessage(text, 8000))
        bridge.prepareDone.connect(self.on_prepare_done)

        # Re-render periodically so the "Set up link" hint appears without a link change.
        self.tick = QTimer(self)
        self.tick.timeout.connect(self.render_links)
        self.tick.start(2000)
        self.render_links()
        self.render_peers()

    # --- engine events -----------------------------------------------------------------------

    def on_links(self, links):
        now = time.monotonic()
        for link in links:
            if link.carrier and not link.address:
                self.no_address_since.setdefault(link.name, now)
            else:
                self.no_address_since.pop(link.name, None)
        self.links = links
        self.render_links()

    def on_peers(self, peers):
        self.peers = peers
        self.render_peers()

    def on_session(self, info: SessionInfo):
        previous = self.sessions.get(info.peer_id)
        self.sessions[info.peer_id] = info
        if info.state == ENDED and info.message:
            self.statusBar().showMessage(info.message, 10000)
        if info.state == CONNECTED and (previous is None or previous.state != CONNECTED):
            self.statusBar().showMessage(f"Connected to {info.peer_host}", 8000)
        self.render_peers()

    def on_request(self, request: IncomingRequest):
        dialog = RequestDialog(self, request, self.engine.user)
        self.dialogs[request.id] = dialog
        notify(f"{request.peer_host} wants to share files",
               f"{request.peer_user}@{request.peer_host} is asking for file sharing. Code {request.code}")
        self.showNormal()
        self.raise_()
        self.activateWindow()
        QApplication.alert(self)
        dialog.finished.connect(lambda result, rid=request.id: self._answer(rid, result))
        dialog.open()

    def _answer(self, request_id: str, result: int):
        if self.dialogs.pop(request_id, None) is not None:
            self.engine.answer_request(request_id, result == QDialog.DialogCode.Accepted)

    def on_request_cancelled(self, request_id: str):
        dialog = self.dialogs.pop(request_id, None)
        if dialog:
            dialog.close()
            self.statusBar().showMessage("The other computer withdrew its request", 8000)

    # --- actions -------------------------------------------------------------------------------

    def open_folder(self, path: Path | None):
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def setup_link(self, name: str, button: QPushButton):
        button.setEnabled(False)
        button.setText("Setting up…")

        def work():
            ok, msg = links_mod.prepare_link(name)
            self.bridge.prepareDone.emit(ok, msg)
            if ok and getattr(self.engine, "discovery", None):
                self.engine.discovery.refresh_now()

        threading.Thread(target=work, daemon=True).start()

    def on_prepare_done(self, ok: bool, msg: str):
        if ok:
            self.statusBar().showMessage(msg, 8000)
        else:
            QMessageBox.warning(self, "Could not set up the link", msg)
        self.render_links()

    # --- rendering -----------------------------------------------------------------------------

    @staticmethod
    def _clear(box: QVBoxLayout):
        while box.count():
            item = box.takeAt(0)
            if widget := item.widget():
                widget.hide()  # deleteLater alone leaves it painted until the next event loop
                widget.setParent(None)
                widget.deleteLater()
            elif item.layout():
                MainWindow._clear(item.layout())

    def render_links(self):
        self._clear(self.links_box)
        if not self.links:
            self.links_box.addWidget(_label("No Ethernet or USB4/Thunderbolt port found on this computer.", "muted"))
            return
        for link in self.links:
            self.links_box.addWidget(LinkRow(link, self.no_address_since.get(link.name), self.setup_link))

    def render_peers(self):
        self._clear(self.peers_box)
        online = {p.id: p for p in self.peers}
        # Forget finished sessions of computers that have left.
        for pid in [pid for pid, s in self.sessions.items() if s.state == ENDED and pid not in online]:
            del self.sessions[pid]
        ids = list(online) + [pid for pid in self.sessions if pid not in online]
        if not ids:
            empty = QWidget()
            col = QVBoxLayout(empty)
            col.setContentsMargins(0, 30, 0, 30)
            pic = QLabel()
            pic.setPixmap(QIcon.fromTheme("network-wired").pixmap(48, 48))
            pic.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col.addWidget(pic)
            usable = any(link.usable for link in self.links)
            msg = ("Looking for another computer running DirectShare…" if usable
                   else "Connect the other computer with an Ethernet cable, or a USB-C cable "
                        "if both have USB4/Thunderbolt ports.")
            text = _label(msg, "muted", wrap=True)
            text.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col.addWidget(text)
            self.peers_box.addWidget(empty)
        for pid in ids:
            peer = online.get(pid)
            session = self.sessions.get(pid)
            title = peer.title if peer else session.peer_title
            link = next((l for l in self.links if peer and l.name == peer.ifname), None)
            via = f"{link.label} ({peer.ifname})" if link and peer else (peer.ifname if peer else "cable")
            self.peers_box.addWidget(PeerCard(self, pid, title, via, peer is not None, session))
        self.peers_box.addStretch(1)

    def closeEvent(self, event):
        active = [s for s in self.sessions.values() if s.state in (WAITING, CONNECTING, CONNECTED)]
        if active:
            answer = QMessageBox.question(
                self, "Stop sharing?",
                "Closing DirectShare stops sharing and unmounts the other computer. Quit anyway?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.statusBar().showMessage("Stopping…")
        QApplication.processEvents()
        self.engine.shutdown()
        event.accept()


def notify(title: str, body: str) -> None:
    if shutil.which("notify-send"):
        try:
            subprocess.Popen(["notify-send", "-a", config.APP_NAME, "-i", "folder-remote", "-u", "critical",
                              title, body], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


def main() -> int:
    from .engine import Engine

    QApplication.setDesktopFileName(config.APP_ID)
    app = QApplication(sys.argv)
    app.setApplicationName(config.APP_NAME)
    app.setApplicationVersion(__version__)
    app.setWindowIcon(app_icon())
    app.setStyleSheet(stylesheet(app.palette()))

    missing = sharing.missing_tools()
    if missing:
        QMessageBox.critical(None, config.APP_NAME,
                             "These programs are required but missing: " + ", ".join(missing) +
                             "\n\nInstall OpenSSH and sshfs (e.g. 'sudo pacman -S openssh sshfs' or "
                             "'sudo apt install openssh-server sshfs') and start DirectShare again.")
        return 1

    bridge = Bridge()
    engine = Engine(bridge)
    try:
        engine.start()
    except (OSError, sharing.ShareError) as exc:
        QMessageBox.critical(None, config.APP_NAME, f"Could not start: {exc}")
        return 1
    window = MainWindow(engine, bridge)
    window.show()
    return app.exec()
