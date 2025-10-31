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
echo -e "The controller will:"
echo -e "  • Connect to your existing WiFi network"
echo -e "  • Set static IP to 192.168.1.2"
echo -e "  • Host MQTT broker for all OWLs"
echo -e "  • Run central dashboard in kiosk mode"
echo -e ""

# Initialize status tracking variables
STATUS_PACKAGES=""
STATUS_WIFI_CONNECT=""
STATUS_STATIC_IP=""
STATUS_MQTT_BROKER=""
STATUS_PYTHON_DEPS=""
STATUS_NGINX_CONFIG=""
STATUS_SSL_CERT=""
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
ERROR_DASHBOARD_SERVICE=""
ERROR_KIOSK_MODE=""
ERROR_UFW_CONFIG=""

# Function to check the exit status
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

# Masked password input function
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
    echo -e "${GREEN}[INFO] Controller Configuration${NC}"
    echo -e "${GREEN}=======================================${NC}"

    # WiFi network to connect to
    read -p "Enter your WiFi network name (SSID): " WIFI_SSID

    # WiFi password
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

    # Static IP configuration
    read -p "Enter controller static IP (default: 192.168.1.2): " STATIC_IP
    STATIC_IP=${STATIC_IP:-192.168.1.2}

    read -p "Enter router/gateway IP (default: 192.168.1.1): " GATEWAY_IP
    GATEWAY_IP=${GATEWAY_IP:-192.168.1.1}

    # Hostname
    read -p "Enter hostname (default: owl-controller): " HOSTNAME
    HOSTNAME=${HOSTNAME:-owl-controller}

    # Kiosk mode
    read -p "Enable kiosk mode on boot? (y/n, default: y): " KIOSK_MODE
    KIOSK_MODE=${KIOSK_MODE:-y}

    # Confirm settings
    echo -e ""
    echo -e "${GREEN}[INFO] Configuration Summary:${NC}"
    echo -e "  WiFi SSID: ${WIFI_SSID}"
    echo -e "  Static IP: ${STATIC_IP}"
    echo -e "  Gateway: ${GATEWAY_IP}"
    echo -e "  Hostname: ${HOSTNAME}"
    echo -e "  Kiosk Mode: ${KIOSK_MODE}"
    echo -e "  MQTT Broker: ${STATIC_IP}:1883"
    echo -e "  Dashboard URL: http://${STATIC_IP}:8000/"
    echo -e ""

    read -p "Continue with these settings? (y/n): " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        echo -e "${RED}[ERROR] Setup cancelled by user.${NC}"
        exit 1
    fi
}

# Step 1: Collect configuration
collect_user_input

# Step 2: Update system packages
echo -e "${GREEN}[INFO] Updating system packages...${NC}"
apt-get update
apt-get install -y \
    mosquitto mosquitto-clients \
    nginx \
    ufw \
    python3-pip python3-venv python3-virtualenv \
    chromium-browser \
    xserver-xorg xinit \
    unclutter \
    network-manager
check_status "Installing packages" "PACKAGES"

# Step 3: Set hostname
echo -e "${GREEN}[INFO] Setting hostname to ${HOSTNAME}...${NC}"
hostnamectl set-hostname ${HOSTNAME}
echo "127.0.1.1    ${HOSTNAME}" >> /etc/hosts

# Step 4: Connect to WiFi network
echo -e "${GREEN}[INFO] Connecting to WiFi network: ${WIFI_SSID}...${NC}"

# Use NetworkManager to connect to WiFi
nmcli dev wifi connect "${WIFI_SSID}" password "${WIFI_PASSWORD}" 2>/dev/null || {
    # If connection exists, modify it
    nmcli con delete "${WIFI_SSID}" 2>/dev/null
    nmcli dev wifi connect "${WIFI_SSID}" password "${WIFI_PASSWORD}"
}
check_status "Connecting to WiFi" "WIFI_CONNECT"

# Step 5: Configure static IP
echo -e "${GREEN}[INFO] Configuring static IP: ${STATIC_IP}...${NC}"

# Get the connection name
CONNECTION_NAME=$(nmcli -t -f NAME con show --active | head -n1)

if [ -n "$CONNECTION_NAME" ]; then
    nmcli con mod "$CONNECTION_NAME" ipv4.addresses ${STATIC_IP}/24
    nmcli con mod "$CONNECTION_NAME" ipv4.gateway ${GATEWAY_IP}
    nmcli con mod "$CONNECTION_NAME" ipv4.dns "8.8.8.8 8.8.4.4"
    nmcli con mod "$CONNECTION_NAME" ipv4.method manual
    nmcli con down "$CONNECTION_NAME"
    nmcli con up "$CONNECTION_NAME"
    check_status "Setting static IP" "STATIC_IP"
