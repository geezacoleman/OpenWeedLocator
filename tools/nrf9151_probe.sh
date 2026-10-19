#!/bin/bash
# nRF9151 modem bring-up probe for the production CM5 carrier board.
#
# Powers the modem (GPIO 26 = 5G_ENABLE), opens UART3 (GPIO 8 TX / GPIO 9 RX,
# /dev/ttyAMA3, no RTS/CTS), checks the Serial LTE Modem firmware answers,
# then tries for a GPS fix. Prints every reading and ends with a verdict.
#
# Run as the normal user (it uses sudo where needed):
#   bash nrf9151_probe.sh                  # wait up to 180 s for a GPS fix
#   GPS_WAIT=600 bash nrf9151_probe.sh     # wait longer
#
# Pins it touches on this carrier: GPIO 26 (modem power). If a ctsrts overlay
# has grabbed GPIO 10/11 (output channels 17 and 6), it returns them to plain
# inputs. It never enables ctsrts or i2c_arm.

set -u

PORT=/dev/ttyAMA3
BAUD=460800   # bench-confirmed rate of the carrier's Serial LTE Modem build
GPS_WAIT=${GPS_WAIT:-180}
BOOT_CONFIG=/boot/firmware/config.txt
[ -f "$BOOT_CONFIG" ] || BOOT_CONFIG=/boot/config.txt
MARKER="# OWL nRF9151 modem (CM5 carrier) - added by nrf9151_probe.sh"
LOG=$(mktemp)
STOPPED_SERVICES=()
REBOOT_NEEDED=0
AT_REPLY=""

GREEN='\033[0;32m'; ORANGE='\033[0;33m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
info() { echo -e "${GREEN}[INFO]${NC} $*"; }
warn() { echo -e "${ORANGE}[WARN]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; }
step() { echo; echo -e "${BOLD}== $* ==${NC}"; }

cleanup() {
  exec 3>&-
  rm -f "$LOG"
  for svc in "${STOPPED_SERVICES[@]}"; do
    info "Restarting $svc"
    sudo systemctl start "$svc"
  done
}
trap cleanup EXIT

# "26: op dh pd | hi // GPIO26 = output" -> "hi"
pin_level() { pinctrl get "$1" | sed -E 's/.*\| *([a-z-]+).*/\1/'; }
# "9: a2 pu | lo // GPIO9 = RXD3" -> "RXD3"
pin_func() { pinctrl get "$1" | sed -E 's/.*= *//'; }

# Send one AT command, print every reply line, stop at OK / ERROR / timeout.
at_cmd() {
  local cmd=$1 wait=${2:-3} line end
  AT_REPLY=""
  echo "    > $cmd"
  printf '%s\r\n' "$cmd" >&3
  end=$((SECONDS + wait))
  while (( SECONDS < end )); do
    IFS= read -r -t 1 line <&3 || continue
    line=${line%$'\r'}
    [ -z "$line" ] && continue
    echo "    < $line"
    AT_REPLY+="$line"$'\n'
    if [[ "$line" == OK || "$line" == ERROR* || "$line" == *"CME ERROR"* ]]; then
      return 0
    fi
  done
}
at_ok() { grep -qx OK <<< "$AT_REPLY"; }

# ---------------------------------------------------------------------------
step "1. Board and tools"
model=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null)
echo "    Board: ${model:-unknown}"
[[ "$model" == *"Compute Module 5"* ]] || warn "Not a Compute Module 5. This probe is written for the production CM5 carrier."
for c in pinctrl stty timeout dtoverlay; do
  command -v "$c" >/dev/null || { fail "Missing command: $c"; exit 1; }
done
sudo -v || exit 1

# ---------------------------------------------------------------------------
step "2. OWL services"
for svc in owl.service owl-dash.service; do
  if systemctl is-active --quiet "$svc"; then
    sudo systemctl stop "$svc"
    STOPPED_SERVICES+=("$svc")
    info "Stopped $svc (restarted when this script exits)"
  else
    echo "    $svc not running"
  fi
done

# ---------------------------------------------------------------------------
step "3. Boot config ($BOOT_CONFIG)"
lines=$(grep -nE 'uart3|gpio=26|i2c_arm|disable-bt|enable_uart' "$BOOT_CONFIG")
if [ -n "$lines" ]; then
  echo "$lines" | sed 's/^/    /'
else
  echo "    No uart3 / gpio=26 / i2c_arm lines"
