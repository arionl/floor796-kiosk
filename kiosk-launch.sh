#!/bin/sh
# Floor796 Kiosk — player launcher (runs inside X session)
# This script is called by xinit. It runs as root initially;
# it drops to the kiosk user to execute the Python player.
export HOME=/home/kiosk
export SDL_AUDIODRIVER=dummy
export SDL_VIDEODRIVER=x11
export DISPLAY=:0

cd /home/kiosk/floor796-kiosk

exec /sbin/runuser -u kiosk -- /usr/bin/python3 \
    /home/kiosk/floor796-kiosk/kiosk_player.py \
    --fullscreen \
    --width "${KIOSK_WIDTH:-1920}" \
    --height "${KIOSK_HEIGHT:-1080}"
