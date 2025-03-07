#!/bin/bash
set -e

# Define colors for status messages
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'
TICK="${GREEN}[OK]${NC}"
CROSS="${RED}[FAIL]${NC}"

# Get current user and home directory
CURRENT_USER=${SUDO_USER:-$(whoami)}
HOME_DIR=$(getent passwd "$CURRENT_USER" | cut -d: -f6)

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
AP_SSID="owl-ap-${OWL_NUMBER}"

# Secure password entry
while true; do
    echo -n "Enter AP password (min 8 chars): "
    read -s AP_PASS
    echo
    if [ ${#AP_PASS} -lt 8 ]; then
        echo -e "${RED}[WARNING] Password must be at least 8 characters. Please try again.${NC}"
    else
        echo -n "Confirm AP password: "
        read -s AP_PASS_CONFIRM
        echo
        if [ "$AP_PASS" != "$AP_PASS_CONFIRM" ]; then
            echo -e "${RED}[WARNING] Passwords do not match. Please try again.${NC}"
        else
            break
        fi
    fi
done

echo -e "${GREEN}[INFO] Setting up OWL Web Interface and Access Point...${NC}"

# Install dependencies
echo -e "${GREEN}[INFO] Installing dependencies...${NC}"
sudo apt update
sudo apt install -y hostapd dnsmasq dhcpcd nginx apache2-utils avahi-daemon ufw
check_status "Installing dependencies"

# Disable NetworkManager to avoid conflicts
echo -e "${GREEN}[INFO] Disabling NetworkManager...${NC}"
sudo systemctl stop NetworkManager
sudo systemctl disable NetworkManager
check_status "Disabled NetworkManager"

# Run OWLAuthSetup (Handles SSL, Authentication, etc.)
echo -e "${GREEN}[INFO] Running OWLAuthSetup...${NC}"
sudo python3 "${HOME_DIR}/owl/dev/setup_auth.py" "${DEVICE_ID}" --dashboard --home-dir "${HOME_DIR}"
check_status "Running OWLAuthSetup"

# Configure static IP for wlan0 with dhcpcd
echo -e "${GREEN}[INFO] Configuring network interface...${NC}"
cat << EOF | sudo tee -a /etc/dhcpcd.conf
interface wlan0
static ip_address=192.168.50.1/24
nohook wpa_supplicant
EOF
sudo systemctl restart dhcpcd
check_status "Configured dhcpcd"

# Configure hostapd
echo -e "${GREEN}[INFO] Setting up Wi-Fi Access Point...${NC}"
cat << EOF | sudo tee /etc/hostapd/hostapd.conf
interface=wlan0
ssid=${AP_SSID}
hw_mode=g
channel=7
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=${AP_PASS}
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
EOF
sudo sed -i 's|#DAEMON_CONF=""|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd
sudo systemctl unmask hostapd  # Ensure hostapd isn’t masked
sudo systemctl enable hostapd
sudo systemctl restart hostapd
sleep 5  # Wait for AP to stabilize
check_status "Configured and started hostapd"

# Configure dnsmasq
echo -e "${GREEN}[INFO] Setting up DHCP server (dnsmasq)...${NC}"
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
sudo systemctl enable dnsmasq
sudo systemctl restart dnsmasq
check_status "Configured and started dnsmasq"

# Restart additional services
echo -e "${GREEN}[INFO] Restarting additional services...${NC}"
sudo systemctl restart nginx avahi-daemon ssh
check_status "Restarted services"

echo -e "${GREEN}[INFO] Setup complete!${NC}"
echo -e "${GREEN}[INFO] Connect to WiFi: ${AP_SSID} (Password: [hidden])${NC}"
echo -e "${GREEN}[INFO] Access OWL securely at: https://owl.local or https://192.168.50.1${NC}"
echo -e "${GREEN}[INFO] Note: You may need to accept the self-signed certificate warning in your browser.${NC}"
echo -e "${GREEN}[INFO] Check status with:${NC}"
echo "  - sudo systemctl status hostapd"
echo "  - sudo systemctl status dnsmasq"
echo "  - sudo ufw status"