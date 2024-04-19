#!/bin/bash

# automatically determine the home directory, to avoid issues with username
source $HOME/.bashrc

# activate the 'owl' virtual environment
source $HOME/.virtualenvs/owl/bin/activate

# change directory to the owl folder
cd $HOME/owl

# run owl.py in the background and save the log output
LOG_DATE=$(date -u +"%Y-%m-%dT%H-%M-%SZ")
./owl.py > $HOME/owl/logs/owl_$LOG_DATE.log 2>&1 &
