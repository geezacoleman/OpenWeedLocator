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

if systemctl is-active --quiet owl.service; then
    echo -e "${ORANGE}[WARNING] The owl.service is currently running.${NC}"
    read -p "Do you want to stop the service to continue with the installation? (y/n): " stop_choice
    if [[ "$stop_choice" =~ ^[Yy]$ ]]; then
        echo -e "${GREEN}[INFO] Stopping owl.service...${NC}"
        sudo systemctl stop owl.service
        sleep 2
    else
        echo -e "${RED}[ERROR] Please stop the owl.service before running this script (sudo systemctl stop owl.service).${NC}"
        exit 1
    fi
fi

if [ "$CURRENT_USER" != "owl" ]; then
   echo -e "${ORANGE}[WARNING] Current user '$CURRENT_USER' differs from expected 'owl'. Some settings may not work correctly.${NC}"
fi

# Initialize status tracking variables
STATUS_UPGRADE=""
STATUS_CAMERA=""
STATUS_CAMERA_TEST=""
STATUS_FULL_UPGRADE=""
STATUS_VENV=""
STATUS_OPENCV=""
STATUS_OWL_DEPS=""
STATUS_OWL_SERVICE=""
STATUS_DESKTOP_ICON=""
STATUS_DASHBOARD_DEPS=""
STATUS_DASHBOARD=""
STATUS_GLOBAL_NUMPY=""
STATUS_NUMPY_COMPAT=""
STATUS_GOG_DEPS=""
STATUS_CONTROLLER=""

ERROR_UPGRADE=""
ERROR_CAMERA=""
ERROR_CAMERA_TEST=""
ERROR_FULL_UPGRADE=""
ERROR_VENV=""
ERROR_OPENCV=""
ERROR_OWL_DEPS=""
ERROR_OWL_SERVICE=""
ERROR_DESKTOP_ICON=""
ERROR_DASHBOARD_DEPS=""
ERROR_DASHBOARD=""
ERROR_GLOBAL_NUMPY=""
ERROR_NUMPY_COMPAT=""
ERROR_GOG_DEPS=""
ERROR_CONTROLLER=""

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

install_dashboard_dependencies() {
  echo -e "${GREEN}[INFO] Installing dashboard Python dependencies...${NC}"
  source $HOME/.virtualenvs/owl/bin/activate
  pip install flask gunicorn paho-mqtt psutil boto3 pyserial
  check_status "Installing dashboard Python dependencies" "DASHBOARD_DEPS"

  echo -e "${GREEN}[INFO] Verifying Python package installations...${NC}"
  FLASK_VERSION=$(python -c "import flask; print(flask.__version__)" 2>/dev/null)
  GUNICORN_VERSION=$(python -c "import gunicorn; print(gunicorn.__version__)" 2>/dev/null)
  PAHO_VERSION=$(python -c "import paho.mqtt.client; print('installed')" 2>/dev/null)

  if [[ -n "$FLASK_VERSION" && -n "$GUNICORN_VERSION" && "$PAHO_VERSION" == "installed" ]]; then
      echo -e "${TICK} Flask: $FLASK_VERSION, Gunicorn: $GUNICORN_VERSION, Paho-MQTT: installed"
      check_status "Verifying Python dependencies" "DASHBOARD_DEPS"
  else
      echo -e "${CROSS} Some Python packages failed to install"
      check_status "Verifying Python dependencies" "DASHBOARD_DEPS"
  fi
}

