#!/bin/bash
# OWL first-boot setup — arm / disarm script
#
# Usage (run with sudo on the OWL):
#   sudo bash install_firstboot.sh --arm            # default: enter setup mode on next boot
#   sudo bash install_firstboot.sh --disarm         # remove all first-boot surface
#   sudo bash install_firstboot.sh --camera-config  # only write the CM camera
#                                                   # overlay to config.txt
#                                                   # (exit 2 = changed, reboot needed)
#
# Arming:
#   - installs + enables owl-firstboot.service (ConditionPathExists gated)
#   - touches the flag on the FAT boot partition (re-armable from any PC,
#     same idea as Raspberry Pi's `ssh` flag file)
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

# Production camera sensor (Arducam IMX296 global shutter). Compute Modules
# have NO camera autodetection — the stock camera_auto_detect=1 silently does
# nothing on CM5, so the sensor overlay must be loaded explicitly in
# config.txt or a fresh flash never finds the camera (no error, no I2C bus).
# If the shipped sensor ever changes, update this AND CAMERA_SENSOR in
# firstboot_state.py to match.
CAMERA_OVERLAY="imx296"
BOOT_CONFIG="/boot/firmware/config.txt"
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

configure_camera() {
    # Write the CM camera overlay config to config.txt. Idempotent — re-runs
    # never stack lines. Returns:
    #   0  nothing to do (already configured, or board has autodetect)
    #   1  error (conflicting overlay / config.txt unwritable) — do not ship
    #   2  config.txt changed — camera appears after the next reboot
    #
    # Hardened against non-CM boards: on a Pi 4/5 camera_auto_detect works
    # and forcing overlay lines can fight it, so anything that is not a
    # Compute Module is skipped with a warning.
    local model=""
    if [[ -r /proc/device-tree/model ]]; then
        model=$(tr -d '\0' < /proc/device-tree/model)
    fi
    if [[ "$model" != *"Compute Module"* ]]; then
        echo -e "${ORANGE}[WARN] '${model:-unknown board}' is not a Compute Module —"
        echo -e "       skipping camera overlay config (autodetect handles the"
        echo -e "       camera on this board; forced overlays are CM-only).${NC}"
        return 0
    fi
    if [[ ! -f "$BOOT_CONFIG" ]]; then
        echo -e "${CROSS} ${BOOT_CONFIG} not found — cannot configure the CM camera."
        return 1
    fi

    # Never stack a second sensor overlay: IMX296 and IMX477 both sit at I2C
    # 0x1a and their device-tree nodes collide when loaded on the same port.
    local other
    other=$(grep -E '^dtoverlay=(imx[0-9]+|ov[0-9]+|arducam)' "$BOOT_CONFIG" \
            | grep -v "^dtoverlay=${CAMERA_OVERLAY}\b" || true)
    if [[ -n "$other" ]]; then
        echo -e "${CROSS} ${BOOT_CONFIG} already loads a different camera overlay:"
        echo "$other" | sed 's/^/        /'
        echo -e "        Two sensor overlays on one CSI port collide at I2C 0x1a."
        echo -e "        Remove the old overlay, then re-run."
        return 1
    fi

    local changed=0

    # Flip the stock autodetect line IN PLACE (never append a duplicate) —
    # on CMs it does nothing except mislead whoever reads the file next.
    if grep -qE '^camera_auto_detect=' "$BOOT_CONFIG"; then
        if ! grep -qE '^camera_auto_detect=0$' "$BOOT_CONFIG"; then
            if ! sed -i 's/^camera_auto_detect=.*/camera_auto_detect=0/' "$BOOT_CONFIG"; then
                echo -e "${CROSS} Could not edit ${BOOT_CONFIG}."
                return 1
            fi
            changed=1
        fi
    fi

    # Appended once, marked so re-runs skip it. The trailing [all] header
    # guarantees the lines are active regardless of which section the file
    # happens to end in (anything under e.g. [pi4] is silently ignored).
    local marker="# OWL camera (${CAMERA_OVERLAY}) — added by install_firstboot.sh"
    if ! grep -qF "$marker" "$BOOT_CONFIG"; then
        {
            echo ""
            echo "[all]"
            echo "$marker"
            # Only if the file had no autodetect line at all to flip above
            grep -qE '^camera_auto_detect=' "$BOOT_CONFIG" || echo "camera_auto_detect=0"
            # Both ports on purpose: the unused one fails silently, so the
            # camera works whichever CSI connector it is plugged into. Only
            # safe because both lines load the SAME sensor.
            echo "dtoverlay=${CAMERA_OVERLAY}"
            echo "dtoverlay=${CAMERA_OVERLAY},cam0"
            # Expose the camera I2C buses as /dev/i2c-* for field debugging
            # with i2cdetect (sensor should ACK at 0x1a)
            echo "dtparam=i2c_csi_dsi=on"
            echo "dtparam=i2c_csi_dsi0=on"
        } >> "$BOOT_CONFIG"
        if [[ $? -ne 0 ]]; then
            echo -e "${CROSS} Could not append to ${BOOT_CONFIG}."
            return 1
        fi
        changed=1
    fi

    if [[ "$changed" == "1" ]]; then
        echo -e "${TICK} CM camera config written to ${BOOT_CONFIG} (${CAMERA_OVERLAY}, both ports)"
        echo -e "${ORANGE}[WARN] The camera cannot appear until the next reboot.${NC}"
        return 2
    fi
    echo -e "${TICK} CM camera config already present in ${BOOT_CONFIG}"
    return 0
}

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

