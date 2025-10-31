#!/bin/bash

# OWL Central Controller Setup Script
# This script sets up a Raspberry Pi as the central controller for multiple OWLs
# It configures network, MQTT broker, dashboard, and kiosk mode

# Define colors for status messages
RED='\033[0;31m'    # Red for ERROR messages
ORANGE='\033[0;33m' # Orange for warnings
GREEN='\033[0;32m'  # Green for INFO and success messages
NC='\033[0m'        # No color (reset)
TICK="${GREEN}[OK]${NC}"
CROSS="${RED}[FAIL]${NC}"

SCRIPT_DIR=$(dirname "$(realpath "$0")")

# It will use 'sudo' for commands that need root permissions.
if [ "$EUID" -eq 0 ]; then
   echo -e "${RED}[ERROR] This script must NOT be run with sudo.${NC}"
   echo -e "${RED}[ERROR] Please run: ./controller_setup.sh${NC}"
   exit 1
fi
CURRENT_USER=$(whoami)
VENV_PATH="${SCRIPT_DIR}/.venv"

echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}    OWL Central Controller Setup${NC}"
echo -e "${GREEN}============================================${NC}"
echo -e ""
echo -e "This script will configure this Raspberry Pi as the"
echo -e "central controller for multiple OWL units."
echo -e ""
echo -e "The controller will:"
echo -e "  • Install all software"
echo -e "  • Create a Python virtual environment"
echo -e "  • Configure Nginx, MQTT, and Kiosk mode"
echo -e "  • Finally, switch WiFi and set a static IP"
echo -e ""

# Initialize status tracking variables
STATUS_PACKAGES=""
STATUS_WIFI_CONNECT=""
STATUS_STATIC_IP=""
STATUS_MQTT_BROKER=""
STATUS_PYTHON_VENV=""
STATUS_NGINX_CONFIG=""
STATUS_SSL_CERT=""
STATUS_AVAHI_CONFIG=""
STATUS_DASHBOARD_SERVICE=""
STATUS_KIOSK_MODE=""
STATUS_UFW_CONFIG=""

# Function to check the exit status
check_status() {
  if [ $? -ne 0 ]; then
    echo -e "${CROSS} $1 failed."
    eval "STATUS_$2='${CROSS}'"
    return 1
  else
    echo -e "${TICK} $1 completed successfully."
    eval "STATUS_$2='${TICK}'"
  fi
}

# Masked password input function
read_password_masked() {
    local prompt="$1"
    local password=""
    local char
    printf "%s" "$prompt"
    while IFS= read -r -s -n1 char; do
        if [ -z "$char" ]; then break; fi
        case "$char" in
            $'\n'|$'\r'|$'\x0a'|$'\x0d') break ;;
            $'\177'|$'\b')
                if [ -n "$password" ]; then
                    password=${password%?}
                    printf '\b \b'
                fi ;;
            *)
                password+="$char"
                printf '*' ;;
        esac
    done
    printf '\n'
    REPLY="$password"
}

# --- SCRIPT FUNCTIONS ---

# Step 1: Collect configuration
collect_user_input() {
    echo -e "${GREEN}[INFO] Controller Configuration${NC}"
    echo -e "${GREEN}=======================================${NC}"

    read -p "Enter your WiFi network name (SSID): " WIFI_SSID
    while true; do
        read_password_masked "Enter WiFi password: "
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

    read -p "Enter controller static IP (default: 192.168.1.2): " STATIC_IP
    STATIC_IP=${STATIC_IP:-192.168.1.2}
    read -p "Enter router/gateway IP (default: 192.168.1.1): " GATEWAY_IP
    GATEWAY_IP=${GATEWAY_IP:-192.168.1.1}
    read -p "Enter hostname (default: owl-controller): " HOSTNAME
    HOSTNAME=${HOSTNAME:-owl-controller}
    read -p "Enable kiosk mode on boot? (y/n, default: y): " KIOSK_MODE
    KIOSK_MODE=${KIOSK_MODE:-y}

    echo -e ""
    echo -e "${GREEN}[INFO] Configuration Summary:${NC}"
    echo -e "WiFi SSID: ${WIFI_SSID}"
    echo -e "Static IP: ${STATIC_IP}"
    echo -e "Gateway: ${GATEWAY_IP}"
    echo -e "Hostname: ${HOSTNAME}"
    echo -e "Kiosk Mode: ${KIOSK_MODE}"
    echo -e "MQTT Broker: ${STATIC_IP}:1883"
    echo -e "Dashboard URL: https://${HOSTNAME}.local/"
    echo -e ""
    read -p "Continue with these settings? (y/n): " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        echo -e "${RED}[ERROR] Setup cancelled by user.${NC}"
        exit 1
    fi
}

