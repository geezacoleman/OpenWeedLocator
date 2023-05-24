#!/bin/bash

cd ~

# Rename the old 'owl' folder to 'owl-DATE'
if [ -d "owl" ]; then
    mv owl "owl_$(date +'%Y%m%d_%H%M%S')"
fi    

# Download the new software from GitHub
git clone https://github.com/geezacoleman/OpenWeedLocator owl     
cd ~/owl

# update the system
echo "[INFO] Upgrading Raspberry Pi system...this may take some time. You will be asked to confirm at some steps."
sudo apt-get update
sudo apt-get upgrade

# Installing the requirements
echo "[INFO] Upgrading OWL requirements."
source `which workon` owl
pip install -r requirements.txt

# Changing permissions to make files executable
chmod a+x owl.py
chmod a+x owl_boot.sh

echo "[COMPLETE] OWL update has executed successfully."
