#!/bin/sh
# Floor796 Kiosk — boot wrapper
# Starts a bare X server (as root for VT access) then runs the player.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- Performance tuning (avoid swap stutter on 4 GB Pi) ---
# 1. Lower swappiness — prefer keeping tile surfaces in RAM over swapping.
sysctl -w vm.swappiness=1

# 2. Set CPU governor to performance for stable frame pacing.
if [ -f /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor ]; then
    for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
        echo performance > "$cpu" 2>/dev/null
    done
fi

# X server starts as root (needs VT7 access).
# The player runs via the launch script as the kiosk user.
# -depth 16 halves video memory usage (critical on 4 GB Pi to avoid swap).
xinit "${SCRIPT_DIR}/kiosk-launch.sh" \
    -- \
    /usr/bin/X :0 vt7 \
    -nolisten tcp -noreset -dpms -s 0 -depth 16
