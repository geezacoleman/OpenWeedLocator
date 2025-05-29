#!/bin/bash
set -e

# color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
ORANGE='\033[0;33m'
NC='\033[0m'

REPO_DIR=~/owl
REPO_URL=https://github.com/geezacoleman/OpenWeedLocator.git
CONFIG_PATH="config/*.ini"

echo -e "${ORANGE}[INFO] Starting OWL updater...${NC}"

# 1) stop any running owl.py instances
if pgrep -f "owl.py" > /dev/null; then
    echo -e "${ORANGE}[INFO] Stopping existing owl.py process...${NC}"
    pkill -f "owl.py"
    sleep 2
    if pgrep -f "owl.py" > /dev/null; then
        echo -e "${RED}[ERROR] Failed to stop owl.py. Please stop it manually and retry.${NC}"
        exit 1
    fi
    echo -e "${GREEN}[INFO] owl.py stopped.${NC}"
fi

# 2) update repo
echo -e "${ORANGE}[INFO] Updating existing OWL repository...${NC}"
cd "$REPO_DIR"

# record old version
OLD_VERSION=$(python3 - <<EOF
import version
print(version.VERSION)
EOF
)

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
echo -e "${ORANGE}[INFO] On branch '${CURRENT_BRANCH}'.${NC}"
git fetch origin

# backup all the .ini config files in config/
STASHED_CONFIG=false
if ! git diff --quiet -- $CONFIG_PATH; then
    echo -e "${ORANGE}[INFO] Detected local changes in your INI files under config/ .${NC}"
    echo -e "${ORANGE}[INFO] Backing them up to a stash...${NC}"
    git stash push -m "auto-backup config INI" -- $CONFIG_PATH >/dev/null
    STASHED_CONFIG=true
    echo -e "${GREEN}[SUCCESS] Your config/*.ini changes have been stashed as stash@{0}.${NC}"
fi

# other uncommitted changes?
if ! git diff --quiet -- . && ! git diff --cached --quiet -- .; then
    echo -e "${ORANGE}[WARNING] You have other uncommitted changes:${NC}"
    git status --short

    read -p "Would you like to see a summary of those changes? [y/N]: " detail
    if [[ "$detail" =~ ^[Yy] ]]; then
        echo -e "${ORANGE}[INFO] Showing change stats...${NC}"
        git diff --stat
    fi

    # now ask about stashing
    read -p "Stash these changes before pulling? [y/N]: " yn
    case "$yn" in
        [Yy]* )
            git stash push -m "auto-stash before OWL update"
            echo -e "${GREEN}[SUCCESS] Other changes stashed.${NC}"
            ;;
        * )
            echo -e "${RED}[ERROR] Please commit or stash manually, then rerun.${NC}"
            exit 1
            ;;
    esac
fi


echo -e "${ORANGE}[INFO] Pulling latest from origin/${CURRENT_BRANCH}...${NC}"
if git pull origin "$CURRENT_BRANCH"; then
    echo -e "${GREEN}[SUCCESS] Repository updated.${NC}"
else
    echo -e "${RED}[ERROR] git pull failed. Resolve and retry.${NC}"
    exit 1
fi

# re-apply the INI stash if made
if [ "$STASHED_CONFIG" = true ]; then
    echo -e "${ORANGE}[INFO] Re-applying your config/*.ini changes...${NC}"
    if git stash pop stash@{0}; then
        echo -e "${GREEN}[SUCCESS] config/*.ini restored and merged successfully.${NC}"
    else
        echo -e "${RED}[WARNING] Conflicts occurred while merging your INI files.${NC}"
        echo -e "${RED}Please open each affected file under ~/owl/config/, look for '<<<<<<<', fix them, then:${NC}"
        echo -e "${RED}  cd ~/owl && git add config/*.ini && git commit${NC}"
        exit 1
    fi
fi

# record new version
NEW_VERSION=$(python3 - <<EOF
import version
print(version.VERSION)
EOF
)

# 3) System upgrade
echo -e "${ORANGE}[INFO] Upgrading system...${NC}"
sudo apt-get update -y
sudo apt full-upgrade -y

# 4) Python requirements
echo -e "${ORANGE}[INFO] Installing OWL Python requirements...${NC}"
if command -v workon > /dev/null; then
    source "$(which workon)"
    workon owl
else
    echo -e "${ORANGE}[WARNING] virtualenvwrapper not found; activate your venv manually if needed.${NC}"
fi
pip install -r requirements.txt

SCRIPT_DIR=$(dirname "$(realpath "$0")")
FOCUS_WRAPPER="${SCRIPT_DIR}/desktop/focus_owl_desktop.sh"
FOCUS_GUI="${SCRIPT_DIR}/desktop/focus_gui.py"
chmod +x "$FOCUS_WRAPPER"
chmod +x "$FOCUS_GUI"

echo -e "${GREEN}[COMPLETE] Upgraded version: ${OLD_VERSION} → ${NEW_VERSION}${NC}"
