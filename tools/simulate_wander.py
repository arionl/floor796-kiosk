#!/usr/bin/env python3
"""
Simulate the floor796 kiosk wandering algorithm and produce a heat map.

Renders:
  1. The overall map grid — animated tiles in one color, blank in another.
  2. The viewpane path overlaid, showing where the viewport has visited
     after simulating 1 hour of wandering.
  3. A heat overlay showing visit frequency per tile.

Usage:
  python tools/simulate_wander.py --hours 1 --output wander_heatmap.png
"""
import argparse
import heapq
import json
import math
import os
import random
import sys
import time
from collections import deque

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ── Constants (must match player.py) ──
SRC_TILE_W = 1024
SRC_TILE_H = 820
TILE_W = SRC_TILE_W
TILE_H = SRC_TILE_H
SPACING_W = 1016
SPACING_H = 812
MASK_COLS = 32
MASK_ROWS = 26
NAV_GRID_RES = 100
NAV_TRANSIT_BLANK = 0.30
DEFAULT_WANDER_SPEED = 15.0

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
TILE_META_PATH = os.path.join(PROJECT_DIR, "tiles_meta.json")


# ── Synthetic content mask ──────────────────────────────────────────
# Floor796 tiles use an isometric diamond layout. The content fills
# roughly a diamond shape within the tile, leaving triangular corners
# as flat background.  We synthesize a realistic density mask per tile.

def make_diamond_mask(seed=42):
    """Create a MASK_ROWS x MASK_COLS density mask for an interior tile.

    Floor796 tiles are densely populated isometric scenes.  Content
    fills ~85% of the tile — only the extreme triangular corners are
    blank background.  We model this as a chamfered rectangle.
    """
    rng = np.random.RandomState(seed)
    mask = np.zeros((MASK_ROWS, MASK_COLS), dtype=np.float32)
    cx, cy = MASK_COLS / 2, MASK_ROWS / 2
    # Chamfer size — how much of each corner is blank (small = dense)
    chamfer_x = MASK_COLS * 0.15
    chamfer_y = MASK_ROWS * 0.15
    for i in range(MASK_ROWS):
        for j in range(MASK_COLS):
            # Distance from center, normalized
            dx = abs(j - cx + 0.5) / (MASK_COLS / 2)
            dy = abs(i - cy + 0.5) / (MASK_ROWS / 2)
            # Chamfer: blank only in corners where dx + dy > threshold
            corner = max(0, (dx + dy - 1.0) / chamfer_x)
            if corner < 1.0:
                base = 0.85 - corner * 0.3
                noise = rng.uniform(-0.06, 0.06)
                mask[i, j] = max(0.5, min(1.0, base + noise))
            else:
                mask[i, j] = 0.0
    return mask


def make_edge_tile_mask(side, seed=42):
    """Edge tiles have content on only one side, but it's still dense."""
    rng = np.random.RandomState(seed)
    mask = np.zeros((MASK_ROWS, MASK_COLS), dtype=np.float32)
    cx, cy = MASK_COLS / 2, MASK_ROWS / 2
    chamfer_x = MASK_COLS * 0.15
    for i in range(MASK_ROWS):
        for j in range(MASK_COLS):
            if side == "left" and j > cx:
                continue
            if side == "right" and j < cx:
                continue
            if side == "top" and i > cy:
                continue
            if side == "bottom" and i < cy:
                continue
            dx = abs(j - cx + 0.5) / (MASK_COLS / 2)
            dy = abs(i - cy + 0.5) / (MASK_ROWS / 2)
            corner = max(0, (dx + dy - 1.0) / chamfer_x)
            if corner < 1.0:
                base = 0.85 - corner * 0.3
                noise = rng.uniform(-0.06, 0.06)
                mask[i, j] = max(0.5, min(1.0, base + noise))
    return mask


