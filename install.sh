#!/usr/bin/env bash
# Install DirectShare for the current user (default) or system-wide (--system).
#
#   ./install.sh            install to ~/.local
#   ./install.sh --system   install to /usr/local (uses sudo)
#   ./install.sh --no-deps  skip the dependency check/installation
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
MODE=user
DEPS=1
for arg in "$@"; do
    case "$arg" in
        --system) MODE=system ;;
        --no-deps) DEPS=0 ;;
        -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

if [ "$MODE" = system ]; then
    PREFIX=/usr/local
    SUDO=sudo
else
    PREFIX="$HOME/.local"
    SUDO=
fi
APPDIR="$PREFIX/lib/directshare"
BINDIR="$PREFIX/bin"
DESKTOPDIR="$PREFIX/share/applications"
ICONDIR="$PREFIX/share/icons/hicolor"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }

# --- dependencies -------------------------------------------------------------------------
missing_deps() {
    local missing=()
    [ -x /usr/bin/sshd ] || [ -x /usr/sbin/sshd ] || command -v sshd >/dev/null || missing+=(sshd)
    command -v sshfs >/dev/null || missing+=(sshfs)
    command -v fusermount3 >/dev/null || missing+=(fusermount3)
    command -v ip >/dev/null || missing+=(ip)
    python3 -c 'import PyQt6.QtWidgets' 2>/dev/null || missing+=(PyQt6)
    python3 -c 'import PyQt6.QtSvg' 2>/dev/null || missing+=(QtSvg)
    echo "${missing[@]:-}"
}

install_deps() {
    local pkgs cmd
    if command -v pacman >/dev/null; then
        pkgs="openssh sshfs fuse3 iproute2 python-pyqt6 qt6-svg"; cmd="sudo pacman -S --needed $pkgs"
    elif command -v apt-get >/dev/null; then
        # openssh-server provides the sshd binary; DirectShare never uses the system ssh service.
        pkgs="openssh-server sshfs fuse3 iproute2 python3-pyqt6 libqt6svg6"; cmd="sudo apt-get install -y $pkgs"
    elif command -v dnf >/dev/null; then
        pkgs="openssh-server fuse-sshfs fuse3 iproute python3-pyqt6 qt6-qtsvg"; cmd="sudo dnf install -y $pkgs"
    elif command -v zypper >/dev/null; then
        pkgs="openssh-server sshfs fuse3 iproute2 python3-PyQt6 qt6-svg"; cmd="sudo zypper install -y $pkgs"
    else
        echo "Unknown package manager. Please install: OpenSSH server (sshd), sshfs, fuse3, iproute2, PyQt6 (with Qt SVG)." >&2
        return 1
    fi
    say "Installing dependencies: $pkgs"
    read -r -p "Run '$cmd'? [Y/n] " answer
    case "${answer:-y}" in
        [Yy]*) $cmd ;;
        *) echo "Skipped. DirectShare won't start until these are installed." ;;
    esac
}

if [ "$DEPS" = 1 ]; then
    missing="$(missing_deps)"
    if [ -n "$missing" ]; then
        say "Missing: $missing"
        install_deps || true
    else
        say "All dependencies are present"
    fi
fi

# --- files ----------------------------------------------------------------------------------
say "Installing to $PREFIX"
$SUDO mkdir -p "$APPDIR" "$BINDIR" "$DESKTOPDIR" "$ICONDIR/scalable/apps" "$ICONDIR/256x256/apps"
$SUDO rm -rf "$APPDIR/directshare"
$SUDO cp -r "$SRC/directshare" "$APPDIR/"
$SUDO find "$APPDIR" -name '__pycache__' -prune -exec rm -rf {} +

$SUDO tee "$BINDIR/directshare" >/dev/null <<EOF
#!/bin/sh
PYTHONPATH="$APPDIR\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m directshare "\$@"
EOF
$SUDO chmod 755 "$BINDIR/directshare"

$SUDO cp "$SRC/directshare/assets/directshare.svg" "$ICONDIR/scalable/apps/directshare.svg"
$SUDO cp "$SRC/directshare/assets/directshare.png" "$ICONDIR/256x256/apps/directshare.png"
sed "s|^Exec=.*|Exec=$BINDIR/directshare|" "$SRC/data/directshare.desktop" | $SUDO tee "$DESKTOPDIR/directshare.desktop" >/dev/null

command -v update-desktop-database >/dev/null && $SUDO update-desktop-database -q "$DESKTOPDIR" 2>/dev/null || true
command -v gtk-update-icon-cache >/dev/null && $SUDO gtk-update-icon-cache -q -t "$ICONDIR" 2>/dev/null || true

say "Done. Start DirectShare from your application menu or run: directshare"
case ":$PATH:" in *":$BINDIR:"*) ;; *) echo "Note: $BINDIR is not in your PATH." ;; esac
