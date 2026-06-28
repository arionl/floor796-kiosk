#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# Floor796 Kiosk — boot wrapper
#
# Starts the player inside a bare X server with no desktop environment and
# prevents the display from ever sleeping (no screensaver, no DPMS power-off).
# Called by the systemd unit on boot.
# ─────────────────────────────────────────────────────────────────────────────
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${SCRIPT_DIR}/venv/bin/python"

# Resolve display dimensions from EDID if possible, otherwise default.
WIDTH="${KIOSK_WIDTH:-1920}"
HEIGHT="${KIOSK_HEIGHT:-1080}"

exec xinit /usr/bin/env \
    KIOSK_WIDTH="${WIDTH}" \
    KIOSK_HEIGHT="${HEIGHT}" \
    "${PYTHON}" "${SCRIPT_DIR}/kiosk_player.py" \
        --fullscreen --width "${WIDTH}" --height "${HEIGHT}" \
    -- \
    /usr/bin/X \
    :0 \
    vt7 \
    -nolisten tcp \
    -noreset \
    -dpms \
    -s 0 \
    +extension RANDR