check_camera_connection() {
  echo -e "${GREEN}[INFO] Checking for connected cameras...${NC}"

  # 1 — Detect Raspberry Pi CSI camera via libcamera
  if command -v rpicam-hello >/dev/null 2>&1; then
    if rpicam-hello --list-cameras 2>&1 | grep -qv "No cameras available"; then
      echo -e "${GREEN}[INFO] Raspberry Pi CSI camera detected.${NC}"
      CAMERA_MODE="rpi"
      STATUS_CAMERA="${TICK}"
      return 0
    fi
  fi

  # 2 — Detect USB camera
  if command -v v4l2-ctl >/dev/null 2>&1 && [[ -e /dev/video0 ]]; then
    # basic sanity check — camera responds
    if v4l2-ctl -d /dev/video0 --all >/dev/null 2>&1; then
      echo -e "${GREEN}[INFO] USB webcam detected at /dev/video0.${NC}"
      CAMERA_MODE="usb"
      STATUS_CAMERA="${TICK}"
      return 0
    fi
  fi

  # 3 — Neither exists, warn but continue install
  echo -e "${RED}[WARNING] No camera detected (no CSI or USB).${NC}"
  echo -e "${RED}[WARNING] OWL will not run until a camera is present.${NC}"
  CAMERA_MODE="none"
  STATUS_CAMERA="${CROSS}"
  return 0
}

