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

Designed for Raspberry Pi 5 (4 GB+) with a 1920×1200 display.
"""

import argparse
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

import pygame

# ─── Configuration ────────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TILE_DIR = os.path.join(BASE_DIR, "tiles")
TILE_META_PATH = os.path.join(BASE_DIR, "tiles_meta.json")
STRIP_DIR = os.path.join(BASE_DIR, "strips")

DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080

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
CACHE_MARGIN = 0
COVERAGE_LOG_INTERVAL = 300.0     # seconds between coverage log lines

BG_COLOR = (0, 0, 0)
STATUS_COLOR = (220, 220, 220)
ACCENT_COLOR = (0, 200, 100)

log = logging.getLogger("floor796")

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

def prepare_strips(tiles_meta, status=None):
    """Decode each tile MP4 to a full-res frame strip PNG via ffmpeg.

    Animated tiles → 1024×(820*60) vertical strip; static → single frame.
    Skips tiles that are already decoded.
    """
    os.makedirs(STRIP_DIR, exist_ok=True)
    tiles = list(tiles_meta["tiles"].items())
    total = len(tiles)
    decoded = 0
    skipped = 0

    for tile_id, info in tiles:
        strip_path = os.path.join(STRIP_DIR, f"{tile_id}.png")
        if os.path.exists(strip_path) and os.path.getsize(strip_path) > 1000:
            try:
                with open(strip_path, "rb") as f:
                    f.seek(16)
                    w_bytes = f.read(4)
                    if len(w_bytes) == 4:
                        import struct
                        strip_w = struct.unpack(">I", w_bytes)[0]
                        if strip_w == TILE_W:
                            decoded += 1
                            skipped += 1
                            continue
            except Exception:
                pass

        mp4_path = os.path.join(TILE_DIR, info["mp4"])
        if not os.path.exists(mp4_path):
            decoded += 1
            continue

        animated = info.get("animated", False)
        if animated:
            vf = f"scale={TILE_W}:{TILE_H}:flags=neighbor,tile=1x{TILE_FRAMES}"
            cmd = ["ffmpeg", "-y", "-i", mp4_path, "-vf", vf,
                   "-frames", "1", "-compression_level", "3", strip_path]
        else:
            cmd = ["ffmpeg", "-y", "-i", mp4_path, "-frames", "1",
                   "-an", "-vf", f"scale={TILE_W}:{TILE_H}:flags=neighbor",
                   "-compression_level", "3", strip_path]

        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if result.returncode != 0:
            cmd_fb = ["ffmpeg", "-y", "-i", mp4_path, "-frames", "1",
                      "-an", strip_path]
            subprocess.run(cmd_fb, capture_output=True, timeout=30)

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

    def __init__(self, strip_dir, max_tiles=12):
        self.strip_dir = strip_dir
        self.cache = {}
        self.max_tiles = max_tiles
        self.load_count = 0
        self._queue = []
        self._results = {}
        self._lock = threading.Lock()
        self._worker = None
        self._stop_flag = False

    def _worker_loop(self):
        while not self._stop_flag:
            tid = None
            with self._lock:
                if self._queue:
                    tid = self._queue.pop(0)
            if tid is None:
                time.sleep(0.01)
                continue

            strip_path = os.path.join(self.strip_dir, f"{tid}.png")
            if not os.path.exists(strip_path):
                continue
            try:
                surf = pygame.image.load(strip_path)
                surf = surf.convert()
                _, h = surf.get_size()
                num_frames = max(1, h // TILE_H)
                with self._lock:
                    self._results[tid] = (surf, num_frames)
            except Exception:
                pass

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
            self._queue.extend(sorted(new_pending & visible_ids))
            self._queue.extend(sorted(new_pending & margin_ids))

            for tid in [t for t in self.cache if t not in needed]:
                del self.cache[tid]
            self._queue = [t for t in self._queue if t in needed]
            for t in [t for t in self._results if t not in needed]:
                del self._results[t]

            while len(self.cache) > self.max_tiles:
                evict = [t for t in self.cache if t not in visible_ids]
                if not evict:
                    evict = list(self.cache.keys())
                del self.cache[evict[0]]

    def poll_results(self):
        with self._lock:
            ready = dict(self._results)
            self._results.clear()
        for tid, (surf, num_frames) in ready.items():
            self.cache[tid] = (surf, num_frames)
            self.load_count += 1

    def preload_all(self, tile_ids, status=None, status_label=""):
        total = len(tile_ids)
        for i, tid in enumerate(tile_ids):
            if tid not in self.cache:
                strip_path = os.path.join(self.strip_dir, f"{tid}.png")
                if os.path.exists(strip_path):
                    try:
                        surf = pygame.image.load(strip_path).convert()
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

class Wanderer:
    """Coverage-weighted waypoint navigator with blank-ratio guard.

    Picks destination tiles based on a visit heat map — least-visited
    animated tiles get priority, with a random factor for organic variation.
    A blank-ratio penalty ensures the viewport stays on animated content
    and never drifts into static / empty areas.
    """

    def __init__(self, map_w, map_h, view_w, view_h,
                 speed=DEFAULT_WANDER_SPEED,
                 content_bounds=None,
                 tiles_meta=None,
                 max_blank_ratio=0.25):
        self.speed = speed
        self.view_w = view_w
        self.view_h = view_h
        self.max_blank_ratio = max_blank_ratio

        # Build animated-tile set and reverse lookup from metadata.
        self.animated_tiles = set()
        self.tile_id_to_rc = {}
        if tiles_meta:
            for tid, info in tiles_meta["tiles"].items():
                rc = (info["row"], info["col"])
                self.tile_id_to_rc[tid] = rc
                if info.get("animated"):
                    self.animated_tiles.add(rc)

        # Hard bounds for viewport position (top-left corner).
        if content_bounds:
            cx_min, cy_min, cx_max, cy_max = content_bounds
            self.min_x = cx_min
            self.max_x = max(cx_min + 1, cx_max - view_w)
            self.min_y = cy_min
            self.max_y = max(cy_min + 1, cy_max - view_h)
        else:
            self.min_x = 0
            self.max_x = max(1, map_w - view_w)
            self.min_y = 0
            self.max_y = max(1, map_h - view_h)

        self.x = (self.min_x + self.max_x) / 2
        self.y = (self.min_y + self.max_y) / 2

        self.visit_counts = {rc: 0 for rc in self.animated_tiles}
        self.current_waypoint = None
        self.target_rc = None
        self.recent_targets = deque(maxlen=4)
        self.waypoint_start_time = time.time()
        self.waypoint_timeout = 90.0
        self.waypoints_picked = 0

        self.angle = random.uniform(0, 2 * math.pi)
        self.vx = math.cos(self.angle) * self.speed
        self.vy = math.sin(self.angle) * self.speed

        self._pick_new_waypoint()

    def _blank_ratio(self, x, y):
        """Fraction of viewport covered by non-animated tiles (0.0–1.0)."""
        col_start = max(0, int(x // SPACING_W))
        col_end = int((x + self.view_w) // SPACING_W) + 1
        row_start = max(0, int(y // SPACING_H))
        row_end = int((y + self.view_h) // SPACING_H) + 1

        animated_count = 0
        total_count = 0
        for r in range(row_start, row_end):
            for c in range(col_start, col_end):
                total_count += 1
                if (r, c) in self.animated_tiles:
                    animated_count += 1
        if total_count == 0:
            return 1.0
        return 1.0 - (animated_count / total_count)

    def _pick_new_waypoint(self):
        """Select next destination — least-visited animated tile wins."""
        if not self.animated_tiles:
            self.current_waypoint = (
                random.uniform(self.min_x, self.max_x),
                random.uniform(self.min_y, self.max_y),
            )
            return

        candidates = []
        for rc in self.animated_tiles:
            row, col = rc
            vp_x = col * SPACING_W + SPACING_W // 2 - self.view_w // 2
            vp_y = row * SPACING_H + SPACING_H // 2 - self.view_h // 2
            blank = self._blank_ratio(vp_x, vp_y)

            score = self.visit_counts.get(rc, 0)
            if rc in self.recent_targets:
                score += 50
            if blank > self.max_blank_ratio:
                score += (blank - self.max_blank_ratio) * 1000
            score += random.uniform(0, 5)
            candidates.append((score, rc))

        candidates.sort()
        target_rc = candidates[0][1]

        row, col = target_rc
        tx = (col + 0.5) * SPACING_W + random.uniform(-TILE_W * 0.3, TILE_W * 0.3)
        ty = (row + 0.5) * SPACING_H + random.uniform(-TILE_H * 0.3, TILE_H * 0.3)
        tx = max(self.min_x, min(self.max_x, tx))
        ty = max(self.min_y, min(self.max_y, ty))

        self.current_waypoint = (tx, ty)
        self.target_rc = target_rc
        self.recent_targets.append(target_rc)
        self.waypoint_start_time = time.time()
        self.waypoints_picked += 1

        dist = math.hypot(tx - self.x, ty - self.y)
        travel_time = dist / max(1.0, self.speed)
        self.waypoint_timeout = max(30.0, min(600.0, travel_time * 1.8))

    def record_visits(self, visible_tile_ids):
        """Increment visit counts for animated tiles in viewport."""
        for tid in visible_tile_ids:
            rc = self.tile_id_to_rc.get(tid)
            if rc and rc in self.visit_counts:
                self.visit_counts[rc] += 1

    def update(self, dt):
        """Steer toward current waypoint; pick new one when reached."""
        if self.current_waypoint is None:
            self._pick_new_waypoint()

        wx, wy = self.current_waypoint
        dx, dy = wx - self.x, wy - self.y
        dist = math.hypot(dx, dy)

        arrived = dist < max(TILE_W, TILE_H) * 0.5
        timed_out = time.time() - self.waypoint_start_time > self.waypoint_timeout
        if arrived or timed_out:
            self._pick_new_waypoint()
            wx, wy = self.current_waypoint
            dx, dy = wx - self.x, wy - self.y

        target_angle = math.atan2(dy, dx)
        diff = target_angle - self.angle
        while diff > math.pi:
            diff -= 2 * math.pi
        while diff < -math.pi:
            diff += 2 * math.pi
        self.angle += diff * min(1.0, 1.2 * dt)

        desired_vx = math.cos(self.angle) * self.speed
        desired_vy = math.sin(self.angle) * self.speed
        blend = min(1.0, 2.0 * dt)
        self.vx += (desired_vx - self.vx) * blend
        self.vy += (desired_vy - self.vy) * blend

        self.x += self.vx * dt
        self.y += self.vy * dt

        # Blank-ratio movement guard.
        if self._blank_ratio(self.x, self.y) > self.max_blank_ratio:
            self._pick_new_waypoint()

        # Hard bounds.
        if self.x <= self.min_x:
            self.x = self.min_x
            self.vx = abs(self.vx) * 0.5
            self._pick_new_waypoint()
        elif self.x >= self.max_x:
            self.x = self.max_x
            self.vx = -abs(self.vx) * 0.5
            self._pick_new_waypoint()
        if self.y <= self.min_y:
            self.y = self.min_y
            self.vy = abs(self.vy) * 0.5
            self._pick_new_waypoint()
        elif self.y >= self.max_y:
            self.y = self.max_y
            self.vy = -abs(self.vy) * 0.5
            self._pick_new_waypoint()

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
        log.info("Coverage: %d/%d tiles visited | visits: min=%d max=%d | "
                 "blank: %d%% | waypoints: %d",
                 visited, total, mn, mx, int(blank * 100), self.waypoints_picked)
        log.info("Recent targets: %s", list(self.recent_targets))
        log.info("Current target: %s -> (%.0f, %.0f)",
                 self.target_rc, self.current_waypoint[0], self.current_waypoint[1])


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

    os.environ.setdefault("SDL_VIDEODRIVER", "x11")
    pygame.init()
    flags = pygame.FULLSCREEN | pygame.DOUBLEBUF if args.fullscreen else pygame.DOUBLEBUF
    screen = pygame.display.set_mode((args.width, args.height), flags)
    pygame.display.set_caption("Floor796 Kiosk")
    pygame.mouse.set_visible(False)
    clock = pygame.time.Clock()

    status = StatusDisplay(screen)
    status.show("Floor796 Kiosk", "Starting up...")

    # ── Check for tile updates (graceful offline fallback) ──
    try:
        import tile_manager
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
            status.show("First run — downloading tiles...", "This will take a few minutes")
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
    prepare_strips(tiles_meta, status=status)

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

    cache = TileCache(STRIP_DIR)
    visible_tile_ids, margin_tile_ids = _visible_and_margin_tile_ids(
        wanderer.x, wanderer.y, args.width, args.height,
        grid_cols, grid_rows, tiles_meta, CACHE_MARGIN, tile_grid=tile_grid,
    )
    cache.preload_all(visible_tile_ids, status=status,
                      status_label="Loading visible tiles")
    cache.start()

    status.show("Ready!", f"{len(cache.cache)} tiles loaded", progress=1.0)
    time.sleep(0.5)

    pos_x, pos_y = wanderer.x, wanderer.y
    wandering = not args.no_wander
    frame_idx = 0
    frame_accumulator = 0.0
    frame_interval = 1.0 / args.fps
    last_coverage_log = time.time()

    log.info("Player ready. Map: %dx%d (%dx%d tiles), %d animated.",
             map_w, map_h, grid_cols, grid_rows, anim_count)
    log.info("Animation: %d fps, %d-frame loop (%.1fs).",
             args.fps, TILE_FRAMES, TILE_FRAMES / args.fps)

    running = True
    while running:
        dt = clock.tick(60) / 1000.0
        dt = min(dt, 1 / 30)

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

        frame_accumulator += dt
        if frame_accumulator >= frame_interval:
            frame_idx = (frame_idx + 1) % TILE_FRAMES
            frame_accumulator = 0.0

        visible_ids, margin_ids = _visible_and_margin_tile_ids(
            pos_x, pos_y, args.width, args.height,
            tiles_meta=tiles_meta,
            margin=CACHE_MARGIN, tile_grid=tile_grid,
            grid_cols=grid_cols, grid_rows=grid_rows,
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

        pygame.display.flip()

    cache.stop()
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
                                  tile_grid=None):
    """Return (visible_ids, margin_ids) — two sets of tile IDs."""
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
    margin_ids = _tiles_in_range(
        max(0, vis_col_start - margin),
        min(grid_cols, vis_col_end + margin),
        max(0, vis_row_start - margin),
        min(grid_rows, vis_row_end + margin),
    )
    margin_ids -= visible_ids
    return visible_ids, margin_ids


if __name__ == "__main__":
    main()
