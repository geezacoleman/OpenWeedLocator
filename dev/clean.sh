#!/bin/bash

cd /home/owl

# Removing user histories and sensitive files
echo "[INFO] Removing history files"
sudo rm -rvf /root/.bash_history /home/owl/.bash_history /root/.viminfo /home/owl/.viminfo /root/.lesshst /home/owl/.lesshst
sudo rm -rvf /root/.ssh /home/owl/.ssh /root/.gnupg /home/owl/.gnupg

# Clearing network information
echo "[INFO] Clearing network information"
sudo rm -rvf /etc/NetworkManager/system-connections/*

# Emptying user-specific and system-wide temporary data
echo "[INFO] Emptying temporary storage"
sudo rm -rvf /tmp/* /var/tmp/*

# Removing logs
echo "[INFO] Removing logs"
sudo rm -rvf /var/log/*

# Clear command history for the current session
history -c

# Zero out free space to help compression and prevent data recovery
echo "[INFO] Zeroing free space - this may take a while"
sudo dd if=/dev/zero of=/bigfile bs=1M status=progress
sudo rm /bigfile

# Shutting down the system
echo "[INFO] Shutting down in 5 seconds"
sleep 5
sudo shutdown -h now