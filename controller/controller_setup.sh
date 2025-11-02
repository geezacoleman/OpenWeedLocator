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

# Check if run with sudo (required for this setup)
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
echo -e "  • Install all required system packages"
echo -e "  • Create a Python virtual environment"
echo -e "  • Configure MQTT broker for OWL communication"
echo -e "  • Set up Nginx web server with SSL"
echo -e "  • Configure WiFi with static IP"
echo -e "  • Optional: Enable kiosk mode for display"
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
STATUS_SERVICES=""

ERROR_PACKAGES=""
ERROR_WIFI_CONNECT=""
ERROR_STATIC_IP=""
ERROR_MQTT_BROKER=""
ERROR_PYTHON_VENV=""
ERROR_NGINX_CONFIG=""
ERROR_SSL_CERT=""
ERROR_AVAHI_CONFIG=""
ERROR_DASHBOARD_SERVICE=""
ERROR_KIOSK_MODE=""
ERROR_UFW_CONFIG=""
ERROR_SERVICES=""

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

# Test MQTT broker function (adapted from web_setup.sh)
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
    for attempt in 1 2 3; do
        if timeout 5 mosquitto_pub -h localhost -t "test/controller/setup" -m "test_local_$attempt" 2>/dev/null; then
            echo -e "${TICK} Local MQTT connection successful (attempt $attempt)"
            mqtt_test_passed=true
            break
        else
            if [ $attempt -eq 3 ]; then
                echo -e "${CROSS} Local MQTT connection failed after 3 attempts"
                return 1
            else
                echo -e "${ORANGE}[INFO] Local connection attempt $attempt failed, retrying...${NC}"
                sleep 2
            fi
        fi
    done

    # Test 4: Check configuration file
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

    # Summary
    if [ "$mqtt_test_passed" = true ]; then
        echo -e "${TICK} MQTT broker core functionality verified"
        return 0
    else
        echo -e "${CROSS} MQTT broker failed core functionality tests"
        return 1
    fi
}

final_mqtt_validation() {
    echo -e "${GREEN}[INFO] Final MQTT connectivity validation...${NC}"
    if timeout 3 mosquitto_pub -h localhost -t "owl/test/controller" -m "controller_ready" 2>/dev/null; then
        echo -e "${TICK} MQTT broker ready for OWL communication"
    else
        echo -e "${ORANGE}[WARNING] Final MQTT test failed - check logs after reboot: journalctl -u mosquitto${NC}"
    fi
}

# Masked password input function (from web_setup.sh)
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

    # WiFi Settings
    read -p "Enter your WiFi network name (SSID): " WIFI_SSID
    while [ -z "$WIFI_SSID" ]; do
        echo -e "${RED}[ERROR] SSID cannot be empty.${NC}"
        read -p "Enter your WiFi network name (SSID): " WIFI_SSID
    done

    # WiFi Password with confirmation
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

    # Network Configuration
    read -p "Enter controller static IP (default: 192.168.1.2): " STATIC_IP
    STATIC_IP=${STATIC_IP:-192.168.1.2}

    read -p "Enter router/gateway IP (default: 192.168.1.1): " GATEWAY_IP
    GATEWAY_IP=${GATEWAY_IP:-192.168.1.1}

    read -p "Enter hostname (default: owl-controller): " HOSTNAME
    HOSTNAME=${HOSTNAME:-owl-controller}

    # Kiosk Mode
    read -p "Enable kiosk mode on boot? (y/n, default: y): " KIOSK_MODE
    KIOSK_MODE=${KIOSK_MODE:-y}

    # Display Summary
    echo -e ""
    echo -e "${GREEN}[INFO] Configuration Summary:${NC}"
    echo -e "======================================="
    echo -e "WiFi SSID: ${WIFI_SSID}"
    echo -e "WiFi Password: [HIDDEN]"
    echo -e "Static IP: ${STATIC_IP}"
    echo -e "Gateway: ${GATEWAY_IP}"
    echo -e "Hostname: ${HOSTNAME}"
    echo -e "Kiosk Mode: ${KIOSK_MODE}"
    echo -e ""
    echo -e "Access Information:"
    echo -e "  Dashboard: https://${HOSTNAME}.local/ or https://${STATIC_IP}/"
    echo -e "  MQTT Broker: ${STATIC_IP}:1883"
    echo -e ""

    read -p "Continue with these settings? (y/n): " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        echo -e "${RED}[ERROR] Setup cancelled by user.${NC}"
        exit 1
    fi
}