# check to see if the camera works
test_camera_functionality() {
  local mode="$1"  # "rpi", "usb" or "none"
  local test_cmd=""

  echo -e "${GREEN}[INFO] Testing camera functionality (mode: ${mode})...${NC}"

  if [[ "$mode" == "none" ]]; then
    echo -e "${RED}[WARN] Skipping camera test: no camera detected.${NC}"
    STATUS_CAMERA_TEST="${CROSS}"
    ERROR_CAMERA_TEST="No camera detected"
    return
  fi

  if [[ "$mode" == "usb" ]]; then
    if ! command -v v4l2-ctl >/dev/null 2>&1; then
      echo -e "${ORANGE}[WARN] v4l2-ctl not found. Install with: sudo apt install v4l-utils${NC}"
      STATUS_CAMERA_TEST="${CROSS}"
      ERROR_CAMERA_TEST="v4l2-ctl missing for USB camera test"
      return
    fi

    if [[ ! -e /dev/video0 ]]; then
      echo -e "${RED}[WARNING] /dev/video0 not found. USB camera not available.${NC}"
      STATUS_CAMERA_TEST="${CROSS}"
      ERROR_CAMERA_TEST="No /dev/video0 device"
      return
    fi

    # Headless-safe: just query controls / capabilities
    test_cmd="v4l2-ctl -d /dev/video0 --all"
  else
    # Default to Raspberry Pi CSI camera mode
    if ! command -v rpicam-hello >/dev/null 2>&1; then
      echo -e "${ORANGE}[WARN] rpicam-hello not found. Skipping Pi camera test.${NC}"
      STATUS_CAMERA_TEST="${CROSS}"
      ERROR_CAMERA_TEST="rpicam-hello missing"
      return
    fi

    # Headless-safe: no preview, short timeout
    test_cmd="rpicam-hello --nopreview --timeout 200"
  fi

  # First test
  ${test_cmd} > /dev/null 2>&1
  if [ $? -ne 0 ]; then
    echo -e "${RED}[WARNING] Camera test failed. Running full system upgrade to resolve potential issues...${NC}"
    sudo apt full-upgrade -y
    check_status "Full system upgrade" "FULL_UPGRADE"

    echo -e "${GREEN}[INFO] Retesting camera after full upgrade...${NC}"
    ${test_cmd} > /dev/null 2>&1
    if [ $? -ne 0 ]; then
      echo -e "${RED}[CRITICAL ERROR] Camera still not working after full upgrade. Please log an issue: https://github.com/geezacoleman/OpenWeedLocator/issues${NC}"
      STATUS_CAMERA_TEST="${CROSS}"
      ERROR_CAMERA_TEST="Camera test failed in mode '${mode}'"
    else
      echo -e "${GREEN}[INFO] Camera test passed after full upgrade.${NC}"
      STATUS_CAMERA_TEST="${TICK}"
    fi
  else
    echo -e "${GREEN}[INFO] Camera is working correctly.${NC}"
    STATUS_CAMERA_TEST="${TICK}"
  fi
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

echo -e "${GREEN}[INFO] Checking NumPy consistency (system vs venv) ...${NC}"

# Helper to read "version path" from a given python
_py_numpy_info() {
  local py="$1"
  "$py" - <<'PY' 2>/dev/null || true
try:
    import numpy as np
    print(np.__version__, np.__file__)
except Exception:
    print("")
PY
}

# is system NumPy present?
if ! /usr/bin/python3 - <<'PY' >/dev/null 2>&1
try:
    import numpy  # noqa
except Exception:
    raise SystemExit(1)
PY
then
  echo -e "${ORANGE}[WARN] System NumPy missing. Recommend: sudo apt-get install -y python3-numpy.${NC}"
  STATUS_GLOBAL_NUMPY="${CROSS}"
  ERROR_GLOBAL_NUMPY="System NumPy not importable."
  GLOBAL_NUMPY_VERSION=""
  GLOBAL_NUMPY_PATH=""
else
  SYS_INFO="$(_py_numpy_info /usr/bin/python3)"
  GLOBAL_NUMPY_VERSION="$(awk '{print $1}' <<<"$SYS_INFO")"
  GLOBAL_NUMPY_PATH="$(awk '{print $2}' <<<"$SYS_INFO")"
  if [ -z "$GLOBAL_NUMPY_VERSION" ]; then
    echo -e "${RED}[ERROR] Could not import system NumPy after check.${NC}"
    STATUS_GLOBAL_NUMPY="${CROSS}"
    ERROR_GLOBAL_NUMPY="System NumPy import failed."
  else
    echo -e "${GREEN}[OK] System NumPy: ${GLOBAL_NUMPY_VERSION} at ${GLOBAL_NUMPY_PATH}.${NC}"
    STATUS_GLOBAL_NUMPY="${TICK}"
  fi
fi

# venv NumPy (could resolve to system via --system-site-packages)
VENV_INFO="$(_py_numpy_info "$VIRTUAL_ENV/bin/python")"
VENV_NUMPY_VERSION="$(awk '{print $1}' <<<"$VENV_INFO")"
VENV_NUMPY_PATH="$(awk '{print $2}' <<<"$VENV_INFO")"

if [ -z "$VENV_NUMPY_VERSION" ]; then
  if [ -n "${GLOBAL_NUMPY_VERSION:-}" ]; then
    echo -e "${ORANGE}[WARN] NumPy not importable inside venv. Installing numpy==${GLOBAL_NUMPY_VERSION} into venv...${NC}"
    if ! pip install --no-input "numpy==${GLOBAL_NUMPY_VERSION}" >/dev/null 2>&1; then
      echo -e "${RED}[ERROR] Failed to install numpy==${GLOBAL_NUMPY_VERSION} into venv.${NC}"
      ERROR_NUMPY_COMPAT="Failed venv numpy install to match system."
    fi
    VENV_INFO="$(_py_numpy_info "$VIRTUAL_ENV/bin/python")"
    VENV_NUMPY_VERSION="$(awk '{print $1}' <<<"$VENV_INFO")"
    VENV_NUMPY_PATH="$(awk '{print $2}' <<<"$VENV_INFO")"
  else
    echo -e "${ORANGE}[WARN] Venv NumPy missing and system version unknown; skipping venv install attempt.${NC}"
    ERROR_NUMPY_COMPAT="Venv numpy missing; system version unknown."
  fi
fi

if [ -z "$VENV_NUMPY_VERSION" ]; then
  echo -e "${RED}[ERROR] NumPy still not importable inside venv after attempt.${NC}"
  [ -z "$ERROR_NUMPY_COMPAT" ] && ERROR_NUMPY_COMPAT="Venv numpy import failed."
else
  echo -e "${GREEN}[INFO] Venv NumPy: ${VENV_NUMPY_VERSION} at ${VENV_NUMPY_PATH}${NC}"

  # Align versions when both known and differ
  if [ -n "${GLOBAL_NUMPY_VERSION:-}" ] && [ "$VENV_NUMPY_VERSION" != "$GLOBAL_NUMPY_VERSION" ]; then
    echo -e "${ORANGE}[WARN] NumPy mismatch detected (venv=${VENV_NUMPY_VERSION}, system=${GLOBAL_NUMPY_VERSION}). Aligning venv to system...${NC}"
    if ! pip install --no-input "numpy==${GLOBAL_NUMPY_VERSION}" >/dev/null 2>&1; then
      echo -e "${RED}[ERROR] Failed to align venv numpy to ${GLOBAL_NUMPY_VERSION}.${NC}"
      ERROR_NUMPY_COMPAT="Failed to align venv numpy to system version."
    else
      VENV_INFO="$(_py_numpy_info "$VIRTUAL_ENV/bin/python")"
      VENV_NUMPY_VERSION="$(awk '{print $1}' <<<"$VENV_INFO")"
      VENV_NUMPY_PATH="$(awk '{print $2}' <<<"$VENV_INFO")"
      if [ "$VENV_NUMPY_VERSION" != "$GLOBAL_NUMPY_VERSION" ]; then
        echo -e "${RED}[ERROR] Post-align mismatch persists (venv=${VENV_NUMPY_VERSION} vs system=${GLOBAL_NUMPY_VERSION}).${NC}"
        ERROR_NUMPY_COMPAT="Post-align mismatch persists."
      else
        echo -e "${GREEN}[OK] Venv NumPy aligned to system: ${VENV_NUMPY_VERSION}${NC}"
        STATUS_NUMPY_COMPAT="${TICK}"
      fi
    fi
  else
    if [ -n "${GLOBAL_NUMPY_VERSION:-}" ]; then
      echo -e "${GREEN}[OK] NumPy versions match: ${VENV_NUMPY_VERSION}${NC}"
      STATUS_NUMPY_COMPAT="${TICK}"
    else
      echo -e "${ORANGE}[WARN] Skipped compatibility check (system NumPy unknown).${NC}"
    fi
  fi
fi


# Step 7: Install OpenCV in the virtual environment
echo -e "${GREEN}[INFO] Installing opencv-contrib-python in the 'owl' virtual environment...${NC}"
source $HOME/.virtualenvs/owl/bin/activate
sleep 1s
pip install opencv-contrib-python
check_status "Installing opencv-contrib-python" "OPENCV"

# 4) Final runtime check with OpenCV present
"$VIRTUAL_ENV/bin/python" - <<'PY' || { echo "[ERROR] Post-check import failed."; exit 1; }
import numpy as np, cv2
print(f"[OK] Final check: NumPy {np.__version__}, OpenCV {cv2.__version__}")
PY

# Step 8: Install OWL dependencies
echo -e "${GREEN}[INFO] Installing the OWL Python dependencies...${NC}"
cd "$SCRIPT_DIR"
pip install -r requirements.txt
check_status "Installing dependencies from requirements.txt" "OWL_DEPS"

# Step 8b: Optional Green-on-Green (YOLO) support
echo -e "${GREEN}[INFO] Green-on-Green (YOLO) support available...${NC}"
read -p "Do you want to install Green-on-Green (YOLO) support? This requires ~2GB of additional packages. (y/n): " gog_choice
case "$gog_choice" in
  y|Y )
    echo -e "${GREEN}[INFO] Installing Green-on-Green dependencies...${NC}"
    pip install -r requirements-gog.txt
    check_status "Installing GoG dependencies from requirements-gog.txt" "GOG_DEPS"
    ;;
  n|N )
    echo -e "${GREEN}[INFO] Green-on-Green setup skipped.${NC}"
    STATUS_GOG_DEPS="SKIPPED"
    ;;
  * )
    echo -e "${RED}[ERROR] Invalid input. Green-on-Green setup skipped.${NC}"
    STATUS_GOG_DEPS="SKIPPED"
    ;;