fi
if grep -qE '^dtoverlay=uart3-pi5,ctsrts' "$BOOT_CONFIG"; then
  warn "config.txt enables ctsrts on UART3. On this carrier GPIO 10/11 are output channels 17 and 6, not RTS/CTS."
  sudo cp "$BOOT_CONFIG" "$BOOT_CONFIG.bak-nrf-probe"
  sudo sed -i 's/^dtoverlay=uart3-pi5,ctsrts.*/dtoverlay=uart3-pi5/' "$BOOT_CONFIG"
  REBOOT_NEEDED=1
  info "Changed to dtoverlay=uart3-pi5 (backup: $BOOT_CONFIG.bak-nrf-probe). Applies after a reboot."
fi
if [ "$(pin_func 2)" == "SDA1" ]; then
  warn "I2C is active on GPIO 2/3, which are output channels 13/14 on this carrier. Reboot to clear it."
  REBOOT_NEEDED=1
fi

# ---------------------------------------------------------------------------
step "4. UART3 port"
if [ ! -e "$PORT" ]; then
  info "$PORT missing, loading uart3-pi5 overlay for this session"
  sudo dtoverlay uart3-pi5
  sleep 1
fi
if [ ! -e "$PORT" ]; then
  fail "$PORT still missing after loading uart3-pi5."
  exit 1
fi
for g in 10 11; do
  f=$(pin_func "$g")
  if [[ "$f" == CTS3 || "$f" == RTS3 ]]; then
    warn "GPIO $g is muxed as $f by a ctsrts overlay. Returning it to a plain input."
    sudo pinctrl set "$g" ip pd
  fi
done
if [ ! -r "$PORT" ] || [ ! -w "$PORT" ]; then
  fail "No read/write access to $PORT. Run: sudo usermod -aG dialout $(id -un), then log out and back in."
  exit 1
fi
stty -F "$PORT" "$BAUD" raw -echo -crtscts clocal
for g in 8 9 26; do
  printf '    GPIO%-3s %-8s %s\n' "$g" "$(pin_func "$g")" "$(pin_level "$g")"
done
[[ "$(pin_func 8)" == TXD3 && "$(pin_func 9)" == RXD3 ]] || warn "GPIO 8/9 are not muxed as TXD3/RXD3."

# ---------------------------------------------------------------------------
step "5. Modem power-up (GPIO 26) with boot capture"
if [ "$(pin_func 26)" == "output" ] && [ "$(pin_level 26)" == "hi" ]; then
  info "Modem already powered. Power-cycling it so the boot message can be captured."
fi
sudo pinctrl set 26 op dl
sleep 2
rx_off=$(pin_level 9)
timeout 6 cat "$PORT" > "$LOG" &
capture=$!
sleep 0.5
sudo pinctrl set 26 op dh
wait "$capture"
rx_on=$(pin_level 9)
echo "    GPIO 26 now: $(pin_level 26)"
echo "    GPIO 9 (nRF TX) with modem off: $rx_off, after power-on: $rx_on"
echo "    Received during power-up:"
if [ -s "$LOG" ]; then
  tr -d '\0' < "$LOG" | sed 's/\r$//; s/^/      | /'
else
  echo "      (nothing)"
fi
BANNER=0
grep -q "Ready" "$LOG" && BANNER=1

# ---------------------------------------------------------------------------
step "6. AT link"
exec 3<>"$PORT"
LINK=0
for attempt in 1 2 3; do
  at_cmd "AT" 2
  if at_ok; then LINK=1; break; fi
done
if (( LINK == 0 )); then
  info "No reply at $BAUD baud. Trying other baud rates."
  for b in 115200 9600 57600 230400 921600 1000000; do
    stty -F "$PORT" "$b" raw -echo -crtscts clocal
    echo "    -- $b baud"
    at_cmd "AT" 2
    if at_ok; then LINK=1; BAUD=$b; break; fi
  done
  stty -F "$PORT" "$BAUD" raw -echo -crtscts clocal
fi

if (( LINK == 0 )); then
  step "Verdict: no AT link"
  if [ "$rx_on" != "hi" ]; then
    fail "The nRF TX line (GPIO 9) stayed low after GPIO 26 went high, so the modem is not getting power."
    echo "    Next: with a meter, check 5G_VDD_GPIO and VDD_FILTERED (TPS22975 U9/U10 outputs)"
    echo "    and that 5G_ENABLE_FILTERED follows GPIO 26."
  elif (( BANNER == 1 )); then
    fail "The modem boots and transmits (Ready received) but never answers commands."
    echo "    Next: the Pi TX -> nRF RX line (GPIO 8 -> P0.19) is not reaching the modem."
  else
    fail "The modem is powered (TX idles high) but says nothing on P0.18/P0.19 at any common baud rate."
    echo "    Most likely the firmware's host UART is on different pins or has hardware flow"
    echo "    control on. It needs a Serial LTE Modem build with the UART on P0.18 (TX) and"
    echo "    P0.19 (RX) and hw-flow-control removed. Ask for that build, or reflash via the"
    echo "    Tag-Connect footprint with the nRF9151 DK."
  fi
  exit 1