# Step 1: Install system packages
install_system_packages() {
    echo -e "${GREEN}[INFO] Updating system package list...${NC}"
    apt-get update
    apt full-upgrade -y
    check_status "System update" "PACKAGES"

    echo -e "${GREEN}[INFO] Installing required system packages...${NC}"
    apt-get install -y \
        mosquitto mosquitto-clients \
        nginx \
        ufw \
        openssl \
        avahi-daemon \
        net-tools \
        python3-pip \
        python3-venv python3-full \
        chromium \
        network-manager

    check_status "Installing system packages" "PACKAGES"
}

# Step 2: Setup Python virtual environment
setup_python_venv() {
    echo -e "${GREEN}[INFO] Setting up Python virtual environment...${NC}"

    VENV_PATH="/home/${CURRENT_USER}/controller_venv"

    # Create venv as the actual user
    sudo -u $CURRENT_USER python3 -m venv "${VENV_PATH}"
    check_status "Creating virtual environment" "PYTHON_VENV"

    echo -e "${GREEN}[INFO] Installing Python dependencies...${NC}"
    sudo -u $CURRENT_USER bash -c "source ${VENV_PATH}/bin/activate && pip install --upgrade pip && pip install flask==2.2.2 werkzeug==2.2.3 gunicorn==23.0.0 paho-mqtt==2.1.0 psutil==5.9.4 boto3==1.39.13"
    check_status "Installing Python dependencies" "PYTHON_VENV"
}

# Step 3: Configure hostname
configure_hostname() {
    echo -e "${GREEN}[INFO] Setting hostname to ${HOSTNAME}...${NC}"
    hostnamectl set-hostname "${HOSTNAME}"

    # Update hosts file
    sed -i "/${HOSTNAME}/d" /etc/hosts
    echo "127.0.0.1 ${HOSTNAME}" >> /etc/hosts
    echo "127.0.1.1 ${HOSTNAME}" >> /etc/hosts

    check_status "Hostname configuration" "HOSTNAME"
}

# Step 4: Configure MQTT broker
configure_mqtt_broker() {
    echo -e "${GREEN}[INFO] Configuring MQTT broker...${NC}"

    # Create MQTT configuration
    tee /etc/mosquitto/conf.d/owl_controller.conf > /dev/null <<EOF
# OWL Controller MQTT Configuration
# Extends the main mosquitto.conf settings

# Allow anonymous connections (required for OWL dashboard)
allow_anonymous true

# Listen on all interfaces so network clients can connect
listener 1883 0.0.0.0
EOF

    # Ensure log directory exists
    mkdir -p /var/log/mosquitto
    chown mosquitto:mosquitto /var/log/mosquitto

    check_status "MQTT broker configuration" "MQTT_BROKER"
}

# Step 5: Generate SSL certificate
generate_ssl_certificate() {
    echo -e "${GREEN}[INFO] Generating SSL certificate...${NC}"

    mkdir -p /etc/ssl/private /etc/ssl/certs

    openssl req -x509 -nodes -days 3650 \
        -newkey rsa:2048 \
        -keyout /etc/ssl/private/owl-controller.key \
        -out /etc/ssl/certs/owl-controller.crt \
        -subj "/CN=${HOSTNAME}.local/C=US/ST=State/L=City/O=OWL/OU=Controller" \
        2>/dev/null

    chmod 600 /etc/ssl/private/owl-controller.key
    chmod 644 /etc/ssl/certs/owl-controller.crt

    check_status "SSL certificate generation" "SSL_CERT"
}

