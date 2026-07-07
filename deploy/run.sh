#!/bin/sh
# Floor796 Kiosk — boot wrapper
# Starts the player with the correct rendering path based on board type.
#
#   - OrangePi 5 Max (RK3588 + Panthor): KMSDRM direct rendering, no X server
#   - Raspberry Pi 5 (V3D): X server + SDL X11 driver
#   - Generic: X server + SDL X11 driver
#
# Board detection is handled by floor796_kiosk.board_detect (Python).
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# --- Performance tuning (avoid swap stutter on 4 GB Pi) ---
# 1. Lower swappiness — prefer keeping tile surfaces in RAM over swapping.
sysctl -w vm.swappiness=1

# 2. Set CPU governor to performance for stable frame pacing.
if [ -f /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor ]; then
    for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
        echo performance > "$cpu" 2>/dev/null
    done
fi

# ── Detect board type and rendering configuration ──
# Uses the Python board_detect module for consistent detection between
# the shell scripts and the player code.
eval "$(python3 -m floor796_kiosk.board_detect --shell 2>/dev/null)"

if [ "${NEEDS_X11}" = "0" ]; then
    # OrangePi 5 Max (RK3588 + Mesa Panthor): KMSDRM direct rendering (no X11)
    # KMSDRM needs root for DRM master access (page flip).
    exec "${SCRIPT_DIR}/kiosk-launch.sh"
else
    # Raspberry Pi 5 or generic: X server starts as root (needs VT7 access).
    # The player runs via the launch script as the kiosk user.
    xinit "${SCRIPT_DIR}/kiosk-launch.sh" \
        -- \
        /usr/bin/X :0 vt7 \
        -nolisten tcp -noreset -dpms -s 0
fi
