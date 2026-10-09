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

# Prints "soft=<0|1> hard=<0|1>" for every Wi-Fi radio.
wifi_rfkill() {
    for r in /sys/class/rfkill/rfkill*; do
        [ "$(cat "$r/type" 2>/dev/null)" = wlan ] || continue
        echo "$(cat "$r/name"): soft=$(cat "$r/soft") hard=$(cat "$r/hard")"
    done
}

bios_hint() {
    local vendor="$1"
    case "$vendor" in
        *Dell*)
            local attr=/sys/class/firmware-attributes/dell-wmi-sysman/attributes/WlanAutoSense
            if [ -d "$attr" ]; then
                echo "      Dell 'Control WLAN radio' (WlanAutoSense) can be switched off from Linux:"
                echo "        echo Disabled | sudo tee $attr/current_value"
                echo "      then reboot (the BIOS applies it at the next start). If a BIOS admin password is set, first:"
                echo "        echo -n '<password>' | sudo tee /sys/class/firmware-attributes/dell-wmi-sysman/authentication/Admin/current_password"
            else
                echo "      Dell: BIOS Setup (F2) > Connection / Power Management > 'Wireless Radio Control' -> untick 'Control WLAN radio'"
            fi ;;
        *HP*|*Hewlett*) echo "      HP: BIOS Setup > Advanced > Built-In Device Options > 'LAN/WLAN Auto Switching' -> disable" ;;
        *LENOVO*|*Lenovo*) echo "      Lenovo: BIOS Setup > Config > Network > 'Wireless Auto Disconnection' (ThinkPad) or 'LAN/WLAN switching' -> disable" ;;
        *Fujitsu*|*FUJITSU*) echo "      Fujitsu: BIOS Setup > Advanced > 'LAN/WLAN switching' -> disable" ;;
        *)         echo "      Look in the BIOS/UEFI setup for 'LAN/WLAN switching', 'Wireless radio control' or 'Wireless auto disconnection' and disable it." ;;
    esac
}

bold "System"
VENDOR="$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null)"
echo "Hardware: $VENDOR $(cat /sys/class/dmi/id/product_name 2>/dev/null) $(cat /sys/class/dmi/id/product_version 2>/dev/null)"
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

bold "5. Firmware LAN/WLAN switching"
attr=/sys/class/firmware-attributes/dell-wmi-sysman/attributes/WlanAutoSense
if [ -d "$attr" ]; then
    value="$(cat "$attr/current_value" 2>/dev/null || sudo -n cat "$attr/current_value" 2>/dev/null)"
    echo "Dell 'Control WLAN radio' (WlanAutoSense): ${value:-unknown, needs root to read}"
    if [ "$value" = Enabled ]; then
        hit "The BIOS switches Wi-Fi off whenever the built-in Ethernet port has a link."
        bios_hint "$VENDOR"
    fi
else
    echo "no switchable firmware option found (check the BIOS setup manually if Wi-Fi gets hard-blocked)"
fi

bold "Before plugging in"
wifi_rfkill
command -v nmcli >/dev/null && nmcli -t -f DEVICE,TYPE,STATE device
ip -4 route show default

echo
read -r -p ">>> Now plug in the Ethernet cable (other end connected to the other computer), wait until Wi-Fi drops, then press Enter. "
since="$(date -d '-90 seconds' '+%Y-%m-%d %H:%M:%S')"

bold "After plugging in"
after="$(wifi_rfkill)"
echo "$after"
if echo "$after" | grep -q 'hard=1'; then
    hit "Wi-Fi is HARD-blocked: the BIOS/firmware switched the radio off because a LAN cable was connected."
    echo "      Linux can't undo a hard block. Disable the option in the BIOS setup:"
    bios_hint "$VENDOR"
    echo "      Workaround without BIOS changes: use a USB Ethernet adapter for the direct cable."
    echo "      The firmware only watches the built-in Ethernet port."
elif echo "$after" | grep -q 'soft=1'; then
    hit "Wi-Fi is soft-blocked (switched off by software: TLP, a script or a desktop setting)."
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
    if echo "$logs" | grep -qiE 'RF_KILL bit toggled|hard blocked'; then
        if ! echo "$after" | grep -q 'hard=1'; then  # not already reported above
            hit "The log shows the firmware hard-blocking Wi-Fi (RF_KILL) right after the cable came up."
            bios_hint "$VENDOR"
            echo "      Workaround without BIOS changes: use a USB Ethernet adapter for the direct cable."
        fi
    elif echo "$logs" | grep -qiE 'tlp.*(disable|wifi)|soft blocked|rfkill.*block'; then
        hit "The log shows software turning Wi-Fi off (see lines above)."
    fi
    if echo "$logs" | grep -qiE 'deauth|reason=3' && ! echo "$logs" | grep -qiE 'RF_KILL|blocked'; then
        echo "note: the access point or the driver deauthenticated (see lines above)"
    fi
fi

bold "Result"
if [ "$FOUND" = 0 ]; then
    echo "No known cause found. Please share the full output of this script."
else
    echo "See the [FOUND] lines above."
fi