# Step 6: Configure Nginx
configure_nginx() {
    echo -e "${GREEN}[INFO] Configuring Nginx web server...${NC}"

    # Disable default site
    rm -f /etc/nginx/sites-enabled/default

    # Create OWL controller configuration with MJPEG streaming support
    tee /etc/nginx/sites-available/owl-controller > /dev/null <<EOF
# HTTP server - redirect to HTTPS
server {
    listen 80;
    listen ${STATIC_IP}:80;
    server_name ${HOSTNAME}.local ${STATIC_IP} _;
    return 301 https://\$host\$request_uri;
}

# HTTPS server with MJPEG streaming support
server {
    listen 443 ssl;
    listen ${STATIC_IP}:443 ssl;
    server_name ${HOSTNAME}.local ${STATIC_IP} _;

    ssl_certificate     /etc/ssl/certs/owl-controller.crt;
    ssl_certificate_key /etc/ssl/private/owl-controller.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    # OWL Controller Dashboard (Port 8000)
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

    # Proxy for MJPEG video streams from OWL units (Port 8001)
    location /video_feed {
        proxy_pass http://127.0.0.1:8001/stream.mjpg;
        proxy_set_header Host \$host;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
        chunked_transfer_encoding off;
    }

    # Support for multiple OWL video feeds (if needed)
    location ~ ^/video_feed/(.+)$ {
        proxy_pass http://127.0.0.1:8001/stream.mjpg?\$1;
        proxy_set_header Host \$host;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
        chunked_transfer_encoding off;
    }

    # MQTT status endpoint for debugging
    location /mqtt-status {
        return 200 "<html><body><h1>MQTT Status</h1><p>MQTT Broker: ${STATIC_IP}:1883</p><p>Status: Running</p></body></html>";
        add_header Content-Type text/html;
    }
}
EOF

    # Enable the site
    ln -sf /etc/nginx/sites-available/owl-controller /etc/nginx/sites-enabled/

    # Test nginx configuration
    nginx -t 2>/dev/null
    check_status "Nginx configuration" "NGINX_CONFIG"
}

# Step 7: Configure Avahi (mDNS)
configure_avahi() {
    echo -e "${GREEN}[INFO] Configuring Avahi for .local domain...${NC}"

    sudo mkdir -p /etc/avahi/services
    sudo tee /etc/avahi/services/owl-controller.service > /dev/null <<EOF
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
    <name replace-wildcards="yes">OWL Controller (${HOSTNAME})</name>
    <service>
        <type>_http._tcp</type>
        <port>80</port>
    </service>
    <service>
        <type>_https._tcp</type>
        <port>443</port>
    </service>
    <service>
        <type>_mqtt._tcp</type>
        <port>1883</port>
    </service>
</service-group>
EOF

    systemctl restart avahi-daemon
    check_status "Avahi service configuration" "AVAHI_CONFIG"
}


# Step 8: Configure UFW firewall
configure_firewall() {
    echo -e "${GREEN}[INFO] Configuring UFW firewall...${NC}"

    ufw --force reset > /dev/null 2>&1
    ufw default deny incoming > /dev/null 2>&1
    ufw default allow outgoing > /dev/null 2>&1
    ufw allow 22/tcp comment 'SSH' > /dev/null 2>&1
    ufw allow 80/tcp comment 'HTTP' > /dev/null 2>&1
    ufw allow 443/tcp comment 'HTTPS' > /dev/null 2>&1
    ufw allow 1883/tcp comment 'MQTT' > /dev/null 2>&1
    ufw allow 5353/udp comment 'mDNS' > /dev/null 2>&1

    ufw allow 'Nginx Full'

    check_status "Firewall configuration" "UFW_CONFIG"
}

