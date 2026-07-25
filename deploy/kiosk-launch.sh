#!/bin/sh
# Floor796 Kiosk — player launcher
# Called by run.sh. On KMSDRM boards (Pi 5, OrangePi 5), runs as root.
# On generic X11 boards, runs as the kiosk user.
#
# Board detection is handled by floor796_kiosk.board_detect (Python).
# The player code in player.py also calls detect_board() and sets
# SDL_VIDEODRIVER accordingly — this script just provides the right
# env vars and decides whether to run as root or the kiosk user.
#
#   - OrangePi 5 Max (Panthor):  KMSDRM, runs as root
#   - Raspberry Pi 5 (V3D):      KMSDRM, runs as root
#   - Generic:                   X11, runs as kiosk user

export HOME=/home/kiosk
export SDL_AUDIODRIVER=dummy

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# KIOSK_WIDTH / KIOSK_HEIGHT default to 0, which means auto-detect
# the native resolution of the connected display via pygame.
WIDTH="${KIOSK_WIDTH:-0}"
HEIGHT="${KIOSK_HEIGHT:-0}"

# KIOSK_OVERSCAN_MARGIN: pixels to inset UI elements for TVs with
# overscan.  0 = disabled (default, correct for modern displays).
OVERSCAN="${KIOSK_OVERSCAN_MARGIN:-0}"

cd "${INSTALL_DIR}"

# ── Detect board type and rendering configuration ──
eval "$(python3 -m floor796_kiosk.board_detect --shell 2>/dev/null)"

if [ "${RUNS_AS_ROOT}" = "1" ]; then
    # OrangePi 5 Max or Raspberry Pi 5: KMSDRM direct rendering.
    # KMSDRM requires DRM master access for page flipping. On this kiosk
    # appliance (no desktop session manager), only root can acquire DRM
    # master without a logind seat session.
    # SDL_VIDEODRIVER is set by the player code (kmsdrm)
    exec /usr/bin/python3 \
        -m floor796_kiosk \
        --fullscreen \
        --width "${WIDTH}" \
        --height "${HEIGHT}" \
        --overscan-margin "${OVERSCAN}"
else
    # Generic / unknown board (X11 fallback):
    # X is started by xinit in run.sh; this script runs inside that X session.
    # The kiosk user can access the display through X.
    export SDL_VIDEODRIVER=x11
    export DISPLAY=:0

    exec /sbin/runuser -u kiosk -- /usr/bin/python3 \
        -m floor796_kiosk \
        --fullscreen \
        --width "${WIDTH}" \
        --height "${HEIGHT}" \
        --overscan-margin "${OVERSCAN}"
fi