# [Action]: Get sudo permissions once at the start
prompt_for_sudo() {
    echo -e "${GREEN}[INFO] This script needs to run some commands as root.${NC}"
    echo -e "Please enter your password for '$CURRENT_USER' to grant sudo permissions."
    if ! sudo -v; then
        echo -e "${RED}[ERROR] Failed to get sudo permissions.${NC}"
        exit 1
    fi
    # Keep sudo session alive
    ( while true; do sudo -n true; sleep 60; done ) &
    SUDO_KEEPALIVE_PID=$!
}

# Step 2: Install all system packages
install_system_packages() {
    echo -e "${GREEN}[INFO] Updating and installing system packages...${NC}"
    sudo apt-get update
    sudo apt-get install -y \
        mosquitto mosquitto-clients \
        nginx \
        ufw \
        openssl \
        avahi-daemon \
        net-tools \
        python3-pip \
        chromium-browser \
        xserver-xorg xinit \
        unclutter \
        lightdm openbox \
        network-manager \
        python3-venv python3-full # [Action]: Added for venv and PEP 668

    check_status "Installing system packages" "PACKAGES"
}

# Step 3: Setup Python virtual environment
setup_python_venv() {
    echo -e "${GREEN}[INFO] Setting up Python virtual environment...${NC}"

    # [Action]: Create venv using modern 'python3 -m venv'
    # This runs as the CURRENT_USER, no sudo needed
    python3 -m venv "${VENV_PATH}"
    check_status "Creating virtual environment" "PYTHON_VENV"

    echo -e "${GREEN}[INFO] Installing Python dependencies into venv...${NC}"
    # [Action]: Activate venv and install packages. No sudo needed.
    # This respects PEP 668 and solves the "externally-managed-environment" error
    source "${VENV_PATH}/bin/activate"
    pip install --upgrade pip
    pip install \
        flask==2.2.2 \
        gunicorn==23.0.0 \
        paho-mqtt==2.1.0 \
        psutil==5.9.4
    deactivate

    check_status "Installing Python dependencies" "PYTHON_VENV"
}

# Step 4: Configure all services (files only)
configure_system_services() {
    echo -e "${GREEN}[INFO] Configuring system services...${NC}"

    # --- Hostname ---
    echo -e "${GREEN}[INFO] Setting hostname to ${HOSTNAME}...${NC}"
    sudo hostnamectl set-hostname "${HOSTNAME}"
    sudo sed -i "/${HOSTNAME}/d" /etc/hosts
    echo "127.0.0.1 ${HOSTNAME}" | sudo tee -a /etc/hosts
    echo "127.0.1.1 ${HOSTNAME}" | sudo tee -a /etc/hosts

    # --- MQTT Broker ---
    sudo tee /etc/mosquitto/conf.d/owl_controller.conf > /dev/null <<EOF
listener 1883 0.0.0.0
allow_anonymous true
log_dest file /var/log/mosquitto/mosquitto.log
log_type all
connection_messages true
log_timestamp true
EOF
    check_status "MQTT broker config" "MQTT_BROKER"

    # --- SSL Cert ---
    sudo mkdir -p /etc/ssl/private
    sudo mkdir -p /etc/ssl/certs
    sudo openssl req -x509 -nodes -days 3650 \
        -newkey rsa:2048 \
        -keyout /etc/ssl/private/owl-controller.key \
        -out /etc/ssl/certs/owl-controller.crt \
        -subj "/CN=${HOSTNAME}.local/C=US/ST=State/L=City/O=OWL/OU=Controller"
    sudo chmod 600 /etc/ssl/private/owl-controller.key
    check_status "SSL certificate" "SSL_CERT"

    # --- Nginx ---
    sudo rm -f /etc/nginx/sites-enabled/default
    sudo tee /etc/nginx/sites-available/owl-controller > /dev/null <<EOF
server {
    listen 80;
    server_name ${HOSTNAME}.local ${STATIC_IP} _;
    return 301 https://\$host\$request_uri;
}
server {
    listen 443 ssl;
    server_name ${HOSTNAME}.local ${STATIC_IP} _;
    ssl_certificate     /etc/ssl/certs/owl-controller.crt;
    ssl_certificate_key /etc/ssl/private/owl-controller.key;
    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
    }
}
EOF
    sudo ln -sf /etc/nginx/sites-available/owl-controller /etc/nginx/sites-enabled/
    check_status "Nginx config" "NGINX_CONFIG"

    # --- Avahi ---
    sudo tee /etc/avahi/services/owl-controller.service > /dev/null <<EOF
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
    <name replace-wildcards="yes">OWL Controller</name>
    <service><type>_https._tcp</type><port>443</port></service>