# Step 9: Create systemd service for dashboard
create_dashboard_service() {
    echo -e "${GREEN}[INFO] Creating systemd service for OWL Controller Dashboard...${NC}"

    VENV_PATH="/home/${CURRENT_USER}/controller_venv"

    tee /etc/systemd/system/owl-controller.service > /dev/null <<EOF
[Unit]
Description=OWL Central Controller Dashboard
After=network-online.target mosquitto.service
Wants=network-online.target
Requires=mosquitto.service

[Service]
Type=simple
User=${CURRENT_USER}
Group=$(id -g -n ${CURRENT_USER})
WorkingDirectory=${SCRIPT_DIR}
Environment="PATH=${VENV_PATH}/bin:/usr/local/bin:/usr/bin:/bin"
Environment="PYTHONUNBUFFERED=1"
ExecStart=${VENV_PATH}/bin/gunicorn --bind 127.0.0.1:8000 --workers 1 --timeout 300 networked_controller:app
Restart=always
RestartSec=5
KillMode=mixed
KillSignal=SIGINT
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    check_status "Dashboard service configuration" "DASHBOARD_SERVICE"
}

# Step 10: Configure kiosk mode
configure_kiosk_mode() {
    if [[ ! "$KIOSK_MODE" =~ ^[Yy]$ ]]; then
        echo -e "${ORANGE}[INFO] Skipping kiosk mode setup.${NC}"
        STATUS_KIOSK_MODE="SKIPPED"
        return 0
    fi

    echo -e "${GREEN}[INFO] Setting up kiosk mode (labwc, Raspberry Pi way)...${NC}"

    # create labwc autostart directory for the actual user
    sudo -u "$CURRENT_USER" mkdir -p /home/"$CURRENT_USER"/.config/labwc

    tee /home/"$CURRENT_USER"/.config/labwc/autostart > /dev/null <<'EOF'
chromium https://localhost/ \
  --kiosk \
  --noerrdialogs \
  --disable-infobars \
  --no-first-run \
  --enable-features=OverlayScrollbar \
  --start-maximized \
  --ignore-certificate-errors &
EOF

    # make sure the user owns it
    chown -R "$CURRENT_USER":"$(id -g -n "$CURRENT_USER")" /home/"$CURRENT_USER"/.config/labwc

    # we NO LONGER touch /etc/lightdm/lightdm.conf
    # because we're using the default Pi desktop session (labwc), not openbox

    check_status "Kiosk mode configuration" "KIOSK_MODE"
}


# Step 11: Configure WiFi with NetworkManager
configure_network() {
    echo -e "${GREEN}[INFO] Configuring WiFi connection: ${WIFI_SSID}...${NC}"

    # Delete any existing connection with the same name
    nmcli con delete "${WIFI_SSID}" 2>/dev/null || true

    # Add new WiFi connection
    nmcli con add type wifi con-name "${WIFI_SSID}" ifname wlan0 ssid "${WIFI_SSID}"

    # Configure WiFi security
    nmcli con modify "${WIFI_SSID}" wifi-sec.key-mgmt wpa-psk
    nmcli con modify "${WIFI_SSID}" wifi-sec.psk "${WIFI_PASSWORD}"

    # Configure static IP
    nmcli con modify "${WIFI_SSID}" ipv4.addresses ${STATIC_IP}/24
    nmcli con modify "${WIFI_SSID}" ipv4.gateway ${GATEWAY_IP}
    nmcli con modify "${WIFI_SSID}" ipv4.dns "8.8.8.8 8.8.4.4"
    nmcli con modify "${WIFI_SSID}" ipv4.method manual

    # Set as default connection
    nmcli con modify "${WIFI_SSID}" connection.autoconnect yes
    nmcli con modify "${WIFI_SSID}" connection.autoconnect-priority 100

    nmcli con up "${WIFI_SSID}" || true
    check_status "WiFi configuration" "WIFI_CONNECT"
}

