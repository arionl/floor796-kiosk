#!/usr/bin/env python3
"""
Floor796 Kiosk Player — animated pixel-art map for embedded displays.

Renders the floor796.com animated pixel-art map as a self-contained kiosk
that boots from cold-start, wanders the scene automatically, and updates
its tile assets when the floor796 author publishes new content.

Architecture:
  - Tile MP4s (1024×820, 60 frames @ 12 fps) are decoded to full-res frame
    strips (PNG) on first run; subsequent boots load the pre-decoded strips.
  - A background thread loads and converts tile surfaces so the render
    loop stays at display refresh rate.
  - Coverage-weighted wandering: a visit heat map ensures every animated
    tile is toured.  A blank-ratio guard keeps the viewport on content.

Controls (for maintenance/testing only):
  Mouse drag / Arrow keys — Pan manually
  Space                   — Toggle auto-wandering
  V                       — Print coverage heatmap to journal
  ESC                     — Quit

Designed for Raspberry Pi 5 (4 GB+) with a 1920×1200 or 1920×1080 display.
The player auto-detects the native display resolution at startup.
"""

import argparse
import heapq
import json
import logging
import math
import os
import random
import subprocess
import sys
import threading
import time
from collections import deque

import numpy as np
import pygame

# Stats telemetry (optional — degrades gracefully if unavailable)
try:
    from floor796_kiosk.stats import StatsCollector, start_stats_server, StatsOverlay
    STATS_AVAILABLE = True
except ImportError:
    STATS_AVAILABLE = False

try:
    from floor796_kiosk.highlighter import (load_objects, ObjectHighlighter,
                                            LABEL_INLINE, LABEL_CORNER)
    HIGHLIGHTER_AVAILABLE = True
except ImportError:
    HIGHLIGHTER_AVAILABLE = False

# ─── Configuration ────────────────────────────────────────────────────────────

from floor796_kiosk.paths import (
    INSTALL_DIR, ASSETS_DIR, TILE_DIR, TILE_META_PATH,
    CONTENT_MASK_PATH, STRIP_DIR, HOLOGRAM_DIR, CHANGELOG_PATH,
    THUMBNAIL_DIR, ensure_dirs,
)

from floor796_kiosk.cpu_affinity import (
    pin_main_thread, pin_background_thread, get_affinity_info,
)

# Resolution of the per-tile content-density mask (must match content_mask.py)
MASK_COLS = 32
MASK_ROWS = 26

# 0 means auto-detect from the connected display.
DEFAULT_WIDTH = 0
DEFAULT_HEIGHT = 0

# Source tile dimensions (from floor796.com)
SRC_TILE_W = 1024
SRC_TILE_H = 820
TILE_FRAMES = 60
TILE_FPS = 12

# Full resolution — pixel art must not be scaled.  Non-integer ratios
# (0.75, 0.5) cause duplicated/stretched pixels and seam artifacts.
SCALE = 1.0
TILE_W = int(SRC_TILE_W * SCALE)   # 1024
TILE_H = int(SRC_TILE_H * SCALE)   # 820

# Tile spacing — floor796.com places tiles at 1016×812 intervals, meaning
# tiles OVERLAP by 8 px on each axis.  See getSizeForZoomFactor() in the
# floor796 front-end JS.
SPACING_W = int(1016 * SCALE)      # 1016
SPACING_H = int(812 * SCALE)       # 812

DEFAULT_WANDER_SPEED = 15.0
ANIM_FPS = 12
CACHE_MARGIN = 2               # prefetch 2 rings beyond viewport (directional)
COVERAGE_LOG_INTERVAL = 300.0     # seconds between coverage log lines

# Memory budget on 4 GB Pi (1080p):
#   Each animated strip: 96 MB (1024x49200x2 at 16-bit)
#   Hologram: ~120 MB per scene (60 frames x 805x646 x 4 bytes)
#   max_tiles=15 -> ~1.4 GB cache; 2 scenes -> ~240 MB holo; ~200 MB other
# At 4K (3840x2160) the viewport shows ~4× more tiles; scale max_tiles
# accordingly, but cap by available memory.
MAX_TILES_BASE = 15          # baseline for 1080p + 4 GB RAM
MAX_TILES_CAP = 40           # hard ceiling (strip cache ~3.8 GB)

BG_COLOR = (0, 0, 0)
STATUS_COLOR = (220, 220, 220)
ACCENT_COLOR = (0, 200, 100)

log = logging.getLogger("floor796")


