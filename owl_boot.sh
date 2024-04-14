#!/bin/bash

# automatically determine the home directory, to avoid issues with usernam
HOME_DIR=$(getent passwd $USER | cut -d: -f6)
source $HOME_DIR/.bashrc

# activate the 'owl' virtual environment
source $HOME_DIR/.virtualenvs/owl/bin/activate

# change directory to the owl folder
cd $HOME_DIR/owl

# run owl.py in the background and save the log output
LOG_DATE=$(date -u +"%Y-%m-%dT%H-%M-%SZ")
./owl.py > $HOME_DIR/owl/logs/owl_$LOG_DATE.log 2>&1 &
