#!/usr/bin/env bash
#
# install.sh — Floor796 Kiosk installer for embedded boards
#
# Supports:
#   - Raspberry Pi 5 (Raspbian Pi OS)  → X11 + Mesa V3D
#   - OrangePi 5 Max (DietPi/Armbian)  → KMSDRM + Mesa Panthor
#   - Generic Linux                    → X11 fallback
#
# Run as root:
#     sudo bash install.sh
#
# What it does:
#   1. Detects the board type
#   2. Installs board-appropriate system dependencies
#   3. Creates a dedicated 'kiosk' user if needed
#   4. Installs the Python packages (pygame, brotli)
#   5. Copies the floor796_kiosk package + deploy scripts
#   6. Installs systemd service for cold-boot auto-start
#   7. Disables desktop, lightdm, and all display sleep / screensaver
#
# On first boot, the player automatically:
#   - Downloads tiles from floor796.com
#   - Downloads object labels (changelog.json) from floor796.com
#   - Decodes tile animation strips
#   - Builds the content density mask (content_mask.npz)
# All cached for subsequent boots.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
INSTALL_DIR="${INSTALL_DIR:-/opt/floor796-kiosk}"
SERVICE_USER="kiosk"
SERVICE_SRC="${SCRIPT_DIR}/floor796-kiosk.service"
SERVICE_DST="/etc/systemd/system/floor796-kiosk.service"

echo "═══════════════════════════════════════════════════════════════"
echo "  Floor796 Kiosk Installer"
echo "═══════════════════════════════════════════════════════════════"
echo "  Source:      ${SOURCE_DIR}"
echo "  Install dir: ${INSTALL_DIR}"
echo ""

# ─── Preflight ───────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Run as root:  sudo bash install.sh"
    exit 1
fi

if [[ ! -f "${SOURCE_DIR}/floor796_kiosk/player.py" ]]; then
    echo "ERROR: floor796_kiosk/player.py not found in ${SOURCE_DIR}"
    exit 1
fi

# ─── 0. Detect board type ────────────────────────────────────────────────────
echo "[0/7] Detecting board type..."

# Try Python board_detect module first (most reliable)
BOARD_TYPE=""
GPU_DRIVER=""
RENDER_BACKEND=""
NEEDS_X11=""

# Install python3 if not present (needed for board detection)
if ! command -v python3 &>/dev/null; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 >/dev/null
fi

if python3 -c "import sys; sys.path.insert(0, '${SOURCE_DIR}'); from floor796_kiosk.board_detect import detect_board, get_render_config" 2>/dev/null; then
    eval "$(cd "${SOURCE_DIR}" && python3 -m floor796_kiosk.board_detect --shell 2>/dev/null)"
fi

# Fallback: shell-based detection if Python module failed
if [[ -z "${BOARD_TYPE}" ]]; then
    MODEL=""
    [[ -f /proc/device-tree/model ]] && MODEL="$(cat /proc/device-tree/model | tr -d '\0')"

    if echo "${MODEL}" | grep -qi "raspberry pi"; then
        BOARD_TYPE="raspberry_pi_5"
        GPU_DRIVER="v3d"
        RENDER_BACKEND="x11"
        NEEDS_X11=1
    elif echo "${MODEL}" | grep -qi "orangepi"; then
        BOARD_TYPE="orangepi_5"
        GPU_DRIVER="panthor"
        RENDER_BACKEND="kmsdrm"
        NEEDS_X11=0
    else
        # Check DRM render nodes for GPU driver
        for i in 128 129 130 131 132; do
            if [[ -f "/sys/class/drm/renderD${i}/device/uevent" ]]; then
                if grep -q panthor "/sys/class/drm/renderD${i}/device/uevent" 2>/dev/null; then
                    BOARD_TYPE="orangepi_5"
                    GPU_DRIVER="panthor"
                    RENDER_BACKEND="kmsdrm"
                    NEEDS_X11=0
                    break
                fi
                if grep -q v3d "/sys/class/drm/renderD${i}/device/uevent" 2>/dev/null; then
                    BOARD_TYPE="raspberry_pi_5"
                    GPU_DRIVER="v3d"
                    RENDER_BACKEND="x11"
                    NEEDS_X11=1
                    break
                fi
            fi
        done
    fi

    # Ultimate fallback
    if [[ -z "${BOARD_TYPE}" ]]; then
        BOARD_TYPE="generic"
        GPU_DRIVER="unknown"
        RENDER_BACKEND="x11"
        NEEDS_X11=1
    fi
