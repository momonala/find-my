#!/bin/bash
# Pushes the local DB and Apple/iCloud session secrets to the pi-cloud clone.
set -e

CYAN='\033[0;36m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

REMOTE_HOST="pi-cloud"
REMOTE_DIR="/home/mnalavadi/findmy"

cd "$(dirname "$0")"

echo -e "${CYAN}==>${NC} Checking connectivity to ${YELLOW}${REMOTE_HOST}${NC}"
if ! ssh "$REMOTE_HOST" "mkdir -p '${REMOTE_DIR}'"; then
    echo -e "${RED}✗ Could not reach ${REMOTE_HOST} or create ${REMOTE_DIR}${NC}"
    exit 1
fi

# src: local path (dirs must end in /). dest_dir: destination dir on the
# remote, relative to REMOTE_DIR, that src's contents/file should land in.
sync_path() {
    local src="$1"
    local dest_dir="$2"
    if [ ! -e "$src" ]; then
        echo -e "${YELLOW}⚠ skipping ${src} (not found locally)${NC}"
        return
    fi
    echo -e "${CYAN}==>${NC} Syncing ${YELLOW}${src}${NC} -> ${YELLOW}${REMOTE_HOST}:${REMOTE_DIR}/${dest_dir}${NC}"
    ssh "$REMOTE_HOST" "mkdir -p '${REMOTE_DIR}/${dest_dir}'"
    rsync -avz --progress -e ssh "$src" "${REMOTE_HOST}:${REMOTE_DIR}/${dest_dir}"
}

sync_path "data/findmy.db" "data/"
sync_path ".icloud_session/" ".icloud_session/"
sync_path ".env" ""

echo -e "${GREEN}✅ Secrets synced to ${REMOTE_HOST}:${REMOTE_DIR}${NC}"
