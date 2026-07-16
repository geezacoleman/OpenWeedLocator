#!/bin/bash
# OWL first-boot setup — arm / disarm script
#
# Usage (run with sudo on the OWL):
#   sudo bash install_firstboot.sh --arm      # default: enter setup mode on next boot
#   sudo bash install_firstboot.sh --disarm   # remove all first-boot surface
#
# Arming:
#   - installs + enables owl-firstboot.service (ConditionPathExists gated)
#   - touches the flag on the FAT boot partition (re-armable from any PC,
#     same idea as Raspberry Pi's `ssh` flag file)
#   - resets the hotspot password to the fixed setup password the phone
#     app knows ("owl-setup")
#   - opens the setup API port in ufw and advertises _owl-setup._tcp via avahi
#
# The service tears most of this down itself when setup finishes; --disarm
# exists for image prep mistakes and bench testing.
#
# Error handling convention (matches owl_setup.sh / controller/shared/setup.sh):
# no `set -e` — each required step is checked explicitly and aborts BEFORE the
# flag is touched, so a failed arm never leaves a unit that boots into a
# broken setup mode. The flag touch is deliberately the last step.

GREEN='\033[0;32m'
RED='\033[0;31m'
ORANGE='\033[0;33m'
NC='\033[0m'
TICK="${GREEN}[OK]${NC}"
CROSS="${RED}[FAIL]${NC}"

FLAG_PATH="/boot/firmware/owl-firstboot.flag"
SETUP_PORT=8088
SETUP_PSK="owl-setup"
AVAHI_FILE="/etc/avahi/services/owl-setup.service"
UNIT_DEST="/etc/systemd/system/owl-firstboot.service"
FALLBACK_UNIT_DEST="/etc/systemd/system/owl-firstboot-fallback.service"
SUDOERS_FILE="/etc/sudoers.d/96-owl-firstboot"
REARM_HELPER="/usr/local/sbin/owl-firstboot-arm"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OWL_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo -e "${CROSS} Run with sudo: sudo bash $0 [--arm|--disarm]"
    exit 1
fi

# Detect the non-root user that owns the OWL install (for sudoers + venv)
CURRENT_USER="${SUDO_USER:-$(logname 2>/dev/null || echo owl)}"
VENV_BIN="/home/${CURRENT_USER}/.virtualenvs/owl/bin"

MODE="${1:---arm}"

find_hotspot_connection() {
    # First wifi connection profile in AP mode
    while IFS=: read -r name type; do
        if [[ "$type" == *wireless* ]]; then
            mode=$(nmcli -t -f 802-11-wireless.mode con show "$name" 2>/dev/null | cut -d: -f2)
            if [[ "$mode" == "ap" ]]; then
                echo "$name"
                return 0
            fi
        fi
    done < <(nmcli -t -f NAME,TYPE con show)
    return 1
}

