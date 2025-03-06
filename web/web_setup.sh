#!/bin/bash
set -e

# Define colors for status messages
RED='\033[0;31m'
GREEN='\033[0;32m'
ORANGE='\033[0;33m'
NC='\033[0m'
TICK="${GREEN}[OK]${NC}"
CROSS="${RED}[FAIL]${NC}"

# Script directory
SCRIPT_DIR=$(dirname "$(realpath "$0")")
CURRENT_USER=${SUDO_USER:-$(whoami)}
HOME_DIR=$(getent passwd "$CURRENT_USER" | cut -d: -f6)
echo "${HOME_DIR}"

# Function to check status
check_status() {
    if [ $? -ne 0 ]; then
        echo -e "${CROSS} $1 failed."
        exit 1
    else
        echo -e "${TICK} $1 completed successfully."
    fi
}

# Prompt for OWL device number
read -p "Enter OWL device number (default: 1): " OWL_NUMBER
OWL_NUMBER=${OWL_NUMBER:-"1"}
DEVICE_ID="owl-${OWL_NUMBER}"

# Prompt for AP SSID
read -p "Enter AP SSID (default: owl-ap-${OWL_NUMBER}): " AP_SSID
AP_SSID=${AP_SSID:-"owl-${DEVICE_ID}-ap"}

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

echo -e "${GREEN}[INFO] Setting up OWL Web Interface and Access Point...${NC}"

# Install dependencies
echo -e "${GREEN}[INFO] Installing dependencies...${NC}"
sudo apt update
sudo apt install -y nginx apache2-utils avahi-daemon ufw
check_status "Installing dependencies"

# Run OWLAuthSetup
echo -e "${GREEN}[INFO] Running OWLAuthSetup...${NC}"
sudo python3 "${HOME_DIR}/owl/dev/setup_auth.py" "${DEVICE_ID}" --dashboard --home-dir "${HOME_DIR}"
check_status "Running OWLAuthSetup"

# setup DHCP for Ip address assignment
echo -e "${GREEN}[INFO] Configuring DHCP server (dnsmasq)...${NC}"
sudo apt-get install -y dnsmasq
sudo systemctl stop dnsmasq

# Configure WiFi AP
echo -e "${GREEN}[INFO] Configuring WiFi Access Point...${NC}"
CON_NAME="OWL-AP-${DEVICE_ID}"
sudo nmcli con add type wifi ifname wlan0 con-name "$CON_NAME" autoconnect yes \
    ssid "$AP_SSID" mode ap ipv4.method manual \
    ipv4.addresses 192.168.50.1/24 \
    wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$AP_PASS"
check_status "Configuring NetworkManager AP"
sudo nmcli con up "$CON_NAME"
if nmcli con show --active | grep -q "$CON_NAME"; then
    echo -e "${TICK} AP is active."
else
    echo -e "${CROSS} Failed to activate AP."
    exit 1
fi

# Configure dnsmasq
cat << EOF | sudo tee /etc/dnsmasq.d/owl.conf
interface=wlan0
dhcp-range=192.168.50.10,192.168.50.100,24h
dhcp-option=option:router,192.168.50.1
dhcp-option=option:dns-server,192.168.50.1
address=/owl.local/192.168.50.1
domain=local
bogus-priv
domain-needed
EOF

# Ensure dnsmasq starts after AP is ready
sudo systemctl enable dnsmasq
sudo systemctl restart dnsmasq
check_status "Configuring DHCP server"

# Restart services
echo -e "${GREEN}[INFO] Restarting services...${NC}"
sudo systemctl restart nginx
sudo systemctl restart avahi-daemon
sudo systemctl restart ssh
check_status "Restarting services"

# Display access information
echo -e "${GREEN}[INFO] Setup complete!${NC}"
echo -e "${GREEN}[INFO] Connect to WiFi: ${AP_SSID} (Password: [hidden]) and access https://owl.local or https://192.168.50.1${NC}"
echo -e "${GREEN}[INFO] SSH access: ssh ${CURRENT_USER}@192.168.50.1${NC}"
echo -e "${GREEN}[INFO] Check status with:${NC}"
echo "  - nmcli con show"
echo "  - sudo ufw status"