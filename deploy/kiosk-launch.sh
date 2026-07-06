#!/bin/sh
# Floor796 Kiosk — player launcher (runs inside X session)
# Called by xinit. Runs as root initially; drops to kiosk user.
export HOME=/home/kiosk
export SDL_AUDIODRIVER=dummy
export SDL_VIDEODRIVER=x11
export DISPLAY=:0

# libmali provides EGL/OpenGL ES via the Mali GPU.  SDL needs these
# env vars to pick the EGL backend instead of desktop GLX (which falls
# back to llvmpipe software rendering).
export SDL_OPENGL_ES=1
export SDL_EGL_LIBRARY=/usr/lib/aarch64-linux-gnu/libmali.so.1
export LIBGL_DRI3_DISABLE=1

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# KIOSK_WIDTH / KIOSK_HEIGHT default to 0, which means auto-detect
# the native resolution of the connected display via pygame.
WIDTH="${KIOSK_WIDTH:-0}"
HEIGHT="${KIOSK_HEIGHT:-0}"

cd "${INSTALL_DIR}"

exec /sbin/runuser -u kiosk -- /usr/bin/python3 \
    -m floor796_kiosk \
    --fullscreen \
    --width "${WIDTH}" \
    --height "${HEIGHT}"
