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

# [Action]: SCRIPT_DIR is needed to find networked_controller.py
SCRIPT_DIR=$(dirname "$(realpath "$0")")
CURRENT_USER=${SUDO_USER:-$(whoami)}

if [ "$EUID" -ne 0 ]; then
   echo -e "${RED}[ERROR] This script must be run with sudo privileges.${NC}"
   echo -e "${RED}[ERROR] Please run: sudo ./controller_setup.sh${NC}"
   exit 1
fi

echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}    OWL Central Controller Setup${NC}"
echo -e "${GREEN}============================================${NC}"
echo -e ""
echo -e "This script will configure this Raspberry Pi as the"
echo -e "central controller for multiple OWL units."
echo -e ""
echo -e "It will:"
echo -e "  • Connect to your existing WiFi network"
echo -e "  • Set a static IP"
echo -e "  • Host the central MQTT broker"
echo -e "  • Run the central dashboard in kiosk mode"
echo -e ""

# Initialize status tracking variables
STATUS_PACKAGES=""
STATUS_WIFI_CONNECT=""
STATUS_STATIC_IP=""
STATUS_MQTT_BROKER=""
STATUS_PYTHON_DEPS=""
STATUS_NGINX_CONFIG=""
STATUS_SSL_CERT=""
STATUS_AVAHI_CONFIG=""
STATUS_DASHBOARD_SERVICE=""
STATUS_KIOSK_MODE=""
STATUS_UFW_CONFIG=""

ERROR_PACKAGES=""
ERROR_WIFI_CONNECT=""
ERROR_STATIC_IP=""
ERROR_MQTT_BROKER=""
ERROR_PYTHON_DEPS=""
ERROR_NGINX_CONFIG=""
ERROR_SSL_CERT=""
ERROR_AVAHI_CONFIG=""
ERROR_DASHBOARD_SERVICE=""
ERROR_KIOSK_MODE=""
ERROR_UFW_CONFIG=""

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

reload_bashrc() {
    if [ -f /home/$CURRENT_USER/.bashrc ]; then
        # We run this as the user to correctly source their environment
        sudo -u $CURRENT_USER /bin/bash -c "source /home/$CURRENT_USER/.bashrc"
        sleep 2
    fi
}

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

    # Test 3: Test local connection (localhost)
    echo -e "${GREEN}[INFO] Testing local MQTT connection...${NC}"
    if timeout 5 mosquitto_pub -h localhost -t "test/owl/setup" -m "test_local_controller" 2>/dev/null; then
        echo -e "${TICK} Local MQTT connection successful"
        mqtt_test_passed=true
    else
        echo -e "${CROSS} Local MQTT connection failed"
        return 1
    fi

    # Test 4: Test network connection (static IP)
    echo -e "${GREEN}[INFO] Testing network MQTT connection (${STATIC_IP})...${NC}"
    if ip addr show | grep -q "${STATIC_IP}"; then
        echo -e "${TICK} Static IP (${STATIC_IP}) is configured"
        if timeout 5 mosquitto_pub -h "${STATIC_IP}" -t "test/owl/setup" -m "test_network_controller" 2>/dev/null; then
            echo -e "${TICK} Network MQTT connection successful"
        else
            warnings+=("Network MQTT connection failed - check firewall or network")
            echo -e "${ORANGE}[WARNING] Network MQTT connection failed${NC}"
        fi
    else
        warnings+=("Static IP not yet configured - network MQTT will be available after reboot")
        echo -e "${ORANGE}[WARNING] Static IP not ready - network access will work after reboot${NC}"
    fi

    # Test 5: Check configuration file
    if [ -f "/etc/mosquitto/conf.d/owl_controller.conf" ]; then
        if grep -q "allow_anonymous true" /etc/mosquitto/conf.d/owl_controller.conf &&
           grep -q "listener 1883 0.0.0.0" /etc/mosquitto/conf.d/owl_controller.conf; then
            echo -e "${TICK} MQTT configuration file is correct"
        else
            echo -e "${CROSS} MQTT configuration file has issues"
            return 1
        fi
    else
        echo -e "${CROSS} MQTT configuration file missing"
        return 1
    fi

    if [ "$mqtt_test_passed" = true ]; then
        echo -e "${TICK} MQTT broker core functionality verified"
        if [ ${#warnings[@]} -gt 0 ];
            then
            echo -e "${ORANGE}[INFO] MQTT Setup Notes:${NC}"
            for warning in "${warnings[@]}"; do echo -e "${ORANGE}  • $warning${NC}"; done
        fi
        return 0
    else
        echo -e "${CROSS} MQTT broker failed core functionality tests"
        return 1
    fi
}

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
    echo -e "  WiFi SSID: ${WIFI_SSID}"
    echo -e "  Static IP: ${STATIC_IP}"
    echo -e "  Gateway: ${GATEWAY_IP}"
    echo -e "  Hostname: ${HOSTNAME}"
    echo -e "  Kiosk Mode: ${KIOSK_MODE}"
    echo -e "  MQTT Broker: ${STATIC_IP}:1883"
    echo -e "  Dashboard URL: https://${HOSTNAME}.local/"
    echo -e ""
    read -p "Continue with these settings? (y/n): " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        echo -e "${RED}[ERROR] Setup cancelled by user.${NC}"
        exit 1
    fi
}

