#!/bin/bash
# =============================================================================
# owl_cloud_provision.sh — connect this device to the Noktura cloud platform
# =============================================================================
#
# Configures a mosquitto bridge from the local MQTT broker to the cloud
# broker, writes the [Cloud] section into config/CONTROLLER.ini, stores
# credentials, and installs the sudoers entries required by remote update,
# restart and reboot commands.
#
# Run on:
#   - a STANDALONE OWL  (bridges its own broker, flat topics owl/...)
#   - a CENTRAL CONTROLLER (bridges the fleet broker — one connection
#     covers every connected OWL via owl/<device_id>/... topics)
#
# USAGE
#   sudo bash owl_cloud_provision.sh \
#       --host mqtt.noktura.tech --port 8883 \
#       --device-id <id issued at registration> \
#       --username <bridge username> \
#       (--password '<secret>' | --password-file <file>) \
#       [--ca-file ca.crt | --system-ca] [--portal-url https://app.noktura.tech]
#       [--storage-host hel1.your-objectstorage.com]
#
#   sudo bash owl_cloud_provision.sh --disable     # tear the bridge down
#
# Idempotent: re-run any time to rotate credentials or change the broker.
#
# Rules enforced (Noktura spec N12): --device-id is a lowercase site slug and
# --username must equal it — the broker ACL `site/%u/owl/#` keys on the
# username, so a mismatch silently drops every message.
#
# Cloud commands land on a SEPARATE local topic (standalone: owl/cloud_commands,
# networked: owl/cloud/<id>/commands) so utils/mqtt_manager.py can apply an
# allowlist to anything the cloud sends, instead of letting a stolen bridge
# credential drive spraying and config. The remote topic (.../commands) is
# unchanged, so Noktura needs no change.
#
# When the phone app provisions the unit, this script runs as a ROOT-OWNED
# COPY in /usr/local/lib/owl/ through /usr/local/sbin/owl-cloud-provision
# (controller/setup/install_firstboot.sh --cloud-helper). The helper sets
# OWL_REPO_DIR and OWL_USER because the copy does not live in the repo and the
# caller may be the root setup service (cross-repo review 2026-09-17, O-1/O-16).
# =============================================================================
set -euo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
ORANGE='\033[0;33m'
NC='\033[0m'

if [[ "$(id -u)" != "0" ]]; then
    echo -e "${RED}[ERROR] Run with sudo: sudo bash $0 ...${NC}" >&2
    exit 1
fi

# OWL_USER / OWL_REPO_DIR come from the root helper; a manual sudo run uses
# the invoking user and this script's own directory.
CURRENT_USER="${OWL_USER:-${SUDO_USER:-$(whoami)}}"
USER_HOME="$(getent passwd "$CURRENT_USER" | cut -d: -f6)"
SCRIPT_DIR="$(dirname "$(realpath "$0")")"
REPO_DIR="${OWL_REPO_DIR:-$SCRIPT_DIR}"
CTRL_INI="$REPO_DIR/config/CONTROLLER.ini"
BRIDGE_CONF="/etc/mosquitto/conf.d/owl-cloud.conf"
CLOUD_DIR="$USER_HOME/.owl/cloud"
SUDOERS_FILE="/etc/sudoers.d/98-owl-cloud"

HOST=""
PORT="8883"
DEVICE_ID=""
USERNAME=""
PASSWORD=""
PASSWORD_FILE_SRC=""
CA_FILE=""
SYSTEM_CA=0
DISABLE=0
PORTAL_URL=""
STORAGE_HOST=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host)          HOST="$2"; shift 2 ;;
        --port)          PORT="$2"; shift 2 ;;
        --device-id)     DEVICE_ID="$2"; shift 2 ;;
        --username)      USERNAME="$2"; shift 2 ;;
        --password)      PASSWORD="$2"; shift 2 ;;
        --password-file) PASSWORD_FILE_SRC="$2"; shift 2 ;;
        --ca-file)       CA_FILE="$2"; shift 2 ;;
        --system-ca)     SYSTEM_CA=1; shift ;;
        --portal-url)    PORTAL_URL="$2"; shift 2 ;;
        --storage-host)  STORAGE_HOST="$2"; shift 2 ;;
        --disable)       DISABLE=1; shift ;;
        *) echo -e "${RED}[ERROR] Unknown option: $1${NC}" >&2; exit 2 ;;
    esac
