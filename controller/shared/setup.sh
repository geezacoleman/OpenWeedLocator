#!/bin/bash

# Define colors for status messages
RED='\033[0;31m'    # Red for ERROR messages
ORANGE='\033[0;33m' # Orange for warnings
GREEN='\033[0;32m'  # Green for INFO and success messages
NC='\033[0m'        # No color (reset)
TICK="${GREEN}[OK]${NC}"
CROSS="${RED}[FAIL]${NC}"
SCRIPT_DIR=$(dirname "$(realpath "$0")")
CURRENT_USER=${SUDO_USER:-$(whoami)}
OWL_MODE=""           # "standalone" or "networked"
CONTROLLER_IP=""      # Only for networked mode
GATEWAY_IP=""         # Only for networked mode
STATIC_IP=""          # Only for networked mode

if [ "$EUID" -ne 0 ]; then
   echo -e "${RED}[ERROR] This script must be run with sudo privileges.${NC}"
   echo -e "${RED}[ERROR] Please run: sudo bash controller/shared/setup.sh${NC}"
   exit 1
fi

if [ "$CURRENT_USER" != "owl" ]; then
   echo -e "${ORANGE}[WARNING] Current user '$CURRENT_USER' differs from expected 'owl'. Some settings may not work correctly.${NC}"
fi

# Initialize status tracking variables
STATUS_PACKAGES=""
STATUS_MQTT_BROKER=""
STATUS_WIFI_CONFIG=""
STATUS_UFW_CONFIG=""
STATUS_NGINX_CONFIG=""
STATUS_SSL_CERT=""
STATUS_AVAHI_CONFIG=""
STATUS_SERVICES=""
STATUS_FAN_PERMISSIONS=""
STATUS_SERVICE_PERMISSIONS=""
STATUS_OWL_CONFIG=""
STATUS_HOSTNAME=""

ERROR_PACKAGES=""
ERROR_MQTT_BROKER=""
ERROR_WIFI_CONFIG=""
ERROR_UFW_CONFIG=""
ERROR_NGINX_CONFIG=""
ERROR_SSL_CERT=""
ERROR_AVAHI_CONFIG=""
ERROR_SERVICES=""
ERROR_FAN_PERMISSIONS=""
ERROR_SERVICE_PERMISSIONS=""
ERROR_OWL_CONFIG=""
ERROR_HOSTNAME=""

# Function to check the exit status of the last executed command
check_status() {
  if [ $? -ne 0 ]; then
    echo -e "${CROSS} $1 failed."
    eval "STATUS_$2='${CROSS}'"
    eval "ERROR_$2='$1 failed'"
    return 1
  else
    echo -e "${TICK} $1 completed successfully."
    eval "STATUS_$2='${TICK}'"
  fi
}