esac

# Step 9: Make scripts executable and set up boot configuration
echo -e "${GREEN}[INFO] Setting up OWL to start on boot with systemd...${NC}"
chmod a+x owl.py

setup_owl_systemd_service
check_status "Creating OWL systemd service" "OWL_SERVICE"

# Step 10: Set desktop background - check for wayland or X11
echo -e "${GREEN}[INFO] Setting desktop background...${NC}"
pcmanfm --set-wallpaper $SCRIPT_DIR/images/owl-background.png
check_status "Setting desktop background" "BOOT_SCRIPTS"
sleep 2

# Step 11: creating desktop icon for focusing
echo -e "${GREEN}[INFO] Creating OWL Focusing desktop icon...${NC}"

FOCUS_WRAPPER="${SCRIPT_DIR}/desktop/focus_owl_desktop.sh"
FOCUS_GUI="${SCRIPT_DIR}/desktop/focus_gui.py"
chmod +x "$FOCUS_WRAPPER"
chmod +x "$FOCUS_GUI"

DESKTOP_DIR="$HOME/Desktop"
if [ ! -d "$DESKTOP_DIR" ]; then
    DESKTOP_DIR="$HOME/.local/share/applications"
    mkdir -p "$DESKTOP_DIR"
fi

DESKTOP_FILE="${DESKTOP_DIR}/Focus.desktop"
cat << EOF > "$DESKTOP_FILE"
[Desktop Entry]
Version=1.0
Type=Application
Name=Focus
Comment=Run OWL focusing mode
Exec=${FOCUS_WRAPPER}
Icon=${SCRIPT_DIR}/images/owl-logo.png
Terminal=false
Categories=Utility;
EOF
chmod +x "$DESKTOP_FILE"
echo -e "${GREEN}[INFO] Focus OWL desktop icon created at: ${DESKTOP_FILE}${NC}"
check_status "Creating desktop icon" "DESKTOP_ICON"

