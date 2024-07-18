#!/bin/bash

set -e  # Exit immediately if a command exits with a non-zero status

# Function to check the exit status of the last executed command
check_status() {
  if [ $? -ne 0 ]; then
    echo "[ERROR] $1 failed."
    exit 1
  else
    echo "[INFO] $1 completed successfully."
  fi
}

# Free up space
echo "[INFO] Freeing up space by removing unnecessary packages..."
sudo apt-get purge -y wolfram-engine
check_status "Removing wolfram-engine"

sudo apt-get purge -y libreoffice*
check_status "Removing libreoffice"

sudo apt-get clean
check_status "Cleaning up"

sudo apt-get autoremove -y
check_status "Removing unnecessary packages"

# Update the system and firmware
echo "[INFO] Updating the system and firmware..."
sudo apt-get update && sudo apt-get upgrade -y
check_status "System update and upgrade"

sudo rpi-update
check_status "Firmware update"

# Set up the virtual environment
echo "[INFO] Setting up the virtual environment..."
echo "# virtualenv and virtualenvwrapper" >> ~/.bashrc
echo "export VIRTUALENVWRAPPER_PYTHON=/usr/bin/python3" >> ~/.bashrc
source ~/.bashrc
check_status "Updating .bashrc for virtualenv"

# Install virtualenv and virtualenvwrapper
echo "[INFO] Installing virtualenv and virtualenvwrapper..."
sudo apt-get install -y python3-virtualenv
check_status "Installing python3-virtualenv"

sudo apt-get install -y python3-virtualenvwrapper
check_status "Installing python3-virtualenvwrapper"

echo "export WORKON_HOME=$HOME/.virtualenvs" >> ~/.bashrc
echo "source /usr/share/virtualenvwrapper/virtualenvwrapper.sh" >> ~/.bashrc
source ~/.bashrc
check_status "Updating .bashrc for virtualenvwrapper"

# Create the owl virtual environment
echo "[INFO] Creating the 'owl' virtual environment..."
mkvirtualenv --system-site-packages -p python3 owl
check_status "Creating virtual environment 'owl'"

# Install OpenCV in the owl virtual environment
echo "[INFO] Installing OpenCV in the 'owl' virtual environment..."
workon owl
pip3 install opencv-contrib-python
check_status "Installing OpenCV"

# Download the owl repository
echo "[INFO] Downloading the OWL repository..."
cd ~
git clone https://github.com/geezacoleman/OpenWeedLocator owl
check_status "Cloning OWL repository"

# Install the OWL Python dependencies
echo "[INFO] Installing the OWL Python dependencies..."
cd ~/owl
pip install -r requirements.txt
check_status "Installing dependencies from requirements.txt"

# Make the scripts executable
echo "[INFO] Making scripts executable..."
chmod a+x owl.py
check_status "Making owl.py executable"

chmod a+x owl_boot.sh
check_status "Making owl_boot.sh executable"

chmod a+x owl_boot_wrapper.sh
check_status "Making owl_boot_wrapper.sh executable"

# Move the boot scripts to /usr/local/bin
echo "[INFO] Moving boot scripts to /usr/local/bin..."
sudo mv owl_boot.sh /usr/local/bin/owl_boot.sh
check_status "Moving owl_boot.sh"

sudo mv owl_boot_wrapper.sh /usr/local/bin/owl_boot_wrapper.sh
check_status "Moving owl_boot_wrapper.sh"

# Add the boot script to cron for startup
echo "[INFO] Adding boot script to cron..."
(crontab -l 2>/dev/null; echo "@reboot /usr/local/bin/owl_boot_wrapper.sh > /home/launch.log 2>&1") | sudo crontab -
check_status "Adding boot script to cron"

echo "[INFO] Setting owl-background.png as the desktop background..."
pcmanfm --set-wallpaper=~/owl/images/owl-background.png
check_status "Setting desktop background"

echo "[INFO] OWL setup complete. Do you want to reboot now? (y/n)"
read -p "Reboot now? (y/n): " choice
case "$choice" in
  y|Y ) echo "[INFO] Rebooting now..."; sudo reboot;;
  n|N ) echo "[INFO] Reboot skipped. Please reboot manually later to complete OWL setup.";;
  * ) echo "[ERROR] Invalid input. Please enter y or n.";;
esac
