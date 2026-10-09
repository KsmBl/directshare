#!/usr/bin/env bash
# Finds out why Wi-Fi drops when an Ethernet cable is plugged in.
#
#   tools/diagnose-wifi-drop.sh
#
# Checks the usual suspects, then asks you to plug in the cable and records what happens.
# Read-only: it changes nothing. Some checks read logs and may ask for sudo.
set -uo pipefail

bold() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
hit() { printf '\033[31m[FOUND]\033[0m %s\n' "$*"; FOUND=1; }
FOUND=0

bold "System"
. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME"
uname -r
command -v nmcli >/dev/null && nmcli --version
systemctl is-active --quiet iwd && echo "iwd is running"
systemctl is-active --quiet connman && echo "connman is running"
systemctl is-active --quiet tlp && echo "tlp is running"

bold "1. TLP radio switching"
tlp_conf=$(grep -hs '^[[:space:]]*DEVICES_TO_DISABLE_ON_LAN_CONNECT' /etc/tlp.conf /etc/tlp.d/*.conf 2>/dev/null | tail -1)
if [ -n "$tlp_conf" ]; then
    echo "$tlp_conf"
    if echo "$tlp_conf" | grep -q 'wifi'; then
        hit "TLP turns Wi-Fi off when a LAN cable is connected."
        echo "      Fix: set DEVICES_TO_DISABLE_ON_LAN_CONNECT=\"\" in /etc/tlp.conf, then: sudo tlp start"
    fi
else
    echo "not configured"
fi
if [ -f /etc/NetworkManager/dispatcher.d/99tlp-rdw-nm ] || ls /usr/lib/NetworkManager/dispatcher.d/ 2>/dev/null | grep -q tlp; then
    echo "TLP radio device wizard dispatcher is installed"
fi

bold "2. ConnMan"
if grep -hs '^[[:space:]]*SingleConnectedTechnology[[:space:]]*=[[:space:]]*true' /etc/connman/main.conf; then
    hit "ConnMan only allows one connected technology at a time."
    echo "      Fix: set SingleConnectedTechnology=false in /etc/connman/main.conf, restart connman"
else
    echo "not configured"
fi

bold "3. NetworkManager dispatcher scripts touching Wi-Fi"
for dir in /etc/NetworkManager/dispatcher.d /usr/lib/NetworkManager/dispatcher.d; do
    [ -d "$dir" ] || continue
    for f in "$dir"/* "$dir"/*/*; do
        [ -f "$f" ] || continue
        if grep -qiE 'radio wifi off|rfkill block|wifi off|nmcli r(adio)? w' "$f" 2>/dev/null; then
            hit "$f switches Wi-Fi off:"
            grep -niE 'radio wifi off|rfkill block|wifi off|nmcli r(adio)? w' "$f" | head -5 | sed 's/^/      /'
        fi
    done
done
echo "done"

bold "4. Wired profiles that would take over the default route"
if command -v nmcli >/dev/null; then
    nmcli -t -f NAME,TYPE connection show | while IFS=: read -r name type; do
        [ "$type" = "802-3-ethernet" ] || continue
        printf '%-32s ipv4=%s never-default=%s metric=%s autoconnect=%s\n' "$name" \
            "$(nmcli -g ipv4.method connection show "$name")" \
            "$(nmcli -g ipv4.never-default connection show "$name")" \
            "$(nmcli -g ipv4.route-metric connection show "$name")" \
            "$(nmcli -g connection.autoconnect connection show "$name")"
    done
fi

bold "Before plugging in"
rfkill list 2>/dev/null | grep -A2 -i wlan
command -v nmcli >/dev/null && nmcli -t -f DEVICE,TYPE,STATE device
ip -4 route show default

echo
read -r -p ">>> Now plug in the Ethernet cable (other end connected to the other computer), wait until Wi-Fi drops, then press Enter. "
since="$(date -d '-90 seconds' '+%Y-%m-%d %H:%M:%S')"

bold "After plugging in"
rfkill list 2>/dev/null | grep -A2 -i wlan
if rfkill list 2>/dev/null | grep -A2 -i wlan | grep -q 'Soft blocked: yes'; then
    hit "Wi-Fi is soft-blocked (switched off by software: TLP, a script or a desktop setting)."
fi
if rfkill list 2>/dev/null | grep -A2 -i wlan | grep -q 'Hard blocked: yes'; then
    hit "Wi-Fi is hard-blocked: the BIOS/firmware turned it off. Look for 'LAN/WLAN switching' or 'Wireless radio control' in the BIOS setup and disable it."
fi
command -v nmcli >/dev/null && nmcli -t -f DEVICE,TYPE,STATE device
ip -4 route show default
default_dev="$(ip -4 route show default | head -1 | sed -n 's/.* dev \([^ ]*\).*/\1/p')"
if [ -n "$default_dev" ] && [ ! -d "/sys/class/net/$default_dev/wireless" ]; then
    hit "The default route now goes over $default_dev, so internet traffic tries to use the cable."
    echo "      Fix: nmcli connection modify '<wired profile>' ipv4.never-default yes ipv6.never-default yes"
    echo "      (or use 'Set up link' in DirectShare, which creates a profile that never takes the default route)"
fi

bold "Log around the plug-in"
journal() { journalctl --no-pager -o short-monotonic --since "$since" "$@" 2>/dev/null; }
logs="$(journal -u NetworkManager -u wpa_supplicant -u iwd -u tlp -u connman; journal -k | grep -iE 'wlan|wlp|iwl|ath|rtw|mt7|brcm|rfkill|link (is )?(up|down)')"
if [ -z "$logs" ]; then
    echo "(no access to the journal; run again with sudo for log output)"
else
    echo "$logs" | grep -iE 'wifi|wlan|wlp|wireless|rfkill|radio|disconnect|deauth|tlp|state change|link' | tail -40
    if echo "$logs" | grep -qiE 'tlp.*(disable|wifi)|radio.*wifi.*off|wifi.*disabled'; then
        hit "The log shows software turning Wi-Fi off (see lines above)."
    fi
    if echo "$logs" | grep -qiE 'deauth|reason=3'; then
        echo "note: the access point or the driver deauthenticated (see lines above)"
    fi
fi

bold "Result"
if [ "$FOUND" = 0 ]; then
    echo "No known cause found. Please share the full output of this script."
else
    echo "See the [FOUND] lines above."
fi