collect_user_input

# Step 2: Update system packages
echo -e "${GREEN}[INFO] Updating system packages...${NC}"
apt-get update
apt-get install -y \
    mosquitto mosquitto-clients \
    nginx \
    ufw \
    openssl \
    avahi-daemon \
    net-tools \
    python3-pip python3-venv python3-virtualenv \
    chromium-browser \
    xserver-xorg xinit \
    unclutter \
    lightdm openbox \
    network-manager
check_status "Installing packages" "PACKAGES"

# Step 3: Set hostname
echo -e "${GREEN}[INFO] Setting hostname to ${HOSTNAME}...${NC}"
hostnamectl set-hostname "${HOSTNAME}"

sed -i "/${HOSTNAME}/d" /etc/hosts
echo "127.0.0.1 ${HOSTNAME}" | tee -a /etc/hosts
echo "127.0.1.1 ${HOSTNAME}" | tee -a /etc/hosts
check_status "Setting hostname" "STATIC_IP" # Group with static IP status

# Step 4: Connect to WiFi network
echo -e "${GREEN}[INFO] Connecting to WiFi network: ${WIFI_SSID}...${NC}"
# Use NetworkManager to connect to WiFi
nmcli dev wifi connect "${WIFI_SSID}" password "${WIFI_PASSWORD}" 2>/dev/null || {
    echo -e "${ORANGE}[INFO] Connection may already exist. Attempting to modify.${NC}"
    # If connection exists, modify it
    nmcli con delete "${WIFI_SSID}" 2>/dev/null
    nmcli dev wifi connect "${WIFI_SSID}" password "${WIFI_PASSWORD}"
}
check_status "Connecting to WiFi" "WIFI_CONNECT"

# Step 5: Configure static IP
echo -e "${GREEN}[INFO] Configuring static IP: ${STATIC_IP}...${NC}"
CONNECTION_NAME="${WIFI_SSID}"
if [ -n "$CONNECTION_NAME" ]; then
    nmcli con mod "$CONNECTION_NAME" ipv4.addresses ${STATIC_IP}/24
    nmcli con mod "$CONNECTION_NAME" ipv4.gateway ${GATEWAY_IP}
    nmcli con mod "$CONNECTION_NAME" ipv4.dns "8.8.8.8 8.8.4.4"
    nmcli con mod "$CONNECTION_NAME" ipv4.method manual
    nmcli con down "$CONNECTION_NAME"
    nmcli con up "$CONNECTION_NAME"
    check_status "Setting static IP" "STATIC_IP"
else
    echo -e "${RED}[ERROR] Could not find connection named '${WIFI_SSID}'${NC}"
    STATUS_STATIC_IP="${CROSS}"
    ERROR_STATIC_IP="No active connection found"
