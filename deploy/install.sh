#!/usr/bin/env bash
#
# install.sh — Floor796 Kiosk installer for Raspberry Pi 5 (Raspbian)
#
# Assumes nothing beyond a fresh Raspberry Pi OS (Bookworm/Trixie) install
# with at least 4 GB RAM and an internet connection.  Run as root:
#
#     sudo bash install.sh
#
# What it does:
#   1. Installs system dependencies (X server, ffmpeg, Python, numpy, scrot)
#   2. Creates a dedicated 'kiosk' user if needed
#   3. Installs the Python packages (pygame, brotli)
#   4. Copies the floor796_kiosk package + deploy scripts
#   5. Downloads the initial tile set (~123 MB)
#   6. Installs systemd service for cold-boot auto-start
#   7. Disables desktop, lightdm, and all display sleep / screensaver
#
# On first boot, the player automatically:
#   - Downloads object labels (changelog.json) from floor796.com
#   - Builds the content density mask (content_mask.npz)
#   - Decodes tile animation strips
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

# ─── 1. System packages ──────────────────────────────────────────────────────
echo "[1/7] Installing system packages..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    xserver-xorg \
    xserver-xorg-video-all \
    xserver-xorg-video-fbdev \
    xinit \
    x11-xserver-utils \
    ffmpeg \
    python3 \
    python3-pip \
    python3-venv \
    python3-numpy \
    scrot \
    mesa-utils \
    libegl-mesa0 \
    libgles2 \
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
if ! python3 -c "import pygame, brotli" 2>/dev/null; then
    pip3 install --break-system-packages pygame-ce brotli 2>/dev/null || \
    pip3 install pygame-ce brotli
fi
echo "    ✓ pygame + brotli ready"

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

# ─── 5. Download tiles (first boot) ──────────────────────────────────────────
echo "[5/7] Downloading Floor796 tiles (~123 MB)..."
if [[ ! -f "${INSTALL_DIR}/assets/tiles_meta.json" ]]; then
    sudo -u "${SERVICE_USER}" python3 -m floor796_kiosk.tile_manager
    echo "    ✓ Tiles downloaded"
else
    echo "    ✓ Tiles already cached (will update on first boot)"
fi

# ─── 6. Disable desktop & display sleep ──────────────────────────────────────
echo "[6/7] Configuring kiosk mode..."

# Disable lightdm/desktop manager — we run our own bare X server
systemctl disable lightdm 2>/dev/null || true
systemctl mask lightdm 2>/dev/null || true

# Set default target to multi-user (no desktop)
systemctl set-default multi-user.target

# Disable HDMI blanking in boot config
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
echo "    ✓ Desktop disabled, display sleep prevented"

# ─── 7. Enable kiosk service ─────────────────────────────────────────────────
echo "[7/7] Enabling kiosk service..."
systemctl daemon-reload
systemctl enable floor796-kiosk.service
echo "    ✓ Kiosk service enabled (starts on boot)"

# ─── Done ────────────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Installation Complete!"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Install dir:  ${INSTALL_DIR}"
echo "  Service:      floor796-kiosk.service"
echo "  User:         ${SERVICE_USER}"
echo ""
echo "  To start now:   sudo systemctl start floor796-kiosk"
echo "  To check logs:  journalctl -u floor796-kiosk -f"
echo "  To stop:        sudo systemctl stop floor796-kiosk"
echo ""
echo "  Reboot to test cold-boot auto-start."
echo ""
