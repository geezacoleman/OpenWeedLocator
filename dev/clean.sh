#!/bin/bash

# Safe OWL Cleaning Script - Preserves Dashboard Configuration
# This script removes sensitive data while keeping OWL dashboard functionality

set -euo pipefail  # Exit on error, undefined vars, pipe failures

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Must run as root or with sudo
if [[ $EUID -ne 0 ]]; then
    echo -e "${RED}[ERROR] This script must be run as root or with sudo${NC}"
    exit 1
fi

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
echo "  • Sudo credential cache"
echo "  • Clipboard data"
echo "  • Recent file lists"
echo

read -p "Continue with cleaning? (y/N): " choice
case "$choice" in
  y|Y )
    echo -e "${GREEN}[INFO] Starting safe cleaning process...${NC}";;
  * )
    echo -e "${RED}[INFO] Cleaning cancelled${NC}"
    exit 0;;
esac

# Stop OWL services during cleaning to prevent log generation
echo -e "${GREEN}[INFO] Stopping OWL services during cleaning${NC}"
systemctl stop owl.service 2>/dev/null || true
systemctl stop owl-dash.service 2>/dev/null || true

cd /home/owl

# Step 1: Remove history files and sensitive personal data
echo -e "${GREEN}[INFO] Removing history files and sensitive data${NC}"
rm -rvf /root/.bash_history /home/owl/.bash_history 2>/dev/null || true
rm -rvf /root/.zsh_history /home/owl/.zsh_history 2>/dev/null || true
rm -rvf /root/.viminfo /home/owl/.viminfo 2>/dev/null || true
rm -rvf /root/.lesshst /home/owl/.lesshst 2>/dev/null || true
rm -rvf /root/.python_history /home/owl/.python_history 2>/dev/null || true
rm -rvf /root/.node_repl_history /home/owl/.node_repl_history 2>/dev/null || true
rm -rvf /root/.wget-hsts /home/owl/.wget-hsts 2>/dev/null || true

# Clear recently-used files list
rm -rvf /home/owl/.local/share/recently-used.xbel 2>/dev/null || true
rm -rvf /root/.local/share/recently-used.xbel 2>/dev/null || true