fi
echo -e "${GREEN}[INFO] Waiting for network to stabilize...${NC}"
sleep 5

# Step 6: Configure MQTT broker for network access
echo -e "${GREEN}[INFO] Configuring MQTT broker...${NC}"
tee /etc/mosquitto/conf.d/owl_controller.conf > /dev/null <<EOF
# OWL Controller MQTT Configuration
listener 1883 0.0.0.0
allow_anonymous true
log_dest file /var/log/mosquitto/mosquitto.log
log_type error
log_type warning
log_type notice
log_type information
connection_messages true
log_timestamp true
EOF
systemctl enable mosquitto
systemctl restart mosquitto
sleep 2
test_mqtt_broker
check_status "Configuring and testing MQTT broker" "MQTT_BROKER"

# Step 7: Set up the virtual environment (using virtualenvwrapper)
echo -e "${GREEN}[INFO] Setting up virtualenvwrapper...${NC}"

# [Action]: Using your existing method from owl_setup.sh for consistency
if ! grep -q "virtualenv and virtualenvwrapper" /home/$CURRENT_USER/.bashrc; then
    cat >> /home/$CURRENT_USER/.bashrc << EOF
# virtualenv and virtualenvwrapper
export VIRTUALENVWRAPPER_PYTHON=/usr/bin/python3
export WORKON_HOME=\$HOME/.virtualenvs
source /usr/share/virtualenvwrapper/virtualenvwrapper.sh
EOF
fi

reload_bashrc
check_status "Configuring virtualenvwrapper in .bashrc" "PYTHON_DEPS"

# Source for the rest of this script to find mkvirtualenv
export VIRTUALENVWRAPPER_PYTHON=/usr/bin/python3
export WORKON_HOME=/home/$CURRENT_USER/.virtualenvs
# We need to source this as root for the script, but mkvirtualenv will be run as the user
source /usr/share/virtualenvwrapper/virtualenvwrapper.sh

# Step 8: Create virtual environment
echo -e "${GREEN}[INFO] Creating 'owl-controller-env' virtual environment...${NC}"

# We MUST run this as the user, not as root
# The 'bash -c' is required to load the newly-sourced functions
sudo -u $CURRENT_USER /bin/bash -c "source /usr/share/virtualenvwrapper/virtualenvwrapper.sh; mkvirtualenv -p python3 owl-controller-env"
check_status "Creating virtual environment" "PYTHON_DEPS"

# Step 9: Install Python dependencies
echo -e "${GREEN}[INFO] Installing Python dependencies...${NC}"

# [Action]: Install packages into the new venv. We must 'workon' it as the user.
sudo -u $CURRENT_USER /bin/bash -c "source /usr/share/virtualenvwrapper/virtualenvwrapper.sh; workon owl-controller-env; pip install --upgrade pip; pip install flask==2.2.2 gunicorn==23.0.0 paho-mqtt==2.1.0 psutil==5.9.4"
check_status "Installing Python dependencies" "PYTHON_DEPS"

# Step 10: Configure controller dashboard application
echo -e "${GREEN}[INFO] Configuring controller dashboard application permissions...${NC}"

chown -R $CURRENT_USER:$CURRENT_USER "${SCRIPT_DIR}"
check_status "Configuring controller application" "DASHBOARD_SERVICE"

# Step 9: Generate SSL certificates
echo -e "${GREEN}[INFO] Generating SSL certificates...${NC}"
mkdir -p /etc/ssl/private
mkdir -p /etc/ssl/certs
openssl req -x509 -nodes -days 3650 \
    -newkey rsa:2048 \
    -keyout /etc/ssl/private/owl-controller.key \
    -out /etc/ssl/certs/owl-controller.crt \
    -subj "/CN=${HOSTNAME}.local/C=US/ST=State/L=City/O=OWL/OU=Controller"
chmod 600 /etc/ssl/private/owl-controller.key
chmod 644 /etc/ssl/certs/owl-controller.crt
check_status "SSL certificate generation" "SSL_CERT"

