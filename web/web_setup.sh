#!/bin/bash
set -e

# Define colors for status messages
RED='\033[0;31m'
ORANGE='\033[0;33m'
GREEN='\033[0;32m'
NC='\033[0m'
TICK="${GREEN}[OK]${NC}"
CROSS="${RED}[FAIL]${NC}"

# Script directory (web/ folder)
SCRIPT_DIR=$(dirname "$(realpath "$0")")
CURRENT_USER=${SUDO_USER:-$(whoami)}
HOME_DIR=$(getent passwd "$CURRENT_USER" | cut -d: -f6)
read -p "Enter OWL device number (default: 1): " OWL_NUMBER
OWL_NUMBER=${OWL_NUMBER:-"1"}
DEVICE_ID="owl-${OWL_NUMBER}"
WEB_PORT=5000
VENV_DIR="$HOME_DIR/.virtualenvs/owl"
AUTH_SCRIPT="$HOME_DIR/owl/dev/setup_auth.py"

echo -e "${GREEN}[INFO] Setting up OWL Web Interface and Access Point...${NC}"

if [ "$SUDO_USER" ]; then
    echo -e "${RED}[ERROR] This script should not be run with sudo. Please run as normal user.${NC}"
    exit 1
fi

# Check if virtualenv exists (assuming owl_install.sh ran)
if [ ! -d "$VENV_DIR" ]; then
    echo -e "${RED}[ERROR] OWL virtual environment not found at $VENV_DIR. Please run owl_install.sh first.${NC}"
    exit 1
fi

# Check if setup_auth.py exists locally
if [ ! -f "$AUTH_SCRIPT" ]; then
    echo -e "${RED}[ERROR] Authentication setup script not found at $AUTH_SCRIPT. Please ensure it exists.${NC}"
    exit 1
fi

# Function to check status
check_status() {
    if [ $? -ne 0 ]; then
        echo -e "${CROSS} $1 failed."
        exit 1
    else
        echo -e "${TICK} $1 completed successfully."
    fi
}

# 1. Install Web Dependencies (requires internet)
echo -e "${GREEN}[INFO] Installing web dependencies...${NC}"
sudo apt update
sudo apt install -y nginx apache2-utils avahi-daemon ufw network-manager
check_status "Installing dependencies"

# 2. Activate virtualenv and install Flask
source "$VENV_DIR/bin/activate"
echo -e "${GREEN}[INFO] Installing Flask in virtualenv...${NC}"
pip install --upgrade pip
pip install flask send2trash==2.0.0
check_status "Installing Flask in virtualenv"

# 4. Run Authentication Setup (local file)
echo -e "${GREEN}[INFO] Setting up authentication and HTTPS...${NC}"
echo -e "${GREEN}[INFO] Creating SSL directory if it doesn't exist...${NC}"
sudo mkdir -p /etc/nginx/ssl
sudo chown $CURRENT_USER:$CURRENT_USER /etc/nginx/ssl
sudo chmod 700 /etc/nginx/ssl
sudo python3 "$AUTH_SCRIPT" "$DEVICE_ID" --dashboard --home-dir "$HOME_DIR"
check_status "Running authentication setup"

# 3. Enable Dashboard in OWL Config
echo -e "${GREEN}[INFO] Enabling dashboard in OWL configuration...${NC}"
CONFIG_FILE="$HOME_DIR/owl/config/DAY_SENSITIVITY_2.ini"
if grep -q "\[System\]" "$CONFIG_FILE"; then
    # System section exists, check if dashboard_enable exists
    if grep -q "dashboard_enable" "$CONFIG_FILE"; then
        # Replace the value
        sed -i "s/dashboard_enable = .*/dashboard_enable = True/" "$CONFIG_FILE"
    else
        # Add the setting under System section
        sed -i "/\[System\]/a dashboard_enable = True\ndashboard_port = $WEB_PORT" "$CONFIG_FILE"
    fi
else
    # System section doesn't exist, add it
    echo -e "\n[System]\ndashboard_enable = True\ndashboard_port = $WEB_PORT" >> "$CONFIG_FILE"
fi
check_status "Enabling dashboard in OWL configuration"

# 5. Configure Firewall
echo -e "${GREEN}[INFO] Configuring firewall...${NC}"
sudo ufw allow 22/tcp comment "SSH"
sudo ufw limit 22/tcp comment "Rate limit SSH"
sudo ufw allow 80/tcp comment "HTTP redirect"
sudo ufw allow 443/tcp comment "HTTPS"
sudo ufw enable
check_status "Configuring UFW"

# 6. Set Up WiFi Access Point (last step, with warning)
echo -e "${ORANGE}[WARNING] The next step will configure the OWL as a WiFi Access Point.${NC}"
echo -e "${ORANGE}[WARNING] This will disconnect your current internet connection (e.g., phone hotspot).${NC}"
echo -e "${GREEN}[INFO] After setup, reconnect to the OWL-AP network or use SSH/Ethernet to continue.${NC}"
read -p "Proceed with AP setup? (y/N): " confirm
if [[ "$confirm" =~ ^[Yy]$ ]]; then
    echo -e "${GREEN}[INFO] Setting up WiFi Access Point...${NC}"
    read -p "Enter AP SSID (default: OWL-AP-${DEVICE_ID}): " AP_SSID
    AP_SSID=${AP_SSID:-"OWL-AP-${DEVICE_ID}"}

    # Secure password entry
    while true; do
        echo -n "Enter AP password (min 8 chars): "
        read -s AP_PASS
        echo
        if [ ${#AP_PASS} -lt 8 ]; then
            echo -e "${ORANGE}[WARNING] Password must be at least 8 characters. Please try again.${NC}"
        else
            echo -n "Confirm AP password: "
            read -s AP_PASS_CONFIRM
            echo
            if [ "$AP_PASS" != "$AP_PASS_CONFIRM" ]; then
                echo -e "${ORANGE}[WARNING] Passwords do not match. Please try again.${NC}"
            else
                break
            fi
        fi
    done

    sudo nmcli con add type wifi ifname wlan0 con-name "OWL-AP" autoconnect yes \
        ssid "$AP_SSID" mode ap ipv4.method manual \
        ipv4.addresses 192.168.50.1/24 \
        wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$AP_PASS"
    check_status "Configuring NetworkManager AP"
    sudo nmcli con up "OWL-AP"
    check_status "Activating OWL-AP connection"

    echo -e "${GREEN}[INFO] Access Point Setup Complete${NC}"
    echo -e "${GREEN}[INFO] Connect to WiFi: $AP_SSID (Password: [hidden]) and access https://owl.local${NC}"
    echo -e "${GREEN}[INFO] Alternatively, use SSH (port 22) or Ethernet to continue setup.${NC}"
    echo -e "${GREEN}[INFO] Check status with:${NC}"
    echo "  - sudo systemctl status owl-web.service"
    echo "  - nmcli con show"
    echo "  - sudo ufw status"
else
    echo -e "${GREEN}[INFO] AP setup skipped. Complete setup via SSH or Ethernet if needed.${NC}"
    echo -e "${GREEN}[INFO] Current setup complete without AP. Access via existing network at https://owl.local${NC}"
fi