def build_content_data(tiles_meta):
    """Build content_mask, content_density, and content_bounds from tiles_meta.
    
    For each animated tile, synthesize a density mask.  Edge/tip tiles
    get partial diamonds so the edge-hugging behavior is visible.
    """
    grid_rows = tiles_meta.get("grid_rows", 11)
    grid_cols = tiles_meta.get("grid_cols", 10)
    animated_tiles = set()
    content_mask = {}
    content_density = {}
    
    # Determine which tiles are on the edge of the animated cluster
    anim_set = set()
    for tid, info in tiles_meta["tiles"].items():
        if info.get("animated"):
            anim_set.add((info["row"], info["col"]))
    
    for tid, info in tiles_meta["tiles"].items():
        r, c = info["row"], info["col"]
        is_anim = info.get("animated", False)
        content_mask[(r, c)] = is_anim
        if is_anim:
            animated_tiles.add((r, c))
            # Determine edge status
            has_left = (r, c - 1) in anim_set
            has_right = (r, c + 1) in anim_set
            has_up = (r - 1, c) in anim_set
            has_down = (r + 1, c) in anim_set
            
            # Corner tiles get quarter-diamonds, edge tiles get half
            seed = hash((r, c)) % 10000
            if not has_left and not has_up and has_right and has_down:
                mask = make_edge_tile_mask("top", seed)  # content on bottom-right
            elif not has_right and not has_up and has_left and has_down:
                mask = make_edge_tile_mask("top", seed)
            elif not has_left and not has_down and has_right and has_up:
                mask = make_edge_tile_mask("bottom", seed)
            elif not has_right and not has_down and has_left and has_up:
                mask = make_edge_tile_mask("bottom", seed)
            elif not has_left and has_right:
                mask = make_edge_tile_mask("left", seed)
            elif not has_right and has_left:
                mask = make_edge_tile_mask("right", seed)
            elif not has_up and has_down:
                mask = make_edge_tile_mask("top", seed)
            elif not has_down and has_up:
                mask = make_edge_tile_mask("bottom", seed)
            else:
                mask = make_diamond_mask(seed)
            
            content_density[(r, c)] = mask
    
    # Compute content bounds per tile (tight box around content)
    content_bounds = {}
    for (r, c), dmask in content_density.items():
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
        content_bounds[(r, c)] = (cb_x0, cb_y0, cb_x1, cb_y1)
    
    return animated_tiles, content_mask, content_density, content_bounds


# ── Blank ratio computation (mirrors player.py) ──

def _blank_ratio_px(x, y, view_w, view_h, grid_rows, grid_cols,
                     content_mask, content_density=None):
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
                dmask = content_density[(r, c)]
                lx0 = ov_left - tile_left
                ly0 = ov_top - tile_top
                lx1 = ov_right - tile_left
                ly1 = ov_bottom - tile_top
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
    visited = {(sx, sy)}
    queue = deque([(sx, sy)])
    while queue:
        cx, cy = queue.popleft()
        for dx, dy in [(-1,0),(1,0),(0,-1),(0,1),(-1,-1),(-1,1),(1,-1),(1,1)]:
            nx, ny = cx + dx, cy + dy
            if 0 <= nx < gx_max and 0 <= ny < gy_max and (nx, ny) not in visited:
                if safe_grid[ny, nx]:
                    visited.add((nx, ny))
                    queue.append((nx, ny))
    return visited


def _astar_path(safe_cells, blank_grid, start_px, goal_px,
                grid_res, gx_max, gy_max):
    sx = max(0, min(gx_max - 1, int(start_px[0] / grid_res)))
    sy = max(0, min(gy_max - 1, int(start_px[1] / grid_res)))
    gx = max(0, min(gx_max - 1, int(goal_px[0] / grid_res)))
    gy = max(0, min(gy_max - 1, int(goal_px[1] / grid_res)))

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
        for dx, dy in [(-1,0),(1,0),(0,-1),(0,1),(-1,-1),(-1,1),(1,-1),(1,1)]:
            nx, ny = cx + dx, cy + dy
            if (nx, ny) in closed:
                continue
            if (nx, ny) not in safe_cells:
                continue
            step = math.sqrt(dx * dx + dy * dy)
            cell_blank = blank_grid[ny, nx] if 0 <= ny < gy_max and 0 <= nx < gx_max else 1.0
            cost = step * (1.0 + cell_blank * 3.0)
            ng = g + cost
            if (nx, ny) not in g_score or ng < g_score[(nx, ny)]:
                g_score[(nx, ny)] = ng
                came_from[(nx, ny)] = (cx, cy)
                heapq.heappush(open_heap, (ng + h(nx, ny), ng, nx, ny, None))
    return []


