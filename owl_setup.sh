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

# Factory provisioning: `bash owl_setup.sh --factory` is the ONE command that
# prepares a production OWL (CM5 on the production carrier) for a customer.
# It runs the full install non-interactively: GoG skipped, controller type
# none, standalone dashboard with the fixed setup hotspot (OWL-XXXX from the
# Pi serial, PSK owl-setup), the carrier's nRF9151 GNSS (serial-port access,
# ModemManager off, [GPS] written after the dashboard step), and arms
# phone-app first-boot setup at the end. The customer's phone app makes all
# network and cloud decisions later.
#
# It replaces --ship and --ship-gps (v3.19.0, cross-repo review 2026-09-17:
# two ship commands had drifted apart in the docs). There are no aliases.
#
# The unit must already carry its hardware identity (sudo bash
# owl_identity.sh --otp) and be registered with Noktura from the bench laptop
# (tools/noktura_register.sh). The identity is checked up front, before 20
# minutes of install; registration is not checked here (it needs internet and
# the factory token, which never touch the unit).
FACTORY_MODE=0
case "${1:-}" in
  --factory)
    FACTORY_MODE=1
    echo -e "${GREEN}[INFO] Factory mode: non-interactive production provisioning${NC}"
    ;;
  "")
    ;;
  *)
    echo -e "${RED}[ERROR] Unknown option '$1' (supported: --factory)${NC}"
    exit 1
    ;;
esac

# Identity gate (fail fast, before the confirmation and the long install).
# A production unit without its OTP identity cannot be claimed in the app.
OWL_IDENTITY_DIR="${OWL_IDENTITY_DIR:-/etc/owl-identity}"
if [[ "$FACTORY_MODE" == "1" ]]; then
    identity_tier=$(cat "${OWL_IDENTITY_DIR}/tier" 2>/dev/null || true)
    if [[ ! -s "${OWL_IDENTITY_DIR}/device.pub" || "$identity_tier" != "otp" ]]; then
        echo -e "${RED}[NOT SHIP-READY] No hardware identity on this unit (tier: ${identity_tier:-none}).${NC}"
        echo -e "${RED}        Run: sudo bash owl_identity.sh --otp, register it from the laptop${NC}"
        echo -e "${RED}        (tools/noktura_register.sh), then re-run bash owl_setup.sh --factory.${NC}"
        exit 1
    fi
fi

# --factory is for units leaving the factory, not for your own build: it deletes
# every WiFi profile except the OWL hotspot, SSH keys and shell history, sets
# the shared setup password and locks detection until phone-app setup finishes.
# A maker running it by mistake locks themselves out, so it asks for a typed
# confirmation. Factory automation passes --yes as the second argument.
if [[ "$FACTORY_MODE" == "1" && -z "${OWL_SETUP_DETACHED:-}" ]]; then
    case "${2:-}" in
      --yes) ;;
      "")
        echo -e "${ORANGE}[WARN] Factory mode prepares a unit for a customer. It will:${NC}"
        echo -e "${ORANGE}       - delete every WiFi profile except the OWL hotspot, plus SSH keys and shell history${NC}"
        echo -e "${ORANGE}       - set the shared setup hotspot password (owl-setup)${NC}"
        echo -e "${ORANGE}       - keep detection off until setup is finished in the phone app${NC}"
        echo -e "${ORANGE}       Building your own OWL? Stop here and run: bash owl_setup.sh${NC}"
        read -r -p "Type FACTORY to continue: " factory_confirm
        if [[ "$factory_confirm" != "FACTORY" ]]; then
            echo -e "${RED}[ERROR] Factory mode cancelled. Nothing was changed.${NC}"
            exit 1
        fi
        ;;
      *)
        echo -e "${RED}[ERROR] Unknown option '$2' (only --yes may follow $1)${NC}"
        exit 1
        ;;
    esac
fi

