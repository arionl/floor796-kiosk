#!/bin/sh
# Floor796 Kiosk — boot wrapper
# Starts a bare X server (as root for VT access) then runs the player.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# X server starts as root (needs VT7 access).
# The player runs via the launch script as the kiosk user.
xinit "${SCRIPT_DIR}/kiosk-launch.sh" \
    -- \
    /usr/bin/X :0 vt7 \
    -nolisten tcp -noreset -dpms -s 0