# Step 12: Start and enable services
start_services() {
    echo -e "${GREEN}[INFO] Starting and enabling services...${NC}"

    # Enable services
    systemctl enable mosquitto nginx avahi-daemon owl-controller

    # Start/restart services
    systemctl restart mosquitto
    sleep 2
    systemctl restart nginx
    systemctl restart avahi-daemon

    # Enable firewall
    ufw --force enable > /dev/null 2>&1

    # Start dashboard service
    systemctl start owl-controller
    sleep 3

    # Check if dashboard service started successfully
    if systemctl is-active --quiet owl-controller; then
        echo -e "${TICK} OWL Controller dashboard service started successfully"
        check_status "Starting services" "SERVICES"
    else
        echo -e "${CROSS} OWL Controller dashboard service failed to start"
        echo -e "${ORANGE}[INFO] Checking service logs...${NC}"
        systemctl status owl-controller --no-pager -l
        check_status "Starting services" "SERVICES"
    fi
}

# Step 13: Create configuration summary file
create_config_summary() {
    echo -e "${GREEN}[INFO] Creating configuration summary...${NC}"

    tee /opt/owl-controller-config.txt > /dev/null <<EOF
OWL Controller Configuration
============================
Hostname: ${HOSTNAME}
Static IP: ${STATIC_IP}
Gateway: ${GATEWAY_IP}
WiFi SSID: ${WIFI_SSID}
WiFi Password: [HIDDEN]

Access URLs:
- Dashboard: https://${HOSTNAME}.local/ or https://${STATIC_IP}/
- MQTT Broker: ${STATIC_IP}:1883

Service Configuration:
- MQTT Config: /etc/mosquitto/conf.d/owl_controller.conf
- MQTT Log: /var/log/mosquitto/mosquitto.log
- Nginx Config: /etc/nginx/sites-available/owl-controller
- SSL Certificate: /etc/ssl/certs/owl-controller.crt
- SSL Private Key: /etc/ssl/private/owl-controller.key
- Avahi Service: /etc/avahi/services/owl-controller.service
- Dashboard Service: /etc/systemd/system/owl-controller.service
- Python Venv: /home/${CURRENT_USER}/controller_venv

Testing Commands:
- mosquitto_pub -h localhost -t "test/message" -m "Hello World"
- mosquitto_sub -h localhost -t "owl/#"
- curl -k https://localhost/
- systemctl status owl-controller mosquitto nginx

OWL Configuration:
- Set network_mode to 'networked_owl' in OWL config
- Set controller_ip to '${STATIC_IP}'
- Set mqtt_broker to '${STATIC_IP}'

Generated: $(date)
EOF

    chmod 644 /opt/owl-controller-config.txt
}

# Step 14: Final validation
final_validation() {
    echo -e "\n${GREEN}[INFO] Performing final system validation...${NC}"

    # Test MQTT broker
    test_mqtt_broker
    final_mqtt_validation

    # Test dashboard service
    echo -e "${GREEN}[INFO] Testing dashboard service...${NC}"
    if curl -k -s https://localhost/ > /dev/null 2>&1; then
        echo -e "${TICK} Dashboard service is responding"
    else
        echo -e "${ORANGE}[WARNING] Dashboard service may not be ready yet${NC}"
    fi

    # Test network connectivity if connected
    echo -e "${GREEN}[INFO] Testing network connectivity...${NC}"
    nmcli con show --active | grep "${WIFI_SSID}" > /dev/null 2>&1
    if [ $? -eq 0 ]; then
        echo -e "${TICK} Connected to ${WIFI_SSID}"

        # Check if we have the static IP
        ip addr show wlan0 | grep "${STATIC_IP}" > /dev/null 2>&1
        if [ $? -eq 0 ]; then
            echo -e "${TICK} Static IP ${STATIC_IP} configured"
            STATUS_STATIC_IP="${TICK}"
        else
            echo -e "${ORANGE}[WARNING] Static IP may not be set yet${NC}"
            STATUS_STATIC_IP="${ORANGE}[WARN]${NC}"
        fi
    else
        echo -e "${ORANGE}[INFO] WiFi connection will be activated on next boot${NC}"
    fi
}