_validate_ip() {
  local ip="$1"
  # Basic format check
  if ! [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    return 1
  fi
  # Octet range check 0..255
  IFS='.' read -r a b c d <<<"$ip"
  for o in "$a" "$b" "$c" "$d"; do
    if (( o < 0 || o > 255 )); then
      return 1
    fi
  done
  return 0
}

_ip_base() {  # first three octets
  IFS='.' read -r a b c _ <<<"$1"
  echo "${a}.${b}.${c}"
}

_last_octet() {
  IFS='.' read -r _ _ _ d <<<"$1"
  echo "$d"
}

_update_ini_key() {
  # Section-aware INI key=value update using awk.
  # Preserves comments and formatting. Only replaces lines that start with
  # "key = " inside the target [section].
  local file="$1"
  local section="$2"
  local key="$3"
  local value="$4"

  awk -v sect="[${section}]" -v key="$key" -v val="$value" '
    /^\[/ { in_sect = ($0 == sect) }
    in_sect && index($0, key " = ") == 1 { $0 = key " = " val }
    { print }
  ' "$file" > "${file}.tmp" && mv "${file}.tmp" "$file"
}

test_mqtt_broker() {
    echo -e "${GREEN}[INFO] Testing MQTT broker...${NC}"
    local mqtt_test_passed=false
    local warnings=()

    # Test 1: Check if mosquitto service is running
    if systemctl is-active --quiet mosquitto; then
        echo -e "${TICK} Mosquitto service is running"
    else
        echo -e "${CROSS} Mosquitto service is not running"
        return 1
    fi

    # Test 2: Check if mosquitto is listening on port 1883
    if netstat -tln 2>/dev/null | grep -q ":1883 "; then
        echo -e "${TICK} Mosquitto is listening on port 1883"
    elif ss -tln 2>/dev/null | grep -q ":1883 "; then
        echo -e "${TICK} Mosquitto is listening on port 1883"
    else
        echo -e "${CROSS} Mosquitto is not listening on port 1883"
        return 1
    fi

    # Test 3: Test local connection (localhost) - this should always work
    echo -e "${GREEN}[INFO] Testing local MQTT connection...${NC}"
    for attempt in 1 2 3; do
        if timeout 5 mosquitto_pub -h localhost -t "test/owl/setup" -m "test_local_$attempt" 2>/dev/null; then
            echo -e "${TICK} Local MQTT connection successful (attempt $attempt)"
            mqtt_test_passed=true
            break
        else
            if [ $attempt -eq 3 ]; then
                echo -e "${CROSS} Local MQTT connection failed after 3 attempts"
                return 1  # This is a hard failure - localhost should always work
            else
                echo -e "${ORANGE}[INFO] Local connection attempt $attempt failed, retrying...${NC}"
                sleep 2
            fi
        fi
    done

    # Test 4: Test network connection (hotspot IP) - this can be flaky due to timing
    echo -e "${GREEN}[INFO] Testing network MQTT connection (10.42.0.1)...${NC}"
    local network_test_passed=false

    # First check if the interface is up
    if ip addr show | grep -q "10.42.0.1"; then
        echo -e "${TICK} Hotspot interface (10.42.0.1) is configured"

        # Test network connection with multiple attempts
        for attempt in 1 2 3; do
            if timeout 5 mosquitto_pub -h 10.42.0.1 -t "test/owl/setup" -m "test_network_$attempt" 2>/dev/null; then
                echo -e "${TICK} Network MQTT connection successful (attempt $attempt)"
                network_test_passed=true
                break
            else
                if [ $attempt -eq 3 ]; then
                    warnings+=("Network MQTT connection failed - may work after reboot when hotspot is fully active")
                    echo -e "${ORANGE}[WARNING] Network MQTT connection failed - this may be normal during setup${NC}"
                else
                    echo -e "${ORANGE}[INFO] Network connection attempt $attempt failed, retrying...${NC}"
                    sleep 3
                fi
            fi
        done
    else
        warnings+=("Hotspot interface not yet configured - network MQTT will be available after reboot")
        echo -e "${ORANGE}[WARNING] Hotspot interface not ready - network access will work after reboot${NC}"
    fi

    # Test 5: Check configuration file
    if [ -f "/etc/mosquitto/conf.d/owl.conf" ]; then
        if grep -q "allow_anonymous true" /etc/mosquitto/conf.d/owl.conf &&
           grep -q "listener 1883 0.0.0.0" /etc/mosquitto/conf.d/owl.conf; then
            echo -e "${TICK} MQTT configuration file is correct"
        else
            echo -e "${CROSS} MQTT configuration file has issues"
            return 1
        fi
    else
        echo -e "${CROSS} MQTT configuration file missing"
        return 1
    fi

    # Summary
    if [ "$mqtt_test_passed" = true ]; then
        echo -e "${TICK} MQTT broker core functionality verified"

        if [ ${#warnings[@]} -gt 0 ]; then
            echo -e "${ORANGE}[INFO] MQTT Setup Notes:${NC}"
            for warning in "${warnings[@]}"; do
                echo -e "${ORANGE}  • $warning${NC}"
            done
        fi

        return 0
    else
        echo -e "${CROSS} MQTT broker failed core functionality tests"
        return 1
    fi
}

final_mqtt_validation() {
    echo -e "${GREEN}[INFO] Final MQTT connectivity validation...${NC}"
    if timeout 3 mosquitto_pub -h localhost -t "owl/test/setup" -m "setup_complete" 2>/dev/null; then
        echo -e "${TICK} MQTT broker ready for OWL communication"
    else
        echo -e "${ORANGE}[WARNING] Final MQTT test failed - check logs after reboot: journalctl -u mosquitto${NC}"
    fi

    if timeout 3 mosquitto_pub -h 10.42.0.1 -t "owl/test/setup" -m "setup_complete" 2>/dev/null; then
        echo -e "${TICK} MQTT broker ready for network clients"
    else
        echo -e "${ORANGE}[INFO] Network MQTT test failed - this is normal during setup, it should work after a reboot${NC}"
    fi
}

# Adds the * as you type the password for a more familiar usecase
read_password_masked() {
    local prompt="$1"
    local password=""
    local char

    printf "%s" "$prompt"

    while IFS= read -r -s -n1 char; do
        if [ -z "$char" ]; then
            break
        fi

        case "$char" in
            $'\n'|$'\r'|$'\x0a'|$'\x0d')
                break
                ;;
            $'\177'|$'\b')
                if [ -n "$password" ]; then
                    password=${password%?}
                    printf '\b \b'
                fi
                ;;
            *)
                password+="$char"
                printf '*'
                ;;
        esac
    done

    printf '\n'
    REPLY="$password"
}

# Collect user input
collect_user_input() {
    echo -e "${GREEN}[INFO] OWL Setup Configuration${NC}"
    echo -e "${GREEN}=======================================${NC}"
    echo ""

    # MODE SELECTION - NEW
    echo -e "${GREEN}[INFO] Select OWL Operation Mode:${NC}"
    echo "  1) Standalone - Create WiFi hotspot with local MQTT broker and dashboard"
    echo "  2) Networked  - Connect to existing WiFi network with remote MQTT broker"
    echo ""

    while true; do
        read -p "Select mode (1 or 2): " mode_choice
        if [[ "$mode_choice" == "1" ]]; then
            OWL_MODE="standalone"
            echo -e "${GREEN}[INFO] Standalone mode selected${NC}"
            break
        elif [[ "$mode_choice" == "2" ]]; then
            OWL_MODE="networked"
            echo -e "${GREEN}[INFO] Networked mode selected${NC}"
            break
        else
            echo -e "${RED}[ERROR] Invalid selection. Please enter 1 or 2.${NC}"
        fi
    done
    echo ""

    # Get OWL ID
    read -p "Enter OWL ID number (default: 1): " OWL_ID
    OWL_ID=${OWL_ID:-1}

    HOSTNAME="owl-${OWL_ID}"

    if [[ "$OWL_MODE" == "standalone" ]]; then
        # STANDALONE MODE - WiFi Hotspot Configuration
        echo -e "${GREEN}[INFO] Configuring WiFi Hotspot (Standalone Mode)${NC}"

        # Get hotspot SSID
        read -p "Enter WiFi hotspot name/SSID (default: OWL-${OWL_ID}): " SSID
        SSID=${SSID:-OWL-${OWL_ID}}

        # Get hotspot password with validation
        while true; do
            read_password_masked "Enter WiFi hotspot password (min 8 characters): "
            WIFI_PASSWORD="$REPLY"

            if [ ${#WIFI_PASSWORD} -lt 8 ]; then
                echo -e "${RED}[ERROR] Password must be at least 8 characters long.${NC}"
                continue
            fi

            read_password_masked "Re-enter WiFi password to confirm: "
            WIFI_PASSWORD_CONFIRM="$REPLY"

            if [ "$WIFI_PASSWORD" != "$WIFI_PASSWORD_CONFIRM" ]; then
                echo -e "${RED}[ERROR] Passwords do not match. Please try again.${NC}"
                continue
            fi

            read -p "Confirm and save this password? (y/n): " pass_confirm
            if [[ "$pass_confirm" =~ ^[Yy]$ ]]; then
                break
            else
                echo -e "${ORANGE}[INFO] Enter the password again.${NC}"
            fi
        done

        # Confirm standalone settings
        echo -e "${GREEN}[INFO] Standalone Configuration Summary:${NC}"
        echo -e "  Mode: Standalone"
        echo -e "  Hostname: ${HOSTNAME}"
        echo -e "  WiFi Hotspot SSID: ${SSID}"
        echo -e "  Hotspot IP: 10.42.0.1/24"
        echo -e "  MQTT Broker: localhost:1883 (local)"
        echo -e "  Dashboard: https://${HOSTNAME}.local/"
        echo ""

    else
        # NETWORKED MODE - Connection Type Selection
        echo -e "${GREEN}[INFO] Select network connection type:${NC}"
        echo "  1) WiFi     - Connect to an existing WiFi network"
        echo "  2) Ethernet - Use a wired LAN connection (eth0)"
        echo ""

        while true; do
            read -p "Select connection type (1 or 2, default: 1): " conn_choice
            conn_choice=${conn_choice:-1}
            if [[ "$conn_choice" == "1" ]]; then
                NET_INTERFACE="wifi"
                echo -e "${GREEN}[INFO] WiFi connection selected${NC}"
                break
            elif [[ "$conn_choice" == "2" ]]; then
                NET_INTERFACE="ethernet"
                echo -e "${GREEN}[INFO] Ethernet connection selected${NC}"
                break
            else
                echo -e "${RED}[ERROR] Invalid selection. Please enter 1 or 2.${NC}"
            fi
        done
        echo ""

        if [[ "$NET_INTERFACE" == "wifi" ]]; then
            echo -e "${GREEN}[INFO] Configuring WiFi Client (Networked Mode)${NC}"

            # Get WiFi network to join
            read -p "Enter WiFi network name (SSID) to join: " SSID
            while [ -z "$SSID" ]; do
                echo -e "${RED}[ERROR] SSID cannot be empty.${NC}"
                read -p "Enter WiFi network name (SSID) to join: " SSID
            done

            # Get WiFi password
            while true; do
                read_password_masked "Enter WiFi network password: "
                WIFI_PASSWORD="$REPLY"
                if [ -z "$WIFI_PASSWORD" ]; then
                    echo -e "${RED}[ERROR] Password cannot be empty.${NC}"
                    continue
                fi
                read_password_masked "Re-enter WiFi password to confirm: "
                WIFI_PASSWORD_CONFIRM="$REPLY"
                if [ "$WIFI_PASSWORD" != "$WIFI_PASSWORD_CONFIRM" ]; then
                    echo -e "${RED}[ERROR] Passwords do not match. Please try again.${NC}"
                    continue
                fi
                break
            done
        else
            echo -e "${GREEN}[INFO] Configuring Ethernet (Networked Mode)${NC}"
            echo -e "${GREEN}[INFO] Make sure the Ethernet cable is connected.${NC}"
            SSID=""
            WIFI_PASSWORD=""
        fi

        # Get network configuration
        while :; do
            # Static IP (no default)
            read -r -p "Enter static IP for this OWL (e.g., 192.168.1.11): " STATIC_IP
            while ! _validate_ip "$STATIC_IP"; do
              echo -e "${RED}[ERROR] Invalid IP format. Please enter a valid IPv4 address.${NC}"
              read -r -p "Enter static IP for this OWL (e.g., 192.168.1.11): " STATIC_IP
            done
            # Avoid .0/.255 for the host
            lo="$(_last_octet "$STATIC_IP")"
            if [[ "$lo" == "0" || "$lo" == "255" ]]; then
              echo -e "${ORANGE}[WARN] Host octet .0 or .255 is usually reserved. Choose another IP.${NC}"
              continue
            fi

            # Gateway (default 192.168.1.1)
            read -r -p "Enter gateway IP (default: 192.168.1.1): " GATEWAY_IP
            GATEWAY_IP=${GATEWAY_IP:-192.168.1.1}
            while ! _validate_ip "$GATEWAY_IP"; do
              echo -e "${RED}[ERROR] Invalid gateway IP.${NC}"
              read -r -p "Enter gateway IP (default: 192.168.1.1): " GATEWAY_IP
              GATEWAY_IP=${GATEWAY_IP:-192.168.1.1}
            done

            # Controller/Broker (default 192.168.1.2)
            read -r -p "Enter central controller IP (MQTT broker; default: 192.168.1.2): " CONTROLLER_IP
            CONTROLLER_IP=${CONTROLLER_IP:-192.168.1.2}
            while ! _validate_ip "$CONTROLLER_IP"; do
              echo -e "${RED}[ERROR] Invalid controller IP.${NC}"
              read -r -p "Enter central controller IP (MQTT broker; default: 192.168.1.2): " CONTROLLER_IP
              CONTROLLER_IP=${CONTROLLER_IP:-192.168.1.2}
            done

            # Base (/24) checks
            STATIC_BASE="$(_ip_base "$STATIC_IP")"
            GATE_BASE="$(_ip_base "$GATEWAY_IP")"
            CTRL_BASE="$(_ip_base "$CONTROLLER_IP")"

            if [[ "$STATIC_BASE" != "$CTRL_BASE" ]]; then
              echo -e "${ORANGE}[WARN] OWL static IP base (${STATIC_BASE}.x) differs from Controller base (${CTRL_BASE}.x).${NC}"
              echo -e "${ORANGE}[WARN] Please re-enter BOTH the OWL static IP and the Controller IP so they share the same subnet.${NC}"
              continue
            fi

            if [[ "$STATIC_BASE" != "$GATE_BASE" ]]; then
              echo -e "${ORANGE}[WARN] OWL static IP base (${STATIC_BASE}.x) differs from Gateway base (${GATE_BASE}.x).${NC}"
              echo -e "${ORANGE}[WARN] Please re-enter the Gateway IP to match the OWL subnet.${NC}"
              continue
            fi

            echo -e "${GREEN}[OK] Network OK: OWL ${STATIC_IP}, Gateway ${GATEWAY_IP}, Controller ${CONTROLLER_IP} (base ${STATIC_BASE}.x).${NC}"
            break
          done

        # Confirm networked settings
        echo -e "${GREEN}[INFO] Networked Configuration Summary:${NC}"
        echo -e "  Mode: Networked"
        echo -e "  Hostname: ${HOSTNAME}"
        if [[ "$NET_INTERFACE" == "wifi" ]]; then
            echo -e "  Connection: WiFi (${SSID})"
        else
            echo -e "  Connection: Ethernet (eth0)"
        fi
        echo -e "  Static IP: ${STATIC_IP}"
        echo -e "  Gateway: ${GATEWAY_IP}"
        echo -e "  Controller IP: ${CONTROLLER_IP}"
        echo -e "  MQTT Broker: ${CONTROLLER_IP}:1883 (remote)"
        echo -e "  Video Feed: https://${HOSTNAME}.local/video_feed"
        echo ""
    fi

    read -p "Continue with these settings? (y/n): " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        echo -e "${RED}[ERROR] Setup cancelled by user.${NC}"
        exit 1
    fi
}

setup_fan_permissions() {
    echo -e "${GREEN}[INFO] Configuring sudo permissions for Pi 5 fan control...${NC}"

    local SUDOERS_FILE="/etc/sudoers.d/99-owl-fan-control"
    # Resolve absolute path to pinctrl (fallback to /usr/bin/pinctrl)
    local PINCTRL_BIN
    PINCTRL_BIN="$(command -v pinctrl 2>/dev/null || echo /usr/bin/pinctrl)"

    sudo tee "$SUDOERS_FILE" > /dev/null <<EOF
# Managed by OWL controller/shared/setup.sh
# Allow 'owl' to control Raspberry Pi 5 fan without a password.
# We restrict to EXACT command lines to minimize risk.

Cmnd_Alias OWL_FAN_TOGGLE = \
    ${PINCTRL_BIN} FAN_PWM a0, \
    ${PINCTRL_BIN} FAN_PWM op dl

${CURRENT_USER} ALL=(root) NOPASSWD: OWL_FAN_TOGGLE
Defaults:${CURRENT_USER} secure_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
EOF

    sudo chmod 0440 "$SUDOERS_FILE"
    check_status "Fan control sudo permissions" "FAN_PERMISSIONS"
}

setup_service_control_permissions() {
    echo -e "${GREEN}[INFO] Configuring sudo permissions for OWL service control...${NC}"

    local SUDOERS_FILE="/etc/sudoers.d/97-owl-service-control"
    local SYSTEMCTL_BIN
    SYSTEMCTL_BIN="$(command -v systemctl 2>/dev/null || echo /usr/bin/systemctl)"

    sudo tee "$SUDOERS_FILE" > /dev/null <<EOF
# This file is managed by the OWL setup script.
# It allows the 'owl' user to manage the main owl.service
# without a password, which is required for the web dashboard power button.

Cmnd_Alias OWL_SERVICE_CMDS = ${SYSTEMCTL_BIN} start owl.service, ${SYSTEMCTL_BIN} stop owl.service, ${SYSTEMCTL_BIN} is-active owl.service, ${SYSTEMCTL_BIN} reset-failed owl.service

# Grant the user permission to run ONLY the commands in the alias.
${CURRENT_USER} ALL=(ALL) NOPASSWD: OWL_SERVICE_CMDS
EOF

    # Sudoers files require strict permissions to be active.
    sudo chmod 0440 "$SUDOERS_FILE"

    check_status "Service control sudo permissions" "SERVICE_PERMISSIONS"
}

update_owl_configs() {
    echo -e "${GREEN}[INFO] Updating OWL configuration files with network settings...${NC}"

    local CONFIG_DIR="/home/${CURRENT_USER}/owl/config"
    local CTRL_INI="${CONFIG_DIR}/CONTROLLER.ini"

    if [ ! -d "$CONFIG_DIR" ]; then
        echo -e "${ORANGE}[WARNING] Config directory not found at ${CONFIG_DIR}, skipping config updates${NC}"
        return 1
    fi

    # Determine values based on mode
    local BROKER_IP DEVICE_ID NET_MODE NET_STATIC_IP NET_CONTROLLER_IP
    if [[ "$OWL_MODE" == "standalone" ]]; then
        BROKER_IP="localhost"
        NET_MODE="standalone"
        NET_STATIC_IP="10.42.0.1"
        NET_CONTROLLER_IP="localhost"
    else
        BROKER_IP="${CONTROLLER_IP}"
        NET_MODE="networked"
        NET_STATIC_IP="${STATIC_IP}"
        NET_CONTROLLER_IP="${CONTROLLER_IP}"
    fi
    DEVICE_ID="${HOSTNAME}"

    cat > "$CTRL_INI" <<EOF
[MQTT]
enable = True
broker_ip = ${BROKER_IP}
broker_port = 1883
device_id = ${DEVICE_ID}

[WebDashboard]
port = 8000

[Network]
mode = ${NET_MODE}
static_ip = ${NET_STATIC_IP}
controller_ip = ${NET_CONTROLLER_IP}

[GPS]
# GPS data source for owl.py (none / serial / tcp)
source = none
# Serial GPS settings (only when source = serial)
port = /dev/ttyUSB0
baudrate = 9600

# Networked controller GPS server (not used by OWL units)
enable = False
nmea_port = 8500
boom_width = 12.0
track_save_directory = tracks

[Actuation]
# Relay timing — used as fallback when no GPS speed data
actuation_duration = 0.15
delay = 0.0
# Speed-adaptive actuation geometry
actuation_length_cm = 10
offset_cm = 30
speed_avg_window = 5.0
EOF

    chown "${CURRENT_USER}:${CURRENT_USER}" "$CTRL_INI"

    echo -e "${TICK} CONTROLLER.ini written"
    echo -e "${GREEN}[INFO]   MQTT: enable=True, broker=${BROKER_IP}:1883, device_id=${DEVICE_ID}${NC}"
    echo -e "${GREEN}[INFO]   Network: mode=${NET_MODE}, static_ip=${NET_STATIC_IP}, controller=${NET_CONTROLLER_IP}${NC}"
    return 0
}

# Step 1: Collect configuration from user
collect_user_input

echo -e "${GREEN}[INFO] If you are using a wifi connection to access the Pi over SSH or for internet, your network connection will be replaced with the new connection settings.${NC}"
echo -e "${ORANGE}[WARNING] If so it is likely this will drop out when the network is reconnected under a different IP address${NC}"
if [[ "$OWL_MODE" == "standalone" ]]; then
    echo -e "${ORANGE}[WARNING] Reconnect under the details entered above. OWL Static IP: 10.42.0.1 ${NC}"
else
    echo -e "${ORANGE}[WARNING] Reconnect under the details entered above. OWL Static IP: ${STATIC_IP} ${NC}"
fi
echo -e "${ORANGE}[WARNING] Make sure you have physical access to the Pi in case of issues${NC}"
read -p "Do you want to continue? (y/n): " network_warning
if [[ ! "$network_warning" =~ ^[Yy]$ ]]; then
    echo -e "${RED}[ERROR] Setup cancelled by user.${NC}"
    exit 1
fi

# Step 2: Install system packages
echo -e "${GREEN}[INFO] Installing required system packages...${NC}"
sudo apt-get update

if [[ "$OWL_MODE" == "standalone" ]]; then
    echo -e "${GREEN}[INFO] Installing MQTT broker and clients (standalone mode)...${NC}"
    sudo apt-get install -y mosquitto mosquitto-clients nginx network-manager avahi-daemon ssl-cert net-tools ufw openssl
else
    echo -e "${GREEN}[INFO] Installing MQTT clients only (networked mode)...${NC}"
    sudo apt-get install -y mosquitto-clients nginx network-manager avahi-daemon ssl-cert net-tools ufw openssl
fi

check_status "Installing system packages (nginx, ufw, openssl, avahi-daemon, mosquitto, net-tools)" "PACKAGES"

# Step 3: Configure MQTT broker
if [[ "$OWL_MODE" == "standalone" ]]; then
    echo -e "${GREEN}[INFO] Configuring MQTT broker (standalone mode)...${NC}"

    # Create mosquitto configuration that works with existing setup
    sudo tee /etc/mosquitto/conf.d/owl.conf > /dev/null <<EOF
# OWL MQTT Configuration
# Extends the main mosquitto.conf settings

# Allow anonymous connections (required for OWL dashboard)
allow_anonymous true

# Listen on all interfaces so hotspot clients can connect
listener 1883 0.0.0.0
EOF

    # Test configuration (may fail if service is running - that's OK)
    echo -e "${GREEN}[INFO] Testing MQTT configuration...${NC}"
    sudo systemctl stop mosquitto 2>/dev/null || true
    sleep 1

    sudo mosquitto -c /etc/mosquitto/mosquitto.conf -t >/dev/null 2>&1
    if [ $? -eq 0 ]; then
        echo -e "${TICK} MQTT configuration syntax is valid"
    else
        echo -e "${ORANGE}[INFO] MQTT config test inconclusive (may be normal if service was running)${NC}"
    fi

    # Start and enable mosquitto
    sudo systemctl enable mosquitto
    echo -e "${GREEN}[INFO] Restarting MQTT service...${NC}"
    sudo systemctl restart mosquitto

    # Wait for service to be fully ready
    echo -e "${GREEN}[INFO] Waiting for MQTT service to stabilize...${NC}"
    for i in {1..10}; do
        if systemctl is-active --quiet mosquitto; then
            sleep 2
            break
        fi
        sleep 1
    done

    if ! systemctl is-active --quiet mosquitto; then
        echo -e "${CROSS} MQTT service failed to start"
        check_status "MQTT broker service startup" "MQTT_BROKER"
    else
        echo -e "${TICK} MQTT service is running"
    fi

    # Test MQTT broker functionality (this is the real test)
    test_mqtt_broker
    check_status "MQTT broker configuration and testing" "MQTT_BROKER"

else
    echo -e "${GREEN}[INFO] Skipping MQTT broker setup (networked mode - using remote broker)${NC}"
    STATUS_MQTT_BROKER="${TICK}"
fi

# Step 4: Configure Network
if [[ "$OWL_MODE" == "standalone" ]]; then
    echo -e "${GREEN}[INFO] Setting up WiFi hotspot: ${SSID}...${NC}"

    # Remove existing connection if it exists
    nmcli connection delete "${SSID}" 2>/dev/null || true

    # create new hotspot connection
    nmcli connection add type wifi ifname wlan0 mode ap con-name "${SSID}" ssid "${SSID}"
    nmcli connection modify "${SSID}" 802-11-wireless.mode ap
    nmcli connection modify "${SSID}" 802-11-wireless-security.key-mgmt wpa-psk
    nmcli connection modify "${SSID}" 802-11-wireless-security.psk "${WIFI_PASSWORD}"
    nmcli connection modify "${SSID}" 802-11-wireless-security.proto rsn
    nmcli connection modify "${SSID}" 802-11-wireless-security.pairwise ccmp
    nmcli connection modify "${SSID}" 802-11-wireless.band bg
    nmcli connection modify "${SSID}" ipv4.method shared
    nmcli connection modify "${SSID}" ipv4.addresses 10.42.0.1/24
    nmcli connection modify "${SSID}" ipv6.method ignore

    # activate the connection
    nmcli connection up "${SSID}"
    check_status "WiFi hotspot configuration" "WIFI_CONFIG"
elif [[ "$NET_INTERFACE" == "wifi" ]]; then
    echo -e "${GREEN}[INFO] Configuring WiFi client connection: ${SSID}...${NC}"

    # Delete any existing connection with the same name
    nmcli con delete "${SSID}" 2>/dev/null || true

    # Add new WiFi connection
    nmcli con add type wifi con-name "${SSID}" ifname wlan0 ssid "${SSID}"

    # Configure WiFi security
    nmcli con modify "${SSID}" wifi-sec.key-mgmt wpa-psk
    nmcli con modify "${SSID}" wifi-sec.psk "${WIFI_PASSWORD}"

    # Configure static IP
    nmcli con modify "${SSID}" ipv4.addresses ${STATIC_IP}/24
    nmcli con modify "${SSID}" ipv4.gateway ${GATEWAY_IP}
    nmcli con modify "${SSID}" ipv4.dns "8.8.8.8 8.8.4.4"
    nmcli con modify "${SSID}" ipv4.method manual

    # Set as default connection
    nmcli con modify "${SSID}" connection.autoconnect yes
    nmcli con modify "${SSID}" connection.autoconnect-priority 100

    nmcli con up "${SSID}" || true

    check_status "WiFi configuration" "WIFI_CONFIG"
else
    # ETHERNET MODE - Static IP on eth0
    echo -e "${GREEN}[INFO] Configuring Ethernet with static IP on eth0...${NC}"

    CON_NAME="owl-ethernet"

    # Delete any existing OWL ethernet connection
    nmcli con delete "${CON_NAME}" 2>/dev/null || true

    # Add new ethernet connection
    nmcli con add type ethernet con-name "${CON_NAME}" ifname eth0

    # Configure static IP
    nmcli con modify "${CON_NAME}" ipv4.addresses ${STATIC_IP}/24
    nmcli con modify "${CON_NAME}" ipv4.gateway ${GATEWAY_IP}
    nmcli con modify "${CON_NAME}" ipv4.dns "8.8.8.8 8.8.4.4"
    nmcli con modify "${CON_NAME}" ipv4.method manual

    # Set as default connection
    nmcli con modify "${CON_NAME}" connection.autoconnect yes
    nmcli con modify "${CON_NAME}" connection.autoconnect-priority 100

    nmcli con up "${CON_NAME}" || true

    check_status "Ethernet configuration" "WIFI_CONFIG"
fi

# Step 5: Configure UFW firewall
echo -e "${GREEN}[INFO] Configuring firewall (UFW)...${NC}"

sudo ufw --force reset
sudo ufw default deny incoming
sudo ufw default allow outgoing

# Common rules for both modes
sudo ufw allow OpenSSH
sudo ufw allow ssh
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 1883/tcp
sudo ufw allow 8500/tcp  # GPS NMEA forwarding from Teltonika router

if [[ "$OWL_MODE" == "standalone" ]]; then
    echo -e "${GREEN}[INFO] Configuring UFW for standalone mode...${NC}"
    sudo ufw allow 'Nginx Full'
    sudo ufw allow 67/udp  # DHCP server
    sudo ufw allow 68/udp  # DHCP client
    sudo ufw allow from 10.42.0.0/24  # Allow all from hotspot network
else
    echo -e "${GREEN}[INFO] Configuring UFW for networked mode...${NC}"
fi

sudo ufw --force enable
check_status "UFW firewall configuration" "UFW_CONFIG"

# Step 6: Set hostname
echo -e "${GREEN}[INFO] Setting hostname to ${HOSTNAME}...${NC}"
sudo hostnamectl set-hostname "${HOSTNAME}"

echo -e "${GREEN}[INFO] Updating local hostname resolution...${NC}"
sudo sed -i "/${HOSTNAME}/d" /etc/hosts
echo "127.0.0.1 ${HOSTNAME}" | sudo tee -a /etc/hosts
echo "127.0.1.1 ${HOSTNAME}" | sudo tee -a /etc/hosts

check_status "Setting hostname and local resolution" "HOSTNAME"

# Set the permissions for each service
setup_fan_permissions
setup_service_control_permissions

# Step 7: Generate SSL certificates
echo -e "${GREEN}[INFO] Generating SSL certificates...${NC}"
sudo mkdir -p /etc/ssl/private
sudo mkdir -p /etc/ssl/certs

# Create self-signed certificate
sudo openssl req -x509 -nodes -days 3650 \
    -newkey rsa:2048 \
    -keyout /etc/ssl/private/owl.key \
    -out /etc/ssl/certs/owl.crt \
    -subj "/CN=${HOSTNAME}.local/C=US/ST=State/L=City/O=OWL/OU=Dashboard"

# Set proper permissions
sudo chmod 600 /etc/ssl/private/owl.key
sudo chmod 644 /etc/ssl/certs/owl.crt
check_status "SSL certificate generation" "SSL_CERT"

# Step 8: Configure Nginx
echo -e "${GREEN}[INFO] Setting up Nginx web server...${NC}"

if [[ "$OWL_MODE" == "standalone" ]]; then
    # STANDALONE MODE - Full dashboard + video
    sudo tee /etc/nginx/sites-available/owl-dash > /dev/null <<EOF
server {
    listen 80;
    server_name ${HOSTNAME}.local 10.42.0.1;
    return 301 https://\$server_name\$request_uri;
}

server {
    listen 443 ssl;
    server_name ${HOSTNAME}.local 10.42.0.1 _;

    ssl_certificate /etc/ssl/certs/owl.crt;
    ssl_certificate_key /etc/ssl/private/owl.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    # OWL Dashboard Flask app (Port 8000)
    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade \$http_upgrade;
        proxy_set_header   Connection "upgrade";
    }

    # Proxy for the MJPEG video stream from owl.py (Port 8001)
    location /video_feed {
        proxy_pass http://127.0.0.1:8001/stream.mjpg;
        proxy_set_header Host \$host;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
        chunked_transfer_encoding off;
    }

    # Single JPEG snapshot from owl.py (Port 8001)
    location /latest_frame.jpg {
        proxy_pass http://127.0.0.1:8001/latest_frame.jpg;
        proxy_set_header Host \$host;
        proxy_buffering off;
        proxy_cache off;
    }

    # MQTT status endpoint for debugging
    location /mqtt-status {
        return 200 "<html><body><h1>MQTT Status</h1><p>MQTT Broker: localhost:1883</p><p>Status: Running</p></body></html>";
        add_header Content-Type text/html;
    }
}
EOF
else
    # NETWORKED MODE - Video feed only
    sudo tee /etc/nginx/sites-available/owl-dash > /dev/null <<EOF
server {
    listen 80;
    server_name ${HOSTNAME}.local ${STATIC_IP};
    return 301 https://\$server_name\$request_uri;
}

server {
    listen 443 ssl;
    server_name ${HOSTNAME}.local ${STATIC_IP};

    ssl_certificate /etc/ssl/certs/owl.crt;
    ssl_certificate_key /etc/ssl/private/owl.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    # Root - informational page
    location = / {
        return 200 "<html><body><h1>OWL ${HOSTNAME}</h1><p>Video feed available at: <a href='/video_feed'>/video_feed</a></p><p>Mode: Networked</p><p>Controller: ${CONTROLLER_IP}</p></body></html>";
        add_header Content-Type text/html;
    }

    # Proxy for the MJPEG video stream from owl.py (Port 8001)
    location /video_feed {
        proxy_pass http://127.0.0.1:8001/stream.mjpg;
        proxy_set_header Host \$host;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
        chunked_transfer_encoding off;
    }

    # Single JPEG snapshot from owl.py (Port 8001)
    location /latest_frame.jpg {
        proxy_pass http://127.0.0.1:8001/latest_frame.jpg;
        proxy_set_header Host \$host;
        proxy_buffering off;
        proxy_cache off;
    }
}
EOF
fi

# Enable the site
sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -sf /etc/nginx/sites-available/owl-dash /etc/nginx/sites-enabled/owl-dash

# Test nginx configuration
if sudo nginx -t; then
    echo -e "${TICK} Nginx configuration is valid"
else
    echo -e "${CROSS} Nginx configuration test failed"
fi

check_status "Nginx configuration" "NGINX_CONFIG"

# Step 9: Configure Avahi for .local domain
echo -e "${GREEN}[INFO] Configuring Avahi for .local domain resolution...${NC}"

# Ensure directory exists
sudo mkdir -p /etc/avahi/services

AVAHI_NAME="OWL ${OWL_ID}"
if [[ -z "$OWL_ID" ]]; then
    AVAHI_NAME="OpenWeedLocator"
fi

# Start file
sudo tee /etc/avahi/services/owl-dash.service > /dev/null <<EOF
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
    <name replace-wildcards="yes">${AVAHI_NAME}</name>
    <service>
        <type>_http._tcp</type>
        <port>80</port>
    </service>
    <service>
        <type>_https._tcp</type>
        <port>443</port>
    </service>
EOF

# ONLY add the MQTT service if we are in standalone mode
if [[ "$OWL_MODE" == "standalone" ]]; then
sudo tee -a /etc/avahi/services/owl-dash.service > /dev/null <<EOF
    <service>
        <type>_mqtt._tcp</type>
        <port>1883</port>
    </service>
EOF
fi

# Close the service-group
sudo tee -a /etc/avahi/services/owl-dash.service > /dev/null <<EOF
</service-group>
EOF
sudo systemctl restart avahi-daemon
check_status "Avahi service configuration" "AVAHI_CONFIG"

# Step 10: Start and enable services
echo -e "${GREEN}[INFO] Starting and enabling services...${NC}"

if [[ "$OWL_MODE" == "standalone" ]]; then
    sudo systemctl enable avahi-daemon mosquitto nginx
    sudo systemctl start avahi-daemon
    sudo systemctl restart mosquitto
    sudo systemctl restart nginx
else
    sudo systemctl enable avahi-daemon nginx
    sudo systemctl start avahi-daemon
    sudo systemctl restart nginx
fi

check_status "Starting services" "SERVICES"

# Step 11: Create systemd service for OWL Dashboard
if [[ "$OWL_MODE" == "standalone" ]]; then
    echo -e "${GREEN}[INFO] Creating systemd service for OWL Dashboard...${NC}"

    STANDALONE_DIR="/home/${CURRENT_USER}/owl/controller/standalone"
    VENV_BIN="/home/${CURRENT_USER}/.virtualenvs/owl/bin"

    sudo tee /etc/systemd/system/owl-dash.service > /dev/null <<EOF
[Unit]
Description=OWL Dashboard Service
After=network-online.target mosquitto.service
Wants=network-online.target
Requires=mosquitto.service

[Service]
Type=simple
User=${CURRENT_USER}
Group=$(id -g -n ${CURRENT_USER})
WorkingDirectory=${STANDALONE_DIR}
Environment="PATH=${VENV_BIN}:/usr/local/bin:/usr/bin:/bin"
ExecStart=${VENV_BIN}/gunicorn --bind 127.0.0.1:8000 --workers 1 --threads 8 --timeout 300 standalone:app
Restart=always
RestartSec=3
KillMode=mixed
KillSignal=SIGINT
TimeoutStopSec=5

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable owl-dash
    sudo systemctl start owl-dash

    # Give the service a moment to start
    sleep 3

    # Check if the service started successfully
    if systemctl is-active --quiet owl-dash; then
        echo -e "${TICK} OWL Dashboard service started successfully"
        check_status "Creating and starting dashboard service" "SERVICES"
    else
        echo -e "${CROSS} OWL Dashboard service failed to start"
        echo -e "${ORANGE}[INFO] Checking service logs...${NC}"
        systemctl status owl-dash --no-pager -l
        check_status "Creating and starting dashboard service" "SERVICES"
    fi

else
    echo -e "${GREEN}[INFO] Skipping dashboard service creation (networked mode)${NC}"
fi

# Step 12: Update OWL config files with network settings
update_owl_configs
check_status "Updating OWL config files" "OWL_CONFIG"

# Step 13: Create configuration summary file
echo -e "${GREEN}[INFO] Creating configuration summary...${NC}"

if [[ "$OWL_MODE" == "standalone" ]]; then
    sudo tee /opt/owl-dash-config.txt > /dev/null <<EOF
OWL Dashboard Configuration
==========================
Mode: Standalone
OWL ID: ${OWL_ID}
SSID: ${SSID}
WiFi Password: ${WIFI_PASSWORD}
IP Address: 10.42.0.1/24
Hostname: ${HOSTNAME}

Access URLs:
- https://${HOSTNAME}.local/
- https://10.42.0.1/

MQTT Configuration:
- Broker: localhost:1883 (local broker)
- Network Access: 10.42.0.1:1883
- Config: /etc/mosquitto/conf.d/owl.conf
- Main Config: /etc/mosquitto/mosquitto.conf
- Log: /var/log/mosquitto/mosquitto.log

SSL Certificate: /etc/ssl/certs/owl.crt
SSL Private Key: /etc/ssl/private/owl.key
Nginx Config: /etc/nginx/sites-available/owl-dash
Avahi Service: /etc/avahi/services/owl-dash.service
Dashboard Service: /etc/systemd/system/owl-dash.service

Testing Commands:
- mosquitto_pub -h localhost -t "test/message" -m "Hello World"
- mosquitto_sub -h localhost -t "test/message"
- curl -k https://localhost/
- systemctl status owl-dash

Generated: $(date)
EOF
else
    sudo tee /opt/owl-dash-config.txt > /dev/null <<EOF
OWL Configuration
================
Mode: Networked
OWL ID: ${OWL_ID}
Hostname: ${HOSTNAME}
Connection: ${NET_INTERFACE:-wifi} ${SSID:+(${SSID})}
Static IP: ${STATIC_IP}
Gateway: ${GATEWAY_IP}

Controller Configuration:
- Controller IP: ${CONTROLLER_IP}
- MQTT Broker: ${CONTROLLER_IP}:1883 (remote)

Access:
- Video Feed: https://${HOSTNAME}.local/video_feed
- Video Feed: https://${STATIC_IP}/video_feed
- SSH: ssh owl@${HOSTNAME}.local or ssh owl@${STATIC_IP}

SSL Certificate: /etc/ssl/certs/owl.crt
SSL Private Key: /etc/ssl/private/owl.key
Nginx Config: /etc/nginx/sites-available/owl-dash
Avahi Service: /etc/avahi/services/owl-dash.service

Testing Commands:
- mosquitto_pub -h ${CONTROLLER_IP} -t "test/message" -m "Hello World"
- mosquitto_sub -h ${CONTROLLER_IP} -t "owl/#"
- curl -k https://localhost/video_feed
- ping ${CONTROLLER_IP}

Generated: $(date)
EOF
fi

sudo chmod 644 /opt/owl-dash-config.txt

# Step 14: Final system validation
echo -e "${GREEN}[INFO] Performing final system validation...${NC}"

if [[ "$OWL_MODE" == "standalone" ]]; then
    final_mqtt_validation

    # Test dashboard service
    echo -e "${GREEN}[INFO] Testing dashboard service...${NC}"
    if curl -k -s https://localhost/ > /dev/null 2>&1; then
        echo -e "${TICK} Dashboard service is responding"
    else
        echo -e "${ORANGE}[WARNING] Dashboard service may not be ready yet${NC}"
    fi
else
    # Test network connectivity
    echo -e "${GREEN}[INFO] Testing network connectivity...${NC}"
    if ping -c 1 ${CONTROLLER_IP} > /dev/null 2>&1; then
        echo -e "${TICK} Can reach controller at ${CONTROLLER_IP}"
    else
        echo -e "${ORANGE}[WARNING] Cannot reach controller - check network${NC}"
    fi
fi

# Final Summary
echo -e "\n${GREEN}[INFO] OWL Setup Summary:${NC}"
echo -e "$STATUS_PACKAGES System Packages"
echo -e "$STATUS_MQTT_BROKER MQTT Configuration"
echo -e "$STATUS_WIFI_CONFIG Network Configuration"
echo -e "$STATUS_HOSTNAME Hostname Configuration"
echo -e "$STATUS_UFW_CONFIG UFW Firewall Configuration"
echo -e "$STATUS_FAN_PERMISSIONS Fan Control Permissions"
echo -e "$STATUS_SERVICE_PERMISSIONS Service Control Permissions"
echo -e "$STATUS_NGINX_CONFIG Nginx Configuration"
echo -e "$STATUS_SSL_CERT SSL Certificate Generation"
echo -e "$STATUS_AVAHI_CONFIG Avahi Service Configuration"
echo -e "$STATUS_SERVICES Service Management"
echo -e "$STATUS_OWL_CONFIG OWL Config Files"

if [[ "$OWL_MODE" == "standalone" ]]; then
    echo -e "\n${GREEN}[INFO] Standalone Mode - Dashboard Access:${NC}"
    echo -e "  SSID: ${SSID}"
    echo -e "  Password: [HIDDEN]"
    echo -e "  URLs: https://${HOSTNAME}.local/ or https://10.42.0.1/"
    echo -e "  MQTT: localhost:1883"
    echo -e "  Configuration: /opt/owl-dash-config.txt"

    echo -e "\n${GREEN}[INFO] Testing Commands:${NC}"
    echo -e "  mosquitto_pub -h localhost -t 'owl/test' -m 'hello'"
    echo -e "  mosquitto_pub -h 10.42.0.1 -t 'owl/test' -m 'hello'"
    echo -e "  mosquitto_sub -h localhost -t 'owl/#'"
    echo -e "  mosquitto_sub -h 10.42.0.1 -t 'owl/#'"
    echo -e "  systemctl status owl-dash mosquitto"
    echo -e "  journalctl -u owl-dash -f"
else
    echo -e "\n${GREEN}[INFO] Networked Mode - Access Information:${NC}"
    echo -e "  Hostname: ${HOSTNAME}"
    echo -e "  Static IP: ${STATIC_IP}"
    if [[ "$NET_INTERFACE" == "wifi" ]]; then
        echo -e "  WiFi Network: ${SSID}"
    else
        echo -e "  Connection: Ethernet (eth0)"
    fi
    echo -e "  Controller: ${CONTROLLER_IP}"
    echo -e "  Video Feed: https://${HOSTNAME}.local/video_feed"
    echo -e "  Configuration: /opt/owl-dash-config.txt"

    echo -e "\n${GREEN}[INFO] Testing Commands:${NC}"
    echo -e "  mosquitto_pub -h ${CONTROLLER_IP} -t 'owl/test' -m 'hello'"
    echo -e "  mosquitto_sub -h ${CONTROLLER_IP} -t 'owl/#'"
    echo -e "  curl -k https://localhost/video_feed"
    echo -e "  ping ${CONTROLLER_IP}"
    echo -e "  ssh owl@${HOSTNAME}.local"
fi

# Config files status
if [[ "$STATUS_OWL_CONFIG" == "${TICK}" ]]; then
    echo -e "\n${GREEN}[INFO] OWL config files updated automatically:${NC}"
    if [[ "$OWL_MODE" == "standalone" ]]; then
        echo -e "  [MQTT] enable=True, broker_ip=localhost, device_id=${HOSTNAME}"
        echo -e "  [Network] mode=standalone, static_ip=10.42.0.1"
    else
        echo -e "  [MQTT] enable=True, broker_ip=${CONTROLLER_IP}, device_id=${HOSTNAME}"
        echo -e "  [Network] mode=networked, static_ip=${STATIC_IP}, controller_ip=${CONTROLLER_IP}"
    fi
else
    echo -e "\n${ORANGE}[WARNING] Config files could not be updated automatically.${NC}"
    echo -e "${ORANGE}[WARNING] Manually set these in your config INI files:${NC}"
    if [[ "$OWL_MODE" == "standalone" ]]; then
        echo -e "  [MQTT] enable=True, broker_ip=localhost, broker_port=1883, device_id=${HOSTNAME}"
    else
        echo -e "  [MQTT] enable=True, broker_ip=${CONTROLLER_IP}, broker_port=1883, device_id=${HOSTNAME}"
        echo -e "  [Network] mode=networked, static_ip=${STATIC_IP}, controller_ip=${CONTROLLER_IP}"
    fi
fi

if [[ "$STATUS_PACKAGES" == "${TICK}" && "$STATUS_MQTT_BROKER" == "${TICK}" && "$STATUS_WIFI_CONFIG" == "${TICK}" && "$STATUS_HOSTNAME" == "${TICK}" && "$STATUS_UFW_CONFIG" == "${TICK}" && "$STATUS_NGINX_CONFIG" == "${TICK}" && "$STATUS_SSL_CERT" == "${TICK}" && "$STATUS_AVAHI_CONFIG" == "${TICK}" && "$STATUS_SERVICES" == "${TICK}" && "$STATUS_FAN_PERMISSIONS" == "${TICK}" && "$STATUS_SERVICE_PERMISSIONS" == "${TICK}" && "$STATUS_OWL_CONFIG" == "${TICK}" ]]; then
    echo -e "\n${GREEN}[COMPLETE] OWL setup completed successfully!${NC}"

    echo -e "\n${GREEN}[INFO] Setup Complete - Reboot Recommended${NC}"
    echo -e "${GREEN}======================================${NC}"
    echo -e "A reboot is recommended to ensure all services start properly."
    echo -e ""

    if [[ "$OWL_MODE" == "standalone" ]]; then
        echo -e "After reboot:"
        echo -e "  • WiFi hotspot '${SSID}' will be active"
        echo -e "  • Dashboard will be available at https://${HOSTNAME}.local/"
        echo -e "  • MQTT broker will be running on port 1883"
        echo -e "  • owl.py and dashboard will launch if enabled"
    else
        echo -e "After reboot:"
        if [[ "$NET_INTERFACE" == "wifi" ]]; then
            echo -e "  • OWL will connect to WiFi '${SSID}'"
        else
            echo -e "  • OWL will use Ethernet (eth0) with static IP ${STATIC_IP}"
        fi
        echo -e "  • Video feed will be at https://${HOSTNAME}.local/video_feed"
        echo -e "  • MQTT will connect to ${CONTROLLER_IP}:1883"
        echo -e "  • owl.py will launch if enabled"
    fi

    echo -e ""
    read -p "Reboot now? (y/n): " reboot_choice

    if [[ "$reboot_choice" =~ ^[Yy]$ ]]; then
        echo -e "${GREEN}[INFO] Rebooting system...${NC}"
        sudo reboot
    else
        echo -e "${GREEN}[INFO] Reboot skipped. Remember to reboot later for full functionality.${NC}"
        echo -e "${GREEN}[INFO] You can reboot manually with: sudo reboot${NC}"
    fi

else
    echo -e "\n${RED}[ERROR] Some components failed to install. Check the status above.${NC}"

    # Show errors
    if [[ -n "$ERROR_PACKAGES" ]]; then echo -e "${RED}[ERROR] Packages: $ERROR_PACKAGES${NC}"; fi
    if [[ -n "$ERROR_MQTT_BROKER" ]]; then echo -e "${RED}[ERROR] MQTT Broker: $ERROR_MQTT_BROKER${NC}"; fi
    if [[ -n "$ERROR_WIFI_CONFIG" ]]; then echo -e "${RED}[ERROR] WiFi Config: $ERROR_WIFI_CONFIG${NC}"; fi
    if [[ -n "$ERROR_HOSTNAME" ]]; then echo -e "${RED}[ERROR] Hostname: $ERROR_HOSTNAME${NC}"; fi
    if [[ -n "$ERROR_UFW_CONFIG" ]]; then echo -e "${RED}[ERROR] UFW Config: $ERROR_UFW_CONFIG${NC}"; fi
    if [[ -n "$ERROR_FAN_PERMISSIONS" ]]; then echo -e "${RED}[ERROR] Fan Permissions: $ERROR_FAN_PERMISSIONS${NC}"; fi
    if [[ -n "$ERROR_SERVICE_PERMISSIONS" ]]; then echo -e "${RED}[ERROR] OWL Service Permissions: $ERROR_SERVICE_PERMISSIONS${NC}"; fi
    if [[ -n "$ERROR_NGINX_CONFIG" ]]; then echo -e "${RED}[ERROR] Nginx Config: $ERROR_NGINX_CONFIG${NC}"; fi
    if [[ -n "$ERROR_SSL_CERT" ]]; then echo -e "${RED}[ERROR] SSL Cert: $ERROR_SSL_CERT${NC}"; fi
    if [[ -n "$ERROR_AVAHI_CONFIG" ]]; then echo -e "${RED}[ERROR] Avahi Config: $ERROR_AVAHI_CONFIG${NC}"; fi
    if [[ -n "$ERROR_SERVICES" ]]; then echo -e "${RED}[ERROR] Services: $ERROR_SERVICES${NC}"; fi
    if [[ -n "$ERROR_OWL_CONFIG" ]]; then echo -e "${RED}[ERROR] OWL Config: $ERROR_OWL_CONFIG${NC}"; fi

    echo -e "\n${RED}[ERROR] Please fix the above issues before rebooting.${NC}"
    exit 1
fi