# Serial-derived hotspot name, matching controller/shared/setup.sh
# default_ssid() and the device_serial the firmware reports to the app.
serial_ssid() {
    local serial suffix
    serial=$(awk -F': ' '/^Serial/ {print $2}' /proc/cpuinfo 2>/dev/null | tr -d ' \t')
    if [[ -z "$serial" || "$serial" =~ ^0+$ ]]; then
        serial=$(cat /etc/machine-id 2>/dev/null)
    fi
    suffix=$(printf '%s' "$serial" | tail -c 4 | tr '[:lower:]' '[:upper:]')
    if [[ -n "$suffix" && ${#suffix} -eq 4 ]]; then
        echo "OWL-${suffix}"
        return 0
    fi
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

    # 2. CM camera overlay — a shipped CM5 without it never finds its camera
    # (autodetect silently no-ops on Compute Modules), so a conflict or write
    # failure is a ship blocker. exit 2 just means a reboot is needed, which
    # arming requires anyway.
    configure_camera
    if [[ $? -eq 1 ]]; then
        echo -e "${CROSS} Camera boot config failed. Nothing was armed."
        exit 1
    fi

    # 3. Reset the hotspot password to the fixed setup password — BEFORE the
    # flag is touched, so an aborted arm never leaves a unit that boots into
    # setup mode with a password the phone app can't match.
    if ! nmcli con modify "$HOTSPOT" wifi-sec.psk "${SETUP_PSK}"; then
        echo -e "${CROSS} Could not reset the hotspot password. Nothing was armed."
        exit 1
    fi
    echo -e "${TICK} Hotspot '${HOTSPOT}' password reset to setup default"

    # 3b. Colliding install-default SSID (OWL-1, OWL-2, ...): rename to the
    # serial-derived name before shipping. Two units broadcasting the same
    # SSID cannot be told apart by the phone. Arm-time only — deployed
    # units are never renamed silently (farmers' phones look for the
    # stored name); custom SSIDs are left alone.
    CURRENT_SSID=$(nmcli -t -f 802-11-wireless.ssid con show "$HOTSPOT" 2>/dev/null | cut -d: -f2)
    if [[ "$CURRENT_SSID" =~ ^OWL-[0-9]+$ ]]; then
        NEW_SSID=$(serial_ssid || true)
        if [[ -n "$NEW_SSID" && "$NEW_SSID" != "$CURRENT_SSID" ]]; then
            if nmcli con modify "$HOTSPOT" 802-11-wireless.ssid "$NEW_SSID"; then
                echo -e "${TICK} Hotspot SSID '${CURRENT_SSID}' is an install default: renamed to '${NEW_SSID}'"
            else
                echo -e "${ORANGE}[WARN] Could not rename colliding SSID '${CURRENT_SSID}'; shipping as-is.${NC}"
            fi
        fi
    fi

    # 4. systemd units — heredocs with expanded variables, same pattern as
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

    # 5. Firewall: setup API reachable after a WiFi join too.
    # Best-effort from here down to the flag — the service's startup()
    # re-creates the ufw rule and avahi advert itself.
    ufw allow ${SETUP_PORT}/tcp >/dev/null 2>&1 || \
        echo -e "${ORANGE}[WARN] ufw rule not added (startup() re-adds it)${NC}"
    echo -e "${TICK} ufw allows ${SETUP_PORT}/tcp"

    # 6. Avahi: the phone rediscovers the OWL on the home network via this
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

    # 7. Dashboard re-arm path. Security: sudoers must never point at a
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

    # 8. State dir + flag — the actual arm, LAST so a failure anywhere above
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
    # Standalone entry for owl_setup.sh (and field debugging): only the
    # camera overlay config, propagating its exit code (2 = reboot needed).
    # Deliberately untouched by --disarm — the overlay is hardware truth,
    # not setup surface.
    --camera-config) configure_camera; exit $? ;;
    *) echo -e "${CROSS} Unknown option '$MODE' (use --arm, --disarm or --camera-config)"; exit 1 ;;
esac
