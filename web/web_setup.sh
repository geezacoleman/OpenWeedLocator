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
STATUS_PYTHON_PACKAGES=""
STATUS_WIFI_CONFIG=""
STATUS_UFW_CONFIG=""
STATUS_NGINX_CONFIG=""
STATUS_SSL_CERT=""
STATUS_AVAHI_CONFIG=""
STATUS_SERVICES=""

ERROR_PACKAGES=""
ERROR_PYTHON_PACKAGES=""
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

# Step 2: Install necessary packages
echo -e "${GREEN}[INFO] Installing required packages...${NC}"
sudo apt update
sudo apt install -y nginx ufw openssl avahi-daemon
check_status "Installing packages (nginx, ufw, openssl, avahi-daemon)" "PACKAGES"

# Install Python dependencies for dashboard
echo -e "${GREEN}[INFO] Installing Python dependencies...${NC}"
sudo -u owl /home/owl/.virtualenvs/owl/bin/pip install flask gunicorn psutil
check_status "Installing Python dependencies" "PYTHON_PACKAGES"

# Step 3: Configure WiFi hotspot with NetworkManager
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

# Step 4: Configure UFW firewall
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
sudo ufw allow from 10.42.0.0/24  # Allow all from hotspot network

sudo ufw --force enable
check_status "UFW firewall configuration" "UFW_CONFIG"

# Step 5: Set hostname
echo -e "${GREEN}[INFO] Setting hostname to ${HOSTNAME}...${NC}"
sudo hostnamectl set-hostname "${HOSTNAME}"
check_status "Setting hostname" "WIFI_CONFIG"

# Step 6: Generate SSL certificates
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

# Step 7: Configure Nginx
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

    # Placeholder for Flask app - will be configured later
    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;
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

# Step 8: Configure Avahi for .local domain
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
</service-group>
EOF

check_status "Avahi service configuration" "AVAHI_CONFIG"

echo -e "${GREEN}[INFO] Starting and enabling services...${NC}"
sudo systemctl enable avahi-daemon
sudo systemctl start avahi-daemon
sudo systemctl enable nginx
sudo systemctl restart nginx
check_status "Starting services" "SERVICES"

# Step 9: Create systemd service for OWL Dashboard
echo -e "${GREEN}[INFO] Creating systemd service for OWL Dashboard...${NC}"
sudo tee /etc/systemd/system/owl-dash.service > /dev/null <<EOF
[Unit]
Description=OWL Dashboard Service
After=network.target

[Service]
Type=exec
User=owl
Group=owl
WorkingDirectory=/home/owl/owl/web
Environment="PATH=/home/owl/.virtualenvs/owl/bin"
ExecStart=/home/owl/.virtualenvs/owl/bin/gunicorn --bind 127.0.0.1:8000 --workers 2 owl_dash:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable owl-dash
sudo systemctl start owl-dash
check_status "Creating and starting dashboard service" "SERVICES"

# Step 10: Create configuration summary file
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

SSL Certificate: /etc/ssl/certs/owl-dash.crt
SSL Private Key: /etc/ssl/private/owl-dash.key
Nginx Config: /etc/nginx/sites-available/owl-dash
Avahi Service: /etc/avahi/services/owl-dash.service

Generated: $(date)
EOF

sudo chmod 644 /opt/owl-dash-config.txt

# Final Summary
echo -e "\n${GREEN}[INFO] OWL Dashboard Setup Summary:${NC}"
echo -e "$STATUS_PACKAGES Required Packages"
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
echo -e "  Configuration saved to: /opt/owl-dash-config.txt"

if [[ "$STATUS_PACKAGES" == "${TICK}" && "$STATUS_WIFI_CONFIG" == "${TICK}" && "$STATUS_UFW_CONFIG" == "${TICK}" && "$STATUS_NGINX_CONFIG" == "${TICK}" && "$STATUS_SSL_CERT" == "${TICK}" && "$STATUS_AVAHI_CONFIG" == "${TICK}" && "$STATUS_SERVICES" == "${TICK}" ]]; then
    echo -e "\n${GREEN}[COMPLETE] OWL Dashboard setup completed successfully!${NC}"
    echo -e "${GREEN}[INFO] Connect to WiFi '${SSID}' and visit https://${HOSTNAME}.local/${NC}"
    echo -e "${GREEN}[INFO] Dashboard service running on port 8000${NC}"
    echo -e "${GREEN}[INFO] Next: Complete the main OWL setup to enable full functionality${NC}"
else
    echo -e "\n${RED}[ERROR] Some components failed to install. Check the status above.${NC}"

    # Show errors
    if [[ -n "$ERROR_PACKAGES" ]]; then echo -e "${RED}[ERROR] Packages: $ERROR_PACKAGES${NC}"; fi
    if [[ -n "$ERROR_WIFI_CONFIG" ]]; then echo -e "${RED}[ERROR] WiFi Config: $ERROR_WIFI_CONFIG${NC}"; fi
    if [[ -n "$ERROR_UFW_CONFIG" ]]; then echo -e "${RED}[ERROR] UFW Config: $ERROR_UFW_CONFIG${NC}"; fi
    if [[ -n "$ERROR_NGINX_CONFIG" ]]; then echo -e "${RED}[ERROR] Nginx Config: $ERROR_NGINX_CONFIG${NC}"; fi
    if [[ -n "$ERROR_SSL_CERT" ]]; then echo -e "${RED}[ERROR] SSL Cert: $ERROR_SSL_CERT${NC}"; fi
    if [[ -n "$ERROR_AVAHI_CONFIG" ]]; then echo -e "${RED}[ERROR] Avahi Config: $ERROR_AVAHI_CONFIG${NC}"; fi
    if [[ -n "$ERROR_SERVICES" ]]; then echo -e "${RED}[ERROR] Services: $ERROR_SERVICES${NC}"; fi

    exit 1
fi