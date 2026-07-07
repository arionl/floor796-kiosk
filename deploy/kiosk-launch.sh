#!/bin/sh
# Floor796 Kiosk — player launcher
# Called by run.sh. Runs as root initially; drops to kiosk user.
#
# On the OrangePi 5 Max (RK3588 + Panthor), we use SDL2's KMSDRM video
# driver to bypass X11 entirely and connect directly to the DRM/KMS
# subsystem via GBM. Mesa's Panthor driver provides a standard DRM
# render node that works with Mesa libgbm/libEGL, giving us hardware-
# accelerated EGL/GLES on the Mali-G610 GPU.
#
# On the Raspberry Pi 5, Mesa's V3D driver works fine through X11, so
# we keep the X11 path (xinit still starts X, SDL_VIDEODRIVER=x11).
#
# The player code in player.py auto-detects Panthor and sets
# SDL_VIDEODRIVER=kmsdrm — this script just provides the right env vars
# and decides whether to start X or not.

export HOME=/home/kiosk
export SDL_AUDIODRIVER=dummy

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# KIOSK_WIDTH / KIOSK_HEIGHT default to 0, which means auto-detect
# the native resolution of the connected display via pygame.
WIDTH="${KIOSK_WIDTH:-0}"
HEIGHT="${KIOSK_HEIGHT:-0}"

cd "${INSTALL_DIR}"

# ── Detect Panthor GPU and choose rendering path ──
PANTHOR_DEVICE=""
for i in 128 129 130 131 132; do
    if [ -f "/sys/class/drm/renderD${i}/device/uevent" ]; then
        if grep -q panthor "/sys/class/drm/renderD${i}/device/uevent" 2>/dev/null; then
            PANTHOR_DEVICE="renderD${i}"
            break
        fi
    fi
done

if [ -n "${PANTHOR_DEVICE}" ]; then
    # OrangePi 5 Max (RK3588 + Mali-G610 via Mesa Panthor):
    # Use KMSDRM for direct GPU access — no X11 needed.
    # Panthor + Mesa provides EGL/OpenGL ES via standard DRM/GBM.
    #
    # KMSDRM requires DRM master access for page flipping. On this kiosk
    # appliance (no desktop session manager), only root can acquire DRM
    # master without a logind seat session. The player runs as root
    # directly — this is a dedicated kiosk with no other users.
    # SDL_VIDEODRIVER is set by the player code (kmsdrm)

    exec /usr/bin/python3 \
        -m floor796_kiosk \
        --fullscreen \
        --width "${WIDTH}" \
        --height "${HEIGHT}"
else
    # Raspberry Pi 5 (Mesa V3D via X11):
    # X is started by xinit in run.sh; this script runs inside that X session.
    # The kiosk user can access the display through X.
    export SDL_VIDEODRIVER=x11
    export DISPLAY=:0

    exec /sbin/runuser -u kiosk -- /usr/bin/python3 \
        -m floor796_kiosk \
        --fullscreen \
        --width "${WIDTH}" \
        --height "${HEIGHT}"
fi