</service-group>
EOF
    check_status "Avahi config" "AVAHI_CONFIG"

    # --- UFW ---
    sudo ufw --force reset
    sudo ufw default deny incoming
    sudo ufw default allow outgoing
    sudo ufw allow 22/tcp     # SSH
    sudo ufw allow 80/tcp     # HTTP (redirect)
    sudo ufw allow 443/tcp    # HTTPS
    sudo ufw allow 1883/tcp   # MQTT
    check_status "UFW config" "UFW_CONFIG"

    # --- Systemd Service ---
    # [Action]: This now uses the local .venv
    sudo tee /etc/systemd/system/owl-controller.service > /dev/null <<EOF
[Unit]
Description=OWL Central Controller Dashboard
After=network.target mosquitto.service
Requires=mosquitto.service

[Service]
Type=simple
User=${CURRENT_USER}
Group=$(id -g -n ${CURRENT_USER})
WorkingDirectory=${SCRIPT_DIR}
Environment="PATH=${VENV_PATH}/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=${VENV_PATH}/bin/gunicorn --bind 127.0.0.1:8000 --workers 2 --timeout 300 networked_controller:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    check_status "Systemd service config" "DASHBOARD_SERVICE"
}

# Step 5: Configure Kiosk Mode
configure_kiosk_mode() {
    if [[ ! "$KIOSK_MODE" =~ ^[Yy]$ ]]; then
        echo -e "${ORANGE}[INFO] Skipping kiosk mode setup.${NC}"
        STATUS_KIOSK_MODE="SKIPPED"
        return 0
    fi

    echo -e "${GREEN}[INFO] Setting up kiosk mode...${NC}"
    # These files are created as the user, no sudo needed
    mkdir -p /home/$CURRENT_USER/.config/autostart
    tee /home/$CURRENT_USER/start-kiosk.sh > /dev/null <<EOF
#!/bin/bash
xset s noblank
xset s off
xset -dpms
unclutter -idle 3 -root &
sleep 15
chromium-browser \\
    --kiosk \\
    --ignore-certificate-errors \\
    --noerrdialogs \\
    --disable-infobars \\
    https://localhost/
EOF
    chmod +x /home/$CURRENT_USER/start-kiosk.sh

    tee /home/$CURRENT_USER/.config/autostart/owl-kiosk.desktop > /dev/null <<EOF
[Desktop Entry]
Type=Application
Name=OWL Controller Kiosk
Exec=/home/$CURRENT_USER/start-kiosk.sh
Hidden=false
X-GNOME-Autostart-enabled=true
EOF

    # This file edit needs sudo
    if [ -f /etc/lightdm/lightdm.conf ]; then
        sudo sed -i -e "s/^\\(#\\|\\)autologin-user=.*/autologin-user=$CURRENT_USER/" /etc/lightdm/lightdm.conf
        sudo sed -i -e "s/^\\(#\\|\\)autologin-session=.*/autologin-session=openbox/" /etc/lightdm/lightdm.conf
    fi
    check_status "Kiosk mode config" "KIOSK_MODE"
}

