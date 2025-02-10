#!/bin/bash

# Define colors for status messages
RED='\033[0;31m'    # Red for ERROR messages
ORANGE='\033[0;33m' # Orange for warnings
GREEN='\033[0;32m'  # Green for INFO and success messages
NC='\033[0m'        # No color (reset)
TICK="${GREEN}[OK]${NC}"
CROSS="${RED}[FAIL]${NC}"
SCRIPT_DIR=$(dirname "$(realpath "$0")")
CURRENT_USER=${SUDO_USER:-$(whoami)}

if [ "$SUDO_USER" ]; then
   echo -e "${RED}[ERROR] This script should not be run with sudo. Please run as normal user.${NC}"
   exit 1
fi

# Initialize status tracking variables
STATUS_UPGRADE=""
STATUS_CAMERA=""
STATUS_CAMERA_TEST=""
STATUS_FULL_UPGRADE=""
STATUS_VENV=""
STATUS_OPENCV=""
STATUS_OWL_DEPS=""
STATUS_BOOT_SCRIPTS=""

ERROR_UPGRADE=""
ERROR_CAMERA=""
ERROR_CAMERA_TEST=""
ERROR_FULL_UPGRADE=""
ERROR_VENV=""
ERROR_OPENCV=""
ERROR_OWL_DEPS=""
ERROR_BOOT_SCRIPTS=""

if [ "$CURRENT_USER" != "owl" ]; then
   echo -e "${ORANGE}[WARNING] Current user '$CURRENT_USER' differs from expected 'owl'. Some settings may not work correctly.${NC}"
fi

# Function to check the exit status of the last executed command
check_status() {
  if [ $? -ne 0 ]; then
    echo -e "${CROSS} $1 failed."
    eval "STATUS_$2='${CROSS}'"
    eval "ERROR_$2='$1 failed'"
    return 1
  else
    echo -e "${TICK} $1 completed successfully."
    eval "STATUS_$2='${TICK}'"
  fi
}

# Source bashrc to ensure virtualenv commands are available
reload_bashrc() {
    if [ -f ~/.bashrc ]; then
        source ~/.bashrc
        sleep 2
    fi
}

# Function to check if the camera is detected
check_camera_connection() {
  echo -e "${GREEN}[INFO] Checking for connected Raspberry Pi camera...${NC}"
  while true; do
    if rpicam-hello --list-cameras 2>&1 | grep -q "No cameras available"; then
      echo -e "${RED}[ERROR] No camera detected!${NC}"
      read -p "Please connect a Raspberry Pi camera and press Enter to retry..." temp
    else
      echo -e "${GREEN}[INFO] Camera detected successfully.${NC}"
      STATUS_CAMERA="${TICK}"
      return 0
    fi
  done
}

# Step 1: Perform a normal system update and upgrade
echo -e "${GREEN}[INFO] Updating and upgrading the system...${NC}"
sudo apt update
sudo apt full-upgrade -y
check_status "System upgrade" "UPGRADE"

# Step 2: Ensure a camera is connected before proceeding
check_camera_connection

# Step 3: Test camera functionality
echo -e "${GREEN}[INFO] Testing camera functionality...${NC}"
rpicam-hello > /dev/null 2>&1
if [ $? -ne 0 ]; then
  echo -e "${RED}[WARNING] Camera test failed. Running full system upgrade to resolve potential issues...${NC}"
  sudo apt full-upgrade -y
  check_status "Full system upgrade" "FULL_UPGRADE"

  echo -e "${GREEN}[INFO] Retesting camera after full upgrade...${NC}"
  rpicam-hello > /dev/null 2>&1
  if [ $? -ne 0 ]; then
    echo -e "${RED}[CRITICAL ERROR] Camera still not working after full upgrade. Please log an issue: https://github.com/geezacoleman/OpenWeedLocator/issues${NC}"
    STATUS_CAMERA_TEST="${CROSS}"
    ERROR_CAMERA_TEST="No camera detected"
  else
    echo -e "${GREEN}[INFO] Camera test passed after full upgrade.${NC}"
    STATUS_CAMERA_TEST="${TICK}"
  fi
else
  echo -e "${GREEN}[INFO] Camera is working correctly.${NC}"
  STATUS_CAMERA_TEST="${TICK}"
fi

# Step 4: Free up space
echo -e "${GREEN}[INFO] Freeing up space by removing unnecessary packages...${NC}"
sudo apt-get purge -y wolfram-engine libreoffice*
sudo apt-get clean
check_status "Cleaning up" "CLEANUP"

# Step 5: Set up the virtual environment
echo -e "${GREEN}[INFO] Setting up the virtual environment...${NC}"