fi

echo "    ✓ Board: ${BOARD_TYPE}"
echo "      GPU: ${GPU_DRIVER}, Render: ${RENDER_BACKEND}, X11: ${NEEDS_X11}"
echo ""

# ─── 1. System packages (board-specific) ─────────────────────────────────────
echo "[1/7] Installing system packages..."

# Common packages needed on all boards
COMMON_PKGS=(
    ffmpeg
    python3
    python3-pip
    python3-venv
    python3-numpy
    mesa-utils
    libegl-mesa0
    libgles2
)

# Board-specific packages
if [[ "${BOARD_TYPE}" == "orangepi_5" ]]; then
    # OrangePi 5 Max: KMSDRM + Panthor — no X11 needed
    # Mesa packages provide libgbm, libEGL, libGLES for KMSDRM
    BOARD_PKGS=(
        libgbm1
        libgl1-mesa-dri
        mesa-va-drivers
    )
    echo "    (OrangePi: installing KMSDRM/Mesa packages, skipping X11)"
elif [[ "${BOARD_TYPE}" == "raspberry_pi_5" ]]; then
    # Raspberry Pi 5: X11 + V3D
    BOARD_PKGS=(
        xserver-xorg
        xserver-xorg-video-all
        xserver-xorg-video-fbdev
        xinit
        x11-xserver-utils
        scrot
    )
    echo "    (Raspberry Pi: installing X11 + Mesa packages)"
else
    # Generic: install X11 + Mesa (works on most boards)
    BOARD_PKGS=(
        xserver-xorg
        xserver-xorg-video-all
        xserver-xorg-video-fbdev
        xinit
        x11-xserver-utils
        scrot
    )
    echo "    (Generic: installing X11 + Mesa packages)"
fi

apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    "${COMMON_PKGS[@]}" \
    "${BOARD_PKGS[@]}" \
    >/dev/null
echo "    ✓ System packages installed"

# ─── 2. Create kiosk user ────────────────────────────────────────────────────
echo "[2/7] Setting up kiosk user..."
if ! id -u "${SERVICE_USER}" &>/dev/null; then
    useradd -m -s /bin/bash "${SERVICE_USER}"
    # Set password interactively
    echo "    Enter a password for '${SERVICE_USER}':"
    passwd "${SERVICE_USER}"
    echo "    ✓ Created user '${SERVICE_USER}'"
else
    echo "    ✓ User '${SERVICE_USER}' already exists"
fi
usermod -aG video,render,input,tty "${SERVICE_USER}" 2>/dev/null || true

# ─── 3. Install pygame ───────────────────────────────────────────────────────
echo "[3/7] Installing Python packages..."
if ! python3 -c "import pygame, brotli, PIL" 2>/dev/null; then
    pip3 install --break-system-packages pygame-ce brotli pillow 2>/dev/null || \
    pip3 install pygame-ce brotli pillow
fi
echo "    ✓ pygame + brotli + pillow ready"

# ─── 4. Copy files ───────────────────────────────────────────────────────────
echo "[4/7] Installing to ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"

if [[ "${SOURCE_DIR}" != "${INSTALL_DIR}" ]]; then
    # Python package (all modules live under floor796_kiosk/)
    cp -r "${SOURCE_DIR}/floor796_kiosk" "${INSTALL_DIR}/"
    # Deploy scripts
    mkdir -p "${INSTALL_DIR}/deploy"
    cp "${SCRIPT_DIR}"/run.sh               "${INSTALL_DIR}/deploy/"
    cp "${SCRIPT_DIR}"/kiosk-launch.sh      "${INSTALL_DIR}/deploy/"
    chmod +x "${INSTALL_DIR}"/deploy/run.sh "${INSTALL_DIR}"/deploy/kiosk-launch.sh