# Factory mode takes 20+ minutes and is usually run over SSH. A dropped session
# sends SIGHUP and kills the install partway (a fresh CM5 was left un-renamed
# and un-armed, 2026-09-15). Re-exec detached from the terminal, logging to a
# file, and follow the log; if SSH drops, the install keeps going. Needs
# passwordless sudo (the default for the first user on Raspberry Pi OS), since
# a detached process cannot answer a password prompt.
if [[ "$FACTORY_MODE" == "1" && -z "${OWL_SETUP_DETACHED:-}" ]]; then
    if ! sudo -n true 2>/dev/null; then
        echo -e "${RED}[ERROR] Factory mode needs passwordless sudo for '$CURRENT_USER'.${NC}"
        exit 1
    fi
    SETUP_LOG="$HOME/owl_setup.log"
    OWL_SETUP_DETACHED=1 setsid nohup bash "$SCRIPT_DIR/owl_setup.sh" "$1" \
        > "$SETUP_LOG" 2>&1 < /dev/null &
    SETUP_PID=$!
    echo -e "${GREEN}[INFO] Install running in the background (PID ${SETUP_PID}), so it survives an SSH drop.${NC}"
    echo -e "${GREEN}[INFO] Log: ${SETUP_LOG}. If you get disconnected, reconnect and run: tail -f ${SETUP_LOG}${NC}"
    tail -n +1 --pid="$SETUP_PID" -f "$SETUP_LOG"
    wait "$SETUP_PID"
    exit $?
fi

# nRF9151 GNSS link on the production CM5 carrier (--factory).
# Confirmed on the bench 2026-09-13: Serial LTE Modem 2.9.1 firmware answers on
# UART3 (GPIO 8/9, TX/RX only, no flow control) at 460800 baud once GPIO 26
# (5G_ENABLE) powers the modem. install_firstboot.sh --gps-config owns that
# boot config; tools/nrf9151_probe.sh is the bench diagnostic.
NRF_GPS_SOURCE="nrf91"
NRF_GPS_PORT="/dev/ttyAMA3"
NRF_GPS_BAUD="460800"

factory_ssid_suffix() {
  # Last 4 hex chars of the Pi serial (uppercase); random fallback.
  # Gives every shipped unit a unique OWL-XXXX the phone app can find.
  local serial
  serial=$(awk '/^Serial/ {print $NF}' /proc/cpuinfo 2>/dev/null)
  if [[ -n "$serial" ]]; then
    echo "${serial: -4}" | tr '[:lower:]' '[:upper:]'
  else
    tr -dc 'A-F0-9' < /dev/urandom | head -c 4
  fi
}

if systemctl is-active --quiet owl.service; then
    echo -e "${ORANGE}[WARNING] The owl.service is currently running.${NC}"
    if [[ "$FACTORY_MODE" == "1" ]]; then
        stop_choice="y"
    else
        read -p "Do you want to stop the service to continue with the installation? (y/n): " stop_choice
    fi
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
STATUS_SYS_DEPS=""
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
STATUS_GPS=""

ERROR_UPGRADE=""
ERROR_SYS_DEPS=""
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
ERROR_GPS=""

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
  # Dashboard deps live in requirements.txt (single source of truth, also used
  # by owl_update.sh) — do not pip-install individual packages here.
  pip install -r "$HOME/owl/requirements.txt"
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

verify_venv_runtime() {
  # Import-check everything the dashboard/first-boot stack needs. 'import
  # flask' pulls werkzeug/click/itsdangerous — this catches half-installed
  # dependency sets, which pip leaves behind if killed mid-install (pip is
  # not transactional; a dropped SSH session mid-run is enough).
  "$HOME/.virtualenvs/owl/bin/python" - <<'PY'
import importlib, sys
missing = []
for m in ("flask", "werkzeug", "gunicorn", "paho.mqtt.client", "numpy", "cv2"):
    try:
        importlib.import_module(m)
    except Exception as e:
        missing.append(f"{m}: {e}")
if missing:
    print("Venv verification FAILED:\n  " + "\n  ".join(missing))
    sys.exit(1)
print("Venv runtime verified (flask/werkzeug/gunicorn/paho/numpy/cv2).")
PY
}