done

# --- INI helpers (section-aware, atomic) --------------------------------------
_update_ini_key() {
    local file="$1" section="$2" key="$3" value="$4"
    local rc=0
    awk -v sect="[${section}]" -v key="$key" -v val="$value" '
        /^\[/ { in_sect = ($0 == sect) }
        in_sect && !done && $0 ~ ("^" key "[ \t]*=") { $0 = key " = " val; done = 1 }
        { print }
        END { exit done ? 0 : 3 }
    ' "$file" > "${file}.tmp" || rc=$?
    if [[ $rc -eq 3 ]]; then
        # Key absent — append it at the end of the section
        rm -f "${file}.tmp"
        awk -v sect="[${section}]" -v key="$key" -v val="$value" '
            /^\[/ {
                if (in_sect && !done) { print key " = " val; done = 1 }
                in_sect = ($0 == sect)
            }
            { print }
            END { if (in_sect && !done) print key " = " val }
        ' "$file" > "${file}.tmp"
    fi
    mv "${file}.tmp" "$file"
    chown "$CURRENT_USER:$CURRENT_USER" "$file"
}

_ensure_cloud_section() {
    if ! grep -q '^\[Cloud\]' "$CTRL_INI"; then
        # Quoted heredoc — nothing inside is shell-expanded.
        cat >> "$CTRL_INI" <<'EOF'

[Cloud]
# Managed by owl_cloud_provision.sh — see config/CONTROLLER_TEMPLATE.ini
enable = False
broker_host =
broker_port = 8883
device_id =
username =
ca_cert =
password_file =
EOF
    fi
}

_read_ini_key() {
    local section="$1" key="$2" fallback="${3:-}"
    local val
    val="$(awk -v sect="[${section}]" -v key="$key" '
        /^\[/ { in_sect = ($0 == sect) }
        in_sect && index($0, key " = ") == 1 { sub(key " = ", ""); print; exit }
    ' "$CTRL_INI")"
    echo "${val:-$fallback}"
}

# --- Disable path ----------------------------------------------------------------
if [[ "$DISABLE" == "1" ]]; then
    echo -e "${ORANGE}[INFO] Disabling cloud bridge...${NC}"
    rm -f "$BRIDGE_CONF"
    if [[ -f "$CTRL_INI" ]] && grep -q '^\[Cloud\]' "$CTRL_INI"; then
        _update_ini_key "$CTRL_INI" "Cloud" "enable" "False"
    fi
    systemctl restart mosquitto 2>/dev/null || true
    echo -e "${GREEN}[OK] Cloud bridge disabled. Credentials in $CLOUD_DIR were kept.${NC}"
    exit 0
fi

# --- Validation -------------------------------------------------------------------
[[ -n "$HOST" ]] || { echo -e "${RED}[ERROR] --host is required${NC}" >&2; exit 2; }
[[ -n "$DEVICE_ID" ]] || { echo -e "${RED}[ERROR] --device-id is required${NC}" >&2; exit 2; }
[[ -n "$USERNAME" ]] || { echo -e "${RED}[ERROR] --username is required${NC}" >&2; exit 2; }
if [[ -z "$PASSWORD" && -z "$PASSWORD_FILE_SRC" ]]; then
    echo -e "${RED}[ERROR] one of --password / --password-file is required${NC}" >&2; exit 2
fi
# Site slug rule (matches Noktura fleet_sites.slug and the broker ACL).
if ! [[ "$DEVICE_ID" =~ ^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$ ]]; then
    echo -e "${RED}[ERROR] invalid --device-id '$DEVICE_ID': lowercase letters, digits and hyphens, 3-32 chars${NC}" >&2; exit 2
fi
if [[ "$USERNAME" != "$DEVICE_ID" ]]; then
    echo -e "${RED}[ERROR] --username must equal --device-id (the cloud ACL keys on the username)${NC}" >&2; exit 2
fi
if [[ -n "$PORTAL_URL" ]] && ! [[ "$PORTAL_URL" =~ ^https://[A-Za-z0-9.-]+(/[A-Za-z0-9._/-]*)?$ ]]; then
    echo -e "${RED}[ERROR] --portal-url must be an https URL${NC}" >&2; exit 2
fi
# Upload host pin for transfer_session / upload_previews URLs (mqtt_manager.py).
if [[ -n "$STORAGE_HOST" ]] && ! [[ "$STORAGE_HOST" =~ ^[a-z0-9.-]{3,253}$ ]]; then
    echo -e "${RED}[ERROR] --storage-host must be a lowercase hostname${NC}" >&2; exit 2
fi
if ! [[ "$PORT" =~ ^[0-9]{1,5}$ ]] || [[ "$PORT" -lt 1 || "$PORT" -gt 65535 ]]; then
    echo -e "${RED}[ERROR] invalid --port${NC}" >&2; exit 2
fi
if [[ ! -f "$CTRL_INI" ]]; then
    echo -e "${RED}[ERROR] $CTRL_INI not found — run controller/shared/setup.sh first${NC}" >&2
    exit 1
fi

# --- Mosquitto server (networked-mode OWLs only ship mosquitto-clients) -------------
if ! command -v mosquitto >/dev/null 2>&1; then
    echo -e "${ORANGE}[INFO] Installing mosquitto...${NC}"
    apt-get update -qq && apt-get install -y -qq mosquitto
fi

# --- Credentials ----------------------------------------------------------------------
echo -e "${GREEN}[INFO] Writing credentials to $CLOUD_DIR...${NC}"
install -d -m 700 -o "$CURRENT_USER" -g "$CURRENT_USER" "$USER_HOME/.owl" "$CLOUD_DIR"
PASSWORD_FILE="$CLOUD_DIR/credentials"
if [[ -n "$PASSWORD_FILE_SRC" ]]; then
    install -m 600 -o "$CURRENT_USER" -g "$CURRENT_USER" "$PASSWORD_FILE_SRC" "$PASSWORD_FILE"
    PASSWORD="$(cat "$PASSWORD_FILE")"
else
    install -m 600 -o "$CURRENT_USER" -g "$CURRENT_USER" /dev/null "$PASSWORD_FILE"
    printf '%s' "$PASSWORD" > "$PASSWORD_FILE"
fi

CA_PATH=""
if [[ -n "$CA_FILE" ]]; then
    install -m 644 -o "$CURRENT_USER" -g "$CURRENT_USER" "$CA_FILE" "$CLOUD_DIR/ca.crt"
    CA_PATH="$CLOUD_DIR/ca.crt"
fi

# --- Topic prefix per [Network] mode -----------------------------------------------------
NET_MODE="$(_read_ini_key Network mode standalone)"
if [[ "$NET_MODE" == "networked" ]]; then
    # Fleet broker on the controller: device-scoped topics (owl/<id>/state).
    # Bridge pattern syntax: topic <pattern> <dir> <qos> <local_prefix> <remote_prefix>
    # — the pattern applies AFTER the prefix, so +/state matches owl/<id>/state
    # for every connected OWL with a single bridge connection.
    # Remote topics carry the site prefix (site/<device-id>/owl/<local-id>/...)
    # so the cloud broker can isolate fleets with a single pattern ACL even
    # when local OWL ids collide between sites.
    #
    # State comes from cloud_out/<id>/state, the slim cloud copy (23 fields, on
    # change plus a 20 s keepalive), not the full 2 s dashboard state on
    # owl/<id>/state. GPS is not bridged: the position rides in the slim state.
    # This keeps a rig at about 0.5 MB per running hour on 4G instead of 20 MB.
    LOCAL_PREFIX="owl/"
    REMOTE_PREFIX="site/${DEVICE_ID}/owl/"
    TOPIC_LINES=$(cat <<EOF
topic +/state out 1 cloud_out/ ${REMOTE_PREFIX}
topic +/status out 1 ${LOCAL_PREFIX} ${REMOTE_PREFIX}
topic +/commands in 1 owl/cloud/ ${REMOTE_PREFIX}
EOF
)
    # Inbound cloud commands land on owl/cloud/<id>/commands, where each OWL's
    # mqtt_manager.py applies the cloud allowlist. They must never reach
    # owl/<id>/commands, the controller's unrestricted command topic (O-4).
    echo -e "${GREEN}[INFO] Networked mode — bridging the fleet's owl/<id>/ topics to site/${DEVICE_ID}/owl/<id>/ on the cloud broker.${NC}"
else
    # Standalone OWL: flat local topics; on the cloud side the unit appears
    # under its own site with local-id = device-id (site/<id>/owl/<id>/...),
    # keeping the topic depth identical to networked fleets.
    LOCAL_PREFIX="owl/"
    REMOTE_PREFIX="site/${DEVICE_ID}/owl/${DEVICE_ID}/"
    # Inbound: remote .../commands lands on LOCAL owl/cloud_commands (the
    # pattern is shared, only the prefixes differ), where mqtt_manager.py
    # applies the cloud allowlist. Never owl/commands.
    # State: the slim cloud copy on cloud_out/state (see networked above).
    TOPIC_LINES=$(cat <<EOF
topic state out 1 cloud_out/ ${REMOTE_PREFIX}
topic status out 1 ${LOCAL_PREFIX} ${REMOTE_PREFIX}
topic commands in 1 owl/cloud_ ${REMOTE_PREFIX}
EOF
)
    echo -e "${GREEN}[INFO] Standalone mode — mapping owl/* to site/${DEVICE_ID}/owl/${DEVICE_ID}/* on the cloud broker.${NC}"
fi

# --- Bridge config --------------------------------------------------------------------------
echo -e "${GREEN}[INFO] Writing $BRIDGE_CONF...${NC}"
{
    echo "# OWL cloud bridge — managed by owl_cloud_provision.sh, do not edit"
    # Broker-wide cap on queued messages per client. While 4G is down only the
    # QoS 1 change messages queue (keepalives are QoS 0), so this bounds the
    # catch-up burst. It must come before the connection block.
    echo "max_queued_messages 200"
    echo "connection noktura"
    echo "address ${HOST}:${PORT}"
    if [[ "$SYSTEM_CA" == "1" || -z "$CA_PATH" ]]; then
        echo "bridge_capath /etc/ssl/certs"
    else
        echo "bridge_cafile ${CA_PATH}"
    fi
    echo "remote_username ${USERNAME}"
    echo "remote_password ${PASSWORD}"
    echo "remote_clientid owl-bridge-${DEVICE_ID}"
    echo "cleansession false"
    echo "notifications true"
    echo "restart_timeout 5 60"
    echo "$TOPIC_LINES"
} > "$BRIDGE_CONF"
chown root:mosquitto "$BRIDGE_CONF" 2>/dev/null || chown root:root "$BRIDGE_CONF"
chmod 640 "$BRIDGE_CONF"

# --- CONTROLLER.ini [Cloud] ---------------------------------------------------------------------
echo -e "${GREEN}[INFO] Updating ${CTRL_INI} [Cloud]...${NC}"
_ensure_cloud_section
_update_ini_key "$CTRL_INI" "Cloud" "enable" "True"
_update_ini_key "$CTRL_INI" "Cloud" "broker_host" "$HOST"
_update_ini_key "$CTRL_INI" "Cloud" "broker_port" "$PORT"
_update_ini_key "$CTRL_INI" "Cloud" "device_id" "$DEVICE_ID"
_update_ini_key "$CTRL_INI" "Cloud" "username" "$USERNAME"
_update_ini_key "$CTRL_INI" "Cloud" "ca_cert" "${CA_PATH:-/etc/ssl/certs}"
_update_ini_key "$CTRL_INI" "Cloud" "password_file" "$PASSWORD_FILE"
if [[ -n "$PORTAL_URL" ]]; then
    _update_ini_key "$CTRL_INI" "Cloud" "portal_url" "$PORTAL_URL"
fi
if [[ -n "$STORAGE_HOST" ]]; then
    _update_ini_key "$CTRL_INI" "Cloud" "storage_host" "$STORAGE_HOST"
fi
# The device token in [Security] must not be world-readable (dev/clean.sh
# used to chmod -R 755 the repo). Owner + group only.
chmod 640 "$CTRL_INI" 2>/dev/null || true

# --- Sudoers for remote update / restart / reboot ---------------------------------------------------
# SECURITY: the systemd-run command must be LITERAL up to the script's own
# arguments. A wildcard before --uid would let a caller smuggle a different
# uid inside the glob (sudoers * matches across spaces).
echo -e "${GREEN}[INFO] Installing sudoers entries...${NC}"
SYSTEMD_RUN_BIN="$(command -v systemd-run || echo /usr/bin/systemd-run)"
SYSTEMCTL_BIN="$(command -v systemctl || echo /usr/bin/systemctl)"
REBOOT_BIN="$(command -v reboot || echo /usr/sbin/reboot)"

SUDOERS_TMP="$(mktemp)"
cat > "$SUDOERS_TMP" <<EOF
# Managed by owl_cloud_provision.sh — remote update/restart/reboot via MQTT.
# Alias names are global across /etc/sudoers.d: 96-owl-update (setup.sh)
# already defines OWL_UPDATE_RUN, so this file uses its own name.
Cmnd_Alias OWL_CLOUD_UPDATE_RUN = ${SYSTEMD_RUN_BIN} --unit=owl-update --collect --property=RuntimeMaxSec=1800 --uid=${CURRENT_USER} --gid=${CURRENT_USER} /bin/bash ${REPO_DIR}/owl_update.sh *
Cmnd_Alias OWL_CLOUD_CMDS = ${SYSTEMCTL_BIN} restart owl.service, ${SYSTEMCTL_BIN} restart owl-controller.service, ${REBOOT_BIN}
${CURRENT_USER} ALL=(ALL) NOPASSWD: OWL_CLOUD_UPDATE_RUN, OWL_CLOUD_CMDS
EOF
if visudo -cf "$SUDOERS_TMP" >/dev/null; then
    install -m 440 -o root -g root "$SUDOERS_TMP" "$SUDOERS_FILE"
    rm -f "$SUDOERS_TMP"
else
    rm -f "$SUDOERS_TMP"
    echo -e "${RED}[ERROR] generated sudoers failed validation — not installed${NC}" >&2
    exit 1
fi

# --- Restart mosquitto + bridge check ------------------------------------------------------------------
echo -e "${GREEN}[INFO] Restarting mosquitto...${NC}"
systemctl restart mosquitto
sleep 3
if ! systemctl is-active --quiet mosquitto; then
    echo -e "${RED}[ERROR] mosquitto failed to start — check: journalctl -u mosquitto -n 30${NC}" >&2
    echo -e "${RED}        Reverting bridge config.${NC}" >&2
    rm -f "$BRIDGE_CONF"
    systemctl restart mosquitto || true
    exit 1
fi

if journalctl -u mosquitto --since "-30s" 2>/dev/null | grep -qi "bridge.*noktura.*connect"; then
    echo -e "${GREEN}[OK] Bridge connected to ${HOST}:${PORT}.${NC}"
else
    echo -e "${ORANGE}[WARNING] No bridge connection seen yet — this is expected if the${NC}"
    echo -e "${ORANGE}          cloud broker is not reachable. mosquitto retries automatically${NC}"
    echo -e "${ORANGE}          (restart_timeout 5..60s). Check later with:${NC}"
    echo -e "${ORANGE}          journalctl -u mosquitto -f | grep -i bridge${NC}"
fi

echo -e "${GREEN}[COMPLETE] Cloud provisioning done for device '${DEVICE_ID}'.${NC}"
