#!/usr/bin/env python3
"""
Board detection for multi-platform kiosk support.

Detects the embedded board type and selects the appropriate rendering
code path:

  - Raspberry Pi 5  →  X11 + Mesa V3D GPU driver
  - OrangePi 5 Max  →  KMSDRM + Mesa Panthor GPU driver (no X11)
  - Other/generic   →  X11 fallback (works on most Linux desktops)

The detection is based on:
  1. /proc/device-tree/model  —  primary identification
  2. GPU driver (panthor vs v3d) in DRM render node uevent files
  3. /proc/device-tree/compatible  —  fallback for SoC identification

All methods are read-only and require no special permissions.

Usage (Python):
    from floor796_kiosk.board_detect import detect_board, get_render_config
    board = detect_board()
    config = get_render_config(board)
    # config.sdl_driver, config.needs_x11, config.gpu_driver, etc.

Usage (shell — for install/run scripts):
    python3 -m floor796_kiosk.board_detect --shell
    # Prints shell-evaluable variables:
    #   BOARD_TYPE=orangepi5
    #   GPU_DRIVER=panthor
    #   RENDER_BACKEND=kmsdrm
    #   NEEDS_X11=0
"""

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Optional

log = logging.getLogger("floor796")


class BoardType(Enum):
    """Supported board types."""
    RASPBERRY_PI_5 = "raspberry_pi_5"
    ORANGEPI_5 = "orangepi_5"
    GENERIC = "generic"


@dataclass
class RenderConfig:
    """Rendering configuration for a detected board."""
    board: BoardType
    sdl_driver: str          # "kmsdrm" or "x11"
    needs_x11: bool          # whether to start X server
    gpu_driver: str          # "panthor", "v3d", "llvmpipe", or "unknown"
    runs_as_root: bool       # KMSDRM needs root for DRM master
    supports_4k_native: bool # enough GPU/memory for native 4K rendering


# ─── Detection helpers ────────────────────────────────────────────────────────

def _read_device_model() -> str:
    """Read the device model string from /proc/device-tree/model."""
    try:
        with open("/proc/device-tree/model", "rb") as f:
            return f.read().rstrip(b"\x00").decode("ascii", errors="replace")
    except OSError:
        return ""


def _read_compatible() -> str:
    """Read the device-tree compatible string."""
    try:
        with open("/proc/device-tree/compatible", "rb") as f:
            # compatible is a NUL-separated list; take the first entry
            return f.read().split(b"\x00")[0].decode("ascii", errors="replace")
    except OSError:
        return ""