def _detect_total_memory_mb():
    """Read total system RAM in MB from /proc/meminfo."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    # "MemTotal:       8174936 kB"
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0


def _compute_max_tiles(view_w, view_h, total_mem_mb):
    """Scale MAX_TILES based on viewport area and available RAM.

    At 1080p (1920×1080 = ~2M px) with 4 GB, max_tiles=15 is tuned.
    At 4K (3840×2160 = ~8.3M px) the viewport shows ~4× more tiles,
    so we need a larger cache. Scale linearly with viewport area, then
    cap by available memory (each strip ~96 MB).
    """
    # Scale factor relative to 1080p baseline
    vp_area = view_w * view_h
    base_area = 1920 * 1080
    area_scale = vp_area / base_area

    # Memory ceiling: leave ~2 GB for OS + other app overhead
    if total_mem_mb > 0:
        mem_budget_mb = max(500, total_mem_mb - 2048)
        mem_cap = int(mem_budget_mb / 96)  # 96 MB per strip
    else:
        mem_cap = MAX_TILES_CAP

    max_t = int(MAX_TILES_BASE * area_scale)
    return max(MAX_TILES_BASE, min(max_t, mem_cap, MAX_TILES_CAP))

# ─── Status Display ───────────────────────────────────────────────────────────

class StatusDisplay:
    """Renders loading / progress messages on the pygame window."""

    def __init__(self, screen):
        self.screen = screen
        self.w = screen.get_width()
        self.h = screen.get_height()
        self.font_lg = pygame.font.Font(None, 48)
        self.font_md = pygame.font.Font(None, 32)
        self.font_sm = pygame.font.Font(None, 24)

    def show(self, message, detail="", progress=None):
        self.screen.fill(BG_COLOR)
        text = self.font_lg.render(message, True, STATUS_COLOR)
        rect = text.get_rect(center=(self.w // 2, self.h // 2 - 40))
        self.screen.blit(text, rect)

        detail_rect = rect
        if detail:
            d = self.font_md.render(detail, True, (140, 140, 140))
            detail_rect = d.get_rect(center=(self.w // 2, rect.bottom + 25))
            self.screen.blit(d, detail_rect)

        if progress is not None:
            bar_w = min(self.w - 200, 600)
            bar_h = 12
            bar_x = (self.w - bar_w) // 2
            bar_y = detail_rect.bottom + 30
            pygame.draw.rect(self.screen, (40, 40, 40),
                             (bar_x, bar_y, bar_w, bar_h), border_radius=6)
            fill_w = int(bar_w * max(0.0, min(1.0, progress)))
            if fill_w > 0:
                pygame.draw.rect(self.screen, ACCENT_COLOR,
                                 (bar_x, bar_y, fill_w, bar_h), border_radius=6)
            pct = self.font_sm.render(f"{int(progress * 100)}%", True, STATUS_COLOR)
            pct_rect = pct.get_rect(center=(self.w // 2, bar_y + bar_h + 18))
            self.screen.blit(pct, pct_rect)

        pygame.display.flip()


# ─── Frame Strip Preparation ──────────────────────────────────────────────────

def prepare_strips(tiles_meta, status=None, display_depth=16):
    """Decode each tile MP4 to a full-res frame strip via ffmpeg, then
    convert to display_depth-bit for minimal memory and zero runtime
    conversion cost.

    Animated tiles → 1024×(820*60) vertical strip; static → single frame.
    Strips are saved as BMP at the target depth so pygame.image.load()
    produces a surface at the correct depth with no conversion needed.
    Skips tiles that are already decoded at the correct depth.
    """
    os.makedirs(STRIP_DIR, exist_ok=True)
    tiles = list(tiles_meta["tiles"].items())
    total = len(tiles)
    decoded = 0
    skipped = 0

    for tile_id, info in tiles:
        # We use .bmp for pre-converted strips, .png for ffmpeg-decoded.
        strip_bmp = os.path.join(STRIP_DIR, f"{tile_id}.bmp")
        strip_png = os.path.join(STRIP_DIR, f"{tile_id}.png")

        # Check if 16-bit BMP already exists
        if os.path.exists(strip_bmp) and os.path.getsize(strip_bmp) > 1000:
            decoded += 1
            skipped += 1
            continue

        # Need to create the strip. Do we have a PNG from a previous run?
        need_ffmpeg = True
        if os.path.exists(strip_png) and os.path.getsize(strip_png) > 1000:
            need_ffmpeg = False

        if need_ffmpeg:
            mp4_path = os.path.join(TILE_DIR, info["mp4"])
            if not os.path.exists(mp4_path):
                decoded += 1
                continue

            animated = info.get("animated", False)
            if animated:
                vf = f"scale={TILE_W}:{TILE_H}:flags=neighbor,tile=1x{TILE_FRAMES}"
                cmd = ["ffmpeg", "-y", "-i", mp4_path, "-vf", vf,
                       "-frames", "1", "-compression_level", "3", strip_png]
            else:
                cmd = ["ffmpeg", "-y", "-i", mp4_path, "-frames", "1",
                       "-an", "-vf", f"scale={TILE_W}:{TILE_H}:flags=neighbor",
                       "-compression_level", "3", strip_png]

            result = subprocess.run(cmd, capture_output=True, timeout=60)
            if result.returncode != 0:
                cmd_fb = ["ffmpeg", "-y", "-i", mp4_path, "-frames", "1",
                          "-an", strip_png]
                subprocess.run(cmd_fb, capture_output=True, timeout=30)

        # Convert PNG strip to target-depth BMP
        if os.path.exists(strip_png):
            try:
                surf = pygame.image.load(strip_png)
                if display_depth != 32:
                    s16 = pygame.Surface(surf.get_size(), 0, display_depth)
                    s16.blit(surf, (0, 0))
                    surf = s16
                pygame.image.save(surf, strip_bmp)
                # Remove the PNG to save disk space
                os.remove(strip_png)
            except Exception:
                pass

        decoded += 1
        if status and (decoded % 5 == 0 or decoded == total):
            status.show("Decoding tile animations",
                        f"{decoded} / {total} tiles",
                        progress=decoded / total)

    if status:
        status.show("Decoding complete",
                    f"{skipped} cached, {total - skipped} decoded",
                    progress=1.0)


# ─── Lazy Tile Cache ─────────────────────────────────────────────────────────

class TileCache:
    """Async tile cache — loads and converts strips in a background thread."""

    def __init__(self, strip_dir, max_tiles=30, display_depth=16):
        self.strip_dir = strip_dir
        self.cache = {}
        self.max_tiles = max_tiles
        self.display_depth = display_depth
        self.load_count = 0
        self._queue = []
        self._results = {}
        self._lock = threading.Lock()
        self._worker = None
        self._stop_flag = False

    def _worker_loop(self):
        # Pin this thread to slow cores on big.LITTLE SoCs (OrangePi 5 Max).
        # No-op on homogeneous SoCs like the Raspberry Pi 5.
        pin_background_thread("tile_cache")

        # Lower this thread's OS priority so it never starves the render loop
        try:
            os.setpriority(os.PRIO_PROCESS, 0, 10)
        except OSError:
            pass

        while not self._stop_flag:
            tid = None
            with self._lock:
                if self._queue:
                    tid = self._queue.pop(0)
            if tid is None:
                time.sleep(0.05)
                continue

            strip_path = os.path.join(self.strip_dir, f"{tid}.bmp")
            if not os.path.exists(strip_path):
                # Fall back to PNG (pre-conversion or older format)
                strip_path = os.path.join(self.strip_dir, f"{tid}.png")
            if not os.path.exists(strip_path):
                continue
            try:
                # Load 16-bit pre-converted strip from disk. No runtime
                # conversion needed — prepare_strips() saved them as 16-bit.
                surf = pygame.image.load(strip_path)
                _, h = surf.get_size()
                num_frames = max(1, h // TILE_H)
                with self._lock:
                    self._results[tid] = (surf, num_frames)
                time.sleep(0.05)
            except Exception:
                pass

    def _convert_to_depth(self, surf):
        """Convert a surface to the display depth (16-bit) to halve memory."""
        if self.display_depth == 32:
            return surf.convert()
        s16 = pygame.Surface(surf.get_size(), 0, self.display_depth)
        s16.blit(surf, (0, 0))
        return s16

    def start(self):
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    def stop(self):
        self._stop_flag = True
        if self._worker:
            self._worker.join(timeout=2)

    def set_needed(self, visible_ids, margin_ids):
        needed = visible_ids | margin_ids
        with self._lock:
            already = set(self.cache.keys()) | set(self._queue) | set(self._results.keys())
            new_pending = needed - already
            new_visible = new_pending & visible_ids
            new_margin = new_pending & margin_ids

            # Priority queue: new visible tiles go to the FRONT of the
            # queue (they're on screen right now), margin tiles go to
            # the back (they're prefetch for the near future).
            #
            # When the wanderer changes direction, new tiles enter the
            # viewport immediately.  Without this reprioritization, they'd
            # wait behind margin tiles queued for the old direction that
            # are no longer relevant.  By inserting visible tiles at the
            # front, the worker picks them up on its very next iteration.
            if new_visible:
                self._queue = list(new_visible) + self._queue
            if new_margin:
                self._queue.extend(sorted(new_margin))

            # Cancel pending loads for tiles no longer needed
            # (don't waste load queue slots on tiles behind us).
            self._queue = [t for t in self._queue if t in needed]
            for t in [t for t in self._results if t not in needed]:
                del self._results[t]

            # Reprioritize: any queued tile that is now visible gets
            # moved to the front of the queue.  This handles direction
            # changes where a previously-queued margin tile enters the
            # viewport — it gets promoted ahead of other margin loads.
            promoted = []
            remaining = []
            for t in self._queue:
                if t in visible_ids:
                    promoted.append(t)
                else:
                    remaining.append(t)
            if promoted:
                self._queue = promoted + remaining

            # Graceful eviction: don't evict old-direction tiles immediately.
            if len(self.cache) > self.max_tiles:
                # Priority 1: evict tiles not in needed set at all
                evictable = [t for t in self.cache if t not in needed]
                while len(self.cache) > self.max_tiles and evictable:
                    del self.cache[evictable.pop(0)]
                # Priority 2: evict non-visible margin tiles
                if len(self.cache) > self.max_tiles:
                    evictable = [t for t in self.cache if t not in visible_ids]
                    evictable.sort(key=lambda t: 0 if t in margin_ids else -1)
                    while len(self.cache) > self.max_tiles and evictable:
                        del self.cache[evictable.pop()]

    def poll_results(self):
        with self._lock:
            ready = dict(self._results)
            self._results.clear()
        for tid, (surf, num_frames) in ready.items():
            # Surface is already converted to 16-bit by the background thread.
            self.cache[tid] = (surf, num_frames)
            self.load_count += 1

    def preload_all(self, tile_ids, status=None, status_label=""):
        total = len(tile_ids)
        for i, tid in enumerate(tile_ids):
            if tid not in self.cache:
                strip_path = os.path.join(self.strip_dir, f"{tid}.bmp")
                if not os.path.exists(strip_path):
                    strip_path = os.path.join(self.strip_dir, f"{tid}.png")
                if os.path.exists(strip_path):
                    try:
                        surf = pygame.image.load(strip_path)
                        if surf.get_bitsize() != self.display_depth:
                            surf = self._convert_to_depth(surf)
                        _, h = surf.get_size()
                        num_frames = max(1, h // TILE_H)
                        self.cache[tid] = (surf, num_frames)
                        self.load_count += 1
                    except Exception:
                        pass
            if status and (i % 2 == 0 or i == total - 1):
                status.show(status_label, f"{i+1} / {total} tiles",
                            progress=(i + 1) / total)

    @property
    def pending_count(self):
        with self._lock:
            return len(self._queue) + len(self._results)

    def get(self, tile_id):
        return self.cache.get(tile_id)


# ─── Coverage-Weighted Wander Navigation ─────────────────────────────────────

# Resolution of the viewport-position grid for safe-region precomputation.
# 100px steps → manageable grid size for A* pathfinding.
NAV_GRID_RES = 100
# Maximum blank ratio allowed during transit between tiles.
NAV_TRANSIT_BLANK = 0.30
# Minimum content density (actual pixel-art fraction) for a viewport position.
# Below this, the position is considered "too sparse" for transit.
NAV_MIN_CONTENT = 0.40


def _blank_ratio_px(x, y, view_w, view_h, grid_rows, grid_cols,
                     content_mask, content_density=None):
    """Pixel-accurate fraction of viewport area that is visually blank.

    If content_density is provided (dict of (r, c) -> 2-D float array),
    the blank fraction for animated tiles accounts for the actual
    pixel-art content (isometric diamonds are ~34% filled, not 100%).
    Otherwise falls back to binary animated/blank.
    """
    total_area = 0.0
    blank_area = 0.0
    vp_right = x + view_w
    vp_bottom = y + view_h
    col_start = max(0, int(x // SPACING_W))
    col_end = min(grid_cols, int(vp_right // SPACING_W) + 1)
    row_start = max(0, int(y // SPACING_H))
    row_end = min(grid_rows, int(vp_bottom // SPACING_H) + 1)
    for r in range(row_start, row_end):
        tile_top = r * SPACING_H
        tile_bottom = tile_top + TILE_H
        ov_top = max(tile_top, y)
        ov_bottom = min(tile_bottom, vp_bottom)
        if ov_bottom <= ov_top:
            continue
        h = ov_bottom - ov_top
        for c in range(col_start, col_end):
            tile_left = c * SPACING_W
            tile_right = tile_left + TILE_W
            ov_left = max(tile_left, x)
            ov_right = min(tile_right, vp_right)
            if ov_right <= ov_left:
                continue
            area = (ov_right - ov_left) * h
            total_area += area
            if content_density and (r, c) in content_density:
                # Pixel-level density: sum content fraction across the
                # overlapping sub-blocks of this tile's density mask.
                dmask = content_density[(r, c)]
                # Tile-local overlap rectangle
                lx0 = ov_left - tile_left
                ly0 = ov_top - tile_top
                lx1 = ov_right - tile_left
                ly1 = ov_bottom - tile_top
                # Convert to mask cell indices
                cx0 = max(0, int(lx0 / TILE_W * MASK_COLS))
                cx1 = min(MASK_COLS, int(lx1 / TILE_W * MASK_COLS) + 1)
                cy0 = max(0, int(ly0 / TILE_H * MASK_ROWS))
                cy1 = min(MASK_ROWS, int(ly1 / TILE_H * MASK_ROWS) + 1)
                region = dmask[cy0:cy1, cx0:cx1]
                if region.size > 0:
                    fill = float(np.mean(region))
                else:
                    fill = 0.0
                blank_area += area * (1.0 - fill)
            elif not content_mask.get((r, c), False):
                blank_area += area
    return blank_area / total_area if total_area > 0 else 1.0


def _bfs_safe_region(safe_grid, sx, sy, gx_max, gy_max):
    """BFS to find all cells connected to (sx, sy) in the safe grid."""
    visited = {(sx, sy)}
    queue = deque([(sx, sy)])
    while queue:
        cx, cy = queue.popleft()
        for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1),
                       (-1, -1), (-1, 1), (1, -1), (1, 1)]:
            nx, ny = cx + dx, cy + dy
            if 0 <= nx < gx_max and 0 <= ny < gy_max and (nx, ny) not in visited:
                if safe_grid[ny, nx]:
                    visited.add((nx, ny))
                    queue.append((nx, ny))
    return visited


def _astar_path(safe_cells, blank_grid, start_px, goal_px,
                grid_res, gx_max, gy_max):
    """A* through the safe grid from start to goal (pixel coords)."""
    sx = max(0, min(gx_max - 1, int(start_px[0] / grid_res)))
    sy = max(0, min(gy_max - 1, int(start_px[1] / grid_res)))
    gx = max(0, min(gx_max - 1, int(goal_px[0] / grid_res)))
    gy = max(0, min(gy_max - 1, int(goal_px[1] / grid_res)))

    # Snap start/goal to nearest safe cell
    if (sx, sy) not in safe_cells:
        best_d = float('inf')
        best = (sx, sy)
        for (nx, ny) in safe_cells:
            d = (nx - sx) ** 2 + (ny - sy) ** 2
            if d < best_d:
                best_d = d
                best = (nx, ny)
        sx, sy = best
    if (gx, gy) not in safe_cells:
        best_d = float('inf')
        best = (gx, gy)
        for (nx, ny) in safe_cells:
            d = (nx - gx) ** 2 + (ny - gy) ** 2
            if d < best_d:
                best_d = d
                best = (nx, ny)
        gx, gy = best

    if sx == gx and sy == gy:
        return [(goal_px[0], goal_px[1])]

    def h(x, y):
        return math.hypot(x - gx, y - gy)

    open_heap = [(h(sx, sy), 0.0, sx, sy, None)]
    came_from = {}
    g_score = {(sx, sy): 0.0}
    closed = set()
    while open_heap:
        f, g, cx, cy, parent = heapq.heappop(open_heap)
        if (cx, cy) in closed:
            continue
        closed.add((cx, cy))
        came_from[(cx, cy)] = parent
        if cx == gx and cy == gy:
            path = []
            node = (cx, cy)
            while node is not None:
                path.append(node)
                node = came_from.get(node)
            path.reverse()
            pixel_path = [(pgx * grid_res, pgy * grid_res) for pgx, pgy in path]
            # Simplify collinear points
            if len(pixel_path) > 2:
                simplified = [pixel_path[0]]
                for i in range(1, len(pixel_path) - 1):
                    prev = simplified[-1]
                    curr = pixel_path[i]
                    nxt = pixel_path[i + 1]
                    d1 = (curr[0] - prev[0], curr[1] - prev[1])
                    d2 = (nxt[0] - curr[0], nxt[1] - curr[1])
                    if d1 != d2:
                        simplified.append(curr)
                simplified.append(pixel_path[-1])
                pixel_path = simplified
            return pixel_path
        for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1),
                       (-1, -1), (-1, 1), (1, -1), (1, 1)]:
            nx, ny = cx + dx, cy + dy
            if (nx, ny) not in safe_cells or (nx, ny) in closed:
                continue
            step_cost = 1.414 if dx != 0 and dy != 0 else 1.0
            blank_penalty = blank_grid[ny, nx] * 2.0
            new_g = g + step_cost + blank_penalty
            if (nx, ny) not in g_score or new_g < g_score[(nx, ny)]:
                g_score[(nx, ny)] = new_g
                f_val = new_g + h(nx, ny)
                heapq.heappush(open_heap, (f_val, new_g, nx, ny, (cx, cy)))
    return None


class Wanderer:
    """Edge-hugging safe-region navigator with coverage-weighted touring.

    Precomputes a grid of safe viewport positions (blank ratio ≤ threshold),
    then navigates via A* pathfinding through that grid.  Plans multi-tile
    nearest-neighbor tours to maximise coverage while minimising transit.

    A tile is "fully viewed" when its entire content rectangle has been
    inside the viewport at some point.
    """

    def __init__(self, map_w, map_h, view_w, view_h,
                 speed=DEFAULT_WANDER_SPEED,
                 content_bounds=None,
                 tiles_meta=None,
                 max_blank_ratio=0.25):
        self.speed = speed
        self.view_w = view_w
        self.view_h = view_h

        # Build animated-tile set and reverse lookup from metadata.
        self.animated_tiles = set()
        self.tile_id_to_rc = {}
        self._grid_rows = tiles_meta.get("grid_rows", 11) if tiles_meta else 11
        self._grid_cols = tiles_meta.get("grid_cols", 10) if tiles_meta else 10
        self.content_mask = {}
        self.content_density = None
        if tiles_meta:
            for tid, info in tiles_meta["tiles"].items():
                rc = (info["row"], info["col"])
                self.tile_id_to_rc[tid] = rc
                is_anim = info.get("animated", False)
                self.content_mask[rc] = is_anim
                if is_anim:
                    self.animated_tiles.add(rc)

        # Load pixel-level content density mask if available
        self.content_bounds = None
        try:
            if os.path.exists(CONTENT_MASK_PATH):
                npz = np.load(CONTENT_MASK_PATH)
                map_mask = npz["map_mask"]  # (grid_rows*MASK_ROWS, grid_cols*MASK_COLS)
                gr = tiles_meta.get("grid_rows", 11) if tiles_meta else 11
                gc = tiles_meta.get("grid_cols", 10) if tiles_meta else 10
                self.content_density = {}
                for tid, info in tiles_meta["tiles"].items():
                    if not info.get("animated"):
                        continue
                    r, c = info["row"], info["col"]
                    self.content_density[(r, c)] = map_mask[
                        r*MASK_ROWS:(r+1)*MASK_ROWS,
                        c*MASK_COLS:(c+1)*MASK_COLS].copy()
                # Compute content bounds per tile (tight box around actual
                # pixel-art content, derived from the density mask).
                # A tile is 'viewed' when all its content pixels have been
                # inside the viewport — NOT when the full tile rectangle fits.
                self.content_bounds = {}
                for (r, c), dmask in self.content_density.items():
                    rows_wc = np.any(dmask > 0, axis=1)
                    cols_wc = np.any(dmask > 0, axis=0)
                    if np.any(rows_wc):
                        rmin = int(np.argmax(rows_wc))
                        rmax = int(len(rows_wc) - np.argmax(rows_wc[::-1]) - 1)
                        cmin = int(np.argmax(cols_wc))
                        cmax = int(len(cols_wc) - np.argmax(cols_wc[::-1]) - 1)
                        cb_x0 = cmin * TILE_W / MASK_COLS
                        cb_y0 = rmin * TILE_H / MASK_ROWS
                        cb_x1 = (cmax + 1) * TILE_W / MASK_COLS
                        cb_y1 = (rmax + 1) * TILE_H / MASK_ROWS
                    else:
                        cb_x0 = cb_y0 = 0
                        cb_x1 = TILE_W
                        cb_y1 = TILE_H
                    self.content_bounds[(r, c)] = (cb_x0, cb_y0, cb_x1, cb_y1)

                avg_dens = float(np.mean(
                    [np.mean(v) for v in self.content_density.values()]))
                log.info("Loaded content density mask: %d tiles, "
                         "avg density %.0f%%",
                         len(self.content_density), avg_dens * 100.0)
        except Exception as e:
            log.warning("Could not load content_mask.npz (%s) - "
                       "using binary animated/blank", e)

        self.map_w = map_w
        self.map_h = map_h
        self.min_x = 0
        self.max_x = max(1, map_w - view_w)
        self.min_y = 0
        self.max_y = max(1, map_h - view_h)

        # ── Precompute safe region and optimal positions ──
        log.info("Precomputing navigation grid...")
        t0 = time.time()
        self._precompute_safe_region()
        self._precompute_optimal_positions()
        self._classify_tiles()
        log.info("Navigation grid ready (%.1fs): %d safe cells, "
                 "%d normal tiles, %d tip tiles",
                 time.time() - t0, len(self._reachable_cells),
                 len(self.normal_tiles), len(self.tip_tiles))

        # Start at center of animated content
        if self.animated_tiles:
            avg_col = sum(c for _, c in self.animated_tiles) / len(self.animated_tiles)
            avg_row = sum(r for r, _ in self.animated_tiles) / len(self.animated_tiles)
        else:
            avg_col, avg_row = self._grid_cols / 2, self._grid_rows / 2
        self.x = avg_col * SPACING_W + SPACING_W / 2 - view_w / 2
        self.y = avg_row * SPACING_H + SPACING_H / 2 - view_h / 2
        # Random jitter so every boot doesn't start at the exact same
        # viewport position — ensures different first-objects highlighted.
        # ±200px keeps the start inside animated content while providing
        # enough offset to change which objects are near center.
        self.x += random.uniform(-200, 200)
        self.y += random.uniform(-200, 200)
        self.x = max(self.min_x, min(self.max_x, self.x))
        self.y = max(self.min_y, min(self.max_y, self.y))

        # Coverage tracking
        self.visit_counts = {rc: 0 for rc in self.animated_tiles}
        self.fully_viewed = set()
        self.viewed_steps = {rc: 0 for rc in self.animated_tiles}

        # Navigation state
        self.current_waypoint = None
        self.path_points = []
        self.path_idx = 0
        self.target_rc = None
        self.recent_targets = deque(maxlen=6)
        self.waypoint_start_time = time.time()
        self.waypoint_timeout = 120.0
        self.waypoints_picked = 0
        self._attempted = set()

        self.angle = random.uniform(0, 2 * math.pi)
        self.vx = math.cos(self.angle) * self.speed
        self.vy = math.sin(self.angle) * self.speed

        self._pick_new_waypoint()

    # ── Precomputation ─────────────────────────────────────────────────

    def _precompute_safe_region(self):
        """Build boolean grid of safe viewport positions."""
        gx_max = int(self.max_x / NAV_GRID_RES) + 1
        gy_max = int(self.max_y / NAV_GRID_RES) + 1
        self.gx_max = gx_max
        self.gy_max = gy_max
        self.safe_grid = np.zeros((gy_max, gx_max), dtype=bool)
        self.blank_grid = np.ones((gy_max, gx_max), dtype=np.float32)
        for gy in range(gy_max):
            py = gy * NAV_GRID_RES
            for gx in range(gx_max):
                px = gx * NAV_GRID_RES
                b = _blank_ratio_px(px, py, self.view_w, self.view_h,
                                    self._grid_rows, self._grid_cols,
                                    self.content_mask, self.content_density)
                self.blank_grid[gy, gx] = b
                if b <= NAV_TRANSIT_BLANK:
                    self.safe_grid[gy, gx] = True

    def _precompute_optimal_positions(self):
        """For each animated tile, find the viewport position that gets
        all of the tile's CONTENT PIXELS inside the viewport while
        minimizing blank ratio.  Uses content bounds (tight box around
        pixel art), NOT the full tile rectangle."""
        self._optimal_positions = {}
        for (r, c) in self.animated_tiles:
            tile_left = c * SPACING_W
            tile_top = r * SPACING_H

            if self.content_bounds and (r, c) in self.content_bounds:
                cb = self.content_bounds[(r, c)]
                # Content bounds in absolute map coordinates
                cb_abs_x0 = tile_left + cb[0]
                cb_abs_y0 = tile_top + cb[1]
                cb_abs_x1 = tile_left + cb[2]
                cb_abs_y1 = tile_top + cb[3]
            else:
                # Fallback: full tile rectangle
                cb_abs_x0 = tile_left
                cb_abs_y0 = tile_top
                cb_abs_x1 = tile_left + TILE_W
                cb_abs_y1 = tile_top + TILE_H

            # Viewing window: viewport must contain ALL content pixels
            x_min = max(self.min_x, cb_abs_x1 - self.view_w)
            x_max = min(self.max_x, cb_abs_x0)
            y_min = max(self.min_y, cb_abs_y1 - self.view_h)
            y_max = min(self.max_y, cb_abs_y0)
            if x_min > x_max:
                x_min = x_max = max(self.min_x, min(self.max_x, (x_min + x_max) / 2))
            if y_min > y_max:
                y_min = y_max = max(self.min_y, min(self.max_y, (y_min + y_max) / 2))

            cx = (x_min + x_max) / 2
            cy = (y_min + y_max) / 2
            best_x, best_y = cx, cy
            best_score = float('inf')
            n = 9
            x_range = x_max - x_min
            y_range = y_max - y_min
            for i in range(n + 1):
                for j in range(n + 1):
                    sx = x_min + x_range * i / n
                    sy = y_min + y_range * j / n
                    b = _blank_ratio_px(sx, sy, self.view_w, self.view_h,
                                       self._grid_rows, self._grid_cols,
                                       self.content_mask, self.content_density)
                    dx_norm = abs(sx - cx) / max(1, x_range / 2)
                    dy_norm = abs(sy - cy) / max(1, y_range / 2)
                    center_dist = math.hypot(dx_norm, dy_norm)
                    score = b * 0.7 + center_dist * 0.3
                    if score < best_score:
                        best_score = score
                        best_x, best_y = sx, sy
            best_blank = _blank_ratio_px(best_x, best_y, self.view_w, self.view_h,
                                        self._grid_rows, self._grid_cols,
                                        self.content_mask, self.content_density)
            self._optimal_positions[(r, c)] = (best_x, best_y, best_blank)

    def _classify_tiles(self):
        """Classify tiles as normal (safe-reachable) or tip (needs excursion)."""
        self.tip_tiles = set()
        self.normal_tiles = set()
        # Find a safe starting cell near center
        cx_g = max(0, min(self.gx_max - 1,
                          int((self.min_x + self.max_x) / 2) // NAV_GRID_RES))
        cy_g = max(0, min(self.gy_max - 1,
                          int((self.min_y + self.max_y) / 2) // NAV_GRID_RES))
        if not self.safe_grid[cy_g, cx_g]:
            found = False
            for r in range(self.gy_max):
                for c in range(self.gx_max):
                    if self.safe_grid[r, c]:
                        cx_g, cy_g = c, r
                        found = True
                        break
                if found:
                    break
        self._reachable_cells = _bfs_safe_region(
            self.safe_grid, cx_g, cy_g, self.gx_max, self.gy_max)
        for (r, c) in self.animated_tiles:
            opt = self._optimal_positions[(r, c)]
            gx = max(0, min(self.gx_max - 1, int(opt[0] / NAV_GRID_RES)))
            gy = max(0, min(self.gy_max - 1, int(opt[1] / NAV_GRID_RES)))
            if (gx, gy) in self._reachable_cells:
                self.normal_tiles.add((r, c))
            else:
                self.tip_tiles.add((r, c))

    # ── Blank ratio (for compatibility) ────────────────────────────────

    def _blank_ratio(self, x, y):
        return _blank_ratio_px(x, y, self.view_w, self.view_h,
                              self._grid_rows, self._grid_cols,
                              self.content_mask, self.content_density)

    def _tiles_fully_visible(self, x, y):
        """Animated tiles whose entire CONTENT is inside the viewport.

        Uses content bounds (tight box around pixel art) when available,
        not the full tile rectangle.  This means an edge tile can be
        'fully viewed' from deep inside the content-safe zone without
        pushing the viewport out into blank space.
        """
        result = set()
        vp_right = x + self.view_w
        vp_bottom = y + self.view_h
        col_start = max(0, int(x // SPACING_W))
        col_end = min(self._grid_cols, int(vp_right // SPACING_W) + 1)
        row_start = max(0, int(y // SPACING_H))
        row_end = min(self._grid_rows, int(vp_bottom // SPACING_H) + 1)
        for r in range(row_start, row_end):
            tile_top_y = r * SPACING_H
            for c in range(col_start, col_end):
                if not self.content_mask.get((r, c), False):
                    continue
                tile_left = c * SPACING_W
                if self.content_bounds and (r, c) in self.content_bounds:
                    cb = self.content_bounds[(r, c)]
                    cb_abs_x0 = tile_left + cb[0]
                    cb_abs_y0 = tile_top_y + cb[1]
                    cb_abs_x1 = tile_left + cb[2]
                    cb_abs_y1 = tile_top_y + cb[3]
                    if cb_abs_x0 >= x and cb_abs_x1 <= vp_right and \
                       cb_abs_y0 >= y and cb_abs_y1 <= vp_bottom:
                        result.add((r, c))
                else:
                    tile_right = tile_left + TILE_W
                    tile_bottom = tile_top_y + TILE_H
                    if tile_left >= x and tile_right <= vp_right and \
                       tile_top_y >= y and tile_bottom <= vp_bottom:
                        result.add((r, c))
        return result

    # ── Waypoint selection ─────────────────────────────────────────────

    def _pick_new_waypoint(self):
        """Build a nearest-neighbor tour through unviewed tiles.

        Tours tiles in content-density order: CORE (dense interior) first,
        EDGE next, sparse TIP tiles last.  This ensures the viewport stays
        in low-blank territory for the majority of the tour.
        """
        if not self.animated_tiles:
            self.path_points = [(self.x, self.y)]
            self.path_idx = 0
            self.current_waypoint = self.path_points[0]
            return

        unviewed = self.animated_tiles - self.fully_viewed
        if unviewed and unviewed <= self._attempted:
            self._attempted.clear()
        unviewed -= self._attempted

        if not unviewed:
            self._pick_sweep_waypoint()
            return

        # Classify tiles by content density for tour ordering
        def tile_class(rc):
            if self.content_density and rc in self.content_density:
                d = float(np.mean(self.content_density[rc]))
            else:
                d = 1.0
            if d >= 0.60: return 0  # CORE
            if d >= 0.20: return 1  # EDGE
            return 2                   # TIP

        # Nearest-neighbor within each density class
        cur_x, cur_y = self.x, self.y
        ordered = []
        remaining = set(unviewed)
        while remaining:
            for cls in (0, 1, 2):
                pool = [rc for rc in remaining if tile_class(rc) == cls]
                if pool:
                    nearest = None
                    nearest_d = float('inf')
                    for rc in pool:
                        opt = self._optimal_positions[rc]
                        d = math.hypot(opt[0] - cur_x, opt[1] - cur_y)
                        if d < nearest_d:
                            nearest_d = d
                            nearest = rc
                    ordered.append(nearest)
                    remaining.discard(nearest)
                    opt = self._optimal_positions[nearest]
                    cur_x, cur_y = opt[0], opt[1]
                    break

        # Build path: A* to first tile, direct segments between tiles
        full_path = []
        first_opt = self._optimal_positions[ordered[0]]
        first_path = _astar_path(
            self._reachable_cells, self.blank_grid,
            (self.x, self.y), (first_opt[0], first_opt[1]),
            NAV_GRID_RES, self.gx_max, self.gy_max)
        if first_path and len(first_path) > 1:
            full_path.extend(first_path[:-1])
        for rc in ordered:
            opt = self._optimal_positions[rc]
            full_path.append((opt[0], opt[1]))

        self.target_rc = ordered[-1]
        self.recent_targets.append(ordered[0])
        self.waypoints_picked += 1
        self.path_points = full_path
        self.path_idx = 0
        self.current_waypoint = self.path_points[0]
        self.waypoint_start_time = time.time()

        total_dist = 0
        prev = (self.x, self.y)
        for pt in full_path:
            total_dist += math.hypot(pt[0] - prev[0], pt[1] - prev[1])
            prev = pt
        travel_time = total_dist / max(1.0, self.speed)
        self.waypoint_timeout = max(60.0, min(7200.0, travel_time * 3.0))

    def _pick_sweep_waypoint(self):
        """Revisit all tiles in a balanced coverage sweep."""
        all_tiles = sorted(self.normal_tiles,
                          key=lambda rc: (self.viewed_steps.get(rc, 0),
                                          random.random()))
        cur_x, cur_y = self.x, self.y
        ordered = []
        remaining = set(all_tiles)
        while remaining:
            nearest = None
            nearest_d = float('inf')
            for rc in remaining:
                opt = self._optimal_positions[rc]
                d = math.hypot(opt[0] - cur_x, opt[1] - cur_y)
                d += self.viewed_steps.get(rc, 0) * 5
                if d < nearest_d:
                    nearest_d = d
                    nearest = rc
            ordered.append(nearest)
            remaining.discard(nearest)
            opt = self._optimal_positions[nearest]
            cur_x, cur_y = opt[0], opt[1]

        full_path = [(self._optimal_positions[rc][0], self._optimal_positions[rc][1])
                     for rc in ordered]
        self.target_rc = ordered[-1]
        self.recent_targets.append(ordered[0])
        self.waypoints_picked += 1
        self.path_points = full_path
        self.path_idx = 0
        self.current_waypoint = self.path_points[0]
        self.waypoint_start_time = time.time()

        total_dist = sum(math.hypot(self.path_points[i+1][0] - self.path_points[i][0],
                                    self.path_points[i+1][1] - self.path_points[i][1])
                        for i in range(len(self.path_points) - 1))
        travel_time = total_dist / max(1.0, self.speed)
        self.waypoint_timeout = max(60.0, min(14400.0, travel_time * 3.0))

    # ── Visit recording (called by main loop) ──────────────────────────

    def record_visits(self, visible_tile_ids):
        """Track fully-viewed tiles and increment visit counts."""
        for tid in visible_tile_ids:
            rc = self.tile_id_to_rc.get(tid)
            if rc and rc in self.visit_counts:
                self.visit_counts[rc] += 1
        # Also track fully-visible tiles at current position
        fully = self._tiles_fully_visible(self.x, self.y)
        for rc in fully:
            self.fully_viewed.add(rc)
            self.viewed_steps[rc] = self.viewed_steps.get(rc, 0) + 1

    # ── Main update ────────────────────────────────────────────────────

    def update(self, dt):
        """Navigate along the current tour path."""
        if not self.path_points:
            self._pick_new_waypoint()

        # ── Phase 1: Move toward current sub-waypoint ──
        tx, ty = self.current_waypoint
        dx, dy = tx - self.x, ty - self.y
        dist = math.hypot(dx, dy)

        if dist > 1.0:
            target_angle = math.atan2(dy, dx)
            diff = target_angle - self.angle
            while diff > math.pi:
                diff -= 2 * math.pi
            while diff < -math.pi:
                diff += 2 * math.pi
            self.angle += diff * min(1.0, 2.5 * dt)
            self.vx = math.cos(self.angle) * self.speed
            self.vy = math.sin(self.angle) * self.speed
            step_dist = self.speed * dt
            if step_dist > dist:
                self.x = tx
                self.y = ty
            else:
                self.x += self.vx * dt
                self.y += self.vy * dt
        else:
            self.x = tx
            self.y = ty

        self.x = max(self.min_x, min(self.max_x, self.x))
        self.y = max(self.min_y, min(self.max_y, self.y))

        # ── Phase 2: Track fully-viewed tiles ──
        fully = self._tiles_fully_visible(self.x, self.y)
        for rc in fully:
            self.fully_viewed.add(rc)
            self.viewed_steps[rc] = self.viewed_steps.get(rc, 0) + 1

        # ── Phase 3: Check arrival (max ONE advance per step) ──
        tx, ty = self.current_waypoint
        dx, dy = tx - self.x, ty - self.y
        dist = math.hypot(dx, dy)
        arrival_thresh = 30

        advanced = False
        if dist < arrival_thresh:
            self.path_idx += 1
            advanced = True
            if self.path_idx < len(self.path_points):
                self.current_waypoint = self.path_points[self.path_idx]
            else:
                if self.target_rc and self.target_rc not in self.fully_viewed:
                    self._attempted.add(self.target_rc)
                self._pick_new_waypoint()

        # Timeout
        if not advanced and time.time() - self.waypoint_start_time > self.waypoint_timeout:
            if self.target_rc and self.target_rc not in self.fully_viewed:
                self._attempted.add(self.target_rc)
            self._pick_new_waypoint()

    def heading(self):
        """Return instantaneous heading toward current sub-waypoint."""
        if self.current_waypoint:
            wx, wy = self.current_waypoint
            dx, dy = wx - self.x, wy - self.y
            dist = math.hypot(dx, dy)
            if dist > 0.5:
                return (dx / dist * self.speed, dy / dist * self.speed)
        return (self.vx, self.vy)

    def coverage_stats(self):
        """Return (visited, total, min_visits, max_visits, current_blank)."""
        if not self.visit_counts:
            return (0, 0, 0, 0, 0.0)
        counts = list(self.visit_counts.values())
        visited = sum(1 for c in counts if c > 0)
        blank = self._blank_ratio(self.x, self.y)
        return (visited, len(counts), min(counts), max(counts), blank)

    def print_coverage(self):
        """Print coverage heatmap to journal."""
        if not self.visit_counts:
            log.info("No animated tiles tracked.")
            return
        visited, total, mn, mx, blank = self.coverage_stats()
        fully_count = len(self.fully_viewed)
        log.info("Coverage: %d/%d tiles visited | fully viewed: %d/%d | "
                 "visits: min=%d max=%d | blank: %d%% | waypoints: %d",
                 visited, total, fully_count, total,
                 mn, mx, int(blank * 100), self.waypoints_picked)
        # Print fully-viewed grid
        grid_str = ""
        for r in range(self._grid_rows):
            for c in range(self._grid_cols):
                if (r, c) in self.animated_tiles:
                    grid_str += "V" if (r, c) in self.fully_viewed else "."
                else:
                    grid_str += " "
            grid_str += "\n"
        log.info("Fully-viewed grid (V=viewed, .=unviewed):\n%s", grid_str)


# ─── Main Player ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Floor796 Kiosk Player")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    player_group = parser.add_mutually_exclusive_group()
    player_group.add_argument("--fullscreen", dest="fullscreen", action="store_true", default=True)
    player_group.add_argument("--no-fullscreen", dest="fullscreen", action="store_false")
    parser.add_argument("--fps", type=int, default=ANIM_FPS)
    parser.add_argument("--no-wander", action="store_true")
    parser.add_argument("--wander-speed", type=float, default=DEFAULT_WANDER_SPEED,
                        help="Wander pan speed in pixels/sec (default: 15)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )

    # Pin main thread to fast cores on big.LITTLE SoCs (e.g. OrangePi 5 Max).
    # No-op on homogeneous SoCs like the Raspberry Pi 5.
    pin_main_thread()

    # ── Select SDL2 video driver ──
    # On the OrangePi 5 Max (RK3588 + Panthor), the proprietary libmali
    # driver was replaced with Mesa's Panthor driver.  Panthor provides
    # a standard DRM render node (/dev/dri/renderD13x) that works with
    # Mesa's libgbm and libEGL, so SDL2's KMSDRM backend works out of
    # the box for hardware-accelerated EGL/GLES on the Mali-G610 GPU.
    #
    # On the Raspberry Pi 5, Mesa's V3D driver works fine through X11, so
    # we keep the default.  KMSDRM is also available on the Pi 5 but X11
    # is more reliable for fullscreen apps there.
    #
    # Detection: if a Panthor render node exists, prefer KMSDRM.
    # Allow override via SDL_VIDEODRIVER env var.
    if "SDL_VIDEODRIVER" not in os.environ:
        has_panthor = any(
            os.path.exists(f"/sys/class/drm/renderD{i}/device/uevent")
            and "panthor" in open(f"/sys/class/drm/renderD{i}/device/uevent").read()
            for i in range(128, 140)
        )
        if has_panthor:
            os.environ["SDL_VIDEODRIVER"] = "kmsdrm"
            log.info("Panthor GPU detected — using KMSDRM for hardware GPU acceleration")
        else:
            os.environ["SDL_VIDEODRIVER"] = "x11"

    pygame.init()

    # ── Auto-detect native display resolution ──
    if args.width <= 0 or args.height <= 0:
        info = pygame.display.Info()
        detected_w = info.current_w
        detected_h = info.current_h
        if detected_w > 0 and detected_h > 0:
            args.width = detected_w
            args.height = detected_h
            log.info("Auto-detected display: %dx%d", args.width, args.height)
        else:
            args.width = 1920
            args.height = 1080
            log.warning("Could not detect display resolution; falling back to 1920x1080")

    # ── 4K handling ──
    # On devices with ≤4 GB RAM (Pi 5), we can't render full 4K in real time
    # (memory + GPU limits), so we switch X to 1080p and let the monitor's
    # hardware scaler upscale to the panel.
    #
    # On devices with ≥6 GB RAM (e.g. OrangePi 5 with 8 GB), we render at
    # native 4K — the viewport shows ~4× more tiles but memory is sufficient.
    #
    # After xrandr, pygame must be quit+re-init so it picks up the new
    # display mode — otherwise set_mode() uses stale dimensions and the
    # fullscreen window ends up positioned in a corner.
    #
    # NOTE: xrandr is X11-only.  When using KMSDRM (OrangePi with libmali),
    # we skip the downscale — native 4K with hardware GPU acceleration is
    # fast enough (~30 FPS).
    physical_w = args.width
    physical_h = args.height
    total_mem_mb = _detect_total_memory_mb()
    log.info("System memory: %d MB", total_mem_mb if total_mem_mb else -1)

    using_kmsdrm = os.environ.get("SDL_VIDEODRIVER") == "kmsdrm"

    if args.width > 3000 and total_mem_mb < 6144 and not using_kmsdrm:
        # Not enough RAM for native 4K — downscale to 1080p (X11 only)
        render_w = 1920
        render_h = 1080
        try:
            subprocess.run(
                ["xrandr", "-s", f"{render_w}x{render_h}"],
                env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":0")},
                capture_output=True, timeout=5,
            )
            time.sleep(1.0)
            # Force pygame to re-read the display after the mode switch.
            # display.quit() invalidates the font module's rendering
            # context, so we must re-init it too — otherwise status/
            # loading text renders garbled.
            pygame.display.quit()
            pygame.display.init()
            pygame.font.init()
            info = pygame.display.Info()
            args.width = info.current_w
            args.height = info.current_h
            log.info("4K display detected — switched X to %dx%d "
                     "(monitor hardware upscales to %dx%d, pygame sees %dx%d)",
                     render_w, render_h, physical_w, physical_h,
                     args.width, args.height)
        except Exception as e:
            log.warning("Could not switch display mode: %s — "
                       "rendering at native %dx%d", e, args.width, args.height)
    elif args.width > 3000:
        log.info("4K display detected — rendering at native %dx%d "
                 "(%d MB RAM sufficient for native 4K)",
                 args.width, args.height, total_mem_mb)

    log.info("Display: %dx%d", args.width, args.height)
    # With KMSDRM + Mali GPU, both FULLSCREEN and SCALED use hardware GLES
    # rendering via the SDL renderer.  SCALED gives us vsync + page-flip.
    # With X11 (Pi 5), SCALED also uses the SDL renderer (Mesa V3D GPU).
    # At 4K with X11 + llvmpipe (old config), FULLSCREEN was faster because
    # SCALED added GL compositing overhead — but that path is no longer used.
    if args.fullscreen:
        flags = pygame.FULLSCREEN | pygame.SCALED
    else:
        flags = pygame.SCALED
    screen = pygame.display.set_mode((args.width, args.height), flags, vsync=1)
    pygame.display.set_caption("Floor796 Kiosk")
    pygame.mouse.set_visible(False)
    clock = pygame.time.Clock()

    status = StatusDisplay(screen)
    status.show("Floor796 Kiosk", "Starting up...")
    ensure_dirs()

    # ── Check for tile updates (graceful offline fallback) ──
    try:
        from floor796_kiosk import tile_manager
        if tile_manager.has_cached_tiles():
            status.show("Checking for updates...")
            result = tile_manager.check_and_update()
            if result.get("offline"):
                status.show("Offline mode", "Using cached tiles")
                time.sleep(1.5)
            elif result.get("updated"):
                new = len(result.get("new_tiles", []))
                status.show("Tiles updated", f"{new} new tiles downloaded", progress=1.0)
                time.sleep(1.5)
            else:
                status.show("Tiles current")
                time.sleep(0.5)
        else:
            status.show("First run — downloading tiles...",
                        "This will take a few minutes")
            result = tile_manager.check_and_update(
                status_callback=lambda done, total, tid, ok: status.show(
                    "Downloading tiles", f"{done} / {total}", progress=done / total)
            )
            if result.get("offline"):
                status.show("No internet connection",
                            "Connect to the network and restart.")
                time.sleep(5)
                sys.exit(1)
    except Exception:
        log.exception("Tile update check failed — continuing with cache.")

    # ── Load metadata ──
    if not os.path.exists(TILE_META_PATH):
        status.show("Error", "tiles_meta.json not found. Run tile_manager.py.")
        time.sleep(5)
        sys.exit(1)

    with open(TILE_META_PATH) as f:
        tiles_meta = json.load(f)

    grid_rows = tiles_meta["grid_rows"]
    grid_cols = tiles_meta["grid_cols"]
    map_w = grid_cols * SPACING_W
    map_h = grid_rows * SPACING_H

    # ── Check for missing MP4s ──
    missing = [tid for tid, i in tiles_meta["tiles"].items()
               if not os.path.exists(os.path.join(TILE_DIR, i["mp4"]))]
    if missing:
        status.show("Error", f"{len(missing)} tile MP4s missing.")
        time.sleep(5)
        sys.exit(1)

    # ── Decode strips ──
    status.show("Checking tile strips...")
    prepare_strips(tiles_meta, status=status, display_depth=16)

    # ── Build content density mask (if missing) ──
    if not os.path.exists(CONTENT_MASK_PATH):
        log.info("content_mask.npz not found — building...")

        def _mask_progress(done, total, msg):
            status.show(msg, f"{done} / {total} tiles",
                        progress=done / total if total else 0)

        try:
            from floor796_kiosk.content_mask import build_and_save
            build_and_save(
                tiles_meta, CONTENT_MASK_PATH, strip_dir=STRIP_DIR,
                progress_callback=_mask_progress)
            status.show("Content mask complete", "", progress=1.0)
            log.info("Content mask built and saved to %s", CONTENT_MASK_PATH)
        except Exception as e:
            log.warning("Could not build content mask (%s) — "
                        "wanderer will use binary animated/blank mask", e)

    # ── Build tile grid lookup ──
    tile_grid = {}
    for tile_id, info in tiles_meta["tiles"].items():
        tile_grid[(info["row"], info["col"])] = tile_id

    content_bounds = _compute_content_bounds(tiles_meta)

    wanderer = Wanderer(map_w, map_h, args.width, args.height,
                        speed=args.wander_speed,
                        content_bounds=content_bounds,
                        tiles_meta=tiles_meta)
    anim_count = len(wanderer.animated_tiles)

    log.info("Content bounds: %s", content_bounds)
    log.info("Animated tiles: %d", anim_count)
    log.info("Wander bounds: x=%.0f-%.0f y=%.0f-%.0f",
             wanderer.min_x, wanderer.max_x, wanderer.min_y, wanderer.max_y)

    max_tiles = _compute_max_tiles(args.width, args.height, total_mem_mb)
    log.info("Tile cache: max_tiles=%d (viewport %dx%d, %d MB RAM)",
             max_tiles, args.width, args.height, total_mem_mb)
    cache = TileCache(STRIP_DIR, max_tiles=max_tiles)
    visible_tile_ids, margin_tile_ids = _visible_and_margin_tile_ids(
        wanderer.x, wanderer.y, args.width, args.height,
        grid_cols, grid_rows, tiles_meta, CACHE_MARGIN, tile_grid=tile_grid,
    )
    cache.preload_all(visible_tile_ids, status=status,
                      status_label="Loading visible tiles")
    cache.start()

    # ── Hologram overlay ──
    hologram = None
    try:
        from floor796_kiosk.hologram import HologramOverlay
        hologram = HologramOverlay(HOLOGRAM_DIR)
        hologram.prepare()
        hologram.start_decoder()
    except Exception as e:
        log.warning("Hologram overlay failed: %s", e)
        hologram = None

    status.show("Ready!", f"{len(cache.cache)} tiles loaded", progress=1.0)
    time.sleep(0.5)

    pos_x, pos_y = wanderer.x, wanderer.y
    wandering = not args.no_wander
    frame_idx = 0
    prev_frame_idx = 0
    frame_accumulator = 0.0
    frame_interval = 1.0 / args.fps
    last_coverage_log = time.time()

    log.info("Player ready. Map: %dx%d (%dx%d tiles), %d animated.",
             map_w, map_h, grid_cols, grid_rows, anim_count)
    log.info("Animation: %d fps, %d-frame loop (%.1fs).",
             args.fps, TILE_FRAMES, TILE_FRAMES / args.fps)

    # ── Stats telemetry ──
    stats_collector = None
    stats_server = None
    stats_overlay = None
    if STATS_AVAILABLE:
        try:
            stats_collector = StatsCollector(
                sorted(wanderer.animated_tiles), map_w, map_h)
            stats_server = start_stats_server(stats_collector)
            stats_overlay = StatsOverlay(
                args.width, args.height, grid_rows, grid_cols)
            log.info("Stats server: http://127.0.0.1:8796/stats")
        except Exception as e:
            log.warning("Stats system unavailable: %s", e)
            stats_collector = None

    # ── Object highlighter ──
    object_highlighter = None
    if HIGHLIGHTER_AVAILABLE:
        changelog_path = CHANGELOG_PATH
        if not os.path.exists(changelog_path):
            status.show("Downloading labels...", "Fetching from floor796.com")
        else:
            status.show("Loading labels...")
        try:
            hl_objects = load_objects(
                tiles_meta,
                spacing_w=SPACING_W, spacing_h=SPACING_H,
                data_dir=ASSETS_DIR)
            if hl_objects:
                object_highlighter = ObjectHighlighter(
                    hl_objects, args.width, args.height,
                    spacing_w=SPACING_W, spacing_h=SPACING_H)
                log.info("Object highlighter: %d objects loaded",
                         len(hl_objects))
            else:
                log.warning("Object highlighter: no objects loaded")
        except Exception as e:
            log.warning("Object highlighter unavailable: %s", e)
            object_highlighter = None

    # Wire highlighter into stats collector for telemetry
    if stats_collector and object_highlighter:
        stats_collector.set_highlighter(object_highlighter)

    running = True
    while running:
        dt = clock.tick(30) / 1000.0
        dt = min(dt, 1 / 15)

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_SPACE:
                    wandering = not wandering
                    if wandering:
                        wanderer.x = pos_x
                        wanderer.y = pos_y
                elif event.key == pygame.K_v:
                    wanderer.print_coverage()
                elif event.key == pygame.K_s:
                    if stats_collector:
                        stats_collector.set_overlay(
                            not stats_collector.overlay_enabled)
                elif event.key == pygame.K_t:
                    if stats_collector:
                        stats_collector.cycle_overlay_window()
                elif event.key == pygame.K_o:
                    if object_highlighter:
                        object_highlighter.enabled = \
                            not object_highlighter.enabled
                        log.info("Object highlighter: %s",
                                 "ON" if object_highlighter.enabled else "OFF")
                elif event.key == pygame.K_l:
                    if object_highlighter:
                        mode = object_highlighter.cycle_label_mode()
                        log.info("Object highlighter label: %s", mode)
                elif event.key in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_UP, pygame.K_DOWN):
                    wandering = False
                    speed = 500
                    if event.key == pygame.K_LEFT:  pos_x -= speed * dt
                    if event.key == pygame.K_RIGHT: pos_x += speed * dt
                    if event.key == pygame.K_UP:    pos_y -= speed * dt
                    if event.key == pygame.K_DOWN:  pos_y += speed * dt

        if wandering:
            wanderer.update(dt)
            pos_x = wanderer.x
            pos_y = wanderer.y

        pos_x = max(0, min(map_w - args.width, pos_x))
        pos_y = max(0, min(map_h - args.height, pos_y))

        # Object highlighter state machine
        if object_highlighter:
            hl_vel_x = 0
            hl_vel_y = 0
            if wandering:
                hv = wanderer.heading()
                hl_vel_x, hl_vel_y = hv[0], hv[1]
            object_highlighter.update(dt, pos_x, pos_y,
                                       hl_vel_x, hl_vel_y)

        frame_accumulator += dt
        if frame_accumulator >= frame_interval:
            prev_frame_idx = frame_idx
            frame_idx = (frame_idx + 1) % TILE_FRAMES
            frame_accumulator = 0.0
            # Cycle hologram when animation loop restarts
            if hologram and prev_frame_idx > frame_idx:
                hologram.cycle_next()

        visible_ids, margin_ids = _visible_and_margin_tile_ids(
            pos_x, pos_y, args.width, args.height,
            tiles_meta=tiles_meta,
            margin=CACHE_MARGIN, tile_grid=tile_grid,
            grid_cols=grid_cols, grid_rows=grid_rows,
            vel_x=wanderer.heading()[0] if wandering else 0,
            vel_y=wanderer.heading()[1] if wandering else 0,
        )
        cache.set_needed(visible_ids, margin_ids)
        cache.poll_results()

        if wandering:
            wanderer.record_visits(visible_ids)

        now = time.time()
        if now - last_coverage_log > COVERAGE_LOG_INTERVAL:
            visited, total_t, mn, mx, blank = wanderer.coverage_stats()
            log.info("[Coverage] %d/%d tiles visited | visits: min=%d max=%d | "
                     "blank: %d%% | waypoints: %d",
                     visited, total_t, mn, mx, int(blank * 100),
                     wanderer.waypoints_picked)
            last_coverage_log = now

        # ── Stats collection (once per frame) ──
        if stats_collector:
            heading = wanderer.heading() if wandering else (0, 0)
            visited, total_t, mn, mx, blank = wanderer.coverage_stats()
            holo_scene = 0
            if hologram:
                holo_scene = getattr(hologram, "_scene_idx", 0)
            stats_collector.update({
                "x": pos_x, "y": pos_y,
                "vx": heading[0], "vy": heading[1],
                "fps": clock.get_fps(),
                "cache_loaded": len(cache.cache),
                "cache_max": cache.max_tiles,
                "cache_pending": cache.pending_count,
                "cache_total_loads": cache.load_count,
                "tiles_visited": visited,
                "tiles_total": total_t,
                "tiles_fully_viewed": len(wanderer.fully_viewed),
                "visit_counts": dict(wanderer.visit_counts),
                "blank_ratio": blank,
                "current_target": wanderer.target_rc,
                "waypoints_picked": wanderer.waypoints_picked,
                "frame_idx": frame_idx,
                "anim_fps": args.fps,
                "holo_scene": holo_scene,
                "render_w": args.width,
                "render_h": args.height,
                "physical_w": physical_w,
                "physical_h": physical_h,
                "scale_mode": ("4k-native" if args.width > 3000
                               else "4k-xrandr" if physical_w > 3000
                               else "native"),
                "cpu_affinity": get_affinity_info(),
            })

        # ── Render ──
        screen.fill(BG_COLOR)

        tile_col_start = max(0, int(pos_x // SPACING_W))
        tile_row_start = max(0, int(pos_y // SPACING_H))
        tile_col_end = min(grid_cols, int((pos_x + args.width) // SPACING_W) + 1)
        tile_row_end = min(grid_rows, int((pos_y + args.height) // SPACING_H) + 1)

        for tr in range(tile_row_start, tile_row_end):
            for tc in range(tile_col_start, tile_col_end):
                tile_id = tile_grid.get((tr, tc))
                if not tile_id:
                    continue
                strip_data = cache.get(tile_id)
                if not strip_data:
                    continue
                strip_surf, num_frames = strip_data
                fi = frame_idx % num_frames if num_frames > 1 else 0
                src_y = fi * TILE_H
                src_rect = pygame.Rect(0, src_y, TILE_W, TILE_H)
                dest_x = tc * SPACING_W - int(pos_x)
                dest_y = tr * SPACING_H - int(pos_y)
                screen.blit(strip_surf, (dest_x, dest_y), area=src_rect)

        # ── Hologram overlay ──
        if hologram:
            hologram.poll_scenes()
            hologram.update(frame_idx)
            hologram.render(screen, pos_x, pos_y)

        # ── Object highlighter ──
        if object_highlighter:
            object_highlighter.render(screen, pos_x, pos_y)

        # ── Stats overlay (alpha-blended, zero cost when off) ──
        if stats_collector and stats_collector.overlay_enabled:
            snap = stats_collector.snapshot()
            stats_overlay.render(screen, snap)

        pygame.display.flip()

    cache.stop()
    if hologram:
        hologram.stop_decoder()
    if stats_server:
        stats_server.shutdown()
    pygame.quit()
    log.info("Player stopped. Total tile loads: %d", cache.load_count)
    wanderer.print_coverage()


def _compute_content_bounds(tiles_meta):
    """Compute pixel bounds of the animated content area."""
    min_col, max_col = 999, 0
    min_row, max_row = 999, 0
    for info in tiles_meta["tiles"].values():
        if info.get("animated"):
            min_col = min(min_col, info["col"])
            max_col = max(max_col, info["col"])
            min_row = min(min_row, info["row"])
            max_row = max(max_row, info["row"])
    if min_col > max_col:
        return None
    return (min_col * SPACING_W, min_row * SPACING_H,
            (max_col + 1) * SPACING_W, (max_row + 1) * SPACING_H)


def _visible_and_margin_tile_ids(pos_x, pos_y, view_w, view_h,
                                  grid_cols, grid_rows, tiles_meta, margin,
                                  tile_grid=None, vel_x=0, vel_y=0):
    """Return (visible_ids, margin_ids) — two sets of tile IDs.

    When vel_x/vel_y are provided (non-zero), margin tiles are filtered
    to only those in the forward direction of travel. This avoids wasting
    loads on tiles behind the viewport that will never scroll in.
    """
    if tile_grid is None:
        tile_grid = {}
        for tile_id, info in tiles_meta["tiles"].items():
            tile_grid[(info["row"], info["col"])] = tile_id

    def _tiles_in_range(col_start, col_end, row_start, row_end):
        result = set()
        for tr in range(row_start, row_end):
            for tc in range(col_start, col_end):
                tid = tile_grid.get((tr, tc))
                if tid:
                    result.add(tid)
        return result

    vis_col_start = max(0, int(pos_x // SPACING_W))
    vis_col_end = min(grid_cols, int((pos_x + view_w) // SPACING_W) + 1)
    vis_row_start = max(0, int(pos_y // SPACING_H))
    vis_row_end = min(grid_rows, int((pos_y + view_h) // SPACING_H) + 1)
    visible_ids = _tiles_in_range(vis_col_start, vis_col_end,
                                  vis_row_start, vis_row_end)

    # Full margin ring (fallback for low velocity or startup)
    all_margin = _tiles_in_range(
        max(0, vis_col_start - margin),
        min(grid_cols, vis_col_end + margin),
        max(0, vis_row_start - margin),
        min(grid_rows, vis_row_end + margin),
    )
    all_margin -= visible_ids

    # If velocity is near-zero, keep full ring (startup, paused, turning)
    speed = math.hypot(vel_x, vel_y)
    if speed < 1.0:
        margin_ids = all_margin
    else:
        # Directional filtering: only keep margin tiles ahead of us.
        # Use the viewport center as the reference point.
        vcx = pos_x + view_w / 2
        vcy = pos_y + view_h / 2
        dx = vel_x / speed
        dy = vel_y / speed

        margin_ids = set()
        for tid in all_margin:
            info = tiles_meta["tiles"].get(tid)
            if not info:
                continue
            # Tile center in pixel space
            tcx = info["col"] * SPACING_W + SPACING_W / 2
            tcy = info["row"] * SPACING_H + SPACING_H / 2
            # Dot product: positive = tile is ahead of viewport center
            forward = (tcx - vcx) * dx + (tcy - vcy) * dy
            if forward > 0:
                margin_ids.add(tid)

    return visible_ids, margin_ids


if __name__ == "__main__":
    main()