fi

# ---------------------------------------------------------------------------
step "7. Modem details"
at_cmd "AT+CGMR"
at_cmd "AT#XSLMVER"
at_cmd "AT%XSYSTEMMODE?"
at_cmd "AT+CFUN?"

# ---------------------------------------------------------------------------
step "8. GPS"
at_cmd "AT+CFUN=0" 5
at_cmd "AT%XSYSTEMMODE=0,0,1,0"
at_ok || warn "GNSS system mode was refused."
at_cmd "AT+CFUN=31" 5
at_ok || warn "GNSS-only functional mode was refused."
GPS_CMD="AT#XGPS"
at_cmd "$GPS_CMD=1,0,1" 5
if ! at_ok; then
  GPS_CMD="AT#XGNSS"
  at_cmd "$GPS_CMD=1,0,1" 5
fi
at_ok || warn "GPS start command was refused."

info "Waiting up to ${GPS_WAIT}s for a fix. The antenna needs open sky."
FIX=""
start=$SECONDS
next_note=$((SECONDS + 15))
while (( SECONDS - start < GPS_WAIT )); do
  if IFS= read -r -t 1 line <&3; then
    line=${line%$'\r'}
    if [ -n "$line" ]; then
      echo "    < $line"
      case "$line" in
        "#XGPS: "*|"#XGNSSPOS: "*)
          body=${line#*: }
          if (( $(awk -F, '{print NF}' <<< "$body") >= 7 )); then
            FIX=$body
            break
          fi
          ;;
      esac
    fi
  fi
  if (( SECONDS >= next_note )); then
    echo "    ... $((SECONDS - start))s, no fix yet"
    next_note=$((SECONDS + 15))
  fi
done
TTFF=$((SECONDS - start))
at_cmd "$GPS_CMD=0"
at_cmd "AT+CFUN=0" 5

# ---------------------------------------------------------------------------
step "Verdict"
info "AT link works on $PORT at $BAUD baud, flow control off."
if [ -n "$FIX" ]; then
  IFS=, read -r lat lon alt acc spd hdg dt <<< "$FIX"
  info "GPS fix after ${TTFF}s: lat $lat, lon $lon, altitude ${alt} m, accuracy ${acc} m"
  echo "    speed $(awk -v s="$spd" 'BEGIN { printf "%.1f", s * 3.6 }') km/h, heading $hdg, time $dt"
else
  warn "No GPS fix within ${GPS_WAIT}s."
  echo "    If the antenna had open sky, suspect the RF path: on the schematic the nRF9151"
  echo "    GPS pin (42) is not connected and only ANT (35) goes to the antenna. The modem"
  echo "    link itself is fine, so the OWL software side can go ahead."
fi

echo
echo "    OWL settings for this carrier:"
echo "      [GPS] port = $PORT"
echo "      [GPS] baudrate = $BAUD"
echo "      boot config: dtoverlay=uart3-pi5 and gpio=26=op,dh"

# Look for the lines themselves, whoever wrote them (owl_setup.sh --factory
# writes them via install_firstboot.sh --gps-config with its own comment).
MISSING=()
grep -qE '^dtoverlay=uart3-pi5[[:space:]]*$' "$BOOT_CONFIG" || MISSING+=("dtoverlay=uart3-pi5")
grep -qE '^gpio=26=op,dh[[:space:]]*$' "$BOOT_CONFIG" || MISSING+=("gpio=26=op,dh")
if (( ${#MISSING[@]} == 0 )); then
  info "Boot config already powers the modem and loads UART3 on every boot."
elif [ -t 0 ]; then
  echo
  read -r -p "Add ${MISSING[*]} so the modem powers up and $PORT exists on every boot? [y/N] " answer
  if [[ "$answer" =~ ^[Yy]$ ]]; then
    sudo cp "$BOOT_CONFIG" "$BOOT_CONFIG.bak-nrf-probe-final"
    printf '%s\n' "" "[all]" "$MARKER" "${MISSING[@]}" | sudo tee -a "$BOOT_CONFIG" > /dev/null
    info "Written (backup: $BOOT_CONFIG.bak-nrf-probe-final)."
    REBOOT_NEEDED=1
  fi
fi

echo
info "The modem is left powered (GPIO 26 high) until the next reboot."
(( REBOOT_NEEDED == 1 )) && warn "Reboot to apply the boot config changes."
exit 0