# Step 10: Configure Nginx
echo -e "${GREEN}[INFO] Configuring Nginx...${NC}"
rm -f /etc/nginx/sites-enabled/default
tee /etc/nginx/sites-available/owl-controller > /dev/null <<EOF
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
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    # OWL Controller Dashboard Flask app (Port 8000)
    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;
    }
}
EOF
ln -sf /etc/nginx/sites-available/owl-controller /etc/nginx/sites-enabled/
nginx -t
check_status "Nginx configuration" "NGINX_CONFIG"

# Step 11: Configure Avahi for .local domain
echo -e "${GREEN}[INFO] Configuring Avahi for .local domain resolution...${NC}"
tee /etc/avahi/services/owl-controller.service > /dev/null <<EOF
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
    <name replace-wildcards="yes">OWL Controller</name>
    <service>
        <type>_http._tcp</type>
        <port>80</port>
    </service>
    <service>
        <type>_https._tcp</type>
        <port>443</port>
    </service>
</service-group>
EOF
check_status "Avahi service configuration" "AVAHI_CONFIG"

# Step 12: Create systemd service for dashboard
echo -e "${GREEN}[INFO] Creating systemd service for controller dashboard...${NC}"
tee /etc/systemd/system/owl-controller.service > /dev/null <<EOF
[Unit]
Description=OWL Central Controller Dashboard
After=network.target mosquitto.service
Requires=mosquitto.service

[Service]
Type=simple
User=$CURRENT_USER
Group=$(id -g -n $CURRENT_USER)
WorkingDirectory=/home/$CURRENT_USER/owl-controller
Environment="PATH=/home/$CURRENT_USER/.virtualenvs/owl-controller-env/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/home/$CURRENT_USER/.virtualenvs/owl-controller-env/bin/gunicorn --bind 127.0.0.1:8000 --workers 1 --timeout 300 networked_controller:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable owl-controller.service
systemctl start owl-controller.service
sleep 3
if systemctl is-active --quiet owl-controller.service; then
    echo -e "${TICK} Dashboard service started successfully"
    check_status "Creating dashboard service" "DASHBOARD_SERVICE"
else
    echo -e "${CROSS} Dashboard service failed to start"
    systemctl status owl-controller.service --no-pager -l
    check_status "Creating dashboard service" "DASHBOARD_SERVICE"
fi


# Step 13: Configure UFW firewall
echo -e "${GREEN}[INFO] Configuring firewall...${NC}"
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp     # SSH
ufw allow 80/tcp     # HTTP (for redirect)
ufw allow 443/tcp    # HTTPS
ufw allow 1883/tcp   # MQTT
ufw --force enable
ufw reload
check_status "Configuring firewall" "UFW_CONFIG"

# Step 14: Setup kiosk mode (if requested)
if [[ "$KIOSK_MODE" =~ ^[Yy]$ ]]; then
    echo -e "${GREEN}[INFO] Setting up kiosk mode...${NC}"
    mkdir -p /home/$CURRENT_USER/.config/autostart
    tee /home/$CURRENT_USER/start-kiosk.sh > /dev/null <<EOF
#!/bin/bash
# OWL Controller Kiosk Mode

xset s noblank
xset s off
xset -dpms
unclutter -idle 3 -root &
sleep 15 # Wait for network and services to be fully up

chromium-browser \\
    --kiosk \\
    --ignore-certificate-errors \\
    --noerrdialogs \\
    --disable-infobars \\
    --disable-translate \\
    --no-first-run \\
    --fast \\
    --fast-start \\
    --disable-features=TranslateUI \\
    --disk-cache-dir=/tmp/chromium-cache \\
    --overscroll-history-navigation=0 \\
    --disable-pinch \\
    https://localhost/
EOF
    chmod +x /home/$CURRENT_USER/start-kiosk.sh
    chown $CURRENT_USER:$CURRENT_USER /home/$CURRENT_USER/start-kiosk.sh

    tee /home/$CURRENT_USER/.config/autostart/owl-kiosk.desktop > /dev/null <<EOF
