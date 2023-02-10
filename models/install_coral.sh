#!/bin/bash

# Single .sh file to install Google Coral requirements for the Raspberry Pi
# Adapted from https://coral.ai/docs/accelerator/get-started/#1-install-the-edge-tpu-runtime with assistance from ChatGPT

# Update and upgrade existing packages
sudo apt-get update
sudo apt-get upgrade

echo "deb https://packages.cloud.google.com/apt coral-edgetpu-stable main" | sudo tee /etc/apt/sources.list.d/coral-edgetpu.list

curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo apt-key add -

sudo apt-get update

echo "Do you want to install with MAX OPERATING FREQUENCY? Doing so will increase framerate but also device temperature and power consumption."
echo "Check official Google Coral documentation for full differences: https://coral.ai/docs/accelerator/get-started/"
read -r -p "Install MAX OPERATING FREQUENCY? [y/N] " response
if [[ "$response" =~ ^([yY][eE][sS]|[yY])+$ ]]
then
    echo "Installing MAX OPERATING FREQUENCY..."
    sudo apt-get install libedgetpu1-max
else
    echo "Installing STANDARD OPERATING FREQUENCY..."
    sudo apt-get install libedgetpu1-std
fi

# Check if Google Coral is installed
while true; do
  # Ask user to plug in USB device
  echo "Please connect the Google-Coral USB device to the USB 3.0 port. Press [y] then enter to continue."
  read -r -p "Continue? [y/N] " response
  if [[ "$response" =~ ^([yY][eE][sS]|[yY])+$ ]]
  then
      break
  else
      echo "Invalid response. Please try again."
  fi
done

echo "The pycoral library will now be installed."

sudo apt-get install python3-pycoral