check_camera_connection() {
  echo -e "${GREEN}[INFO] Checking for connected cameras...${NC}"

  # 1 — Detect Raspberry Pi CSI camera via libcamera. Match an actual camera
  # entry ("0 : imx296 [...]") rather than grep -qv on the no-camera message,
  # which false-positives whenever the output has any extra lines.
  if command -v rpicam-hello >/dev/null 2>&1; then
    if rpicam-hello --list-cameras 2>/dev/null | grep -qE '^[[:space:]]*[0-9]+[[:space:]]*:'; then
      echo -e "${GREEN}[INFO] Raspberry Pi CSI camera detected.${NC}"
      CAMERA_MODE="rpi"
      STATUS_CAMERA="${TICK}"
      return 0
    fi
  fi

  # 2 — Detect USB camera
  if command -v v4l2-ctl >/dev/null 2>&1 && [[ -e /dev/video0 ]]; then
    if v4l2-ctl -d /dev/video0 --all >/dev/null 2>&1; then
      echo -e "${GREEN}[INFO] USB webcam detected at /dev/video0.${NC}"
      CAMERA_MODE="usb"
      STATUS_CAMERA="${TICK}"
      return 0
    fi
  fi

  # 3 — Neither found, warn but continue install
  echo -e "${RED}[WARNING] No camera detected (no CSI or USB).${NC}"
  echo -e "${RED}[WARNING] OWL will not run until a camera is present.${NC}"
  CAMERA_MODE="none"
  STATUS_CAMERA="${CROSS}"
  return 0
}

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

setup_owl_systemd_service() {
  echo -e "${GREEN}[INFO] Creating systemd service for OWL...${NC}"

  local SERVICE_FILE="/etc/systemd/system/owl.service"
  local VENV_BIN="$HOME/.virtualenvs/owl/bin"

  sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=OpenWeedLocator (OWL) Main Application
After=network-online.target mosquitto.service NetworkManager-wait-online.service
Wants=network-online.target mosquitto.service NetworkManager-wait-online.service

[Service]
Type=simple
User=$CURRENT_USER
Group=$(id -g -n $CURRENT_USER)
WorkingDirectory=$SCRIPT_DIR
Environment="PATH=$VENV_BIN:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="PYTHONUNBUFFERED=1"
ExecStart=$VENV_BIN/python -u $SCRIPT_DIR/owl.py
Restart=always
RestartSec=5
KillMode=mixed
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOF

  sudo systemctl daemon-reload
  sudo systemctl enable owl.service

  echo -e "${GREEN}[INFO] Starting OWL service...${NC}"
  sudo systemctl start owl.service

  sleep 2

  if systemctl is-active --quiet owl.service; then
    echo -e "${TICK} OWL systemd service is active"
    STATUS_OWL_SERVICE="${TICK}"
  else
    echo -e "${CROSS} OWL systemd service failed to start"
    echo -e "${ORANGE}[INFO] Showing service logs for debugging:${NC}"
    systemctl status owl.service --no-pager -l || true
    echo -e "${ORANGE}[INFO] For live logs, run: journalctl -u owl.service -f${NC}"
    STATUS_OWL_SERVICE="${CROSS}"
    ERROR_OWL_SERVICE="owl.service failed to start"
    return 1
  fi
}