arm() {
    echo -e "${GREEN}[INFO] Arming first-boot setup mode...${NC}"

    # 1. Preconditions FIRST — arming without a hotspot profile would boot
    # the unit into a crash-looping setup service with no AP for the phone
    # to join, and there is no self-service recovery from that.
    HOTSPOT=$(find_hotspot_connection || true)
    if [[ -z "$HOTSPOT" ]]; then
        echo -e "${CROSS} No AP-mode connection found. Run controller/shared/setup.sh"
        echo -e "        (standalone mode) first to create the OWL hotspot,"
        echo -e "        then re-run this script. Nothing was changed."
        exit 1
    fi
    if [[ "$HOTSPOT" != OWL-* ]]; then
        echo -e "${ORANGE}[WARN] Hotspot SSID '$HOTSPOT' does not start with 'OWL-'."
        echo -e "       The phone app only finds OWL-* hotspots.${NC}"
    fi

    # 2. Reset the hotspot password to the fixed setup password — BEFORE the
    # flag is touched, so an aborted arm never leaves a unit that boots into
    # setup mode with a password the phone app can't match.
    if ! nmcli con modify "$HOTSPOT" wifi-sec.psk "${SETUP_PSK}"; then
        echo -e "${CROSS} Could not reset the hotspot password. Nothing was armed."
        exit 1
    fi
    echo -e "${TICK} Hotspot '${HOTSPOT}' password reset to setup default"

    # 3. systemd units — heredocs with expanded variables, same pattern as
    # owl_setup.sh and controller/shared/setup.sh (a template+sed step here
    # once shipped a doubled python path).
    # OnFailure: if setup_app.py cannot start (broken venv, bad path), the
    # fallback unit serves the journal tail on the same port so the phone
    # app shows the real error instead of "Failed to fetch".
    if ! cat > "${UNIT_DEST}" <<EOF
[Unit]
Description=OWL First-Boot Setup Service
ConditionPathExists=${FLAG_PATH}
After=NetworkManager.service network-online.target
Wants=network-online.target
OnFailure=owl-firstboot-fallback.service
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
# Root: nmcli hotspot control, ufw, avahi service file, flag removal
User=root
WorkingDirectory=${OWL_DIR}/controller/setup
ExecStart=${VENV_BIN}/python ${OWL_DIR}/controller/setup/setup_app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    then
        echo -e "${CROSS} Could not install the systemd unit. Nothing was armed."
        exit 1
    fi

    # System python on purpose: the venv is one of the things that may be
    # broken when this unit fires. Started only via OnFailure, never enabled.
    if ! cat > "${FALLBACK_UNIT_DEST}" <<EOF
[Unit]
Description=OWL First-Boot Setup Fallback (error responder on :${SETUP_PORT})
ConditionPathExists=${FLAG_PATH}

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 ${OWL_DIR}/controller/setup/fallback_server.py
Restart=on-failure
RestartSec=5
EOF
    then
        echo -e "${CROSS} Could not install the fallback unit. Nothing was armed."
        exit 1
    fi

    systemctl daemon-reload
    if ! systemctl enable owl-firstboot.service; then
        echo -e "${CROSS} Could not enable owl-firstboot.service. Nothing was armed."
        exit 1
    fi
    echo -e "${TICK} owl-firstboot.service installed and enabled (+ failure responder)"

    # 4. Firewall: setup API reachable after a WiFi join too.
    # Best-effort from here down to the flag — the service's startup()
    # re-creates the ufw rule and avahi advert itself.
    ufw allow ${SETUP_PORT}/tcp >/dev/null 2>&1 || \
        echo -e "${ORANGE}[WARN] ufw rule not added (startup() re-adds it)${NC}"
    echo -e "${TICK} ufw allows ${SETUP_PORT}/tcp"

    # 5. Avahi: the phone rediscovers the OWL on the home network via this
    cat > "${AVAHI_FILE}" <<EOF
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">OWL Setup on %h</name>
  <service>
    <type>_owl-setup._tcp</type>
    <port>${SETUP_PORT}</port>
  </service>
</service-group>
EOF
    systemctl reload avahi-daemon 2>/dev/null || true
    echo -e "${TICK} avahi advertises _owl-setup._tcp:${SETUP_PORT}"

    # 6. Dashboard re-arm path. Security: sudoers must never point at a
    # user-writable file (the repo belongs to ${CURRENT_USER}), so install
    # a self-contained root-owned helper and allow only that. Touching the
    # flag is a complete re-arm — the service's startup() re-applies the
    # setup password, avahi advert, and firewall rule itself.
    cat > "${REARM_HELPER}" <<EOF
#!/bin/sh
# Created by install_firstboot.sh — re-arms OWL first-boot setup mode.
# Deliberately minimal: the owl-firstboot service restores everything
# else it needs at startup.
touch "${FLAG_PATH}"
EOF
    chown root:root "${REARM_HELPER}"
    chmod 755 "${REARM_HELPER}"

    cat > "${SUDOERS_FILE}" <<EOF
# Allow the OWL dashboard to re-enter first-boot setup mode
${CURRENT_USER} ALL=(root) NOPASSWD: ${REARM_HELPER}
EOF
    chmod 440 "${SUDOERS_FILE}"
    echo -e "${TICK} root-owned re-arm helper (${REARM_HELPER}) + sudoers entry"

    # 7. State dir + flag — the actual arm, LAST so a failure anywhere above
    # leaves the unit un-armed rather than half-armed.
    mkdir -p /var/lib/owl
    if ! touch "${FLAG_PATH}"; then
        echo -e "${CROSS} Could not write the flag file (${FLAG_PATH}). Not armed."
        exit 1
    fi
    echo -e "${TICK} First-boot flag set (${FLAG_PATH})"

    echo -e "${GREEN}[DONE] Reboot to enter first-boot setup mode.${NC}"
}

disarm() {
    echo -e "${GREEN}[INFO] Disarming first-boot setup mode...${NC}"
    rm -f "${FLAG_PATH}"
    systemctl disable owl-firstboot.service 2>/dev/null || true
    systemctl stop owl-firstboot.service 2>/dev/null || true
    systemctl stop owl-firstboot-fallback.service 2>/dev/null || true
    rm -f "${UNIT_DEST}" "${FALLBACK_UNIT_DEST}"
    systemctl daemon-reload
    rm -f "${AVAHI_FILE}"
    systemctl reload avahi-daemon 2>/dev/null || true
    ufw delete allow ${SETUP_PORT}/tcp >/dev/null 2>&1 || true
    rm -f "${SUDOERS_FILE}"
    rm -f "${REARM_HELPER}"
    echo -e "${TICK} First-boot surface removed"
}

case "$MODE" in
    --arm) arm ;;
    --disarm) disarm ;;
    *) echo -e "${CROSS} Unknown option '$MODE' (use --arm or --disarm)"; exit 1 ;;
esac