else
    echo -e "${RED}[ERROR] Could not find active connection${NC}"
    STATUS_STATIC_IP="${CROSS}"
    ERROR_STATIC_IP="No active connection found"
fi

# Wait for network to stabilize
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

systemctl restart mosquitto
systemctl enable mosquitto
check_status "Configuring MQTT broker" "MQTT_BROKER"

# Test MQTT
sleep 2
if timeout 3 mosquitto_pub -h localhost -t "test/controller" -m "test"; then
    echo -e "${TICK} MQTT broker is working"
else
    echo -e "${CROSS} MQTT broker test failed"
fi

# Step 7: Setup Python environment and dependencies
echo -e "${GREEN}[INFO] Setting up Python environment...${NC}"

# Create virtual environment for controller
sudo -u $CURRENT_USER python3 -m venv /home/$CURRENT_USER/owl-controller-env

# Install Python dependencies
sudo -u $CURRENT_USER /home/$CURRENT_USER/owl-controller-env/bin/pip install --upgrade pip
sudo -u $CURRENT_USER /home/$CURRENT_USER/owl-controller-env/bin/pip install \
    flask==2.2.2 \
    gunicorn==23.0.0 \
    paho-mqtt==2.1.0 \
    psutil==5.9.4
check_status "Installing Python dependencies" "PYTHON_DEPS"

# Step 8: Create controller dashboard application
echo -e "${GREEN}[INFO] Creating controller dashboard application...${NC}"

mkdir -p /home/$CURRENT_USER/owl-controller/{templates,static}
chown -R $CURRENT_USER:$CURRENT_USER /home/$CURRENT_USER/owl-controller

chown $CURRENT_USER:$CURRENT_USER /home/$CURRENT_USER/owl/controller/networked_controller.py
check_status "Creating controller application" "DASHBOARD_SERVICE"

# Step 9: Configure Nginx
echo -e "${GREEN}[INFO] Configuring Nginx...${NC}"

tee /etc/nginx/sites-available/owl-controller > /dev/null <<EOF
server {
    listen 80;
    server_name ${HOSTNAME} ${STATIC_IP};

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;
    }
}
EOF

ln -sf /etc/nginx/sites-available/owl-controller /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
check_status "Configuring Nginx" "NGINX_CONFIG"

# Step 10: Create SSL certificate (self-signed for HTTPS)
echo -e "${GREEN}[INFO] Creating SSL certificate...${NC}"

openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
    -keyout /etc/ssl/private/owl-controller.key \
    -out /etc/ssl/certs/owl-controller.crt \
    -subj "/C=US/ST=State/L=City/O=OWL/CN=${HOSTNAME}"
check_status "Creating SSL certificate" "SSL_CERT"

# Step 11: Create systemd service for dashboard
echo -e "${GREEN}[INFO] Creating systemd service for controller dashboard...${NC}"

tee /etc/systemd/system/owl-controller.service > /dev/null <<EOF
[Unit]
Description=OWL Central Controller Dashboard
After=network.target mosquitto.service
Requires=mosquitto.service

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=/home/$CURRENT_USER/owl-controller
Environment="PATH=/home/$CURRENT_USER/owl-controller-env/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=/home/$CURRENT_USER/owl-controller-env/bin/gunicorn --bind 127.0.0.1:8000 --workers 2 --timeout 300 central_controller:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable owl-controller.service
systemctl start owl-controller.service
check_status "Creating dashboard service" "DASHBOARD_SERVICE"

# Step 12: Configure UFW firewall
echo -e "${GREEN}[INFO] Configuring firewall...${NC}"

ufw --force enable
ufw allow 22/tcp     # SSH
ufw allow 80/tcp     # HTTP
ufw allow 443/tcp    # HTTPS
ufw allow 1883/tcp   # MQTT
ufw allow 8000/tcp   # Dashboard
ufw reload
check_status "Configuring firewall" "UFW_CONFIG"

# Step 13: Setup kiosk mode (if requested)
if [[ "$KIOSK_MODE" =~ ^[Yy]$ ]]; then
    echo -e "${GREEN}[INFO] Setting up kiosk mode...${NC}"

    # Create autostart directory
    mkdir -p /home/$CURRENT_USER/.config/autostart

    # Create kiosk startup script
    tee /home/$CURRENT_USER/start-kiosk.sh > /dev/null <<EOF
#!/bin/bash
# OWL Controller Kiosk Mode