# Step 12: Hardware Controller Setup
echo -e "${GREEN}[INFO] Hardware controller setup...${NC}"
echo -e "${GREEN}[INFO] If you have a physical switch panel connected to the GPIO pins,${NC}"
echo -e "${GREEN}[INFO] select the matching controller type below.${NC}"
echo ""
echo "  Controller types:"
echo "    none     - No hardware controller (dashboard-only control)"
echo "    ute      - Single toggle switch (recording or detection)"
echo "               Default pin: BOARD36, purpose: recording"
echo "    advanced - Multi-switch panel (recording, sensitivity, detection mode)"
echo "               Default pins: recording=BOARD38, sensitivity=BOARD40,"
echo "               detection_up=BOARD36, detection_down=BOARD35"
echo ""
read -p "Enter controller type [none/ute/advanced] (default: none): " ctrl_choice
ctrl_choice="${ctrl_choice,,}"  # lowercase
ctrl_choice="${ctrl_choice:-none}"

case "$ctrl_choice" in
  none|ute|advanced )
    echo -e "${GREEN}[INFO] Setting controller_type = ${ctrl_choice}${NC}"

    OWL_CONFIG="${SCRIPT_DIR}/config/GENERAL_CONFIG.ini"
    if [ -f "$OWL_CONFIG" ]; then
      # Use awk to update key within the [Controller] section
      awk -v sect="[Controller]" -v key="controller_type" -v val="$ctrl_choice" '
        /^\[/ { in_sect = ($0 == sect) }
        in_sect && index($0, key " = ") == 1 { $0 = key " = " val }
        { print }
      ' "$OWL_CONFIG" > "${OWL_CONFIG}.tmp" && mv "${OWL_CONFIG}.tmp" "$OWL_CONFIG"

      echo -e "${TICK} controller_type = ${ctrl_choice} written to GENERAL_CONFIG.ini"
      STATUS_CONTROLLER="${TICK}"
    else
      echo -e "${CROSS} GENERAL_CONFIG.ini not found at ${OWL_CONFIG}"
      STATUS_CONTROLLER="${CROSS}"
      ERROR_CONTROLLER="GENERAL_CONFIG.ini not found"
    fi
    ;;
  * )
    echo -e "${RED}[ERROR] Invalid controller type '${ctrl_choice}'. Keeping current setting.${NC}"
    STATUS_CONTROLLER="${CROSS}"
    ERROR_CONTROLLER="Invalid controller type entered"
    ;;
esac