# ── Wanderer (simulation version with edge-hugging) ──

class SimWanderer:
    """Edge-hugging wanderer for simulation.

    Key improvements over the original:
    1. A tile is 'viewed' when its entire content (content_bounds) has
       been visible inside the viewport — not when its center is at
       viewport center.
    2. Edge hugging: when the viewport reaches the edge of an animated
       tile and the adjoining tile is blank, the wanderer moves along
       the edge toward the next waypoint instead of pushing into blank.
    3. The safe region is precomputed so transit paths never exceed
       the blank ratio threshold.
    """

    def __init__(self, map_w, map_h, view_w, view_h, speed,
                 content_mask, content_density, content_bounds,
                 animated_tiles, grid_rows, grid_cols, max_blank_ratio=0.25):
        self.speed = speed
        self.view_w = view_w
        self.view_h = view_h
        self.map_w = map_w
        self.map_h = map_h
        self.min_x = 0
        self.max_x = max(1, map_w - view_w)
        self.min_y = 0
        self.max_y = max(1, map_h - view_h)
        self.content_mask = content_mask
        self.content_density = content_density
        self.content_bounds = content_bounds
        self.animated_tiles = animated_tiles
        self._grid_rows = grid_rows
        self._grid_cols = grid_cols
        self.max_blank_ratio = max_blank_ratio

        # Precompute safe region
        self._precompute_safe_region()
        self._precompute_optimal_positions()
        self._classify_tiles()

        # Start at center of animated content
        if animated_tiles:
            avg_col = sum(c for _, c in animated_tiles) / len(animated_tiles)
            avg_row = sum(r for r, _ in animated_tiles) / len(animated_tiles)
        else:
            avg_col, avg_row = grid_cols / 2, grid_rows / 2
        self.x = avg_col * SPACING_W + SPACING_W / 2 - view_w / 2
        self.y = avg_row * SPACING_H + SPACING_H / 2 - view_h / 2
        self.x += random.uniform(-200, 200)
        self.y += random.uniform(-200, 200)
        self.x = max(self.min_x, min(self.max_x, self.x))
        self.y = max(self.min_y, min(self.max_y, self.y))

        # Coverage tracking
        self.visit_counts = {rc: 0 for rc in animated_tiles}
        self.fully_viewed = set()
        self.viewed_steps = {rc: 0 for rc in animated_tiles}

        # Navigation state
        self.current_waypoint = None
        self.path_points = []
        self.path_idx = 0
        self.target_rc = None
        self.recent_targets = deque(maxlen=6)
        self.waypoint_start_time = 0.0
        self.waypoint_timeout = 120.0
        self.waypoints_picked = 0
        self._attempted = set()

        # Path recording for heat map
        self.visited_positions = []  # list of (x, y, t)

        self.angle = random.uniform(0, 2 * math.pi)
        self.vx = math.cos(self.angle) * self.speed
        self.vy = math.sin(self.angle) * self.speed

        self._pick_new_waypoint(0.0)

    # ── Precomputation ──

    def _precompute_safe_region(self):
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
        minimizing blank ratio.

        Edge-hugging improvement: for edge tiles (where the adjoining
        tile is blank), the optimal position pushes the viewport toward
        the interior of the animated cluster, so the blank side of the
        viewport overlaps with other animated tiles rather than blank
        space.
        """
        self._optimal_positions = {}
        anim_set = self.animated_tiles
        for (r, c) in self.animated_tiles:
            tile_left = c * SPACING_W
            tile_top = r * SPACING_H
            if self.content_bounds and (r, c) in self.content_bounds:
                cb = self.content_bounds[(r, c)]
                cb_abs_x0 = tile_left + cb[0]
                cb_abs_y0 = tile_top + cb[1]
                cb_abs_x1 = tile_left + cb[2]
                cb_abs_y1 = tile_top + cb[3]
            else:
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

            # Determine which neighbors are animated (interior direction)
            has_left = (r, c - 1) in anim_set
            has_right = (r, c + 1) in anim_set
            has_up = (r - 1, c) in anim_set
            has_down = (r + 1, c) in anim_set

            # Bias the search toward the interior: offset the center
            # toward the direction where animated neighbors exist
            interior_x = 0
            interior_y = 0
            if has_left and not has_right:
                interior_x = -1  # push viewport left (toward interior)
            elif has_right and not has_left:
                interior_x = 1   # push viewport right
            if has_up and not has_down:
                interior_y = -1  # push viewport up
            elif has_down and not has_up:
                interior_y = 1   # push viewport down

            # The ideal position pushes the viewport as far toward the
            # interior as possible while keeping content in view
            if interior_x > 0:
                cx = x_max  # push right
            elif interior_x < 0:
                cx = x_min  # push left
            else:
                cx = (x_min + x_max) / 2
            if interior_y > 0:
                cy = y_max  # push down
            elif interior_y < 0:
                cy = y_min  # push up
            else:
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
                    # Distance from the interior-biased center
                    dx_norm = abs(sx - cx) / max(1, x_range / 2)
                    dy_norm = abs(sy - cy) / max(1, y_range / 2)
                    center_dist = math.hypot(dx_norm, dy_norm)
                    score = b * 0.8 + center_dist * 0.2
                    if score < best_score:
                        best_score = score
                        best_x, best_y = sx, sy
            best_blank = _blank_ratio_px(best_x, best_y, self.view_w, self.view_h,
                                        self._grid_rows, self._grid_cols,
                                        self.content_mask, self.content_density)
            self._optimal_positions[(r, c)] = (best_x, best_y, best_blank)

    def _classify_tiles(self):
        self.tip_tiles = set()
        self.normal_tiles = set()
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

        # For each tip tile, find the nearest safe cell as an anchor point
        self._tip_anchors = {}
        for (r, c) in self.animated_tiles:
            opt = self._optimal_positions[(r, c)]
            gx = max(0, min(self.gx_max - 1, int(opt[0] / NAV_GRID_RES)))
            gy = max(0, min(self.gy_max - 1, int(opt[1] / NAV_GRID_RES)))
            if (gx, gy) in self._reachable_cells:
                self.normal_tiles.add((r, c))
            else:
                self.tip_tiles.add((r, c))
                # Find nearest safe cell to this tip tile's optimal position
                best_d = float('inf')
                best_cell = None
                for (sx, sy) in self._reachable_cells:
                    d = (sx - gx) ** 2 + (sy - gy) ** 2
                    if d < best_d:
                        best_d = d
                        best_cell = (sx, sy)
                self._tip_anchors[(r, c)] = best_cell

    # ── Tile visibility ──

    def _tiles_fully_visible(self, x, y):
        """Return set of animated tiles whose content_bounds are fully
        inside the viewport at (x, y)."""
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

    def _blank_ratio(self, x, y):
        return _blank_ratio_px(x, y, self.view_w, self.view_h,
                              self._grid_rows, self._grid_cols,
                              self.content_mask, self.content_density)

    # ── Waypoint selection ──

    def _pick_new_waypoint(self, sim_time):
        unviewed = self.animated_tiles - self.fully_viewed
        if unviewed and unviewed <= self._attempted:
            self._attempted.clear()
        unviewed -= self._attempted

        if not unviewed:
            self._pick_sweep_waypoint(sim_time)
            return

        # Split unviewed into tip and normal, prioritize tip tiles
        # (they require excursions — do them first while we have time)
        unviewed_tips = unviewed & self.tip_tiles
        unviewed_normal = unviewed & self.normal_tiles

        # If we have tip tiles to visit, focus on just a few of them
        # rather than building a massive tour
        if unviewed_tips:
            # Visit tip tiles nearest to current position, one at a time
            nearest_tip = None
            nearest_d = float('inf')
            for rc in unviewed_tips:
                opt = self._optimal_positions[rc]
                d = math.hypot(opt[0] - self.x, opt[1] - self.y)
                if d < nearest_d:
                    nearest_d = d
                    nearest_tip = rc

            opt = self._optimal_positions[nearest_tip]
            # Route through anchor if available
            full_path = []
            if nearest_tip in self._tip_anchors:
                anchor = self._tip_anchors[nearest_tip]
                anchor_px = (anchor[0] * NAV_GRID_RES, anchor[1] * NAV_GRID_RES)
                first_path = _astar_path(
                    self._reachable_cells, self.blank_grid,
                    (self.x, self.y), anchor_px,
                    NAV_GRID_RES, self.gx_max, self.gy_max)
                if first_path and len(first_path) > 1:
                    full_path.extend(first_path[:-1])
                full_path.append(anchor_px)
            full_path.append((opt[0], opt[1]))

            self.target_rc = nearest_tip
            self.recent_targets.append(nearest_tip)
            self.waypoints_picked += 1
            self.path_points = full_path
            self.path_idx = 0
            self.current_waypoint = self.path_points[0]
            self.waypoint_start_time = sim_time
            total_dist = sum(math.hypot(full_path[i+1][0] - full_path[i][0],
                                        full_path[i+1][1] - full_path[i][1])
                            for i in range(len(full_path) - 1))
            travel_time = total_dist / max(1.0, self.speed)
            self.waypoint_timeout = max(60.0, min(3600.0, travel_time * 3.0))
            return

        # No tip tiles left — tour the normal tiles
        def tile_class(rc):
            if self.content_density and rc in self.content_density:
                d = float(np.mean(self.content_density[rc]))
            else:
                d = 1.0
            if d >= 0.60: return 0
            if d >= 0.20: return 1
            return 2

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

        # Build path: A* to first tile, then direct segments between tiles
        full_path = []
        first_opt = self._optimal_positions[ordered[0]]
        # If first tile is a tip, route through its anchor first
        if ordered[0] in self.tip_tiles and ordered[0] in self._tip_anchors:
            anchor = self._tip_anchors[ordered[0]]
            anchor_px = (anchor[0] * NAV_GRID_RES, anchor[1] * NAV_GRID_RES)
            first_path = _astar_path(
                self._reachable_cells, self.blank_grid,
                (self.x, self.y), anchor_px,
                NAV_GRID_RES, self.gx_max, self.gy_max)
            if first_path and len(first_path) > 1:
                full_path.extend(first_path[:-1])
            full_path.append(anchor_px)
            full_path.append((first_opt[0], first_opt[1]))
        else:
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
        self.waypoint_start_time = sim_time

        total_dist = 0
        prev = (self.x, self.y)
        for pt in full_path:
            total_dist += math.hypot(pt[0] - prev[0], pt[1] - prev[1])
            prev = pt
        travel_time = total_dist / max(1.0, self.speed)
        self.waypoint_timeout = max(60.0, min(7200.0, travel_time * 3.0))

    def _pick_sweep_waypoint(self, sim_time):
        # If no normal tiles, use all animated tiles; if none, just stay
        pool = self.normal_tiles if self.normal_tiles else self.animated_tiles
        if not pool:
            self.path_points = [(self.x, self.y)]
            self.path_idx = 0
            self.current_waypoint = self.path_points[0]
            self.target_rc = None
            self.waypoint_start_time = sim_time
            return
        all_tiles = sorted(pool,
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
        self.waypoint_start_time = sim_time

    # ── Main update ──

    def update(self, dt, sim_time):
        if not self.path_points:
            self._pick_new_waypoint(sim_time)

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

            # ── Edge-hugging steering ──
            # Before taking a step, check if the projected position would
            # exceed the blank ratio threshold.  If so, try to slide along
            # the edge by testing perpendicular directions.
            step_dist = self.speed * dt
            if step_dist > dist:
                step_dist = dist

            nx = self.x + self.vx * dt
            ny = self.y + self.vy * dt
            nx = max(self.min_x, min(self.max_x, nx))
            ny = max(self.min_y, min(self.max_y, ny))

            # Check blank at projected position
            # Allow higher blank when approaching a tip tile (edge excursion)
            is_tip_target = (self.target_rc in self.tip_tiles)
            move_blank_limit = 0.70 if is_tip_target else 0.40
            proj_blank = self._blank_ratio(nx, ny)

            if proj_blank > move_blank_limit:
                # Try sliding along the edge: test 8 directions,
                # pick the one that makes progress toward the target
                # while staying under the blank threshold
                best_dir = None
                best_score = float('inf')
                for da in [0, math.pi/4, -math.pi/4, math.pi/2, -math.pi/2,
                           3*math.pi/4, -3*math.pi/4, math.pi]:
                    test_angle = target_angle + da
                    tx2 = self.x + math.cos(test_angle) * step_dist
                    ty2 = self.y + math.sin(test_angle) * step_dist
                    tx2 = max(self.min_x, min(self.max_x, tx2))
                    ty2 = max(self.min_y, min(self.max_y, ty2))
                    b = self._blank_ratio(tx2, ty2)
                    if b > move_blank_limit:
                        continue
                    # Score: prefer directions that make progress toward target
                    progress = ((tx2 - self.x) * dx + (ty2 - self.y) * dy) / max(1, dist)
                    # Penalize blank
                    score = -progress + b * 2.0
                    if score < best_score:
                        best_score = score
                        best_dir = (tx2, ty2)

                if best_dir is not None:
                    self.x, self.y = best_dir
                    # Track whether we're making progress toward target
                    new_dist = math.hypot(self.current_waypoint[0] - self.x,
                                          self.current_waypoint[1] - self.y)
                    if new_dist >= dist - 1:
                        self._stuck_count = getattr(self, '_stuck_count', 0) + 1
                    else:
                        self._stuck_count = 0
                else:
                    # No good direction — allow the move but track stuckness
                    self._stuck_count = getattr(self, '_stuck_count', 0) + 1
                if self._stuck_count > 120:  # ~10 seconds at 12fps
                    # Force waypoint advance to break out
                    self._stuck_count = 0
                    if self.target_rc and self.target_rc not in self.fully_viewed:
                        self._attempted.add(self.target_rc)
                    self._pick_new_waypoint(sim_time)
                elif best_dir is None:
                    # No edge-hugging direction found, move directly
                    self.x = nx
                    self.y = ny
            else:
                self.x = nx
                self.y = ny
        else:
            self.x = tx
            self.y = ty

        self.x = max(self.min_x, min(self.max_x, self.x))
        self.y = max(self.min_y, min(self.max_y, self.y))

        self.visited_positions.append((self.x, self.y, sim_time))

        fully = self._tiles_fully_visible(self.x, self.y)
        for rc in fully:
            self.fully_viewed.add(rc)
            self.viewed_steps[rc] = self.viewed_steps.get(rc, 0) + 1

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
                self._pick_new_waypoint(sim_time)

        if not advanced and sim_time - self.waypoint_start_time > self.waypoint_timeout:
            if self.target_rc and self.target_rc not in self.fully_viewed:
                self._attempted.add(self.target_rc)
            self._pick_new_waypoint(sim_time)

    def coverage_stats(self):
        if not self.visit_counts:
            return (0, 0, 0, 0, 0.0)
        counts = list(self.visit_counts.values())
        visited = sum(1 for c in counts if c > 0)
        blank = self._blank_ratio(self.x, self.y)
        return (visited, len(counts), min(counts), max(counts), blank)


# ── Heat map rendering ──

def render_heatmap(wanderer, tiles_meta, output_path,
                   view_w, view_h, grid_rows, grid_cols,
                   sim_duration, animated_tiles):
    """Render the heat map as a PNG.

    The image shows:
    - Animated tiles in green
    - Blank tiles in dark gray
    - The viewpane path as a semi-transparent overlay (blue → red by time)
    - Fully-viewed tiles highlighted with a yellow border
    - Content bounds outlined in cyan
    """
    map_w = grid_cols * SPACING_W
    map_h = grid_rows * SPACING_H

    # Scale down for reasonable image size
    scale = 0.05  # ~50px per tile
    img_w = int(map_w * scale)
    img_h = int(map_h * scale)

    img = Image.new("RGB", (img_w, img_h), (10, 10, 10))
    draw = ImageDraw.Draw(img, "RGBA")

    # Draw tiles
    content_mask = wanderer.content_mask
    for r in range(grid_rows):
        for c in range(grid_cols):
            x0 = int(c * SPACING_W * scale)
            y0 = int(r * SPACING_H * scale)
            x1 = int((c * SPACING_W + TILE_W) * scale)
            y1 = int((r * SPACING_H + TILE_H) * scale)
            if content_mask.get((r, c), False):
                # Animated tile — green
                draw.rectangle([x0, y0, x1, y1], fill=(30, 120, 50, 255))
            else:
                # Blank tile — dark gray
                draw.rectangle([x0, y0, x1, y1], fill=(40, 40, 40, 255))

    # Draw content bounds for animated tiles
    for (r, c) in animated_tiles:
        if (r, c) in wanderer.content_bounds:
            cb = wanderer.content_bounds[(r, c)]
            tile_left = c * SPACING_W
            tile_top = r * SPACING_H
            x0 = int((tile_left + cb[0]) * scale)
            y0 = int((tile_top + cb[1]) * scale)
            x1 = int((tile_left + cb[2]) * scale)
            y1 = int((tile_top + cb[3]) * scale)
            draw.rectangle([x0, y0, x1, y1], outline=(0, 200, 200, 100), width=1)

    # Draw the viewpane path as colored rectangles
    positions = wanderer.visited_positions
    if positions:
        n = len(positions)
        for i in range(0, n, max(1, n // 2000)):  # sample ~2000 positions
            vx, vy, t = positions[i]
            # Color: blue (early) → red (late)
            frac = t / sim_duration if sim_duration > 0 else 0
            r_val = int(50 + 200 * frac)
            b_val = int(200 - 150 * frac)
            g_val = 50
            vx0 = int(vx * scale)
            vy0 = int(vy * scale)
            vx1 = int((vx + view_w) * scale)
            vy1 = int((vy + view_h) * scale)
            draw.rectangle([vx0, vy0, vx1, vy1], fill=(r_val, g_val, b_val, 60))

    # Draw fully-viewed tiles with yellow border
    for (r, c) in wanderer.fully_viewed:
        x0 = int(c * SPACING_W * scale)
        y0 = int(r * SPACING_H * scale)
        x1 = int((c * SPACING_W + TILE_W) * scale)
        y1 = int((r * SPACING_H + TILE_H) * scale)
        draw.rectangle([x0, y0, x1, y1], outline=(255, 220, 0, 200), width=2)

    # Draw tile grid lines
    for c in range(grid_cols + 1):
        x = int(c * SPACING_W * scale)
        draw.line([(x, 0), (x, img_h)], fill=(60, 60, 60, 80), width=1)
    for r in range(grid_rows + 1):
        y = int(r * SPACING_H * scale)
        draw.line([(0, y), (img_w, y)], fill=(60, 60, 60, 80), width=1)

    # Legend
    legend_y = 10
    draw.rectangle([10, legend_y, 30, legend_y + 15], fill=(30, 120, 50))
    draw.text((35, legend_y), "Animated tile", fill=(255, 255, 255))
    legend_y += 25
    draw.rectangle([10, legend_y, 30, legend_y + 15], fill=(40, 40, 40))
    draw.text((35, legend_y), "Blank tile", fill=(255, 255, 255))
    legend_y += 25
    draw.rectangle([10, legend_y, 30, legend_y + 15], fill=(100, 50, 100, 60))
    draw.text((35, legend_y), "Viewpane path", fill=(255, 255, 255))
    legend_y += 25
    draw.rectangle([10, legend_y, 30, legend_y + 15], outline=(255, 220, 0), width=2)
    draw.text((35, legend_y), "Fully viewed", fill=(255, 255, 255))
    legend_y += 25
    draw.rectangle([10, legend_y, 30, legend_y + 15], outline=(0, 200, 200), width=1)
    draw.text((35, legend_y), "Content bounds", fill=(255, 255, 255))

    img.save(output_path)
    print(f"Heat map saved to {output_path} ({img_w}x{img_h})")


# ── Main ──

def main():
    parser = argparse.ArgumentParser(
        description="Simulate floor796 kiosk wandering and produce a heat map.")
    parser.add_argument("--hours", type=float, default=1.0,
                        help="Simulation duration in hours (default: 1)")
    parser.add_argument("--width", type=int, default=1920,
                        help="Viewport width (default: 1920)")
    parser.add_argument("--height", type=int, default=1080,
                        help="Viewport height (default: 1080)")
    parser.add_argument("--speed", type=float, default=DEFAULT_WANDER_SPEED,
                        help=f"Wander speed px/s (default: {DEFAULT_WANDER_SPEED})")
    parser.add_argument("--output", default="wander_heatmap.png",
                        help="Output PNG path (default: wander_heatmap.png)")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed (default: 42)")
    args = parser.parse_args()

    random.seed(args.seed)
    np.random.seed(args.seed)

    # Load tiles metadata
    with open(TILE_META_PATH) as f:
        tiles_meta = json.load(f)
    grid_rows = tiles_meta.get("grid_rows", 11)
    grid_cols = tiles_meta.get("grid_cols", 10)

    print(f"Map: {grid_rows}x{grid_cols} tiles")
    print(f"Viewport: {args.width}x{args.height}")
    print(f"Speed: {args.speed} px/s")
    print(f"Duration: {args.hours} hours = {args.hours * 3600:.0f} seconds")

    # Build content data
    print("Building synthetic content masks...")
    animated_tiles, content_mask, content_density, content_bounds = \
        build_content_data(tiles_meta)
    print(f"  {len(animated_tiles)} animated tiles")

    # Map dimensions
    map_w = grid_cols * SPACING_W
    map_h = grid_rows * SPACING_H
    print(f"Map size: {map_w}x{map_h} px")

    # Create wanderer
    print("Initializing wanderer (precomputing safe region)...")
    t0 = time.time()
    wanderer = SimWanderer(
        map_w, map_h, args.width, args.height, args.speed,
        content_mask, content_density, content_bounds,
        animated_tiles, grid_rows, grid_cols)
    print(f"  Ready in {time.time() - t0:.1f}s")
    print(f"  Safe cells: {len(wanderer._reachable_cells)}")
    print(f"  Normal tiles: {len(wanderer.normal_tiles)}")
    print(f"  Tip tiles: {len(wanderer.tip_tiles)}")

    # Run simulation
    sim_duration = args.hours * 3600.0
    dt = 1.0 / 12.0  # 12 FPS like the real player
    steps = int(sim_duration / dt)
    print(f"\nSimulating {steps} steps ({dt:.3f}s each)...")

    t0 = time.time()
    blank_samples = []
    for step in range(steps):
        sim_time = step * dt
        wanderer.update(dt, sim_time)

        # Sample blank ratio every 100 steps
        if step % 100 == 0:
            blank = wanderer._blank_ratio(wanderer.x, wanderer.y)
            blank_samples.append(blank)

        # Progress
        if step % max(1, steps // 20) == 0:
            pct = step / steps * 100
            visited, total, _, _, blank = wanderer.coverage_stats()
            fully = len(wanderer.fully_viewed)
            print(f"  {pct:5.1f}% — step {step}/{steps} | "
                  f"fully viewed: {fully}/{total} | "
                  f"blank: {blank:.1%} | waypoints: {wanderer.waypoints_picked}")

    elapsed = time.time() - t0
    print(f"\nSimulation complete in {elapsed:.1f}s ({len(wanderer.visited_positions)} positions)")

    # Final stats
    visited, total, mn, mx, blank = wanderer.coverage_stats()
    fully = len(wanderer.fully_viewed)
    avg_blank = sum(blank_samples) / len(blank_samples) if blank_samples else 0
    max_blank = max(blank_samples) if blank_samples else 0

    print(f"\n── Results ──")
    print(f"Fully viewed:   {fully}/{total} ({fully/total*100:.0f}%)")
    print(f"Waypoints:      {wanderer.waypoints_picked}")
    print(f"Avg blank:      {avg_blank:.1%}")
    print(f"Max blank:      {max_blank:.1%}")
    print(f"Final blank:    {blank:.1%}")
    print(f"Positions:      {len(wanderer.visited_positions)}")

    # Print coverage grid
    print(f"\nFully-viewed grid (V=viewed, .=unviewed):")
    for r in range(grid_rows):
        row_str = ""
        for c in range(grid_cols):
            if (r, c) in animated_tiles:
                row_str += "V" if (r, c) in wanderer.fully_viewed else "."
            else:
                row_str += " "
        print(f"  {row_str}")

    # Render heat map
    print(f"\nRendering heat map...")
    render_heatmap(wanderer, tiles_meta, args.output,
                   args.width, args.height, grid_rows, grid_cols,
                   sim_duration, animated_tiles)


if __name__ == "__main__":
    main()