# Add config to bashrc if not already present
if ! grep -q "virtualenv and virtualenvwrapper" /home/$CURRENT_USER/.bashrc; then
    cat >> /home/$CURRENT_USER/.bashrc << EOF
# virtualenv and virtualenvwrapper
export VIRTUALENVWRAPPER_PYTHON=/usr/bin/python3
export WORKON_HOME=\$HOME/.virtualenvs
source /usr/share/virtualenvwrapper/virtualenvwrapper.sh
EOF
fi

reload_bashrc
sudo apt-get install -y python3-virtualenv python3-virtualenvwrapper
check_status "Installing virtualenv packages" "VENV"

reload_bashrc
source /usr/share/virtualenvwrapper/virtualenvwrapper.sh
check_status "Virtualenv configuration" "VENV"

reload_bashrc
# Step 6: Create and configure the virtual environment
echo -e "${GREEN}[INFO] Creating the 'owl' virtual environment...${NC}"
mkvirtualenv --system-site-packages -p python3 owl
check_status "Creating virtual environment 'owl'" "VENV"

sleep 1s

# Step 7: Install OpenCV in the virtual environment
echo -e "${GREEN}[INFO] Installing OpenCV in the 'owl' virtual environment...${NC}"
source $HOME/.virtualenvs/owl/bin/activate
sleep 1s
pip3 install opencv-contrib-python
check_status "Installing OpenCV" "OPENCV"

sleep 1s

# Step 8: Install OWL dependencies
echo -e "${GREEN}[INFO] Installing the OWL Python dependencies...${NC}"
cd "$SCRIPT_DIR"
pip install -r requirements.txt
check_status "Installing dependencies from requirements.txt" "OWL_DEPS"

# Step 9: Make scripts executable and set up boot configuration
echo -e "${GREEN}[INFO] Making scripts executable...${NC}"
chmod a+x owl.py
check_status "Making owl.py executable" "BOOT_SCRIPTS"

chmod a+x owl_boot.sh
chmod a+x owl_boot_wrapper.sh
check_status "Making boot scripts executable" "BOOT_SCRIPTS"

echo -e "${GREEN}[INFO] Moving boot scripts...${NC}"
sudo mv owl_boot.sh /usr/local/bin/
sudo mv owl_boot_wrapper.sh /usr/local/bin/
check_status "Moving boot scripts" "BOOT_SCRIPTS"

# Add boot script to cron
echo -e "${GREEN}[INFO] Adding boot script to cron...${NC}"
(crontab -l 2>/dev/null; echo "@reboot /usr/local/bin/owl_boot_wrapper.sh > /home/launch.log 2>&1") | sudo crontab -
check_status "Adding boot script to cron" "BOOT_SCRIPTS"

# set desktop background - check for wayland or X11
echo -e "${GREEN}[INFO] Setting desktop background...${NC}"
pcmanfm --set-wallpaper $SCRIPT_DIR/images/owl-background.png
check_status "Setting desktop background" "BOOT_SCRIPTS"

# Final Summary
echo -e "\n${GREEN}[INFO] Installation Summary:${NC}"
echo -e "$STATUS_UPGRADE System Upgrade"
echo -e "$STATUS_CAMERA Camera Detected"
echo -e "$STATUS_CAMERA_TEST Camera Test"

if [[ -n "$STATUS_FULL_UPGRADE" ]]; then
    echo -e "$STATUS_FULL_UPGRADE Full System Upgrade"
fi

echo -e "$STATUS_VENV Virtual Environment Created"
echo -e "$STATUS_OPENCV OpenCV Installed"
echo -e "$STATUS_OWL_DEPS OWL Dependencies Installed"
echo -e "$STATUS_BOOT_SCRIPTS Boot Scripts Moved"

# Step 10: Start OWL focusing
read -p "Start OWL focusing? (y/n): " choice
case "$choice" in
  y|Y ) echo -e "${GREEN}[INFO] Starting focusing...${NC}"; ./owl.py --focus;;
  n|N ) echo -e "${GREEN}[INFO] Focusing skipped. Run './owl.py --focus' to focus the OWL later.${NC}";;
  * ) echo -e "${RED}[ERROR] Invalid input. Please enter y or n.${NC}";;
esac

# Step 11: Launch OWL
read -p "Launch OWL software? (y/n): " choice
case "$choice" in
  y|Y ) echo -e "${GREEN}[INFO] Launching OWL...${NC}"; ./owl.py --show-display;;
  n|N ) echo -e "${GREEN}[INFO] Skipped. Run './owl.py --show-display' to launch OWL later.${NC}";;
  * ) echo -e "${RED}[ERROR] Invalid input. Please enter y or n.${NC}";;
esac