# Step 6: Switch Network and Start Services
switch_network_and_finish() {
    echo -e "\n${ORANGE}================================================================${NC}"
    echo -e "${ORANGE}[WARNING] FINAL STEP: NETWORK SWITCH${NC}"
    echo -e "The script will now disconnect from the current network"
    echo -e "and attempt to connect to '${WIFI_SSID}' with static IP ${STATIC_IP}."
    echo -e "${RED}YOU WILL BE DISCONNECTED FROM SSH if connected via WiFi.${NC}"
    echo -e "${ORANGE}The script will continue to run and start all services.${NC}"
    echo -e "Please reconnect to the Pi at its new IP ${STATIC_IP} after a minute."
    read -p "Press Enter to continue, or Ctrl+C to abort... "
    echo -e "${ORANGE}================================================================${NC}\n"

    # [Action]: Robust NMCLI network switching
    echo -e "${GREEN}[INFO] Configuring WiFi: ${WIFI_SSID}...${NC}"
    # We must run all nmcli commands with sudo

    # 1. Rescan for networks
    sudo nmcli dev wifi rescan

    # 2. Delete any old connection with this name to avoid conflicts
    sudo nmcli con delete "${WIFI_SSID}" 2>/dev/null || true

    # 3. Add a new connection with a known name
    sudo nmcli con add type wifi con-name "${WIFI_SSID}" ifname wlan0 ssid "${WIFI_SSID}"

    # 4. Configure the connection
    sudo nmcli con modify "${WIFI_SSID}" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "${WIFI_PASSWORD}"
    sudo nmcli con modify "${WIFI_SSID}" ipv4.addresses ${STATIC_IP}/24
    sudo nmcli con modify "${WIFI_SSID}" ipv4.gateway ${GATEWAY_IP}
    sudo nmcli con modify "${WIFI_SSID}" ipv4.dns "8.8.8.8 8.8.4.4"
    sudo nmcli con modify "${WIFI_SSID}" ipv4.method manual
    sudo nmcli con modify "${WIFI_SSID}" connection.autoconnect yes

    # 5. Bring up the connection
    sudo nmcli con up "${WIFI_SSID}"
    check_status "WiFi connection (${WIFI_SSID})" "WIFI_CONNECT"
    check_status "Static IP setup (${STATIC_IP})" "STATIC_IP"

    echo -e "${GREEN}[INFO] Starting all services...${NC}"
    # [Action]: Start all services *after* network is set
    sudo systemctl enable mosquitto nginx avahi-daemon owl-controller
    sudo systemctl restart mosquitto
    sudo systemctl restart nginx
    sudo systemctl restart avahi-daemon
    sudo systemctl start owl-controller.service

    sudo ufw --force enable

    # Clean up sudo keepalive
    kill $SUDO_KEEPALIVE_PID 2>/dev/null || true
}

# --- Main script execution ---

# 1. Get all user settings
collect_user_input

# 2. Get sudo timestamp at the beginning
prompt_for_sudo

# 3. Install all packages (needs internet)
install_system_packages

# 4. Set up Python (needs internet)
setup_python_venv

# 5. Write all config files
configure_system_services

# 6. Set up Kiosk files
configure_kiosk_mode

# 7. Disconnect from current WiFi and start services
switch_network_and_finish

# --- Final Summary ---
echo -e "\n${GREEN}============================================${NC}"
echo -e "${GREEN}[INFO] OWL Controller Setup Summary:${NC}"
echo -e "${GREEN}============================================${NC}"
echo -e "$STATUS_PACKAGES System Packages"
echo -e "$STATUS_PYTHON_VENV Python Environment"
echo -e "$STATUS_MQTT_BROKER MQTT Broker Config"
echo -e "$STATUS_SSL_CERT SSL Certificate"
echo -e "$STATUS_NGINX_CONFIG Nginx Config"
echo -e "$STATUS_AVAHI_CONFIG Avahi (.local) Config"
echo -e "$STATUS_UFW_CONFIG Firewall Config"
echo -e "$STATUS_DASHBOARD_SERVICE Dashboard Service Config"
echo -e "$STATUS_KIOSK_MODE Kiosk Mode Config"
echo -e "$STATUS_WIFI_CONNECT WiFi Connection"
echo -e "$STATUS_STATIC_IP Static IP Setup"

echo -e "\n${GREEN}[INFO] Access Information:${NC}"
echo -e "  Controller IP: ${STATIC_IP}"
echo -e "  Dashboard URL: https://${HOSTNAME}.local/ (or https://${STATIC_IP}/)"
echo -e "  MQTT Broker: ${STATIC_IP}:1883"
echo -e "  Hostname: ${HOSTNAME}"

echo -e "\n${GREEN}[INFO] Next Steps:${NC}"
echo -e "1. Configure OWL units with network mode 'networked_owl'"
echo -e "2. Set controller_ip & mqtt_broker to ${STATIC_IP} in their config"
echo -e "3. OWLs will automatically connect to this controller"

if [[ "$STATUS_PACKAGES" == "${TICK}" && "$STATUS_PYTHON_VENV" == "${TICK}" && "$STATUS_WIFI_CONNECT" == "${TICK}" ]]; then
    echo -e "\n${GREEN}[COMPLETE] Controller setup finished.${NC}"
    echo -e "If you were disconnected, you should now be able to SSH to ${CURRENT_USER}@${STATIC_IP}"
    echo -e "A reboot is recommended to ensure all services start correctly."
else
    echo -e "\n${RED}[ERROR] Some components failed. Check the errors above.${NC}"
    echo -e "You may need to re-run the script."
    exit 1
fi