else
    echo "    (source is install dir — skipping copy)"
fi

# Service file always goes to systemd location
cp "${SCRIPT_DIR}"/floor796-kiosk.service "${SERVICE_DST}"

# Create empty assets and cache directories
mkdir -p "${INSTALL_DIR}/assets/tiles" "${INSTALL_DIR}/assets/holograms"
mkdir -p "${INSTALL_DIR}/cache/strips" "${INSTALL_DIR}/cache/thumbnails"

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
echo "    ✓ Files copied"

# ─── 5. Board-specific configuration ─────────────────────────────────────────
echo "[5/7] Configuring kiosk mode..."

if [[ "${BOARD_TYPE}" == "orangepi_5" ]]; then
    # OrangePi 5 Max: No X11, no desktop manager to disable.
    # Set default target to multi-user (no desktop).
    systemctl set-default multi-user.target

    # Disable display sleep via console blanking
    if [ -f /sys/class/graphics/fb0/blank ]; then
        echo 0 > /sys/class/graphics/fb0/blank 2>/dev/null || true
    fi

    # Set console blanking to 0 (never blank)
    if [ -f /etc/sysctl.d/ ]; then
        echo "kernel.consoleblank=0" > /etc/sysctl.d/99-kiosk-consoleblank.conf
    fi

    echo "    ✓ KMSDRM mode configured (no X11, no desktop)"
else
    # Raspberry Pi 5 / generic: disable desktop manager
    systemctl disable lightdm 2>/dev/null || true
    systemctl mask lightdm 2>/dev/null || true

    # Set default target to multi-user (no desktop)
    systemctl set-default multi-user.target

    # Disable HDMI blanking in boot config (Raspberry Pi)
    CONFIG_TXT="/boot/firmware/config.txt"
    [[ ! -f "${CONFIG_TXT}" ]] && CONFIG_TXT="/boot/config.txt"

    if [[ -f "${CONFIG_TXT}" ]]; then
        if ! grep -q "hdmi_blanking=0" "${CONFIG_TXT}" 2>/dev/null; then
            echo "hdmi_blanking=0" >> "${CONFIG_TXT}"
        fi
        if ! grep -q "hdmi_force_hotplug=1" "${CONFIG_TXT}" 2>/dev/null; then
            echo "hdmi_force_hotplug=1" >> "${CONFIG_TXT}"
        fi
    fi

    # Set console blanking to 0 (never blank)
    if [ -d /etc/sysctl.d/ ]; then
        echo "kernel.consoleblank=0" > /etc/sysctl.d/99-kiosk-consoleblank.conf
    fi

    echo "    ✓ Desktop disabled, display sleep prevented"
fi

# ─── 6. Enable kiosk service ─────────────────────────────────────────────────
echo "[6/7] Enabling kiosk service..."
systemctl daemon-reload
systemctl enable floor796-kiosk.service
echo "    ✓ Kiosk service enabled (starts on boot)"

# ─── Done ────────────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Installation Complete!"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Board:        ${BOARD_TYPE}"
echo "  GPU driver:   ${GPU_DRIVER}"
echo "  Render:       ${RENDER_BACKEND}"
echo "  Install dir:  ${INSTALL_DIR}"
echo "  Service:      floor796-kiosk.service"
echo "  User:         ${SERVICE_USER}"
echo ""
if [[ "${BOARD_TYPE}" == "orangepi_5" ]]; then
    echo "  NOTE: OrangePi uses KMSDRM (no X11). The service runs as root"
    echo "  for DRM master access. Ensure the Panthor kernel module is"
    echo "  loaded and /dev/dri/renderD* exists."
    echo ""
fi
echo "  To start now:   sudo systemctl start floor796-kiosk"
echo "  To check logs:  journalctl -u floor796-kiosk -f"
echo "  To stop:        sudo systemctl stop floor796-kiosk"
echo ""
echo "  Reboot to test cold-boot auto-start."
echo ""