# Main execution flow
main() {
    # Step 1: Collect user input
    collect_user_input

    # Step 2: Install system packages
    install_system_packages

    # Step 3: Setup Python environment
    setup_python_venv

    # Step 4: Configure hostname
    configure_hostname

    # Step 5: Configure MQTT broker
    configure_mqtt_broker

    # Step 6: Generate SSL certificate
    generate_ssl_certificate

    # Step 7: Configure Nginx
    configure_nginx

    # Step 8: Configure Avahi
    configure_avahi

    # Step 9: Configure firewall
    configure_firewall

    # Step 10: Create dashboard service
    create_dashboard_service

    # Step 11: Configure kiosk mode
    configure_kiosk_mode

    # Step 12: Configure network
    configure_network

    # Step 13: Start services
    start_services

    # Step 14: Create config summary
    create_config_summary

    # Step 15: Final validation
    final_validation

    # Final Summary
    echo -e "\n${GREEN}============================================${NC}"
    echo -e "${GREEN}[INFO] OWL Controller Setup Summary:${NC}"
    echo -e "${GREEN}============================================${NC}"
    echo -e "$STATUS_PACKAGES System Packages"
    echo -e "$STATUS_PYTHON_VENV Python Environment"
    echo -e "$STATUS_MQTT_BROKER MQTT Broker Configuration"
    echo -e "$STATUS_SSL_CERT SSL Certificate"
    echo -e "$STATUS_NGINX_CONFIG Nginx Configuration"
    echo -e "$STATUS_AVAHI_CONFIG Avahi (.local) Configuration"
    echo -e "$STATUS_UFW_CONFIG Firewall Configuration"
    echo -e "$STATUS_DASHBOARD_SERVICE Dashboard Service"
    echo -e "$STATUS_SERVICES Service Management"

    if [[ "$STATUS_KIOSK_MODE" == "${TICK}" ]]; then
        echo -e "$STATUS_KIOSK_MODE Kiosk Mode Configuration"
    elif [[ "$STATUS_KIOSK_MODE" == "SKIPPED" ]]; then
        echo -e "${ORANGE}[SKIPPED]${NC} Kiosk Mode"
    fi

    echo -e "$STATUS_WIFI_CONNECT WiFi Configuration"
    echo -e "$STATUS_STATIC_IP Static IP Configuration"

    echo -e "\n${GREEN}[INFO] Controller Access Information:${NC}"
    echo -e "======================================="
    echo -e "  Controller IP: ${STATIC_IP}"
    echo -e "  Dashboard URL: https://${HOSTNAME}.local/ or https://${STATIC_IP}/"
    echo -e "  MQTT Broker: ${STATIC_IP}:1883"
    echo -e "  Hostname: ${HOSTNAME}"
    echo -e "  Configuration: /opt/owl-controller-config.txt"

    echo -e "\n${GREEN}[INFO] Testing Commands:${NC}"
    echo -e "  mosquitto_pub -h ${STATIC_IP} -t 'owl/test' -m 'hello'"
    echo -e "  mosquitto_sub -h ${STATIC_IP} -t 'owl/#'"
    echo -e "  systemctl status owl-controller mosquitto nginx"
    echo -e "  journalctl -u owl-controller -f"

    echo -e "\n${GREEN}[INFO] Next Steps for OWL Configuration:${NC}"
    echo -e "1. On each OWL unit, edit the configuration file"
    echo -e "2. Set network_mode = 'networked_owl'"
    echo -e "3. Set controller_ip = '${STATIC_IP}'"
    echo -e "4. Set mqtt_broker = '${STATIC_IP}'"
    echo -e "5. OWLs will automatically connect to this controller"

    # Check overall success
    if [[ "$STATUS_PACKAGES" == "${TICK}" && "$STATUS_PYTHON_VENV" == "${TICK}" && "$STATUS_MQTT_BROKER" == "${TICK}" && "$STATUS_NGINX_CONFIG" == "${TICK}" && "$STATUS_SSL_CERT" == "${TICK}" && "$STATUS_AVAHI_CONFIG" == "${TICK}" && "$STATUS_UFW_CONFIG" == "${TICK}" && "$STATUS_DASHBOARD_SERVICE" == "${TICK}" && "$STATUS_SERVICES" == "${TICK}" ]]; then
        echo -e "\n${GREEN}[COMPLETE] OWL Controller setup completed successfully!${NC}"
        echo -e "${GREEN}============================================${NC}"

        echo -e "\nA reboot is recommended to ensure all services start properly."
        echo -e ""
        echo -e "After reboot:"
        echo -e "  • Controller will connect to WiFi '${WIFI_SSID}'"
        echo -e "  • Dashboard will be available at https://${STATIC_IP}/"
        echo -e "  • MQTT broker will be running on port 1883"

        if [[ "$STATUS_KIOSK_MODE" == "${TICK}" ]]; then
            echo -e "  • Kiosk mode will launch automatically on boot"
        fi

        echo -e ""
        read -p "Reboot now? (y/n): " reboot_choice

        if [[ "$reboot_choice" =~ ^[Yy]$ ]]; then
            echo -e "${GREEN}[INFO] Rebooting system...${NC}"
            reboot
        else
            echo -e "${GREEN}[INFO] Reboot skipped. Remember to reboot later for full functionality.${NC}"
            echo -e "${GREEN}[INFO] You can reboot manually with: sudo reboot${NC}"
        fi
    else
        echo -e "\n${RED}[ERROR] Some components failed to install. Check the status above.${NC}"

        # Show specific errors
        if [[ -n "$ERROR_PACKAGES" ]]; then echo -e "${RED}[ERROR] Packages: $ERROR_PACKAGES${NC}"; fi
        if [[ -n "$ERROR_PYTHON_VENV" ]]; then echo -e "${RED}[ERROR] Python Environment: $ERROR_PYTHON_VENV${NC}"; fi
        if [[ -n "$ERROR_MQTT_BROKER" ]]; then echo -e "${RED}[ERROR] MQTT Broker: $ERROR_MQTT_BROKER${NC}"; fi
        if [[ -n "$ERROR_SSL_CERT" ]]; then echo -e "${RED}[ERROR] SSL Certificate: $ERROR_SSL_CERT${NC}"; fi
        if [[ -n "$ERROR_NGINX_CONFIG" ]]; then echo -e "${RED}[ERROR] Nginx: $ERROR_NGINX_CONFIG${NC}"; fi
        if [[ -n "$ERROR_AVAHI_CONFIG" ]]; then echo -e "${RED}[ERROR] Avahi: $ERROR_AVAHI_CONFIG${NC}"; fi
        if [[ -n "$ERROR_UFW_CONFIG" ]]; then echo -e "${RED}[ERROR] Firewall: $ERROR_UFW_CONFIG${NC}"; fi
        if [[ -n "$ERROR_DASHBOARD_SERVICE" ]]; then echo -e "${RED}[ERROR] Dashboard Service: $ERROR_DASHBOARD_SERVICE${NC}"; fi
        if [[ -n "$ERROR_SERVICES" ]]; then echo -e "${RED}[ERROR] Services: $ERROR_SERVICES${NC}"; fi
        if [[ -n "$ERROR_KIOSK_MODE" ]]; then echo -e "${RED}[ERROR] Kiosk Mode: $ERROR_KIOSK_MODE${NC}"; fi
        if [[ -n "$ERROR_WIFI_CONNECT" ]]; then echo -e "${RED}[ERROR] WiFi: $ERROR_WIFI_CONNECT${NC}"; fi

        echo -e "\n${RED}[ERROR] Please fix the above issues and try again.${NC}"
        exit 1
    fi
}

# Run main function
main