# Remove SSH keys and known hosts (security critical!)
echo -e "${GREEN}[INFO] Removing SSH keys and known hosts${NC}"
rm -rvf /root/.ssh/* /home/owl/.ssh/* 2>/dev/null || true
mkdir -p /root/.ssh /home/owl/.ssh
chmod 700 /root/.ssh /home/owl/.ssh
chown root:root /root/.ssh
chown owl:owl /home/owl/.ssh

# Remove GPG keys
rm -rvf /root/.gnupg /home/owl/.gnupg 2>/dev/null || true

# Remove sudo timestamp (cached credentials)
rm -rvf /var/run/sudo/* /run/sudo/* 2>/dev/null || true
rm -rvf /var/lib/sudo/lectured/* 2>/dev/null || true

# Step 2: Clean network connections but preserve OWL hotspot
echo -e "${GREEN}[INFO] Cleaning network connections (preserving OWL hotspot)${NC}"

# Find OWL hotspot connection name - be more flexible with pattern matching
OWL_HOTSPOT=$(nmcli -t -f NAME,TYPE connection show | grep -E "OWL|Hotspot" | grep "wireless" | cut -d: -f1 | head -n1)

if [[ -n "$OWL_HOTSPOT" ]]; then
    echo -e "${GREEN}[INFO] Found OWL hotspot: $OWL_HOTSPOT - will preserve${NC}"

    # Get list of all connections except the hotspot
    while IFS= read -r conn; do
        if [[ -n "$conn" && "$conn" != "$OWL_HOTSPOT" ]]; then
            echo "Removing connection: $conn"
            nmcli connection delete "$conn" 2>/dev/null || true
        fi
    done < <(nmcli -t -f NAME connection show | grep -v "^$OWL_HOTSPOT$")
else
    echo -e "${YELLOW}[WARNING] No OWL hotspot found - preserving all connections for safety${NC}"
    echo -e "${YELLOW}[WARNING] Please manually remove unwanted network connections${NC}"
fi

# Step 3: Clean browser data
echo -e "${GREEN}[INFO] Removing browser data${NC}"
rm -rvf /home/owl/.mozilla 2>/dev/null || true
rm -rvf /home/owl/.config/chromium 2>/dev/null || true
rm -rvf /home/owl/.config/google-chrome 2>/dev/null || true
rm -rvf /home/owl/.config/BraveSoftware 2>/dev/null || true

# Step 4: Remove personal files but preserve OWL application
echo -e "${GREEN}[INFO] Cleaning personal files (preserving OWL application)${NC}"

# Remove common personal directories
for dir in Downloads Documents Pictures Videos Music Templates Public; do
    if [[ -d "/home/owl/$dir" ]]; then
        rm -rvf "/home/owl/$dir"
    fi
done

# Clean desktop files
rm -rvf /home/owl/Desktop/* 2>/dev/null || true

# Remove any personal Python packages but keep the OWL virtual environment
echo -e "${GREEN}[INFO] Cleaning personal Python packages (preserving OWL virtualenv)${NC}"
rm -rvf /home/owl/.local/lib/python*/site-packages/* 2>/dev/null || true
rm -rvf /home/owl/.pip 2>/dev/null || true
rm -rvf /root/.pip 2>/dev/null || true

# Clean pip cache
rm -rvf /home/owl/.cache/pip 2>/dev/null || true
rm -rvf /root/.cache/pip 2>/dev/null || true

# Step 5: Clean all caches
echo -e "${GREEN}[INFO] Cleaning all caches${NC}"
rm -rvf /home/owl/.cache/* 2>/dev/null || true
rm -rvf /root/.cache/* 2>/dev/null || true
rm -rvf /home/owl/.thumbnails 2>/dev/null || true

# Step 6: Selectively clean logs
echo -e "${GREEN}[INFO] Cleaning logs${NC}"

# Clear all log content but keep files (for service stability)
find /var/log -name "*.log" -type f -exec truncate -s 0 {} \; 2>/dev/null || true
find /var/log -name "*.log.*" -type f -delete 2>/dev/null || true
find /var/log -name "*.gz" -type f -delete 2>/dev/null || true
find /var/log -name "*.old" -type f -delete 2>/dev/null || true
find /var/log -name "*.[0-9]" -type f -delete 2>/dev/null || true

# Clear specific sensitive logs completely
truncate -s 0 /var/log/auth.log 2>/dev/null || true
truncate -s 0 /var/log/secure 2>/dev/null || true
truncate -s 0 /var/log/wtmp 2>/dev/null || true
truncate -s 0 /var/log/btmp 2>/dev/null || true
truncate -s 0 /var/log/lastlog 2>/dev/null || true

# Clean journal logs completely
journalctl --rotate 2>/dev/null || true
journalctl --vacuum-time=1s 2>/dev/null || true

# Step 7: Clean temporary files
echo -e "${GREEN}[INFO] Emptying temporary storage${NC}"
rm -rvf /tmp/* 2>/dev/null || true
rm -rvf /var/tmp/* 2>/dev/null || true

# Step 8: Clean apt cache
echo -e "${GREEN}[INFO] Cleaning package manager cache${NC}"
apt clean
apt autoremove -y 2>/dev/null || true

# Step 9: Remove any stored credentials/tokens
echo -e "${GREEN}[INFO] Removing stored credentials and tokens${NC}"
rm -rvf /home/owl/.netrc 2>/dev/null || true
rm -rvf /root/.netrc 2>/dev/null || true
rm -rvf /home/owl/.git-credentials 2>/dev/null || true
rm -rvf /root/.git-credentials 2>/dev/null || true
rm -rvf /home/owl/.gitconfig 2>/dev/null || true  # May contain email/name
rm -rvf /root/.gitconfig 2>/dev/null || true

# Clear any AWS/cloud credentials
rm -rvf /home/owl/.aws 2>/dev/null || true
rm -rvf /root/.aws 2>/dev/null || true
rm -rvf /home/owl/.config/gcloud 2>/dev/null || true

# Step 10: Reset file permissions for OWL
echo -e "${GREEN}[INFO] Resetting OWL file permissions${NC}"
chown -R owl:owl /home/owl/owl /home/owl/.virtualenvs 2>/dev/null || true
chmod -R 755 /home/owl/owl 2>/dev/null || true

# Ensure home directory has correct permissions
chmod 750 /home/owl
chown owl:owl /home/owl

# Step 11: Clear command history for current session
echo -e "${GREEN}[INFO] Clearing current session history${NC}"
history -c 2>/dev/null || true
cat /dev/null > ~/.bash_history 2>/dev/null || true

# Step 12: Restart OWL services
echo -e "${GREEN}[INFO] Restarting OWL services${NC}"
systemctl start owl-dash.service 2>/dev/null || true
sleep 2

# Step 13: Verify OWL services are still configured
echo -e "${GREEN}[INFO] Verifying OWL services status${NC}"
echo "OWL Service:"
systemctl is-enabled owl 2>/dev/null && echo "  ✓ Enabled" || echo "  ✗ Not enabled"

echo "OWL Dashboard Service:"
systemctl is-enabled owl-dash 2>/dev/null && echo "  ✓ Enabled" || echo "  ✗ Not enabled"

echo "MQTT Broker:"
systemctl is-enabled mosquitto 2>/dev/null && echo "  ✓ Enabled" || echo "  ✗ Not enabled"

echo "Nginx Web Server:"
systemctl is-enabled nginx 2>/dev/null && echo "  ✓ Enabled" || echo "  ✗ Not enabled"

echo "WiFi Hotspot:"
if nmcli connection show --active 2>/dev/null | grep -qE "(OWL|Hotspot)"; then
    echo "  ✓ Active"
else
    echo "  ⚠ Not currently active (may need reboot)"
fi

# Step 14: Create clean status file
tee /opt/owl-clean-status.txt > /dev/null <<EOF
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
- Command history (bash, zsh, python, node)
- SSH keys and known hosts
- Personal network connections
- Browser data and cache
- Personal files
- Log files
- Git credentials and config
- Cloud provider credentials
- Sudo credential cache
- Pip cache
- All temporary files

OWL Services Status:
- OWL: $(systemctl is-enabled owl 2>/dev/null || echo "disabled")
- Dashboard: $(systemctl is-enabled owl-dash 2>/dev/null || echo "disabled")
- MQTT: $(systemctl is-enabled mosquitto 2>/dev/null || echo "disabled")
- Nginx: $(systemctl is-enabled nginx 2>/dev/null || echo "disabled")

Next steps for new user:
1. Connect to OWL-WIFI-* network (password: check device label)
2. Visit https://owl-*.local/ (accept SSL warning)
3. Configure OWL settings as needed
4. Optionally change WiFi password:
   sudo nmcli connection modify "OWL-WIFI-*" 802-11-wireless-security.psk "new_password"
EOF

echo
echo -e "${GREEN}[INFO] Cleaning completed successfully!${NC}"
echo -e "${GREEN}[INFO] Clean status saved to: /opt/owl-clean-status.txt${NC}"

# Optional: Secure delete free space
echo
echo -e "${YELLOW}[OPTIONAL] Zero free space for extra security?${NC}"
echo "This overwrites deleted data to prevent recovery."
echo "Takes 5-15 minutes depending on free space."
read -p "Zero free space? (y/N): " zero_choice
case "$zero_choice" in
  y|Y )
    echo -e "${GREEN}[INFO] Zeroing free space (this will take a while)...${NC}"
    # Use sync to ensure writes complete
    dd if=/dev/zero of=/bigfile bs=4M status=progress conv=fsync 2>/dev/null || true
    sync
    rm -f /bigfile
    sync
    echo -e "${GREEN}[INFO] Free space zeroed successfully${NC}";;
  * )
    echo -e "${GREEN}[INFO] Zeroing skipped${NC}";;
esac

# Show final disk usage
echo
echo -e "${GREEN}[INFO] Final disk usage:${NC}"
df -h /

echo
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}[COMPLETE] Device is ready for handover!${NC}"
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  ✓ All OWL configurations preserved${NC}"
echo -e "${GREEN}  ✓ Personal/sensitive data removed${NC}"
echo

# Option to shutdown
read -p "Shutdown device now? (y/N): " shutdown_choice
case "$shutdown_choice" in
  y|Y )
    echo -e "${GREEN}[INFO] Syncing filesystems...${NC}"
    sync
    echo -e "${GREEN}[INFO] Shutting down in 5 seconds...${NC}"
    sleep 5
    shutdown -h now;;
  * )
    echo -e "${GREEN}[INFO] Device ready - shutdown skipped${NC}"
    echo -e "${YELLOW}[REMINDER] Remember to shutdown when ready: sudo shutdown -h now${NC}";;
esac