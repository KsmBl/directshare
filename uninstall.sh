#!/usr/bin/env bash
# Remove DirectShare.
#
#   ./uninstall.sh            remove the per-user installation (~/.local)
#   ./uninstall.sh --system   remove the system-wide installation (/usr/local, uses sudo)
#   ./uninstall.sh --purge    also remove settings: host key, network profiles, bookmarks
#
# Installed dependencies (OpenSSH, sshfs, PyQt6) are left alone.
set -euo pipefail

MODE=user
PURGE=0
for arg in "$@"; do
    case "$arg" in
        --system) MODE=system ;;
        --purge) PURGE=1 ;;
        -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

if [ "$MODE" = system ]; then PREFIX=/usr/local; SUDO=sudo; else PREFIX="$HOME/.local"; SUDO=; fi
say() { printf '\033[1m==>\033[0m %s\n' "$*"; }

if pgrep -u "$(id -u)" -f 'python3 -m directshare' >/dev/null; then
    say "Stopping running DirectShare"
    pkill -u "$(id -u)" -f 'python3 -m directshare' || true
    sleep 2
fi

# Unmount anything a crashed instance may have left behind.
if [ -d "$HOME/DirectShare" ]; then
    for d in "$HOME/DirectShare"/*/; do
        [ -d "$d" ] && fusermount3 -uz "$d" 2>/dev/null || true
        rmdir "$d" 2>/dev/null || true
    done
    rmdir "$HOME/DirectShare" 2>/dev/null || true
fi

say "Removing files from $PREFIX"
$SUDO rm -rf "$PREFIX/lib/directshare"
$SUDO rm -f "$PREFIX/bin/directshare" \
    "$PREFIX/share/applications/directshare.desktop" \
    "$PREFIX/share/icons/hicolor/scalable/apps/directshare.svg" \
    "$PREFIX/share/icons/hicolor/256x256/apps/directshare.png"
command -v update-desktop-database >/dev/null && $SUDO update-desktop-database -q "$PREFIX/share/applications" 2>/dev/null || true

if [ "$PURGE" = 1 ]; then
    say "Removing settings"
    rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/directshare"
    bookmarks="${XDG_CONFIG_HOME:-$HOME/.config}/gtk-3.0/bookmarks"
    if [ -f "$bookmarks" ]; then
        grep -v "file://$HOME/DirectShare/" "$bookmarks" > "$bookmarks.tmp" || true
        mv "$bookmarks.tmp" "$bookmarks"
    fi
    if command -v nmcli >/dev/null; then
        nmcli -g NAME connection show 2>/dev/null | grep '^DirectShare ' | while read -r name; do
            nmcli connection delete "$name" >/dev/null && echo "Removed network profile '$name'"
        done
    fi
fi

say "DirectShare has been removed"
