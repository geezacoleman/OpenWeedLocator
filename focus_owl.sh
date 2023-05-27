#!/bin/bash

# Check if owl.py or greenonbrown.py is running
if pgrep -f "owl.py" >/dev/null; then
  program_name="owl.py"
elif pgrep -f "greenonbrown.py" >/dev/null; then
  program_name="greenonbrown.py"
fi

if [[ -n $program_name ]]; then
  echo "[INFO] '$program_name' is running and needs to be stopped before continuing with focusing."

  # Get PID of owl.py or greenonbrown.py
  pid=$(pgrep -f "$program_name")

  # Kill the process using sudo
  sudo kill "$pid"

  echo "[INFO] '$program_name' (PID: $pid) has been stopped. Continuing with focusing..."
else
  echo "[INFO] Neither owl.py nor greenonbrown.py were found to be running. Continuing with focusing..."
fi

# Launch owl.py with focusing flag
cd ~/owl
./owl.py --focusing --show-display
