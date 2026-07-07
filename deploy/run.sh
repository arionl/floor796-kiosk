#!/bin/sh
# Floor796 Kiosk — boot wrapper
# Starts the player either with X11 (Pi 5) or KMSDRM (OrangePi with Panthor).
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

# ── Choose rendering path ──
# On the OrangePi 5 Max (RK3588 + Mesa Panthor), use KMSDRM — no X server
# needed. The player connects directly to the DRM/KMS subsystem via
# GBM and gets hardware-accelerated EGL/GLES on the Mali-G610 GPU.
#
# On the Raspberry Pi 5, start X (needed for Mesa V3D driver).
# Detect Panthor by looking for a panthor render node.
HAS_PANTHOR=""
for i in 128 129 130 131 132; do
    if [ -f "/sys/class/drm/renderD${i}/device/uevent" ]; then
        if grep -q panthor "/sys/class/drm/renderD${i}/device/uevent" 2>/dev/null; then
            HAS_PANTHOR=1
            break
        fi
    fi
done

if [ -n "${HAS_PANTHOR}" ]; then
    # OrangePi: KMSDRM direct rendering (no X11)
    # KMSDRM needs root for DRM master access (page flip).
    exec "${SCRIPT_DIR}/kiosk-launch.sh"
else
    # Raspberry Pi: X server starts as root (needs VT7 access).
    # The player runs via the launch script as the kiosk user.
    xinit "${SCRIPT_DIR}/kiosk-launch.sh" \
        -- \
        /usr/bin/X :0 vt7 \
        -nolisten tcp -noreset -dpms -s 0
fi
