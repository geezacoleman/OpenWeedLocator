#!/bin/bash

RED='\033[0;31m'
GREEN='\033[0;32m'
ORANGE='\033[0;33m'
NC='\033[0m'

# Kill any existing owl.py process
if pgrep -f "owl.py" > /dev/null; then
    echo -e "${ORANGE}[INFO] Stopping existing owl.py process...${NC}"
    pkill -f "owl.py"
    sleep 2

    if pgrep -f "owl.py" > /dev/null; then
        echo -e "${RED}[ERROR] Failed to stop owl.py process. Please try again or stop it manually.${NC}"
        exit 1
    else
        echo -e "${GREEN}[INFO] Successfully stopped existing owl.py process.${NC}"
    fi
fi

VENV_ACTIVATE="$HOME/.virtualenvs/owl/bin/activate"

if [ ! -f "$VENV_ACTIVATE" ]; then
    echo -e "${RED}[ERROR] Virtual environment not found at $VENV_ACTIVATE${NC}"
    echo -e "${RED}Please run the OWL setup script first.${NC}"
    exit 1
fi

source "$VENV_ACTIVATE"

FOCUS_SCRIPT="$HOME/owl/desktop/focus_gui.py"

if [ ! -f "$FOCUS_SCRIPT" ]; then
    echo -e "${RED}[ERROR] OWL script not found at $FOCUS_SCRIPT${NC}"
    exit 1
fi

echo -e "${GREEN}[INFO] Starting OWL focus mode...${NC}"
"$FOCUS_SCRIPT"