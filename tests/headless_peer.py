"""Headless DirectShare instance for end-to-end tests (see tests/e2e_netns.sh).

--role initiator: requests a share with the first peer it finds, checks it can read and
                  write the peer's file system, then stops sharing.
--role responder: accepts the request, checks the reverse direction, waits for the end.
Exit code 0 means every check passed.
"""

import argparse
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from directshare import engine as eng  # noqa: E402


class Listener:
    def __init__(self, name, role, accept=True):
        self.name, self.role, self.accept = name, role, accept
        self.engine = None
        self.peers = []
        self.connected = threading.Event()
        self.peer_mounted = threading.Event()
        self.ended = threading.Event()
        self.info = None

    def log(self, *args):
        print(f"[{self.name}]", *args, flush=True)

    def links_changed(self, links):
        self.log("links:", [(l.name, l.carrier, l.address) for l in links])

    def peers_changed(self, peers):
        self.peers = peers
        self.log("peers:", [p.title for p in peers])

    def incoming_request(self, request):
        self.log("incoming request from", request.peer_title, "code", request.code)
        self.engine.answer_request(request.id, self.accept)

    def request_cancelled(self, request_id):
        self.log("request cancelled")

    def session_changed(self, info):
        self.log("session:", info.state, info.mountpoint, "peer_mounted" if info.peer_mounted else "", info.message)
        self.info = info
        if info.state == eng.CONNECTED:
            self.connected.set()
        if info.peer_mounted:
            self.peer_mounted.set()
        if info.state == eng.ENDED:
            self.ended.set()

    def notice(self, text):
        self.log("notice:", text)


def check_mount(listener, marker_dir: Path, peer_marker: str, expect_mode: str) -> bool:
    mnt = listener.info.mountpoint
    ok = True
    if listener.info.mode == expect_mode:
        listener.log(f"OK transport is {expect_mode}")
    else:
        listener.log(f"FAIL transport is {listener.info.mode}, expected {expect_mode}")
        ok = False
    remote_marker = mnt / str(marker_dir).lstrip("/") / peer_marker
    deadline = time.time() + 10
    while not remote_marker.exists() and time.time() < deadline:
        time.sleep(0.3)
    if remote_marker.exists() and remote_marker.read_text().strip() == peer_marker:
        listener.log("OK read peer file", remote_marker)
    else:
        listener.log("FAIL could not read peer marker", remote_marker)
        ok = False
    written = mnt / str(marker_dir).lstrip("/") / f"written-by-{listener.name}"
    try:
        written.write_text("hello")
        listener.log("OK wrote", written)
    except OSError as exc:
        listener.log("FAIL write", exc)
        ok = False
    try:
        list((mnt / "root").iterdir())
        listener.log("FAIL /root should not be readable for a normal user")
        ok = False
    except PermissionError:
        listener.log("OK /root is denied (same permissions as the remote user)")
    except OSError as exc:
        listener.log("note: /root check:", exc)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", choices=["initiator", "responder"], required=True)
    ap.add_argument("--marker-dir", required=True, help="dir shared by the test; markers live here")
    ap.add_argument("--decline", action="store_true")
    ap.add_argument("--hold", action="store_true", help="initiator: don't stop, wait for the link to drop")
    ap.add_argument("--expect-mode", choices=["ssh", "direct"], default="ssh")
    args = ap.parse_args()
    name = args.role
    peer_name = "responder" if name == "initiator" else "initiator"
    marker_dir = Path(args.marker_dir)
    (marker_dir / f"marker-{name}").write_text(f"marker-{name}\n")

    listener = Listener(name, args.role, accept=not args.decline)
    engine = eng.Engine(listener)
    listener.engine = engine
    engine.start()
    ok = True
    try:
        if args.role == "initiator":
            deadline = time.time() + 20
            while not listener.peers and time.time() < deadline:
                time.sleep(0.2)
            if not listener.peers:
                listener.log("FAIL no peer discovered")
                return 1
            engine.request_share(listener.peers[0].id)
            if args.decline:
                listener.ended.wait(30)
                ok = listener.info is not None and "declined" in listener.info.message
                listener.log("OK declined" if ok else "FAIL expected decline")
                return 0 if ok else 1
            if not listener.connected.wait(40):
                listener.log("FAIL not connected")
                return 1
            ok &= check_mount(listener, marker_dir, f"marker-{peer_name}", args.expect_mode)
            listener.peer_mounted.wait(20)
            time.sleep(4)  # give the responder time to run its checks
            mountpoint = listener.info.mountpoint
            if args.hold:
                listener.log("holding the share until the cable is pulled")
                if not listener.ended.wait(60):
                    listener.log("FAIL session did not end after the cable was pulled")
                    ok = False
                time.sleep(0.5)
            else:
                engine.stop_share(listener.peers[0].id)
                listener.ended.wait(15)
            if mountpoint and os.path.ismount(mountpoint):
                listener.log("FAIL still mounted after stop")
                ok = False
            else:
                listener.log("OK unmounted after stop")
        else:
            if args.decline:
                time.sleep(15)
                return 0
            if not listener.connected.wait(60):
                listener.log("FAIL not connected")
                return 1
            ok &= check_mount(listener, marker_dir, f"marker-{peer_name}", args.expect_mode)
            mountpoint = listener.info.mountpoint
            if not listener.ended.wait(40):
                listener.log("FAIL session did not end")
                ok = False
            time.sleep(0.5)
            if mountpoint and os.path.ismount(mountpoint):
                listener.log("FAIL still mounted after peer stopped")
                ok = False
            else:
                listener.log("OK unmounted after peer stopped")
    finally:
        engine.shutdown()
    listener.log("RESULT", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
