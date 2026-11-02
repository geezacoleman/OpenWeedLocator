#!/bin/bash
# OWL hotspot / power / boot diagnostic. Read-only: it changes nothing.
#
# Each section tests one assumption made while chasing the "phone sees the
# OWL hotspot but can't join" bug (Pi 5, 2026-10-28). Part 1 is a snapshot;
# part 2 follows the logs live while you try to join from the phone, so the
# AP's side of every attempt is recorded.
#
# Run as the normal user (it uses sudo where needed):
#   bash owl_wifi_diag.sh              # snapshot, then 120 s live watch
#   WATCH=300 bash owl_wifi_diag.sh    # watch longer
#   WATCH=0 bash owl_wifi_diag.sh      # snapshot only
#
# Everything is also written to ~/owl_diag_<time>.txt. Hotspot and Wi-Fi
# passwords are masked.

set -u

WATCH=${WATCH:-120}
HOTSPOT=${HOTSPOT:-$(nmcli -t -f NAME,TYPE con show 2>/dev/null | awk -F: '$2=="802-11-wireless"{print $1}' | while read -r c; do [ "$(nmcli -g 802-11-wireless.mode con show "$c" 2>/dev/null)" = ap ] && echo "$c" && break; done)}
BOOT_CONFIG=/boot/firmware/config.txt
[ -f "$BOOT_CONFIG" ] || BOOT_CONFIG=/boot/config.txt
CMDLINE=/boot/firmware/cmdline.txt
[ -f "$CMDLINE" ] || CMDLINE=/boot/cmdline.txt
OUT="$HOME/owl_diag_$(date +%Y%m%d_%H%M%S).txt"

section() { echo; echo "=================== $* ==================="; }
run() { echo "\$ $*"; "$@" 2>&1; echo; }

sudo -v || exit 1

