#!/bin/sh
# Floor796 Kiosk — player launcher
# Called by run.sh. On OrangePi, runs as root; on Pi 5, drops to kiosk user.
#
# Board detection is handled by floor796_kiosk.board_detect (Python).
# The player code in player.py also calls detect_board() and sets
# SDL_VIDEODRIVER accordingly — this script just provides the right
# env vars and decides whether to run as root or the kiosk user.
#
#   - OrangePi 5 Max (Panthor): KMSDRM, runs as root (DRM master access)
#   - Raspberry Pi 5 (V3D):     X11, runs as kiosk user
#   - Generic:                   X11, runs as kiosk user

export HOME=/home/kiosk
export SDL_AUDIODRIVER=dummy

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# KIOSK_WIDTH / KIOSK_HEIGHT default to 0, which means auto-detect
# the native resolution of the connected display via pygame.
WIDTH="${KIOSK_WIDTH:-0}"
HEIGHT="${KIOSK_HEIGHT:-0}"

cd "${INSTALL_DIR}"

# ── Detect board type and rendering configuration ──
eval "$(python3 -m floor796_kiosk.board_detect --shell 2>/dev/null)"

if [ "${RUNS_AS_ROOT}" = "1" ]; then
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
    # Raspberry Pi 5 (Mesa V3D via X11) or generic board:
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