def _read_total_memory_mb() -> int:
    """Read total system RAM in MB from /proc/meminfo."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _find_gpu_driver() -> str:
    """Detect the GPU driver by scanning DRM render node uevent files.

    Looks for 'panthor', 'v3d', or other drivers in
    /sys/class/drm/renderD*/device/uevent.
    """
    for i in range(128, 140):
        uevent_path = f"/sys/class/drm/renderD{i}/device/uevent"
        try:
            with open(uevent_path) as f:
                content = f.read()
        except OSError:
            continue

        if "panthor" in content:
            return "panthor"
        if "v3d" in content:
            return "v3d"

    return "unknown"


# ─── Main detection ───────────────────────────────────────────────────────────

_detected: Optional[BoardType] = None


def detect_board() -> BoardType:
    """Detect the board type.

    Returns a BoardType enum.  The result is cached after the first call.

    Detection order:
      1. Device-tree model string (most reliable)
      2. GPU driver in DRM render nodes (panthor → OrangePi, v3d → Pi)
      3. Device-tree compatible string (SoC-level fallback)
    """
    global _detected
    if _detected is not None:
        return _detected

    model = _read_device_model()
    compatible = _read_compatible().lower()
    gpu = _find_gpu_driver()

    # Primary: device-tree model
    model_lower = model.lower()

    if "raspberry pi" in model_lower and ("5" in model_lower or "pi 5" in model_lower):
        _detected = BoardType.RASPBERRY_PI_5
        log.info("Board: Raspberry Pi 5 (model=%s, gpu=%s)", model, gpu)
        return _detected

    if "orangepi" in model_lower and "5" in model_lower:
        _detected = BoardType.ORANGEPI_5
        log.info("Board: OrangePi 5 (model=%s, gpu=%s)", model, gpu)
        return _detected

    # Fallback 1: GPU driver
    if gpu == "panthor":
        _detected = BoardType.ORANGEPI_5
        log.info("Board: OrangePi 5 (detected via Panthor GPU, model=%s)", model)
        return _detected

    if gpu == "v3d":
        _detected = BoardType.RASPBERRY_PI_5
        log.info("Board: Raspberry Pi 5 (detected via V3D GPU, model=%s)", model)
        return _detected

    # Fallback 2: compatible string
    if "brcm,bcm2712" in compatible:
        _detected = BoardType.RASPBERRY_PI_5
        log.info("Board: Raspberry Pi 5 (compatible=%s)", compatible)
        return _detected

    if "rockchip,rk3588" in compatible:
        _detected = BoardType.ORANGEPI_5
        log.info("Board: OrangePi 5 / RK3588 (compatible=%s)", compatible)
        return _detected

    # Unknown board — fall back to X11 which works almost everywhere
    _detected = BoardType.GENERIC
    log.info("Board: generic/unknown (model=%s, compatible=%s, gpu=%s) "
             "— falling back to X11", model, compatible, gpu)
    return _detected


def get_render_config(board: Optional[BoardType] = None) -> RenderConfig:
    """Get the rendering configuration for a board type.

    If board is None, auto-detects.
    """
    if board is None:
        board = detect_board()

    total_mem = _read_total_memory_mb()

    if board == BoardType.ORANGEPI_5:
        # OrangePi 5 Max (RK3588 + Mali-G610 via Mesa Panthor):
        # KMSDRM direct rendering — no X11 needed.
        # Panthor + Mesa provides EGL/OpenGL ES via standard DRM/GBM.
        # KMSDRM requires DRM master access; on a dedicated kiosk with no
        # desktop session manager, only root can acquire DRM master without
        # a logind seat session.
        return RenderConfig(
            board=board,
            sdl_driver="kmsdrm",
            needs_x11=False,
            gpu_driver="panthor",
            runs_as_root=True,
            supports_4k_native=total_mem >= 6144,
        )

    if board == BoardType.RASPBERRY_PI_5:
        # Raspberry Pi 5 (Mesa V3D via KMSDRM):
        # The Pi 5 has two DRM nodes: card0 (v3d, GPU only) and card1
        # (vc4-drm, display controller + HDMI output).  KMSDRM uses the
        # vc4-drm card for display and v3d for GL ES acceleration — both
        # via the same Mesa stack.
        #
        # KMSDRM was previously avoided on Pi 5 in favour of X11, but
        # testing shows it works well: 62 FPS at 1080p with full tile
        # blits + alpha overlays.  Using KMSDRM unifies the display
        # subsystem across all supported boards (Pi 5, OrangePi 5).
        #
        # KMSDRM requires DRM master access; on this kiosk appliance
        # (no desktop session manager), only root can acquire DRM master.
        return RenderConfig(
            board=board,
            sdl_driver="kmsdrm",
            needs_x11=False,
            gpu_driver="v3d",
            runs_as_root=True,
            supports_4k_native=total_mem >= 8192,
        )

    # Generic / unknown board:
    # Use X11 which works on most Linux desktops and embedded boards.
    return RenderConfig(
        board=board,
        sdl_driver="x11",
        needs_x11=True,
        gpu_driver="unknown",
        runs_as_root=False,
        supports_4k_native=total_mem >= 8192,
    )


# ─── CLI for shell scripts ────────────────────────────────────────────────────

def _print_shell_vars():
    """Print board detection results as shell-evaluable variables.

    Used by deploy/run.sh and deploy/kiosk-launch.sh:
        eval "$(python3 -m floor796_kiosk.board_detect --shell)"
    """
    board = detect_board()
    config = get_render_config(board)
    total_mem = _read_total_memory_mb()

    print(f"BOARD_TYPE={board.value}")
    print(f"GPU_DRIVER={config.gpu_driver}")
    print(f"RENDER_BACKEND={config.sdl_driver}")
    print(f"NEEDS_X11={'1' if config.needs_x11 else '0'}")
    print(f"RUNS_AS_ROOT={'1' if config.runs_as_root else '0'}")
    print(f"SUPPORTS_4K_NATIVE={'1' if config.supports_4k_native else '0'}")
    print(f"TOTAL_MEM_MB={total_mem}")


def main():
    parser = argparse.ArgumentParser(
        description="Detect embedded board type and rendering configuration."
    )
    parser.add_argument(
        "--shell",
        action="store_true",
        help="Print shell-evaluable variables for use in deploy scripts.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print detection results as JSON.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    if args.json:
        import json
        board = detect_board()
        config = get_render_config(board)
        print(json.dumps({
            "board_type": board.value,
            "sdl_driver": config.sdl_driver,
            "needs_x11": config.needs_x11,
            "gpu_driver": config.gpu_driver,
            "runs_as_root": config.runs_as_root,
            "supports_4k_native": config.supports_4k_native,
            "total_mem_mb": _read_total_memory_mb(),
        }, indent=2))
    else:
        # Default: shell variables
        _print_shell_vars()


if __name__ == "__main__":
    main()
