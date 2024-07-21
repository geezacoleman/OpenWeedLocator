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

read -p "Zero free space? (y/n): " choice
case "$choice" in
  y|Y )
    echo "[INFO] Zeroing free space"
    sudo dd if=/dev/zero of=/bigfile bs=1M status=progress
    sudo rm /bigfile
    df -h  # Display disk usage after zeroing
    echo "[INFO] Free space zeroed successfully";;
  n|N )
    echo "[INFO] Zeroing skipped";;
  * )
    echo "[ERROR] Invalid input. Please enter y or n.";;
esac

# Shutting down the system
echo "[INFO] Shutting down in 5 seconds"
sleep 5
sudo shutdown -h now