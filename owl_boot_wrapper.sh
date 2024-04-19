#!/bin/bash

# This script will find the user's home directory, making OWL software more portable.
for dir in /home/*; do
  if [ -d "$dir" ]; then
    username=$(basename "$dir")
    if [ "$username" != "root" ]; then
      HOME_DIR="$dir"
      break
    fi
  fi
done

if [ -z "$HOME_DIR" ]; then
  echo "No suitable user directory found."
  exit 1
fi

sudo -u "$username" -H /usr/local/bin/owl_boot.sh