setup_factory_gps() {
  # --factory only. Must run AFTER controller/shared/setup.sh, which writes
  # [GPS] source = none into CONTROLLER.ini.
  echo -e "${GREEN}[INFO] Configuring nRF9151 GPS...${NC}"
  local ctrl_ini="${SCRIPT_DIR}/config/CONTROLLER.ini"

  if [[ -z "$NRF_GPS_SOURCE" || -z "$NRF_GPS_PORT" ]]; then
    echo -e "${CROSS} nRF9151 GPS source/port are not set in owl_setup.sh"
    echo -e "        (NRF_GPS_SOURCE / NRF_GPS_PORT are pending bench discovery)."
    STATUS_GPS="${CROSS}"
    ERROR_GPS="nRF9151 GPS source/port not confirmed"
    return 1
  fi

  # Serial-port access for owl.service (group applies from the next boot).
  sudo usermod -aG dialout "$CURRENT_USER"

  # OWL owns the nRF9151 port outright (GNSS only, no SIM). ModemManager
  # would probe it with AT commands and hold it open.
  if systemctl list-unit-files ModemManager.service 2>/dev/null | grep -q '^ModemManager.service'; then
    echo -e "${GREEN}[INFO] Disabling ModemManager (it would claim the nRF9151 port)...${NC}"
    sudo systemctl disable --now ModemManager.service
  fi

  # Boot config: UART3 overlay + modem power on GPIO 26, also applied now so
  # the link check below works without a reboot. Exit 2 = config.txt changed.
  sudo bash "${SCRIPT_DIR}/controller/setup/install_firstboot.sh" --gps-config
  if [ $? -eq 1 ]; then
    echo -e "${CROSS} nRF9151 boot config failed (see above)"
    STATUS_GPS="${CROSS}"
    ERROR_GPS="nRF9151 boot config failed"
    return 1
  fi

  if [ ! -f "$ctrl_ini" ]; then
    echo -e "${CROSS} CONTROLLER.ini not found at ${ctrl_ini}"
    STATUS_GPS="${CROSS}"
    ERROR_GPS="CONTROLLER.ini not found"
    return 1
  fi

  # Update keys in place inside [GPS] only (index()==1 keeps nmea_port intact).
  # CONTROLLER.ini holds the device token: the temp copy is created private
  # (umask 077) and the result gets setup.sh's 0640, because the mv would
  # otherwise leave the file readable by every local user.
  ( umask 077
    awk -v src="$NRF_GPS_SOURCE" -v port="$NRF_GPS_PORT" -v baud="$NRF_GPS_BAUD" '
      /^\[/ { in_gps = ($0 == "[GPS]") }
      in_gps && index($0, "source = ") == 1   { $0 = "source = " src }
      in_gps && index($0, "port = ") == 1     { $0 = "port = " port }
      in_gps && index($0, "baudrate = ") == 1 { $0 = "baudrate = " baud }
      { print }
    ' "$ctrl_ini" > "${ctrl_ini}.tmp"
  ) && mv "${ctrl_ini}.tmp" "$ctrl_ini" && chmod 0640 "$ctrl_ini" \
    || rm -f "${ctrl_ini}.tmp"

  if ! awk -v want="source = $NRF_GPS_SOURCE" '
      /^\[/ { g = ($0 == "[GPS]") }
      g && $0 == want { found = 1 }
      END { exit !found }' "$ctrl_ini"; then
    echo -e "${CROSS} Writing [GPS] settings to CONTROLLER.ini failed"
    STATUS_GPS="${CROSS}"
    ERROR_GPS="[GPS] settings not written to CONTROLLER.ini"
    return 1
  fi
  echo -e "${TICK} [GPS] source = ${NRF_GPS_SOURCE}, port = ${NRF_GPS_PORT}, baudrate = ${NRF_GPS_BAUD}"
  STATUS_GPS="${TICK}"

  # Link check (ship blocker). --gps-config above has powered the modem and
  # loaded UART3 for this session, so a healthy unit answers AT within a few
  # seconds. A boot-time ctsrts overlay or a first flash can need one reboot;
  # re-running this script is safe.
  #
  # owl.service must not hold the port meanwhile: step 9 (re)started it, and on
  # a re-run it reads the source = nrf91 written last time, so its GPS reader
  # would swallow the modem's OK. It is stopped unconditionally (a unit with no
  # camera sits in "activating", which is-active does not catch) and started
  # again afterwards if enabled, when it picks up the new [GPS] and dialout.
  sudo systemctl stop owl.service 2>/dev/null
  local answered=0 attempt
  for attempt in $(seq 1 10); do
    if [ -e "$NRF_GPS_PORT" ] && sudo timeout 3 bash -c '
        stty -F "$1" "$2" raw -echo -crtscts clocal || exit 1
        exec 3<>"$1"
        printf "AT\r\n" >&3
        while IFS= read -r -t 2 line <&3; do
          [[ "$line" == OK* ]] && exit 0
        done
        exit 1' _ "$NRF_GPS_PORT" "$NRF_GPS_BAUD"; then
      answered=1
      break
    fi
    sleep 1
  done
  if systemctl is-enabled --quiet owl.service 2>/dev/null; then
    sudo systemctl start owl.service
  fi
  if [ "$answered" == "1" ]; then
    echo -e "${TICK} nRF9151 answered AT on ${NRF_GPS_PORT} @ ${NRF_GPS_BAUD}"
  else
    echo -e "${CROSS} nRF9151 did not answer AT on ${NRF_GPS_PORT} @ ${NRF_GPS_BAUD}"
    echo -e "        Reboot, then re-run 'bash owl_setup.sh --factory' (re-runs are safe)."
    echo -e "        Diagnose with: bash tools/nrf9151_probe.sh"
    STATUS_GPS="${CROSS}"
    ERROR_GPS="nRF9151 did not answer AT on ${NRF_GPS_PORT}"
    return 1
  fi
}

# Step 1: Perform a normal system update and upgrade
echo -e "${GREEN}[INFO] Updating and upgrading the system...${NC}"
sudo apt update
sudo apt full-upgrade -y
check_status "System upgrade" "UPGRADE"

# Step 1b: System packages OWL depends on. The Desktop image ships all of
# these; OS Lite ships none of them, so install explicitly:
#   git                 - owl_update.sh / cloning
#   libgl1, libglib2.0-0 - runtime libs for the pip opencv-contrib-python wheel
#                          (libGL.so.1 import error on Lite without them)
#   python3-picamera2   - camera stack, reached through --system-site-packages
#   python3-numpy       - system NumPy that picamera2 builds against
#   v4l-utils           - USB camera detection/test (v4l2-ctl)
#   i2c-tools           - camera/sensor debugging (i2cdetect)
#   usbutils            - lsusb, used by the dashboard USB device listing
echo -e "${GREEN}[INFO] Installing system dependencies (git, OpenCV libs, picamera2)...${NC}"
sudo apt-get install -y git libgl1 libglib2.0-0 python3-picamera2 python3-numpy v4l-utils i2c-tools usbutils
check_status "Installing system dependencies" "SYS_DEPS"

# rpicam-apps is preinstalled on Desktop; Lite images need the lite build
# (falls back to the full package if the lite variant is unavailable).
if ! command -v rpicam-hello >/dev/null 2>&1; then
    echo -e "${GREEN}[INFO] rpicam-hello not found. Installing rpicam-apps...${NC}"
    sudo apt-get install -y rpicam-apps-lite || sudo apt-get install -y rpicam-apps
    check_status "Installing rpicam-apps" "SYS_DEPS"
fi

# Step 1b: CM camera overlay — Compute Modules have no camera autodetection
# (camera_auto_detect=1 silently no-ops on CM5), so a fresh flash never loads
# the sensor overlay and the checks below can't succeed. Single source of
# truth lives in install_firstboot.sh; it is a no-op on Pi 4/5 (autodetect
# works there) and idempotent on re-runs. Exit 2 = config.txt was changed.
CAMERA_CONFIG_CHANGED=0
sudo bash "${SCRIPT_DIR}/controller/setup/install_firstboot.sh" --camera-config
CAMERA_CONFIG_RC=$?
if [ "$CAMERA_CONFIG_RC" -eq 2 ]; then
    CAMERA_CONFIG_CHANGED=1
    echo -e "${ORANGE}[WARN] config.txt was just updated for the CM camera — the sensor"
    echo -e "       overlay only loads at boot, so the camera checks below WILL fail."
    echo -e "       Reboot when this script finishes, then re-run it (re-runs are safe).${NC}"
elif [ "$CAMERA_CONFIG_RC" -eq 1 ]; then
    echo -e "${RED}[WARNING] Camera boot config failed (see above) — fix config.txt"
    echo -e "          before expecting the camera to work on this Compute Module.${NC}"
fi

# Step 2: Ensure a camera is connected before proceeding
check_camera_connection

# Step 3: Test camera functionality
test_camera_functionality "$CAMERA_MODE"

if [[ "$CAMERA_CONFIG_CHANGED" == "1" && "$CAMERA_MODE" == "none" ]]; then
    echo -e "${ORANGE}[INFO] No camera found, as expected right after the config.txt"
    echo -e "       camera update — reboot and re-run to verify the camera.${NC}"
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
if ! "$VIRTUAL_ENV/bin/python" - <<'PY'
import numpy as np, cv2
print(f"[OK] Final check: NumPy {np.__version__}, OpenCV {cv2.__version__}")
PY
then
  echo -e "${RED}[ERROR] Post-check import failed. If the error above mentions"
  echo -e "        libGL.so.1 or libglib, run: sudo apt-get install -y libgl1 libglib2.0-0"
  echo -e "        then re-run this script.${NC}"
  exit 1
fi

# Step 8: Install OWL dependencies
echo -e "${GREEN}[INFO] Installing the OWL Python dependencies...${NC}"
cd "$SCRIPT_DIR"
pip install -r requirements.txt
check_status "Installing dependencies from requirements.txt" "OWL_DEPS"

# Step 8b: Optional Green-on-Green (YOLO) support
echo -e "${GREEN}[INFO] Green-on-Green (YOLO) support available...${NC}"
if [[ "$FACTORY_MODE" == "1" ]]; then
    gog_choice="n"
    echo -e "${GREEN}[INFO] Factory mode: skipping Green-on-Green${NC}"
else
    read -p "Do you want to install Green-on-Green (YOLO) support? This requires ~2GB of additional packages. (y/n): " gog_choice
fi
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

# Step 10: Set desktop background - check for wayland or X11 (skipped on
# OS Lite, which has no desktop environment)
if command -v pcmanfm >/dev/null 2>&1; then
  echo -e "${GREEN}[INFO] Setting desktop background...${NC}"
  pcmanfm --set-wallpaper $SCRIPT_DIR/images/owl-background.png
  check_status "Setting desktop background" "BOOT_SCRIPTS"
  sleep 2
else
  echo -e "${ORANGE}[INFO] No desktop environment detected (OS Lite). Skipping wallpaper.${NC}"
fi

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
if [[ "$FACTORY_MODE" == "1" ]]; then
    ctrl_choice="none"
    echo -e "${GREEN}[INFO] Factory mode: controller_type = none${NC}"
else
    read -p "Enter controller type [none/ute/advanced] (default: none): " ctrl_choice
fi
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
if [[ "$FACTORY_MODE" == "1" ]]; then
    dashboard_choice="y"
    echo -e "${GREEN}[INFO] Factory mode: standalone dashboard with setup hotspot${NC}"
else
    read -p "Do you want to add a web dashboard for remote control? (y/n): " dashboard_choice
fi
case "$dashboard_choice" in
  y|Y )
    echo -e "${GREEN}[INFO] Setting up OWL Dashboard...${NC}"
    if [ -f "${SCRIPT_DIR}/controller/shared/setup.sh" ]; then
      install_dashboard_dependencies
      chmod +x "${SCRIPT_DIR}/controller/shared/setup.sh"
      cd "$SCRIPT_DIR"  # Ensure we're in the right directory
      if [[ "$FACTORY_MODE" == "1" ]]; then
        # Ship gate: arming first-boot on a broken venv ships a brick. Verify
        # the full import chain; one repair attempt, then hard abort — armed
        # units must never leave the bench with missing Python deps.
        echo -e "${GREEN}[INFO] Ship gate: verifying venv runtime before arming first-boot...${NC}"
        if ! verify_venv_runtime; then
            echo -e "${ORANGE}[WARN] Venv incomplete — retrying requirements install once...${NC}"
            pip install -r "${SCRIPT_DIR}/requirements.txt"
            if ! verify_venv_runtime; then
                echo -e "${RED}[ERROR] NOT SHIP-READY: venv still broken after retry.${NC}"
                echo -e "${RED}        First-boot NOT armed, dev cleanup skipped — unit stays reachable for repair.${NC}"
                exit 1
            fi
        fi
        SHIP_SSID="OWL-$(factory_ssid_suffix)"
        echo -e "${GREEN}[INFO] Ship hotspot: ${SHIP_SSID} (PSK: setup default)${NC}"
        echo -e "${ORANGE}[WARN] wlan0 switches to hotspot mode now. An SSH session over Wi-Fi"
        echo -e "       will drop here. The install carries on; join ${SHIP_SSID} or check"
        echo -e "       ~/owl_setup.log to follow it.${NC}"
        sudo "${SCRIPT_DIR}/controller/shared/setup.sh" --mode standalone \
            --owl-id 1 --ssid "${SHIP_SSID}" --password owl-setup \
            --arm-firstboot --yes
      else
        sudo "${SCRIPT_DIR}/controller/shared/setup.sh"
      fi
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

# Step 13b: nRF9151 GPS (--factory). Must run after the dashboard step
# above, which writes [GPS] source = none into CONTROLLER.ini.
if [[ "$FACTORY_MODE" == "1" ]]; then
    setup_factory_gps
fi

# Step 13c: maker identity (interactive installs only). A self-built OWL can
# join Noktura from the phone app (community registration) once it has an
# identity. A key file is the maker tier; a CM5 or Pi 5 with rpi-fw-crypto can
# use the OTP tier instead (sudo bash owl_identity.sh --otp, permanent).
if [[ "$FACTORY_MODE" != "1" ]]; then
    if [[ -s "${OWL_IDENTITY_DIR}/device.pub" ]]; then
        echo -e "${GREEN}[INFO] Device identity already present ($(cat "${OWL_IDENTITY_DIR}/tier" 2>/dev/null || echo unknown) tier).${NC}"
    else
        read -p "Create a device identity so this OWL can join Noktura from the phone app? (y/N): " identity_choice
        if [[ "$identity_choice" =~ ^[Yy]$ ]]; then
            if sudo bash "${SCRIPT_DIR}/owl_identity.sh" --disk; then
                STATUS_IDENTITY="${TICK}"
            else
                STATUS_IDENTITY="${CROSS}"
            fi
        fi
    fi
fi

# Step 14: Final Summary
echo -e "\n${GREEN}[INFO] Installation Summary:${NC}"
echo -e "$STATUS_UPGRADE System Upgrade"
echo -e "$STATUS_SYS_DEPS System Dependencies Installed"
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
if [[ "$FACTORY_MODE" == "1" ]]; then
    echo -e "$STATUS_GPS nRF9151 GPS Configured"
fi
if [[ -n "$STATUS_IDENTITY" ]]; then
    echo -e "$STATUS_IDENTITY Device Identity (key file) Created"
fi

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

# Step 15: Start OWL focusing (skipped in factory mode — the customer's phone
# app runs the camera check on their first boot)
if [[ "$FACTORY_MODE" == "1" ]]; then
    # Final ship gate: any critical failure above means this unit must not be
    # cleaned or boxed. Dev cleanup is deliberately skipped so the unit stays
    # reachable (WiFi profiles intact) for repair. owl.service is NOT gated
    # here — it needs a camera, which the customer's first boot verifies.
    SHIP_BLOCKERS=""
    [ -n "$ERROR_OPENCV" ] && SHIP_BLOCKERS+="OpenCV install; "
    [ -n "$ERROR_OWL_DEPS" ] && SHIP_BLOCKERS+="OWL dependencies; "
    [ -n "$ERROR_DASHBOARD_DEPS" ] && SHIP_BLOCKERS+="dashboard dependencies; "
    [ -n "$ERROR_DASHBOARD" ] && SHIP_BLOCKERS+="dashboard/first-boot setup; "
    [ -n "$ERROR_GPS" ] && SHIP_BLOCKERS+="nRF9151 GPS setup; "
    if [ -n "$SHIP_BLOCKERS" ]; then
        echo -e ""
        echo -e "${RED}[NOT SHIP-READY] Critical failures: ${SHIP_BLOCKERS%??}${NC}"
        echo -e "${RED}Fix the above and re-run 'bash owl_setup.sh $1'. Dev cleanup was"
        echo -e "skipped so the unit remains reachable over your current network.${NC}"
        exit 1
    fi

    echo -e ""
    # Everything provisioning wrote (the FAT32 first-boot flag above all)
    # must be on disk before anyone pulls the plug — an unsynced power-off
    # shipped an un-armed unit with a zero-filled /etc/hosts (2026-08-15).
    sync

    echo -e "${GREEN}[SHIP-READY] Unit provisioned for shipping:${NC}"
    echo -e "  • Hotspot: ${SHIP_SSID:-OWL-XXXX} (setup password)"
    if [[ "$FACTORY_MODE" == "1" ]]; then
        echo -e "  • GPS: nRF9151 on ${NRF_GPS_PORT} (source = ${NRF_GPS_SOURCE})"
    fi
    echo -e "  • Identity: $(cat "${OWL_IDENTITY_DIR}/tier") tier, fingerprint $(openssl pkey -pubin -in "${OWL_IDENTITY_DIR}/device.pub" -outform DER 2>/dev/null | sha256sum | cut -c1-16)…"
    echo -e "  • First-boot setup armed — the phone app takes over on next boot"
    echo -e "  • Focus the camera, then SHUT DOWN CLEANLY (sudo shutdown now)"
    echo -e "    before boxing — pulling power mid-write can un-arm the unit."

    # Factory hygiene, deliberately the LAST step: dev/clean.sh --yes strips
    # dev residue — every WiFi profile except the OWL hotspot (a dev phone
    # hotspot password must never ship on a unit), shell history, SSH keys,
    # logs, cached credentials. Deleting the active WiFi profile drops an
    # SSH session over that network, so it runs detached (setsid) and
    # finishes on its own; .txt log name so clean.sh's own /var/log/*.log
    # truncation doesn't eat it.
    echo -e ""
    echo -e "${GREEN}[INFO] Removing dev residue (dev/clean.sh --yes, detached)...${NC}"
    echo -e "${ORANGE}[WARN] If you are SSH'd in over a personal WiFi/hotspot, the"
    echo -e "       connection will drop now — that is the cleanup removing the"
    echo -e "       profile. Give it a minute to finish before powering off."
    echo -e "       Log: /var/log/owl-ship-clean.txt${NC}"
    sudo setsid bash -c \
        "bash '${SCRIPT_DIR}/dev/clean.sh' --yes > /var/log/owl-ship-clean.txt 2>&1" \
        < /dev/null &
else
    read -p "Start OWL focusing? (y/n): " choice
    case "$choice" in
      y|Y ) echo -e "${GREEN}[INFO] Starting focusing...${NC}"; "$FOCUS_WRAPPER" &;;
      n|N ) echo -e "${GREEN}[INFO] Focusing skipped. Double click the desktop icon to focus the OWL later.${NC}";;
      * ) echo -e "${RED}[ERROR] Invalid input. Please enter y or n.${NC}";;
    esac
fi
