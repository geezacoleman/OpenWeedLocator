#!/bin/bash
# OWL recording / control-loss diagnostic. Read-only: it changes nothing.
#
# For the Pi 5 field report (2026-10-29): Record -> Stop in the app, then no
# sessions on the Data or Map page, and later every control dead while the
# video kept streaming. Run it RIGHT AFTER reproducing, before rebooting.
#
# Each section tests one assumption:
#   - the services and the MQTT loop are alive (state still flowing)
#   - owl.py and the dashboard agree on where recordings go
#   - the recording really landed on disk
#   - the USB drive stayed on the bus (power, resets, I/O errors)
#   - nothing is stuck in uninterruptible I/O (D state)
#   - the dashboard answers quickly (hung gunicorn threads)
#
# Copy it over with scp (pasting big scripts over the hotspot loses lines):
#   scp tools/owl_record_diag.sh owl@<owl>:~ && ssh owl@<owl> bash owl_record_diag.sh
#
# Everything is also written to ~/owl_record_diag_<time>.txt.

set -u

OWL_DIR=${OWL_DIR:-$HOME/owl}
OUT="$HOME/owl_record_diag_$(date +%Y%m%d_%H%M%S).txt"

section() { echo; echo "=================== $* ==================="; }
run() { echo "\$ $*"; "$@" 2>&1; echo; }

sudo -v || exit 1

{
echo "OWL record diagnostic $(date)  uptime $(cut -d. -f1 /proc/uptime)s"
run cat /proc/device-tree/model
echo
run git -C "$OWL_DIR" log --oneline -1

# --- Assumption: every service is up --------------------------------------------
section "SERVICES"
for unit in owl.service owl-dash.service mosquitto nginx; do
    printf '%-18s %s\n' "$unit" "$(systemctl is-active "$unit")"
done
run systemctl show owl.service owl-dash.service -p User -p NRestarts -p ActiveEnterTimestamp

# --- Assumption: owl.py still publishes state (MQTT loop alive) ------------------
# No owl/state within 10 s while owl.service is active = the OWL's MQTT thread
# is stuck or dead (controls dead, video still live).
section "OWL STATE (one owl/state message, 10 s max)"
STATE=$(timeout 10 mosquitto_sub -h localhost -t 'owl/state' -C 1 2>&1)
if [ -z "$STATE" ]; then
    echo "NO owl/state message in 10 s"
else
    echo "$STATE" | python3 -c '
import json, sys
s = json.load(sys.stdin)
keys = ["owl_running", "detection_enable", "image_sample_enable", "recording_paused",
        "save_directory", "storage_available", "recording_location", "storage_fallback",
        "recording_blocked_reason", "storage_free_mb", "storage_warning", "fault",
        "gps_fix", "gps_source", "gps_speed_kmh", "speed_kmh", "actuation_source"]
missing = "<absent>"
for k in keys:
    print(f"{k:26} {s.get(k, missing)!r}")
' 2>&1
fi

# --- Assumption: the dashboard answers quickly -----------------------------------
# A hang here (8 stuck gunicorn threads) makes the app say "Unreachable" while
# the video, served by owl.py through nginx, keeps playing.
section "DASHBOARD RESPONSE TIMES"
for i in 1 2 3; do
    curl -sk -m 8 -o /dev/null -w "system_stats   %{http_code} %{time_total}s\n" https://localhost/api/system_stats
done
curl -sk -m 8 -o /dev/null -w "latest_frame   %{http_code} %{time_total}s\n" https://localhost/latest_frame.jpg

# --- Assumption: owl.py and the dashboard agree on the recordings directory ------
section "SESSIONS AS THE APP SEES THEM"
curl -sk -m 15 https://localhost/api/downloads/sessions | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("error:", d.get("error"))
print("storage:", d.get("storage"))
for s in (d.get("sessions") or [])[:10]:
    print(" ", s.get("session_id"), "images:", s.get("image_count"))
print("sessions listed:", len(d.get("sessions") or []))
' 2>&1
section "STATS (recording / storage / gps)"
curl -sk -m 8 https://localhost/api/system_stats | python3 -c '
import json, sys
d = json.load(sys.stdin)
for k in sorted(d):
    if any(w in k for w in ("record", "storage", "save", "gps", "speed", "fault", "image_sample", "owl_running")):
        print(f"{k:28} {d[k]!r}")
' 2>&1

# --- Assumption: the recording landed on disk -----------------------------------
section "STORAGE CONFIG"
run cat "$OWL_DIR/config/active_config.txt"
grep -nE "^(save_directory|storage_location|internal_save_directory|min_free_gb|controller_type)" "$OWL_DIR"/config/*.ini 2>&1
section "MOUNTS AND SPACE"
run findmnt -l -o TARGET,SOURCE,FSTYPE,OPTIONS -t vfat,exfat,ext4,ntfs,ntfs3,fuseblk
run df -h / /media/*/* "$HOME"
run df -i / /media/*/*
section "RECORDING FILES (newest sessions, last 3 h)"
for root in /media/*/* "$HOME/owl_images" /home/owl/owl_images "$OWL_DIR/owl_data"; do
    [ -d "$root" ] || continue
    echo "--- $root"
    find "$root" -maxdepth 2 -mindepth 2 -type d -mmin -180 2>/dev/null | sort | tail -n 10 | while read -r d; do
        echo "  $d  jpg=$(find "$d" -maxdepth 1 -name '*.jpg' | wc -l)  $(ls "$d" | grep -E 'metadata|locations|track' | tr '\n' ' ')"
    done
done

# --- Assumption: the USB drive stayed on the bus ---------------------------------
# usb_max_current_enable=0 = PSU did not negotiate 5 A, USB capped at 600 mA.
section "POWER AND USB"
run vcgencmd get_throttled
run vcgencmd get_config usb_max_current_enable
sudo dmesg -T | grep -iE "usb [0-9-]+: (USB disconnect|reset|new)|I/O error|blk_update_request|Buffer I/O|exfat|FAT-fs|EXT4-fs error|over-current|under-voltage|blocked for more than" | tail -n 40

# --- Assumption: nothing is stuck in uninterruptible I/O -------------------------
section "PROCESSES IN D STATE / STUCK HELPERS"
ps -eo pid,stat,etime,wchan:28,cmd | awk 'NR==1 || $2 ~ /D/ || /lsusb|vcgencmd/' | grep -v awk
section "THREAD DUMPS (py-spy, if installed)"
if command -v py-spy >/dev/null 2>&1; then
    OWL_PID=$(pgrep -f "python.*owl.py" | head -n 1)
    DASH_PID=$(pgrep -f "gunicorn.*standalone" | tail -n 1)
    [ -n "$OWL_PID" ] && run sudo py-spy dump --pid "$OWL_PID"
    [ -n "$DASH_PID" ] && run sudo py-spy dump --pid "$DASH_PID"
else
    echo "py-spy not installed (optional: pip install py-spy in the owl venv)"
fi

# --- What the logs say -----------------------------------------------------------
section "OWL LOG (this boot, filtered)"
journalctl -u owl.service -b --no-pager 2>&1 | grep -E "storage|Storage|recording|Recording|New recording session|Received command|metadata|FAULT|control_lost|Traceback|Error|CRITICAL|MQTT" | tail -n 80
section "DASHBOARD LOG (this boot, filtered)"
journalctl -u owl-dash.service -b --no-pager 2>&1 | grep -E "MQTT|Timeout|TIMEOUT|WORKER|Traceback|Error|error" | tail -n 40
} 2>&1 | tee "$OUT"

echo
echo "Saved to $OUT"