# Disable screen blanking and power management
xset s noblank
xset s off
xset -dpms

# Hide cursor after 3 seconds of inactivity
unclutter -idle 3 -root &

# Wait for network and services
sleep 10

# Start Chromium in kiosk mode
chromium-browser \\
    --kiosk \\
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
    http://localhost:8000/
EOF

    chmod +x /home/$CURRENT_USER/start-kiosk.sh
    chown $CURRENT_USER:$CURRENT_USER /home/$CURRENT_USER/start-kiosk.sh

    # Create autostart entry for LXDE
    tee /home/$CURRENT_USER/.config/autostart/owl-kiosk.desktop > /dev/null <<EOF
[Desktop Entry]
Type=Application
Name=OWL Controller Kiosk
Exec=/home/$CURRENT_USER/start-kiosk.sh
Hidden=false
X-GNOME-Autostart-enabled=true
EOF

    chown -R $CURRENT_USER:$CURRENT_USER /home/$CURRENT_USER/.config

    # Configure auto-login for the user (for Raspberry Pi OS with desktop)
    if [ -f /etc/lightdm/lightdm.conf ]; then
        sed -i "s/^#autologin-user=.*/autologin-user=$CURRENT_USER/" /etc/lightdm/lightdm.conf
        sed -i "s/^autologin-user=.*/autologin-user=$CURRENT_USER/" /etc/lightdm/lightdm.conf
    fi

    # Alternative: Use .bashrc to start kiosk if in console mode
    echo "" >> /home/$CURRENT_USER/.bashrc
    echo "# Auto-start kiosk mode if on console 1" >> /home/$CURRENT_USER/.bashrc
    echo 'if [ "$(tty)" = "/dev/tty1" ]; then' >> /home/$CURRENT_USER/.bashrc
    echo '    startx /home/'$CURRENT_USER'/start-kiosk.sh -- -nocursor' >> /home/$CURRENT_USER/.bashrc
    echo 'fi' >> /home/$CURRENT_USER/.bashrc

    check_status "Setting up kiosk mode" "KIOSK_MODE"
fi

# Step 14: Final summary
echo -e "\n${GREEN}============================================${NC}"
echo -e "${GREEN}[INFO] OWL Controller Setup Summary:${NC}"
echo -e "${GREEN}============================================${NC}"
echo -e "$STATUS_PACKAGES System Packages"
echo -e "$STATUS_WIFI_CONNECT WiFi Connection"
echo -e "$STATUS_STATIC_IP Static IP Configuration"
echo -e "$STATUS_MQTT_BROKER MQTT Broker"
echo -e "$STATUS_PYTHON_DEPS Python Dependencies"
echo -e "$STATUS_NGINX_CONFIG Nginx Configuration"
echo -e "$STATUS_DASHBOARD_SERVICE Dashboard Service"
echo -e "$STATUS_UFW_CONFIG Firewall Configuration"

if [[ "$KIOSK_MODE" =~ ^[Yy]$ ]]; then
    echo -e "$STATUS_KIOSK_MODE Kiosk Mode"
fi

echo -e "\n${GREEN}[INFO] Access Information:${NC}"
echo -e "  Controller IP: ${STATIC_IP}"
echo -e "  Dashboard URL: http://${STATIC_IP}:8000/"
echo -e "  MQTT Broker: ${STATIC_IP}:1883"
echo -e "  Hostname: ${HOSTNAME}"

echo -e "\n${GREEN}[INFO] Next Steps:${NC}"
echo -e "1. Configure OWL units with network mode 'networked_owl'"
echo -e "2. Set controller_ip to ${STATIC_IP} in their config"
echo -e "3. OWLs will automatically connect to this controller"

if [[ "$KIOSK_MODE" =~ ^[Yy]$ ]]; then
    echo -e "\n${GREEN}[INFO] Kiosk Mode:${NC}"
    echo -e "  The dashboard will start automatically on boot"
    echo -e "  To exit kiosk: Alt+F4 or Ctrl+Alt+T for terminal"
fi

echo -e "\n${GREEN}[INFO] Testing Instructions:${NC}"
echo -e "  Test MQTT: mosquitto_sub -h ${STATIC_IP} -t 'owl/#' -v"
echo -e "  Check services: systemctl status owl-controller mosquitto nginx"
echo -e "  View logs: journalctl -u owl-controller -f"

# Check if all critical components succeeded
if [[ "$STATUS_PACKAGES" == "${TICK}" && "$STATUS_MQTT_BROKER" == "${TICK}" && "$STATUS_DASHBOARD_SERVICE" == "${TICK}" ]]; then
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