{
echo "OWL diagnostic $(date)  uptime $(cut -d. -f1 /proc/uptime)s  hotspot profile: ${HOTSPOT:-NOT FOUND}"

# --- Assumption: power is fine -------------------------------------------------
# get_throttled bits: 0x1 under-voltage now, 0x10000 under-voltage since boot,
# 0x4/0x40000 throttled. usb_max_current_enable=0 means the PSU did not
# negotiate 5 A (USB limited to 600 mA).
section "POWER"
run vcgencmd get_throttled
run vcgencmd pmic_read_adc
run vcgencmd get_config usb_max_current_enable
run vcgencmd measure_temp
run sudo rpi-eeprom-config
run vcgencmd bootloader_version
dmesg | grep -iE "voltage|brownout|power supply|pd |usb-pd" | tail -n 20

# --- Assumption: the boot config is stock (no antenna/display/camera edits) ----
section "BOOT CONFIG"
run cat "$BOOT_CONFIG"
run cat "$CMDLINE"
run cat /proc/cmdline
run cat /proc/device-tree/model
echo
run vcgencmd version
run uname -a

# --- Assumption: the hotspot settings we changed actually stuck ----------------
section "HOTSPOT PROFILE"
if [ -n "${HOTSPOT:-}" ]; then
    run nmcli -f connection.autoconnect,connection.autoconnect-priority,802-11-wireless.mode,802-11-wireless.band,802-11-wireless.channel,802-11-wireless.powersave,802-11-wireless.hidden,802-11-wireless-security.key-mgmt,802-11-wireless-security.proto,802-11-wireless-security.pairwise,802-11-wireless-security.group,802-11-wireless-security.pmf,ipv4.method,ipv4.addresses con show "$HOTSPOT"
    f=$(sudo grep -l "^ssid=$HOTSPOT$" /etc/NetworkManager/system-connections/* 2>/dev/null | head -1)
    [ -n "$f" ] && { echo "\$ cat $f (passwords masked)"; sudo sed -E 's/^(psk|password)=.*/\1=<hidden>/' "$f"; echo; }
    echo "Hotspot password length: $(sudo nmcli -s -g 802-11-wireless-security.psk con show "$HOTSPOT" 2>/dev/null | tr -d '\n' | wc -c)"
fi

# --- Assumption: no other profile competes with the hotspot --------------------
section "ALL CONNECTION PROFILES"
run nmcli -f NAME,TYPE,AUTOCONNECT,AUTOCONNECT-PRIORITY,ACTIVE,DEVICE,TIMESTAMP-REAL con show
for f in /etc/NetworkManager/NetworkManager.conf /etc/NetworkManager/conf.d/* /usr/lib/NetworkManager/conf.d/*; do
    [ -f "$f" ] && { echo "--- $f"; cat "$f"; }
done

# --- Assumption: the radio is really running the AP the way NM says -----------
section "RADIO"
run iw dev wlan0 info
run iw dev wlan0 get power_save
run iw reg get
run rfkill list
run iw dev wlan0 station dump
run iw dev wlan0 survey dump
echo "\$ iw phy phy0 info (modes, ciphers, commands)"
iw phy phy0 info 2>&1 | sed -n '/Supported Ciphers/,/Available Antennas/p;/Supported interface modes/,/Band /p' | head -n 60
echo

# --- Assumption: the Wi-Fi firmware is the normal one -------------------------
# Raspberry Pi OS ships more than one CYW43455 firmware build behind
# update-alternatives; AP behaviour differs between them.
section "WIFI FIRMWARE"
dmesg | grep -iE "brcmfmac.*(Firmware|version)" | tail -n 3
for alt in cyfmac43455-sdio.bin cyfmac43455-sdio.clm_blob brcmfmac43455-sdio.bin; do
    update-alternatives --display "$alt" 2>/dev/null
done
ls -l /lib/firmware/cypress/ 2>/dev/null | grep -i 43455
ls -l /lib/firmware/brcm/ 2>/dev/null | grep -i 43455
dpkg -l 2>/dev/null | grep -E "firmware-brcm80211|raspi-firmware|linux-image-rpi|network-manager |wpasupplicant" | awk '{print $2, $3}'

# --- Assumption: what wpa_supplicant was told for the AP (this boot) ----------
section "AP CONFIG + EVENTS THIS BOOT"
journalctl -b -q --no-pager -o short-monotonic | grep -E "Config: added '(frequency|key_mgmt|proto|pairwise|ieee80211w|ht40)'|AP-ENABLED|AP-DISABLED|AP-STA-|PSK-MISMATCH|SAE|EAPOL|auth|deauth|brcmf_cyw|timedout|DHCP(DISCOVER|OFFER|REQUEST|ACK|NAK)" | tail -n 80

# --- Assumption: once joined, the phone gets an address and the app can reach us
section "FIREWALL + SERVICES"
run sudo ufw status verbose
run systemctl is-active NetworkManager wpa_supplicant owl owl-dash nginx mosquitto avahi-daemon
run sudo ss -tulpn
echo "\$ curl https://10.42.0.1/ (from the Pi itself)"
curl -sk -o /dev/null -w "HTTP %{http_code} in %{time_total}s\n" --max-time 10 https://10.42.0.1/ 2>&1
echo "\$ dashboard certificate dates"
openssl x509 -in /etc/ssl/certs/owl.crt -noout -dates 2>&1
run timedatectl

# --- Assumption: nothing on USB is a 2.4 GHz noise source ----------------------
# USB 3 devices and wireless keyboard/mouse dongles both sit near the Pi's
# Wi-Fi antenna and both are known 2.4 GHz noise sources.
section "USB + DISPLAY + DESKTOP"
run lsusb
run lsusb -t
for s in /sys/class/drm/card*-*/status; do echo "$s: $(cat "$s")"; done
echo
run loginctl list-sessions --no-legend
echo "\$ who holds the camera"
sudo fuser -v /dev/media* /dev/video* 2>&1 | head -n 20
echo

# --- Assumption: the SD card and USB stick are healthy -------------------------
section "STORAGE"
run df -h / /boot/firmware /media/"$USER"/*
dmesg | grep -iE "mmc|exfat|fat-fs|ext4-fs error|i/o error" | tail -n 20

section "BOOT HISTORY"
run journalctl --list-boots --no-pager
} 2>&1 | tee "$OUT"

if [ "$WATCH" -gt 0 ]; then
    {
    section "LIVE WATCH for ${WATCH}s: TRY TO JOIN FROM THE PHONE NOW"
    echo "(Each phone attempt should show AP-STA / SAE / EAPOL / DHCP lines below.)"
    sudo timeout "$WATCH" journalctl -f -q --no-pager -o short-monotonic -n 0 \
        | grep --line-buffered -iE "wpa_supplicant|dnsmasq|brcmf|wlan0|AP-STA|SAE|EAPOL|PSK|auth|nginx"
    # Is the AP still up after the failed joins, or did it vanish?
    section "AFTER WATCH: IS THE AP STILL UP"
    iw dev wlan0 info 2>&1
    iw dev wlan0 station dump 2>&1
    nmcli -f DEVICE,TYPE,STATE,CONNECTION dev 2>&1
    sudo dmesg | tail -n 15
    } 2>&1 | tee -a "$OUT"
fi

echo
echo "Saved: $OUT"
