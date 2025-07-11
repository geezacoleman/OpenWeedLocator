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

if [ "$EUID" -ne 0 ]; then
   echo -e "${RED}[ERROR] This script must be run with sudo privileges.${NC}"
   echo -e "${RED}[ERROR] Please run: sudo ./web_setup.sh${NC}"
   exit 1
fi

if [ "$CURRENT_USER" != "owl" ]; then
   echo -e "${ORANGE}[WARNING] Current user '$CURRENT_USER' differs from expected 'owl'. Some settings may not work correctly.${NC}"
fi

# Initialize status tracking variables
STATUS_PACKAGES=""
STATUS_MQTT_BROKER=""
STATUS_PYTHON_PACKAGES=""
STATUS_NUMPY_VERSION=""
STATUS_WIFI_CONFIG=""
STATUS_UFW_CONFIG=""
STATUS_NGINX_CONFIG=""
STATUS_SSL_CERT=""
STATUS_AVAHI_CONFIG=""
STATUS_SERVICES=""

ERROR_PACKAGES=""
ERROR_MQTT_BROKER=""
ERROR_PYTHON_PACKAGES=""
ERROR_NUMPY_VERSION=""
ERROR_WIFI_CONFIG=""
ERROR_UFW_CONFIG=""
ERROR_NGINX_CONFIG=""
ERROR_SSL_CERT=""
ERROR_AVAHI_CONFIG=""
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