[Desktop Entry]
Type=Application
Name=OWL Controller Kiosk
Exec=/home/$CURRENT_USER/start-kiosk.sh
Hidden=false
X-GNOME-Autostart-enabled=true
EOF
    chown -R $CURRENT_USER:$CURRENT_USER /home/$CURRENT_USER/.config

    # [Action]: Configure lightdm to auto-login the user
    # This command finds the line starting with #autologin-user or autologin-user
    # and replaces it with the correct value.
    if [ -f /etc/lightdm/lightdm.conf ]; then
        sed -i -e "s/^\\(#\\|\\)autologin-user=.*/autologin-user=$CURRENT_USER/" /etc/lightdm/lightdm.conf
        sed -i -e "s/^\\(#\\|\\)autologin-session=.*/autologin-session=openbox/" /etc/lightdm/lightdm.conf
    else
        echo -e "${ORANGE}[WARNING] /etc/lightdm/lightdm.conf not found. Kiosk autologin may fail.${NC}"
    fi

    # [Action]: Removed the conflicting .bashrc 'startx' logic
    check_status "Setting up kiosk mode" "KIOSK_MODE"
fi

# Step 15: Final summary
echo -e "\n${GREEN}============================================${NC}"
echo -e "${GREEN}[INFO] OWL Controller Setup Summary:${NC}"
echo -e "${GREEN}============================================${NC}"
echo -e "$STATUS_PACKAGES System Packages"
echo -e "$STATUS_WIFI_CONNECT WiFi Connection"
echo -e "$STATUS_STATIC_IP Static IP Configuration"
echo -e "$STATUS_MQTT_BROKER MQTT Broker"
echo -e "$STATUS_PYTHON_DEPS Python Dependencies"
echo -e "$STATUS_SSL_CERT SSL Certificate"
echo -e "$STATUS_NGINX_CONFIG Nginx Configuration"
echo -e "$STATUS_AVAHI_CONFIG Avahi (.local) Service"
echo -e "$STATUS_DASHBOARD_SERVICE Dashboard Service"
echo -e "$STATUS_UFW_CONFIG Firewall Configuration"
if [[ "$KIOSK_MODE" =~ ^[Yy]$ ]]; then
    echo -e "$STATUS_KIOSK_MODE Kiosk Mode"
fi

echo -e "\n${GREEN}[INFO] Access Information:${NC}"
echo -e "  Controller IP: ${STATIC_IP}"
echo -e "  Dashboard URL: https://${HOSTNAME}.local/ (or https://${STATIC_IP}/)"
echo -e "  MQTT Broker: ${STATIC_IP}:1883"
echo -e "  Hostname: ${HOSTNAME}"

echo -e "\n${GREEN}[INFO] Next Steps:${NC}"
echo -e "1. Configure OWL units with network mode 'networked_owl'"
echo -e "2. Set controller_ip & mqtt_broker to ${STATIC_IP} in their config"
echo -e "3. OWLs will automatically connect to this controller"

echo -e "\n${GREEN}[INFO] Testing Instructions:${NC}"
echo -e "  Test MQTT: mosquitto_sub -h ${STATIC_IP} -t 'owl/#' -v"
echo -e "  Check services: systemctl status owl-controller mosquitto nginx"
echo -e "  View logs: journalctl -u owl-controller -f"

# Check if all critical components succeeded
if [[ "$STATUS_PACKAGES" == "${TICK}" && "$STATUS_MQTT_BROKER" == "${TICK}" && "$STATUS_DASHBOARD_SERVICE" == "${TICK}" && "$STATUS_STATIC_IP" == "${TICK}" ]]; then
    echo -e "\n${GREEN}[COMPLETE] Controller setup completed successfully!${NC}"
    read -p "Reboot now to apply all changes? (y/n): " reboot_choice
    if [[ "$reboot_choice" =~ ^[Yy]$ ]]; then
        echo -e "${GREEN}[INFO] Rebooting system...${NC}"
        reboot
    else
        echo -e "${GREEN}[INFO] Please reboot manually when ready: sudo reboot${NC}"
    fi
else
    echo -e "\n${RED}[ERROR] Some components failed. Check the errors above.${NC}"
    exit 1
fi