# Step 13: Dashboard Setup
echo -e "${GREEN}[INFO] Dashboard setup available...${NC}"
read -p "Do you want to add a web dashboard for remote control? (y/n): " dashboard_choice
case "$dashboard_choice" in
  y|Y )
    echo -e "${GREEN}[INFO] Setting up OWL Dashboard...${NC}"
    if [ -f "${SCRIPT_DIR}/controller/shared/setup.sh" ]; then
      install_dashboard_dependencies
      chmod +x "${SCRIPT_DIR}/controller/shared/setup.sh"
      cd "$SCRIPT_DIR"  # Ensure we're in the right directory
      sudo "${SCRIPT_DIR}/controller/shared/setup.sh"
      check_status "Dashboard setup" "DASHBOARD"
    else
      echo -e "${RED}[ERROR] setup.sh not found in ${SCRIPT_DIR}/controller/shared/${NC}"
      STATUS_DASHBOARD="${CROSS}"
      ERROR_DASHBOARD="controller/shared/setup.sh not found"
    fi
    ;;
  n|N )
    echo -e "${GREEN}[INFO] Dashboard setup skipped.${NC}"
    STATUS_DASHBOARD="SKIPPED"
    ;;
  * )
    echo -e "${RED}[ERROR] Invalid input. Dashboard setup skipped.${NC}"
    STATUS_DASHBOARD="SKIPPED"
    ;;
esac

# Step 14: Final Summary
echo -e "\n${GREEN}[INFO] Installation Summary:${NC}"
echo -e "$STATUS_UPGRADE System Upgrade"
echo -e "$STATUS_CAMERA Camera Detected"
echo -e "$STATUS_CAMERA_TEST Camera Test"

if [[ -n "$STATUS_FULL_UPGRADE" ]]; then
    echo -e "$STATUS_FULL_UPGRADE Full System Upgrade"
fi

echo -e "$STATUS_VENV Virtual Environment Created"
echo -e "$STATUS_GLOBAL_NUMPY Global NumPy Version Detected"
echo -e "$STATUS_OPENCV OpenCV Installed"
echo -e "$STATUS_NUMPY_COMPAT NumPy Versions Aligned"
echo -e "$STATUS_OWL_DEPS OWL Dependencies Installed"

if [[ "$STATUS_GOG_DEPS" == "${TICK}" ]]; then
    echo -e "$STATUS_GOG_DEPS Green-on-Green (YOLO) Dependencies"
elif [[ "$STATUS_GOG_DEPS" == "SKIPPED" ]]; then
    echo -e "${ORANGE}[SKIPPED]${NC} Green-on-Green (YOLO) Dependencies"
fi

echo -e "$STATUS_OWL_SERVICE OWL Service (systemd) Started"
echo -e "$STATUS_DESKTOP_ICON Desktop Icon Created"
echo -e "$STATUS_CONTROLLER Hardware Controller Configured"

if [[ "$STATUS_DASHBOARD" == "${TICK}" ]]; then
    echo -e "$STATUS_DASHBOARD_DEPS Dashboard Python dependencies installed"
    echo -e "$STATUS_DASHBOARD Web Dashboard Configured"
elif [[ "$STATUS_DASHBOARD" == "SKIPPED" ]]; then
    echo -e "${ORANGE}[SKIPPED]${NC} Web Dashboard"
else
    echo -e "$STATUS_DASHBOARD Web Dashboard"
fi

OWL_VERSION=$(python3 - <<EOF
import version
print(version.VERSION)
EOF
)

echo -e "${GREEN}[COMPLETE] OWL version installed: ${OWL_VERSION}${NC}"

# Step 15: Start OWL focusing
read -p "Start OWL focusing? (y/n): " choice
case "$choice" in
  y|Y ) echo -e "${GREEN}[INFO] Starting focusing...${NC}"; "$FOCUS_WRAPPER" &;;
  n|N ) echo -e "${GREEN}[INFO] Focusing skipped. Double click the desktop icon to focus the OWL later.${NC}";;
  * ) echo -e "${RED}[ERROR] Invalid input. Please enter y or n.${NC}";;
esac
