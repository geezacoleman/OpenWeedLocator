#!/bin/bash

# Function to check the exit status of the last executed command
check_status() {
  if [ $? -ne 0 ]; then
    echo "[ERROR] $1 failed."
    exit 1
  else
    echo "[INFO] $1 completed successfully."
  fi
}

# Get device type and number first
echo "[INFO] Setting up OWL device..."
read -p "Is this a dashboard Pi? (y/n): " is_dashboard
case "$is_dashboard" in
  y|Y )
    device_type="dashboard"
    device_id="dashboard"
    ;;
  n|N )
    device_type="owl"
    read -p "Enter OWL number (e.g., 1 for owl-1): " owl_number
    while [[ ! $owl_number =~ ^[0-9]+$ ]]; do
      echo "Invalid input. Please enter a number."
      read -p "Enter OWL number (e.g., 1 for owl-1): " owl_number
    done
    device_id="owl-${owl_number}"
    ;;
  * )
    echo "[ERROR] Invalid input. Please enter y or n."
    exit 1
    ;;
esac

# Free up space
echo "[INFO] Freeing up space by removing unnecessary packages..."
sudo apt-get purge -y wolfram-engine
sudo apt-get purge -y libreoffice*
sudo apt-get clean
check_status "Cleaning up"

sudo apt-get autoremove -y
check_status "Removing unnecessary packages"

# Update the system and firmware
echo "[INFO] Updating the system and firmware..."
sudo apt-get update && sudo apt full-upgrade -y
check_status "System update and upgrade"

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
source /usr/share/virtualenvwrapper/virtualenvwrapper.sh
check_status "Updating .bashrc for virtualenvwrapper"

sleep 1s

# Create the owl virtual environment
echo "[INFO] Creating the 'owl' virtual environment..."
mkvirtualenv --system-site-packages -p python3 owl
check_status "Creating virtual environment 'owl'"

sleep 1s

# Install OpenCV in the owl virtual environment
echo "[INFO] Installing OpenCV in the 'owl' virtual environment..."
source $HOME/.virtualenvs/owl/bin/activate
sleep 1s
pip3 install opencv-contrib-python
check_status "Installing OpenCV"

sleep 1s

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
pcmanfm --set-wallpaper ~/owl/images/owl-background.png
check_status "Setting desktop background"

echo "[INFO] OWL setup complete."
echo "Device ID: $device_id"
echo "Device Type: $device_type"
echo "Authentication credentials have been saved."

read -p "Start OWL focusing? (y/n): " choice
case "$choice" in
  y|Y ) echo "[INFO] Starting focusing..."; ./owl.py --focus;;
  n|N ) echo "[INFO] Focusing skipped. Run './owl.py --focus' to focus the OWL at a later point";;
  * ) echo "[ERROR] Invalid input. Please enter y or n.";;
esac

read -p "Launch OWL software? (y/n): " choice
case "$choice" in
  y|Y ) echo "[INFO] Launching OWL..."; ./owl.py --show-display;;
  n|N ) echo "[INFO] Skipped. Run './owl.py --show-display' to launch the OWL at a later point";;
  * ) echo "[ERROR] Invalid input. Please enter y or n.";;
esac