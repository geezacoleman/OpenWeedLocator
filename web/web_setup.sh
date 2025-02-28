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
DEVICE_ID=${DEVICE_ID:-"owl-1"}
WEB_PORT=5000
VENV_DIR="$HOME_DIR/.virtualenvs/owl"
REPO_URL="https://github.com/your-repo/OpenWeedLocator.git"  # Adjust to your repo

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

# Function to check status
check_status() {
    if [ $? -ne 0 ]; then
        echo -e "${CROSS} $1 failed."
        exit 1
    else
        echo -e "${TICK} $1 completed successfully."
    fi
}

# 1. Install Web and AP Dependencies
echo -e "${GREEN}[INFO] Installing web and AP dependencies...${NC}"
sudo apt update
sudo apt install -y nginx apache2-utils avahi-daemon ufw network-manager
check_status "Installing dependencies"

# Activate virtualenv and install Flask
source "$VENV_DIR/bin/activate"
pip install --upgrade pip
pip install flask
check_status "Installing Flask in virtualenv"

# 2. Set Up WiFi Access Point (Maker Medic style, programmatic)
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

# 3. Set Up OWL Web Service
echo -e "${GREEN}[INFO] Configuring OWL web service...${NC}"
cat > /tmp/owl-web.service << EOF
[Unit]
Description=OWL Web Interface
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$SCRIPT_DIR
ExecStart=$VENV_DIR/bin/python $SCRIPT_DIR/owl_web_interface.py --port $WEB_PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo mv /tmp/owl-web.service /etc/systemd/system/owl-web.service
sudo systemctl daemon-reload
sudo systemctl enable owl-web.service
sudo systemctl restart owl-web.service
check_status "Setting up OWL web service"

# 4. Run Authentication Setup
echo -e "${GREEN}[INFO] Setting up authentication and HTTPS...${NC}"
wget -O /tmp/setup_auth.py "$REPO_URL/raw/main/setup_auth.py"
python3 /tmp/setup_auth.py "$DEVICE_ID" --dashboard --home-dir "$HOME_DIR"
check_status "Running authentication setup"
rm /tmp/setup_auth.py

# 5. Configure Firewall
echo -e "${GREEN}[INFO] Configuring firewall...${NC}"
sudo ufw allow 22/tcp comment "SSH"
sudo ufw limit 22/tcp comment "Rate limit SSH"
sudo ufw allow 80/tcp comment "HTTP redirect"
sudo ufw allow 443/tcp comment "HTTPS"
sudo ufw --force enable
check_status "Configuring UFW"

# Done
echo -e "${GREEN}[INFO] Setup Complete${NC}"
echo -e "${GREEN}[INFO] Connect to WiFi: $AP_SSID (Password: [hidden]) ${NC}"
echo -e "${GREEN}[INFO] Access the dashboard at: https://owl.local${NC}"
echo -e "${GREEN}[INFO] SSH available on port 22 (rate-limited)${NC}"
echo -e "${GREEN}[INFO] Check status with:${NC}"
echo "  - sudo systemctl status owl-web.service"
echo "  - nmcli con show"
echo "  - sudo ufw status"