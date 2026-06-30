#!/bin/sh
# Floor796 Kiosk — player launcher (runs inside X session)
# Called by xinit. Runs as root initially; drops to kiosk user.
export HOME=/home/kiosk
export SDL_AUDIODRIVER=dummy
export SDL_VIDEODRIVER=x11
export DISPLAY=:0

INSTALL_DIR=/opt/floor796-kiosk
# KIOSK_WIDTH / KIOSK_HEIGHT default to 0, which means auto-detect
# the native resolution of the connected display via pygame.
WIDTH="${KIOSK_WIDTH:-0}"
HEIGHT="${KIOSK_HEIGHT:-0}"

cd "${INSTALL_DIR}"

exec /sbin/runuser -u kiosk -- /usr/bin/python3 \
    "${INSTALL_DIR}/kiosk_player.py" \
    --fullscreen \
    --width "${WIDTH}" \
    --height "${HEIGHT}"
