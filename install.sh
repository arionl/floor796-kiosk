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
#   1. Installs system dependencies (X server, ffmpeg, Python, etc.)
#   2. Creates a dedicated 'kiosk' user if needed
#   Installs the Python venv + pygame
#   4. Downloads the initial tile set (~123 MB)
#   5. Installs the systemd service for cold-boot auto-start
#   6. Disables display sleep / screensaver at every level
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/floor796-kiosk"
SERVICE_USER="kiosk"
SERVICE_SRC="${SCRIPT_DIR}/floor796-kiosk.service"
SERVICE_DST="/etc/systemd/system/floor796-kiosk.service"
TTY_SERVICE="/etc/systemd/system/getty@tty7.service.d"

echo "═══════════════════════════════════════════════════════════════"
echo "  Floor796 Kiosk Installer"
echo "═══════════════════════════════════════════════════════════════"
echo "  Source:      ${SCRIPT_DIR}"
echo "  Install dir: ${INSTALL_DIR}"
echo ""

# ─── Preflight checks ────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Run as root:  sudo bash install.sh"
    exit 1
fi

if [[ ! -f "${SCRIPT_DIR}/kiosk_player.py" ]]; then
    echo "ERROR: kiosk_player.py not found in ${SCRIPT_DIR}"
    exit 1
fi

# Check we're on a Pi (armv8/aarch64 or the model file exists)
if [[ ! -f /proc/device-tree/model ]] && [[ "$(uname -m)" != "aarch64" ]] && [[ "$(uname -m)" != "armv7l" ]]; then
    echo "WARNING: This doesn't appear to be a Raspberry Pi."
    echo "         Continuing anyway in 3s..."
    sleep 3
fi

# ─── 1. Install system dependencies ──────────────────────────────────────────
echo "[1/6] Installing system packages..."
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
    mesa-utils \
    libegl-mesa0 \
    libgles2-mesa \
    >/dev/null 2>&1
echo "    ✓ System packages installed"

# ─── 2. Create kiosk user ────────────────────────────────────────────────────
echo "[2/6] Setting up kiosk user..."
if ! id -u "${SERVICE_USER}" &>/dev/null; then
    useradd -m -s /bin/bash "${SERVICE_USER}"
    # Auto-login on tty7 for X server
    usermod -aG video,render,input,tty "${SERVICE_USER}"
    echo "    ✓ Created user '${SERVICE_USER}'"
else
    usermod -aG video,render,input,tty "${SERVICE_USER}" 2>/dev/null || true
    echo "    ✓ User '${SERVICE_USER}' already exists"
fi

# ─── 3. Copy files to install dir ────────────────────────────────────────────
echo "[3/6] Installing to ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"
cp "${SCRIPT_DIR}/kiosk_player.py"      "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/tile_manager.py"      "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/run.sh"               "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/floor796-kiosk.service" "${SERVICE_DST}"

# Keep tiles_meta.json if it already exists in the source dir
if [[ -f "${SCRIPT_DIR}/tiles_meta.json" ]]; then
    cp "${SCRIPT_DIR}/tiles_meta.json" "${INSTALL_DIR}/"
fi

# Copy tiles if already downloaded in source dir
if [[ -d "${SCRIPT_DIR}/tiles" ]]; then
    cp -r "${SCRIPT_DIR}/tiles" "${INSTALL_DIR}/"
fi

chmod +x "${INSTALL_DIR}/run.sh"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
echo "    ✓ Files copied"

# ─── 4. Create Python venv + install pygame ──────────────────────────────────
echo "[4/6] Setting up Python environment..."
if [[ ! -d "${INSTALL_DIR}/venv" ]]; then
    python3 -m venv "${INSTALL_DIR}/venv"
fi
"${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install --quiet pygame-ce
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}/venv"
echo "    ✓ Python venv ready (pygame-ce)"

# ─── 5. Download tiles ───────────────────────────────────────────────────────
echo "[5/6] Downloading Floor796 tiles (~123 MB)..."
if [[ ! -f "${INSTALL_DIR}/tiles_meta.json" ]]; then
    sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/venv/bin/python" \
        "${INSTALL_DIR}/tile_manager.py"
    chown "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}/tiles_meta.json"
    echo "    ✓ Tiles downloaded"
else
    echo "    ✓ Tiles already cached (will update on first boot)"
fi

# ─── 6. Configure auto-start + disable display sleep ─────────────────────────
echo "[6/6] Configuring kiosk boot..."

# Auto-login on tty7 so xinit can grab VT7
mkdir -p "${TTY_SERVICE}"
cat > "${TTY_SERVICE}/autologin.conf" << EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin ${SERVICE_USER} --noclear %I \$TERM
EOF

# Disable sleep / screensaver / DPMS at the OS level
# --- config.txt (boot firmware) ---
CONFIG_TXT="/boot/firmware/config.txt"
[[ ! -f "${CONFIG_TXT}" ]] && CONFIG_TXT="/boot/config.txt"

if [[ -f "${CONFIG_TXT}" ]]; then
    # Ensure HDMI stays on (force hotplug, explicit group/mode)
    if ! grep -q "hdmi_force_hotplug=1" "${CONFIG_TXT}" 2>/dev/null; then
        sed -i 's/^#*hdmi_force_hotplug=.*/hdmi_force_hotplug=1/' "${CONFIG_TXT}" 2>/dev/null || true
        grep -q "hdmi_force_hotplug=1" "${CONFIG_TXT}" 2>/dev/null || \
            echo "hdmi_force_hotplug=1" >> "${CONFIG_TXT}"
    fi
    echo "    ✓ HDMI force hotplug enabled"
fi

# --- Disable graphical target (we run our own bare X server) ---
systemctl set-default multi-user.target
echo "    ✓ Default target: multi-user (no desktop)"

# --- Enable the kiosk service ---
systemctl daemon-reload
systemctl enable floor796-kiosk.service
echo "    ✓ Kiosk service enabled"

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
