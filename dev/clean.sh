#!/bin/bash

# Safe OWL Cleaning Script - Preserves Dashboard Configuration
# This script removes sensitive data while keeping OWL dashboard functionality

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}[INFO] OWL Safe Cleaning Script${NC}"
echo -e "${GREEN}================================${NC}"
echo -e "${YELLOW}[WARNING] This will remove personal data but preserve OWL configuration${NC}"
echo

# Show what will be preserved
echo -e "${GREEN}[INFO] The following OWL configurations will be PRESERVED:${NC}"
echo "  • WiFi hotspot configuration"
echo "  • MQTT broker settings (/etc/mosquitto/)"
echo "  • Nginx web server configuration"
echo "  • SSL certificates"
echo "  • OWL dashboard systemd service"
echo "  • Avahi/mDNS configuration"
echo "  • UFW firewall rules"
echo "  • OWL application files (/home/owl/owl/)"
echo

echo -e "${YELLOW}[WARNING] The following will be REMOVED:${NC}"
echo "  • Command history files"
echo "  • SSH keys and known hosts"
echo "  • Personal WiFi network connections (keeps hotspot)"
echo "  • Browser data and bookmarks"
echo "  • Most log files (keeps system essentials)"
echo "  • Temporary files"
echo "  • Personal files in home directory"
echo

read -p "Continue with cleaning? (y/N): " choice
case "$choice" in
  y|Y )
    echo -e "${GREEN}[INFO] Starting safe cleaning process...${NC}";;
  * )
    echo -e "${RED}[INFO] Cleaning cancelled${NC}"
    exit 0;;
esac

cd /home/owl

# Step 1: Remove history files and sensitive personal data
echo -e "${GREEN}[INFO] Removing history files and sensitive data${NC}"
sudo rm -rvf /root/.bash_history /home/owl/.bash_history
sudo rm -rvf /root/.viminfo /home/owl/.viminfo
sudo rm -rvf /root/.lesshst /home/owl/.lesshst
sudo rm -rvf /root/.python_history /home/owl/.python_history

