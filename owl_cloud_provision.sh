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
#       [--ca-file ca.crt | --system-ca]
#
#   sudo bash owl_cloud_provision.sh --disable     # tear the bridge down
#
# Idempotent: re-run any time to rotate credentials or change the broker.
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

CURRENT_USER="${SUDO_USER:-$(whoami)}"
USER_HOME="$(getent passwd "$CURRENT_USER" | cut -d: -f6)"
SCRIPT_DIR="$(dirname "$(realpath "$0")")"
REPO_DIR="$SCRIPT_DIR"
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
        in_sect && index($0, key " = ") == 1 { $0 = key " = " val; done = 1 }
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
if ! [[ "$DEVICE_ID" =~ ^[A-Za-z0-9_-]{1,64}$ ]]; then
    echo -e "${RED}[ERROR] invalid --device-id (allowed: A-Z a-z 0-9 _ -)${NC}" >&2; exit 2
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
    LOCAL_PREFIX="owl/"
    REMOTE_PREFIX="site/${DEVICE_ID}/owl/"
    TOPIC_LINES=$(cat <<EOF
topic +/state out 1 ${LOCAL_PREFIX} ${REMOTE_PREFIX}
topic +/status out 1 ${LOCAL_PREFIX} ${REMOTE_PREFIX}
topic +/gps out 1 ${LOCAL_PREFIX} ${REMOTE_PREFIX}
topic +/commands in 1 ${LOCAL_PREFIX} ${REMOTE_PREFIX}
EOF
)
    echo -e "${GREEN}[INFO] Networked mode — bridging the fleet's owl/<id>/ topics to site/${DEVICE_ID}/owl/<id>/ on the cloud broker.${NC}"
else
    # Standalone OWL: flat local topics; on the cloud side the unit appears
    # under its own site with local-id = device-id (site/<id>/owl/<id>/...),
    # keeping the topic depth identical to networked fleets.
    LOCAL_PREFIX="owl/"
    REMOTE_PREFIX="site/${DEVICE_ID}/owl/${DEVICE_ID}/"
    TOPIC_LINES=$(cat <<EOF
topic state out 1 ${LOCAL_PREFIX} ${REMOTE_PREFIX}
topic status out 1 ${LOCAL_PREFIX} ${REMOTE_PREFIX}
topic gps out 1 ${LOCAL_PREFIX} ${REMOTE_PREFIX}
topic commands in 1 ${LOCAL_PREFIX} ${REMOTE_PREFIX}
EOF
)
    echo -e "${GREEN}[INFO] Standalone mode — mapping owl/* to site/${DEVICE_ID}/owl/${DEVICE_ID}/* on the cloud broker.${NC}"
fi

# --- Bridge config --------------------------------------------------------------------------
echo -e "${GREEN}[INFO] Writing $BRIDGE_CONF...${NC}"
{
    echo "# OWL cloud bridge — managed by owl_cloud_provision.sh, do not edit"
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
Cmnd_Alias OWL_UPDATE_RUN = ${SYSTEMD_RUN_BIN} --unit=owl-update --collect --property=RuntimeMaxSec=1800 --uid=${CURRENT_USER} --gid=${CURRENT_USER} /bin/bash ${REPO_DIR}/owl_update.sh *
Cmnd_Alias OWL_CLOUD_CMDS = ${SYSTEMCTL_BIN} restart owl.service, ${SYSTEMCTL_BIN} restart owl-controller.service, ${REBOOT_BIN}
${CURRENT_USER} ALL=(ALL) NOPASSWD: OWL_UPDATE_RUN, OWL_CLOUD_CMDS
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