# Function to check NumPy version and detect conflicts
check_numpy_version() {
    echo -e "${GREEN}[INFO] Checking NumPy installation...${NC}"

    # Check for multiple NumPy installations
    echo -e "${GREEN}[INFO] Detecting NumPy installations...${NC}"
    NUMPY_LOCATIONS=$(sudo -u owl /home/owl/.virtualenvs/owl/bin/python -c "
import sys
import pkg_resources
numpy_dists = [d for d in pkg_resources.working_set if d.project_name.lower() == 'numpy']
if len(numpy_dists) > 1:
    print('MULTIPLE')
    for dist in numpy_dists:
        print(f'{dist.version} at {dist.location}')
elif len(numpy_dists) == 1:
    print('SINGLE')
    print(f'{numpy_dists[0].version}')
else:
    print('NONE')
" 2>/dev/null)

    if [ $? -ne 0 ]; then
        echo -e "${ORANGE}[WARNING] NumPy detection failed. This may cause issues with OpenCV.${NC}"
        eval "STATUS_NUMPY_VERSION='${ORANGE}[WARN]${NC}'"
        eval "ERROR_NUMPY_VERSION='NumPy detection failed'"
        return 1
    fi

    FIRST_LINE=$(echo "$NUMPY_LOCATIONS" | head -n 1)

    if [[ "$FIRST_LINE" == "MULTIPLE" ]]; then
        echo -e "${RED}[ERROR] Multiple NumPy installations detected!${NC}"
        echo -e "${RED}This causes conflicts and camera access issues.${NC}"
        echo "$NUMPY_LOCATIONS" | tail -n +2

        echo -e "${GREEN}[INFO] Completely removing all NumPy installations...${NC}"

        # Uninstall all NumPy packages
        sudo -u owl /home/owl/.virtualenvs/owl/bin/pip uninstall numpy -y 2>/dev/null || true

        # Clean up any remaining numpy files
        sudo -u owl find /home/owl/.virtualenvs/owl -name "*numpy*" -type d -exec rm -rf {} + 2>/dev/null || true

        # Reinstall compatible NumPy version for current OpenCV
        echo -e "${GREEN}[INFO] Installing compatible NumPy version...${NC}"

        # Check OpenCV version to determine compatible NumPy
        OPENCV_VERSION=$(sudo -u owl /home/owl/.virtualenvs/owl/bin/python -c "import cv2; print(cv2.__version__)" 2>/dev/null)

        if [[ "$OPENCV_VERSION" =~ ^4\.1[2-9] ]] || [[ "$OPENCV_VERSION" =~ ^4\.[2-9] ]]; then
            # OpenCV 4.12+ supports NumPy 2.x
            echo -e "${GREEN}[INFO] OpenCV $OPENCV_VERSION detected - installing NumPy 2.x${NC}"
            sudo -u owl /home/owl/.virtualenvs/owl/bin/pip install "numpy>=2.0,<3.0"
        else
            # Older OpenCV versions need NumPy 1.x
            echo -e "${GREEN}[INFO] OpenCV $OPENCV_VERSION detected - installing NumPy 1.x${NC}"
            sudo -u owl /home/owl/.virtualenvs/owl/bin/pip install "numpy>=1.21,<2.0"
        fi

        if [ $? -eq 0 ]; then
            NEW_NUMPY_VERSION=$(sudo -u owl /home/owl/.virtualenvs/owl/bin/python -c "import numpy; print(numpy.__version__)" 2>/dev/null)
            echo -e "${TICK} NumPy conflicts resolved - installed version $NEW_NUMPY_VERSION"
            eval "STATUS_NUMPY_VERSION='${TICK}'"
        else
            echo -e "${CROSS} Failed to resolve NumPy conflicts"
            eval "STATUS_NUMPY_VERSION='${CROSS}'"
            eval "ERROR_NUMPY_VERSION='Failed to resolve multiple NumPy installations'"
            return 1
        fi

    elif [[ "$FIRST_LINE" == "SINGLE" ]]; then
        NUMPY_VERSION=$(echo "$NUMPY_LOCATIONS" | tail -n 1)
        echo -e "${TICK} Single NumPy installation detected: version $NUMPY_VERSION"
        eval "STATUS_NUMPY_VERSION='${TICK}'"

    elif [[ "$FIRST_LINE" == "NONE" ]]; then
        echo -e "${ORANGE}[WARNING] NumPy not installed. Installing compatible version...${NC}"

        # Install appropriate NumPy version
        OPENCV_VERSION=$(sudo -u owl /home/owl/.virtualenvs/owl/bin/python -c "import cv2; print(cv2.__version__)" 2>/dev/null)

        if [[ "$OPENCV_VERSION" =~ ^4\.1[2-9] ]] || [[ "$OPENCV_VERSION" =~ ^4\.[2-9] ]]; then
            sudo -u owl /home/owl/.virtualenvs/owl/bin/pip install "numpy>=2.0,<3.0"
        else
            sudo -u owl /home/owl/.virtualenvs/owl/bin/pip install "numpy>=1.21,<2.0"
        fi

        if [ $? -eq 0 ]; then
            NEW_NUMPY_VERSION=$(sudo -u owl /home/owl/.virtualenvs/owl/bin/python -c "import numpy; print(numpy.__version__)" 2>/dev/null)
            echo -e "${TICK} NumPy installed: version $NEW_NUMPY_VERSION"
            eval "STATUS_NUMPY_VERSION='${TICK}'"
        else
            echo -e "${CROSS} Failed to install NumPy"
            eval "STATUS_NUMPY_VERSION='${CROSS}'"
            eval "ERROR_NUMPY_VERSION='Failed to install NumPy'"
            return 1
        fi
    fi

    # Final verification - test import
    echo -e "${GREEN}[INFO] Verifying NumPy installation...${NC}"
    sudo -u owl /home/owl/.virtualenvs/owl/bin/python -c "
import numpy as np
import cv2
print(f'NumPy {np.__version__} imported successfully')
print(f'OpenCV {cv2.__version__} imported successfully')
print('Camera access test...')
# Quick camera test
try:
    cap = cv2.VideoCapture(0)
    if cap.isOpened():
        print('Camera access: OK')
        cap.release()
    else:
        print('Camera access: Failed to open')
except Exception as e:
    print(f'Camera access: Error - {e}')
" 2>/dev/null

    if [ $? -eq 0 ]; then
        echo -e "${TICK} NumPy and OpenCV verification passed"
    else
        echo -e "${ORANGE}[WARNING] NumPy/OpenCV verification failed - camera issues may occur${NC}"
    fi
}

# Function to test MQTT broker
test_mqtt_broker() {
    echo -e "${GREEN}[INFO] Testing MQTT broker...${NC}"

    # Test if mosquitto is running
    if systemctl is-active --quiet mosquitto; then
        echo -e "${TICK} Mosquitto broker is running"

        # Test local connection
        timeout 5 mosquitto_pub -h localhost -t "test/owl/setup" -m "test message" 2>/dev/null
        if [ $? -eq 0 ]; then
            echo -e "${TICK} MQTT broker accepting local connections"
        else
            echo -e "${ORANGE}[WARNING] MQTT broker may not be accepting local connections${NC}"
            return 1
        fi

        # Test network connection (hotspot IP)
        timeout 5 mosquitto_pub -h 10.42.0.1 -t "test/owl/setup" -m "test message" 2>/dev/null
        if [ $? -eq 0 ]; then
            echo -e "${TICK} MQTT broker accepting network connections"
            return 0
        else
            echo -e "${ORANGE}[WARNING] MQTT broker may not be accepting network connections${NC}"
            return 1
        fi
    else
        echo -e "${CROSS} Mosquitto broker is not running"
        return 1
    fi
}

# Function to collect user input
collect_user_input() {
    echo -e "${GREEN}[INFO] OWL Dashboard Setup Configuration${NC}"
    echo -e "${GREEN}=======================================${NC}"

    # Get OWL ID
    read -p "Enter OWL ID number (default: 1): " OWL_ID
    OWL_ID=${OWL_ID:-1}

    # Validate OWL ID is a number
    if ! [[ "$OWL_ID" =~ ^[0-9]+$ ]]; then
        echo -e "${RED}[ERROR] OWL ID must be a number. Using default: 1${NC}"
        OWL_ID=1
    fi

    SSID="OWL-WIFI-${OWL_ID}"

    # Get WiFi password
    while true; do
        read -s -p "Enter WiFi password (minimum 8 characters): " WIFI_PASSWORD
        echo
        if [ ${#WIFI_PASSWORD} -lt 8 ]; then
            echo -e "${RED}[ERROR] Password must be at least 8 characters long.${NC}"
        else
            break
        fi
    done

    # Confirm settings
    echo -e "${GREEN}[INFO] Configuration Summary:${NC}"
    echo -e "  SSID: ${SSID}"
    echo -e "  IP Address: 10.42.0.1/24"
    echo -e "  Hostname: owl-${OWL_ID}"
    echo -e "  MQTT Broker: localhost:1883"
    echo

    read -p "Continue with these settings? (y/n): " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        echo -e "${RED}[ERROR] Setup cancelled by user.${NC}"
        exit 1
    fi

    HOSTNAME="owl-${OWL_ID}"
}

# Step 1: Collect configuration from user
collect_user_input

# Step 2: Install system packages
echo -e "${GREEN}[INFO] Installing required system packages...${NC}"
sudo apt update
sudo apt install -y nginx ufw openssl avahi-daemon mosquitto mosquitto-clients
check_status "Installing system packages (nginx, ufw, openssl, avahi-daemon, mosquitto)" "PACKAGES"

# Step 3: Configure and test MQTT broker
echo -e "${GREEN}[INFO] Configuring MQTT broker...${NC}"

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
sudo systemctl restart mosquitto
sleep 3

# Test MQTT broker functionality (this is the real test)
test_mqtt_broker
check_status "MQTT broker configuration and testing" "MQTT_BROKER"

# Step 4: Check and fix NumPy version
check_numpy_version

# Step 5: Install Python dependencies
echo -e "${GREEN}[INFO] Installing Python dependencies...${NC}"

# Install Flask, Gunicorn, and other web dependencies
sudo -u owl /home/owl/.virtualenvs/owl/bin/pip install flask gunicorn psutil boto3

# Install MQTT client
sudo -u owl /home/owl/.virtualenvs/owl/bin/pip install paho-mqtt

# Verify installations
echo -e "${GREEN}[INFO] Verifying Python package installations...${NC}"
FLASK_VERSION=$(sudo -u owl /home/owl/.virtualenvs/owl/bin/python -c "import flask; print(flask.__version__)" 2>/dev/null)
GUNICORN_VERSION=$(sudo -u owl /home/owl/.virtualenvs/owl/bin/python -c "import gunicorn; print(gunicorn.__version__)" 2>/dev/null)
PAHO_VERSION=$(sudo -u owl /home/owl/.virtualenvs/owl/bin/python -c "import paho.mqtt.client; print('installed')" 2>/dev/null)

if [[ -n "$FLASK_VERSION" && -n "$GUNICORN_VERSION" && "$PAHO_VERSION" == "installed" ]]; then
    echo -e "${TICK} Flask: $FLASK_VERSION, Gunicorn: $GUNICORN_VERSION, Paho-MQTT: installed"
    check_status "Installing Python dependencies" "PYTHON_PACKAGES"
else
    echo -e "${CROSS} Some Python packages failed to install"
    check_status "Installing Python dependencies" "PYTHON_PACKAGES"
fi

# Step 6: Configure WiFi hotspot with NetworkManager
echo -e "${GREEN}[INFO] Setting up WiFi hotspot: ${SSID}...${NC}"

# Remove existing connection if it exists
sudo nmcli connection delete "${SSID}" 2>/dev/null || true

# Create new hotspot connection
sudo nmcli connection add type wifi ifname wlan0 mode ap con-name "${SSID}" ssid "${SSID}"
sudo nmcli connection modify "${SSID}" 802-11-wireless.mode ap
sudo nmcli connection modify "${SSID}" 802-11-wireless-security.key-mgmt wpa-psk
sudo nmcli connection modify "${SSID}" 802-11-wireless-security.psk "${WIFI_PASSWORD}"
sudo nmcli connection modify "${SSID}" 802-11-wireless-security.proto rsn
sudo nmcli connection modify "${SSID}" 802-11-wireless-security.pairwise ccmp
sudo nmcli connection modify "${SSID}" 802-11-wireless.band bg
sudo nmcli connection modify "${SSID}" ipv4.method shared
sudo nmcli connection modify "${SSID}" ipv4.addresses 10.42.0.1/24
sudo nmcli connection modify "${SSID}" ipv6.method ignore

# Activate the connection
sudo nmcli connection up "${SSID}"
check_status "WiFi hotspot configuration" "WIFI_CONFIG"

# Step 7: Configure UFW firewall
echo -e "${GREEN}[INFO] Configuring UFW firewall...${NC}"
sudo ufw --force reset
sudo ufw default deny incoming
sudo ufw default allow outgoing

# Allow essential services
sudo ufw allow OpenSSH
sudo ufw allow ssh
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 'Nginx Full'
sudo ufw allow 67/udp  # DHCP server
sudo ufw allow 68/udp  # DHCP client
sudo ufw allow 1883/tcp # MQTT broker (local only)
sudo ufw allow from 10.42.0.0/24  # Allow all from hotspot network

sudo ufw --force enable
check_status "UFW firewall configuration" "UFW_CONFIG"

# Step 8: Set hostname
echo -e "${GREEN}[INFO] Setting hostname to ${HOSTNAME}...${NC}"
sudo hostnamectl set-hostname "${HOSTNAME}"
check_status "Setting hostname" "WIFI_CONFIG"

echo -e "${GREEN}[INFO] Updating local hostname resolution...${NC}"
sudo sed -i "/${HOSTNAME}/d" /etc/hosts
echo "127.0.0.1 ${HOSTNAME}" | sudo tee -a /etc/hosts
echo "127.0.1.1 ${HOSTNAME}" | sudo tee -a /etc/hosts

check_status "Setting hostname and local resolution" "WIFI_CONFIG"

# Step 9: Generate SSL certificates
echo -e "${GREEN}[INFO] Generating SSL certificates...${NC}"
sudo mkdir -p /etc/ssl/private
sudo mkdir -p /etc/ssl/certs

# Create self-signed certificate
sudo openssl req -x509 -nodes -days 3650 \
    -newkey rsa:2048 \
    -keyout /etc/ssl/private/owl-dash.key \
    -out /etc/ssl/certs/owl-dash.crt \
    -subj "/CN=${HOSTNAME}.local/C=US/ST=State/L=City/O=OWL/OU=Dashboard"

# Set proper permissions
sudo chmod 600 /etc/ssl/private/owl-dash.key
sudo chmod 644 /etc/ssl/certs/owl-dash.crt
check_status "SSL certificate generation" "SSL_CERT"

# Step 10: Configure Nginx
echo -e "${GREEN}[INFO] Configuring Nginx...${NC}"

# Remove default site
sudo rm -f /etc/nginx/sites-enabled/default

# Create OWL dashboard site configuration
sudo tee /etc/nginx/sites-available/owl-dash > /dev/null <<EOF
server {
    listen 80;
    listen 10.42.0.1:80;
    server_name ${HOSTNAME}.local 10.42.0.1 _;

    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    listen 10.42.0.1:443 ssl;
    server_name ${HOSTNAME}.local 10.42.0.1 _;

    ssl_certificate     /etc/ssl/certs/owl-dash.crt;
    ssl_certificate_key /etc/ssl/private/owl-dash.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    # OWL Dashboard Flask app
    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;

        # WebSocket support for real-time updates
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    # MQTT status endpoint for debugging
    location /mqtt-status {
        return 200 "<html><body><h1>MQTT Status</h1><p>MQTT Broker: localhost:1883</p><p>Status: Running</p><p>Test: mosquitto_pub -h localhost -t test/message -m 'Hello'</p></body></html>";
        add_header Content-Type text/html;
    }

    location /ssl-help {
        return 200 "<html><body><h1>OWL Dashboard Access</h1><p>To access the dashboard:</p><ol><li>Visit <a href='https://${HOSTNAME}.local/'>https://${HOSTNAME}.local/</a> or <a href='https://10.42.0.1/'>https://10.42.0.1/</a></li><li>If you see a security warning, click 'Advanced'</li><li>Click 'Continue to ${HOSTNAME}.local (unsafe)'</li><li>You will then see the OWL dashboard</li></ol></body></html>";
        add_header Content-Type text/html;
    }
}
EOF

# Enable the site
sudo ln -sf /etc/nginx/sites-available/owl-dash /etc/nginx/sites-enabled/owl-dash

# Test nginx configuration
sudo nginx -t
check_status "Nginx configuration" "NGINX_CONFIG"

# Step 11: Configure Avahi for .local domain
echo -e "${GREEN}[INFO] Configuring Avahi for .local domain resolution...${NC}"

# Create Avahi service file
sudo tee /etc/avahi/services/owl-dash.service > /dev/null <<EOF
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
    <name replace-wildcards="yes">OWL Dashboard ${OWL_ID}</name>
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

check_status "Avahi service configuration" "AVAHI_CONFIG"

# Step 12: Start and enable services
echo -e "${GREEN}[INFO] Starting and enabling services...${NC}"
sudo systemctl enable avahi-daemon mosquitto nginx
sudo systemctl start avahi-daemon
sudo systemctl restart mosquitto
sudo systemctl restart nginx
check_status "Starting services" "SERVICES"

# Step 13: Create systemd service for OWL Dashboard
echo -e "${GREEN}[INFO] Creating systemd service for OWL Dashboard...${NC}"
sudo tee /etc/systemd/system/owl-dash.service > /dev/null <<EOF
[Unit]
Description=OWL Dashboard Service
After=network.target mosquitto.service
Requires=mosquitto.service

[Service]
Type=exec
User=owl
Group=owl
WorkingDirectory=/home/owl/owl/web
Environment="PATH=/home/owl/.virtualenvs/owl/bin"
ExecStart=/home/owl/.virtualenvs/owl/bin/gunicorn --bind 127.0.0.1:8000 --workers 2 --timeout 300 owl_dash:app
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

# Step 14: Create configuration summary file
echo -e "${GREEN}[INFO] Creating configuration summary...${NC}"
sudo tee /opt/owl-dash-config.txt > /dev/null <<EOF
OWL Dashboard Configuration
==========================
OWL ID: ${OWL_ID}
SSID: ${SSID}
WiFi Password: ${WIFI_PASSWORD}
IP Address: 10.42.0.1/24
Hostname: ${HOSTNAME}

Access URLs:
- https://${HOSTNAME}.local/
- https://10.42.0.1/

MQTT Configuration:
- Broker: localhost:1883 (accessible on hotspot network)
- Network Access: 10.42.0.1:1883
- Config: /etc/mosquitto/conf.d/owl.conf
- Main Config: /etc/mosquitto/mosquitto.conf
- Log: /var/log/mosquitto/mosquitto.log

SSL Certificate: /etc/ssl/certs/owl-dash.crt
SSL Private Key: /etc/ssl/private/owl-dash.key
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

sudo chmod 644 /opt/owl-dash-config.txt

# Step 15: Final system validation
echo -e "${GREEN}[INFO] Performing final system validation...${NC}"

# Test MQTT broker thoroughly
echo -e "${GREEN}[INFO] Final MQTT connectivity test...${NC}"

# Test local MQTT connection
timeout 3 mosquitto_pub -h localhost -t "owl/test/setup" -m "setup_complete" 2>/dev/null
if [ $? -eq 0 ]; then
    echo -e "${TICK} MQTT broker ready for local OWL communication"
else
    echo -e "${ORANGE}[WARNING] MQTT local connection test failed${NC}"
fi

# Test network MQTT connection (hotspot clients)
timeout 3 mosquitto_pub -h 10.42.0.1 -t "owl/test/setup" -m "setup_complete" 2>/dev/null
if [ $? -eq 0 ]; then
    echo -e "${TICK} MQTT broker ready for network communication"
else
    echo -e "${ORANGE}[WARNING] MQTT network connection test failed${NC}"
fi

# Test dashboard service
echo -e "${GREEN}[INFO] Testing dashboard service...${NC}"
if curl -k -s https://localhost/ > /dev/null 2>&1; then
    echo -e "${TICK} Dashboard service is responding"
else
    echo -e "${ORANGE}[WARNING] Dashboard service may not be ready yet${NC}"
fi

# Final Summary
echo -e "\n${GREEN}[INFO] OWL Dashboard Setup Summary:${NC}"
echo -e "$STATUS_PACKAGES System Packages"
echo -e "$STATUS_MQTT_BROKER MQTT Broker Configuration"
echo -e "$STATUS_NUMPY_VERSION NumPy Version Check"
echo -e "$STATUS_PYTHON_PACKAGES Python Dependencies"
echo -e "$STATUS_WIFI_CONFIG WiFi Hotspot Configuration"
echo -e "$STATUS_UFW_CONFIG UFW Firewall Configuration"
echo -e "$STATUS_NGINX_CONFIG Nginx Configuration"
echo -e "$STATUS_SSL_CERT SSL Certificate Generation"
echo -e "$STATUS_AVAHI_CONFIG Avahi Service Configuration"
echo -e "$STATUS_SERVICES Service Management"

echo -e "\n${GREEN}[INFO] Dashboard Access Information:${NC}"
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

# Check if OWL dashboard is enabled in config
echo -e "\n${GREEN}[INFO] Checking OWL configuration for dashboard...${NC}"
OWL_CONFIG_FILE="/home/owl/owl/config/DAY_SENSITIVITY_2.ini"
DASHBOARD_ENABLED="false"

if [ -f "$OWL_CONFIG_FILE" ]; then
    # Check if dashboard is enabled in the config
    DASHBOARD_ENABLED=$(grep -i "^dashboard_enable\s*=" "$OWL_CONFIG_FILE" | cut -d'=' -f2 | tr -d ' ' | tr '[:upper:]' '[:lower:]' 2>/dev/null)

    if [[ "$DASHBOARD_ENABLED" == "true" ]]; then
        echo -e "${TICK} Dashboard enabled in OWL configuration"
        echo -e "${GREEN}[INFO] OWL will start with dashboard integration on boot${NC}"
    else
        echo -e "${ORANGE}[WARNING] Dashboard NOT enabled in OWL configuration${NC}"
        echo -e "${ORANGE}[WARNING] Edit $OWL_CONFIG_FILE and set dashboard_enable=True${NC}"
        echo -e "${ORANGE}[WARNING] Or OWL will start without dashboard integration${NC}"
    fi
else
    echo -e "${ORANGE}[WARNING] OWL config file not found at $OWL_CONFIG_FILE${NC}"
    echo -e "${ORANGE}[WARNING] OWL may not start properly${NC}"
fi

if [[ "$STATUS_PACKAGES" == "${TICK}" && "$STATUS_MQTT_BROKER" == "${TICK}" && "$STATUS_PYTHON_PACKAGES" == "${TICK}" && "$STATUS_WIFI_CONFIG" == "${TICK}" && "$STATUS_UFW_CONFIG" == "${TICK}" && "$STATUS_NGINX_CONFIG" == "${TICK}" && "$STATUS_SSL_CERT" == "${TICK}" && "$STATUS_AVAHI_CONFIG" == "${TICK}" && "$STATUS_SERVICES" == "${TICK}" ]]; then
    echo -e "\n${GREEN}[COMPLETE] OWL Dashboard setup completed successfully!${NC}"

    # Check if reboot is needed
    echo -e "\n${GREEN}[INFO] Setup Complete - Reboot Recommended${NC}"
    echo -e "${GREEN}======================================${NC}"
    echo -e "A reboot is recommended to ensure all services start properly."
    echo -e ""
    echo -e "After reboot:"
    echo -e "  • WiFi hotspot '${SSID}' will be active"
    echo -e "  • Dashboard will be available at https://${HOSTNAME}.local/"
    echo -e "  • MQTT broker will be running on port 1883"

    if [[ "$DASHBOARD_ENABLED" == "true" ]]; then
        echo -e "  • OWL will start with dashboard integration enabled"
        echo -e "  • Both owl.py and dashboard will launch automatically"
    else
        echo -e "  • ${ORANGE}OWL dashboard integration is DISABLED in config${NC}"
        echo -e "  • ${ORANGE}Enable it manually or OWL won't connect to dashboard${NC}"
    fi

    echo -e ""
    echo -e "To start OWL manually after reboot:"
    echo -e "  cd /home/owl/owl && python owl.py"
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
    if [[ -n "$ERROR_NUMPY_VERSION" ]]; then echo -e "${ORANGE}[WARNING] NumPy: $ERROR_NUMPY_VERSION${NC}"; fi
    if [[ -n "$ERROR_PYTHON_PACKAGES" ]]; then echo -e "${RED}[ERROR] Python Packages: $ERROR_PYTHON_PACKAGES${NC}"; fi
    if [[ -n "$ERROR_WIFI_CONFIG" ]]; then echo -e "${RED}[ERROR] WiFi Config: $ERROR_WIFI_CONFIG${NC}"; fi
    if [[ -n "$ERROR_UFW_CONFIG" ]]; then echo -e "${RED}[ERROR] UFW Config: $ERROR_UFW_CONFIG${NC}"; fi
    if [[ -n "$ERROR_NGINX_CONFIG" ]]; then echo -e "${RED}[ERROR] Nginx Config: $ERROR_NGINX_CONFIG${NC}"; fi
    if [[ -n "$ERROR_SSL_CERT" ]]; then echo -e "${RED}[ERROR] SSL Cert: $ERROR_SSL_CERT${NC}"; fi
    if [[ -n "$ERROR_AVAHI_CONFIG" ]]; then echo -e "${RED}[ERROR] Avahi Config: $ERROR_AVAHI_CONFIG${NC}"; fi
    if [[ -n "$ERROR_SERVICES" ]]; then echo -e "${RED}[ERROR] Services: $ERROR_SERVICES${NC}"; fi

    echo -e "\n${RED}[ERROR] Please fix the above issues before rebooting.${NC}"
    exit 1
fi