# Remove SSH keys but keep the directory structure
echo -e "${GREEN}[INFO] Removing SSH keys${NC}"
sudo rm -rvf /root/.ssh/* /home/owl/.ssh/*
sudo mkdir -p /root/.ssh /home/owl/.ssh
sudo chmod 700 /root/.ssh /home/owl/.ssh
sudo chown root:root /root/.ssh
sudo chown owl:owl /home/owl/.ssh

# Remove GPG keys
sudo rm -rvf /root/.gnupg /home/owl/.gnupg

# Step 2: Clean network connections but preserve OWL hotspot
echo -e "${GREEN}[INFO] Cleaning network connections (preserving OWL hotspot)${NC}"

# Find OWL hotspot connection name
OWL_HOTSPOT=$(nmcli connection show | grep -E "(OWL-WIFI|Hotspot)" | awk '{print $1}' | head -n1)

if [[ -n "$OWL_HOTSPOT" ]]; then
    echo -e "${GREEN}[INFO] Found OWL hotspot: $OWL_HOTSPOT - will preserve${NC}"

    # Remove all connections except the OWL hotspot
    for conn in $(nmcli connection show | grep -v "$OWL_HOTSPOT" | awk 'NR>1 {print $1}'); do
        if [[ "$conn" != "NAME" && "$conn" != "$OWL_HOTSPOT" ]]; then
            echo "Removing connection: $conn"
            sudo nmcli connection delete "$conn" 2>/dev/null || true
        fi
    done
else
    echo -e "${YELLOW}[WARNING] No OWL hotspot found - removing all network connections${NC}"
    sudo rm -rvf /etc/NetworkManager/system-connections/*
fi

# Step 3: Clean browser data
echo -e "${GREEN}[INFO] Removing browser data${NC}"
sudo rm -rvf /home/owl/.mozilla /home/owl/.config/chromium /home/owl/.cache

# Step 4: Remove personal files but preserve OWL application
echo -e "${GREEN}[INFO] Cleaning personal files (preserving OWL application)${NC}"

# Remove common personal directories but preserve OWL
for dir in Downloads Documents Pictures Videos Music Templates Public; do
    if [[ -d "/home/owl/$dir" ]]; then
        sudo rm -rvf "/home/owl/$dir"
    fi
done

# Clean desktop files
sudo rm -rvf /home/owl/Desktop/* 2>/dev/null || true

# Remove any personal Python packages but keep the OWL virtual environment
echo -e "${GREEN}[INFO] Cleaning personal Python packages (preserving OWL virtualenv)${NC}"
sudo rm -rvf /home/owl/.local/lib/python*/site-packages/* 2>/dev/null || true

# Step 5: Selectively clean logs (preserve essential system logs)
echo -e "${GREEN}[INFO] Cleaning logs (preserving essential system logs)${NC}"

# Clear OWL-specific logs but keep recent system logs
sudo find /var/log -name "*.log" -type f -exec truncate -s 0 {} \; 2>/dev/null || true
sudo find /var/log -name "*.log.*" -type f -delete 2>/dev/null || true

# Clean journal logs older than 1 day (keeps recent for debugging)
sudo journalctl --vacuum-time=1d

# Preserve but truncate important logs
for log in syslog daemon.log kern.log auth.log; do
    if [[ -f "/var/log/$log" ]]; then
        sudo tail -n 100 "/var/log/$log" > "/tmp/temp_$log" 2>/dev/null || true
        sudo mv "/tmp/temp_$log" "/var/log/$log" 2>/dev/null || true
    fi
done

# Step 6: Clean temporary files
echo -e "${GREEN}[INFO] Emptying temporary storage${NC}"
sudo rm -rvf /tmp/*
sudo rm -rvf /var/tmp/*
sudo rm -rvf /home/owl/.cache/*

# Step 7: Clear command history for current session
history -c

# Step 8: Clean apt cache
echo -e "${GREEN}[INFO] Cleaning package manager cache${NC}"
sudo apt clean
sudo apt autoremove -y

# Step 9: Reset file permissions for OWL
echo -e "${GREEN}[INFO] Resetting OWL file permissions${NC}"
sudo chown -R owl:owl /home/owl/owl /home/owl/.virtualenvs 2>/dev/null || true
sudo chmod -R 755 /home/owl/owl 2>/dev/null || true

# Step 10: Verify OWL services are still configured
echo -e "${GREEN}[INFO] Verifying OWL services status${NC}"
echo "OWL Dashboard Service:"
systemctl is-enabled owl-dash 2>/dev/null && echo "  ✓ Enabled" || echo "  ✗ Not enabled"

echo "MQTT Broker:"
systemctl is-enabled mosquitto 2>/dev/null && echo "  ✓ Enabled" || echo "  ✗ Not enabled"

echo "Nginx Web Server:"
systemctl is-enabled nginx 2>/dev/null && echo "  ✓ Enabled" || echo "  ✗ Not enabled"

echo "WiFi Hotspot:"
if nmcli connection show --active | grep -qE "(OWL-WIFI|Hotspot)"; then
    echo "  ✓ Active"
else
    echo "  ✗ Not active"
fi

# Step 11: Create clean status file
sudo tee /opt/owl-clean-status.txt > /dev/null <<EOF
OWL Device Cleaned: $(date)
================================

Device has been cleaned and is ready for handover.

Preserved configurations:
- OWL Dashboard (https://owl-*.local/)
- WiFi Hotspot (OWL-WIFI-*)
- MQTT Broker (localhost:1883)
- Web server and SSL certificates
- All OWL application files

Removed:
- Command history
- SSH keys
- Personal network connections
- Browser data and cache
- Personal files
- Most log files

OWL Services Status:
- Dashboard: $(systemctl is-enabled owl-dash 2>/dev/null || echo "disabled")
- MQTT: $(systemctl is-enabled mosquitto 2>/dev/null || echo "disabled")
- Nginx: $(systemctl is-enabled nginx 2>/dev/null || echo "disabled")

Next steps for new user:
1. Connect to OWL-WIFI-* network
2. Visit https://owl-*.local/ (accept SSL warning)
3. Configure OWL settings as needed
4. Change WiFi password if desired: sudo nmcli connection modify "OWL-WIFI-*" 802-11-wireless-security.psk "new_password"
EOF

echo
echo -e "${GREEN}[INFO] Cleaning completed successfully!${NC}"
echo -e "${GREEN}[INFO] Clean status saved to: /opt/owl-clean-status.txt${NC}"

# Optional: Zero free space
echo
read -p "Zero free space for security? (y/N): " zero_choice
case "$zero_choice" in
  y|Y )
    echo -e "${GREEN}[INFO] Zeroing free space (this may take a while)${NC}"
    sudo dd if=/dev/zero of=/bigfile bs=1M status=progress 2>/dev/null || true
    sudo rm -f /bigfile 2>/dev/null || true
    echo -e "${GREEN}[INFO] Free space zeroed successfully${NC}";;
  * )
    echo -e "${GREEN}[INFO] Zeroing skipped${NC}";;
esac

# Show final disk usage
echo
echo -e "${GREEN}[INFO] Final disk usage:${NC}"
df -h

echo
echo -e "${GREEN}[COMPLETE] Device is ready for handover!${NC}"
echo -e "${GREEN}[INFO] All OWL configurations preserved${NC}"
echo -e "${GREEN}[INFO] Personal data removed${NC}"

# Option to shutdown
echo
read -p "Shutdown device now? (y/N): " shutdown_choice
case "$shutdown_choice" in
  y|Y )
    echo -e "${GREEN}[INFO] Shutting down in 5 seconds...${NC}"
    sleep 5
    sudo shutdown -h now;;
  * )
    echo -e "${GREEN}[INFO] Device ready - shutdown skipped${NC}"
    echo -e "${GREEN}[INFO] Remember to shutdown when ready: sudo shutdown -h now${NC}";;
esac