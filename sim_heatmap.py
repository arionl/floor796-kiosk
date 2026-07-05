#!/usr/bin/env python3
"""
Wanderer heat-map visualisation.

Runs the Wanderer for a simulated duration, then renders a PNG overview:
  - Animated tiles in warm colour (orange/red)
  - Blank tiles in dark blue/gray
  - Viewport visit trail overlaid as a heat gradient (hot = many visits)
  - Fully-viewed tiles highlighted with a green border
  - Edge-hugging path: the viewport trail shows how well the wanderer
    stays within animated content vs. straying into blank areas

Does NOT require pygame or a display.
"""

import json
import math
import os
import sys
import struct
import time
import zlib
from collections import defaultdict

# Add kiosk dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Stub pygame before importing kiosk_player
import types
fake_pygame = types.ModuleType("pygame")
fake_pygame.font = types.ModuleType("pygame.font")
fake_pygame.font.Font = lambda *a, **kw: None
fake_pygame.image = types.ModuleType("pygame.image")
fake_pygame.Surface = lambda *a, **kw: None
fake_pygame.Rect = lambda *a, **kw: None
fake_pygame.display = types.ModuleType("pygame.display")
fake_pygame.display.set_mode = lambda *a, **kw: None
fake_pygame.display.set_caption = lambda *a, **kw: None
fake_pygame.display.quit = lambda: None
fake_pygame.display.init = lambda: None
fake_pygame.FULLSCREEN = 0
fake_pygame.SCALED = 0
fake_pygame.QUIT = 0
fake_pygame.KEYDOWN = 0
fake_pygame.K_ESCAPE = 0
fake_pygame.K_SPACE = 0
fake_pygame.K_v = 0
fake_pygame.K_s = 0
fake_pygame.K_t = 0
fake_pygame.K_o = 0
fake_pygame.K_l = 0
fake_pygame.K_LEFT = 0
fake_pygame.K_RIGHT = 0
fake_pygame.K_UP = 0
fake_pygame.K_DOWN = 0
fake_pygame.mouse = types.ModuleType("pygame.mouse")
fake_pygame.mouse.set_visible = lambda *a, **kw: None
fake_pygame.time = types.ModuleType("pygame.time")
fake_pygame.time.Clock = lambda: None
fake_pygame.init = lambda: None
fake_pygame.quit = lambda: None
fake_pygame.event = types.ModuleType("pygame.event")
fake_pygame.event.get = lambda: []
fake_pygame.draw = types.ModuleType("pygame.draw")
fake_pygame.draw.rect = lambda *a, **kw: None
fake_pygame.display.flip = lambda: None
sys.modules["pygame"] = fake_pygame

import numpy as np
from kiosk_player import (
    Wanderer, SPACING_W, SPACING_H, TILE_W, TILE_H,
    DEFAULT_WANDER_SPEED, _compute_content_bounds,
    _visible_and_margin_tile_ids,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _encode_rgba_png(rgba):
    """Encode an (H,W,4) uint8 array as PNG bytes."""
    h, w = rgba.shape[:2]
    raw = rgba.tobytes()
    row_size = w * 4
    raw_rows = b""
    for y in range(h):
        raw_rows += b"\x00"
        raw_rows += raw[y * row_size:(y + 1) * row_size]
    compressed = zlib.compress(raw_rows, 9)

    def _chunk(chunk_type, data):
        c = chunk_type + data
        crc = zlib.crc32(c) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + c + struct.pack(">I", crc)

    png = b"\x89PNG\r\n\x1a\n"
    png += _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    png += _chunk(b"IDAT", compressed)
    png += _chunk(b"IEND", b"")
    return png


def run_simulation(tiles_meta_path, view_w, view_h, sim_hours=1.0,
                   speed=15.0, dt=1/30, seed=42):
    """Run wanderer simulation, returning position trail + visit data."""
    import random as _r
    _r.seed(seed)

    with open(tiles_meta_path) as f:
        tiles_meta = json.load(f)

    grid_rows = tiles_meta["grid_rows"]
    grid_cols = tiles_meta["grid_cols"]
    map_w = grid_cols * SPACING_W
    map_h = grid_rows * SPACING_H

    content_bounds = _compute_content_bounds(tiles_meta)

    tile_grid = {}
    for tile_id, info in tiles_meta["tiles"].items():
        tile_grid[(info["row"], info["col"])] = tile_id

    wanderer = Wanderer(
        map_w, map_h, view_w, view_h,
        speed=speed,
        content_bounds=content_bounds,
        tiles_meta=tiles_meta,
    )

    total_steps = int(sim_hours * 3600 / dt)
    sample_interval = max(1, total_steps // 2000)

    positions = []
    visit_grid = np.zeros((grid_rows, grid_cols), dtype=np.float32)
    blank_at_pos = []  # blank ratio at each sampled position

    # Patch time for wanderer timeout logic
    sim_time = [0.0]
    original_time = time.time

    class FakeTime:
        @staticmethod
        def time():
            return sim_time[0]
        @staticmethod
        def monotonic():
            return sim_time[0]
        @staticmethod
        def sleep(s):
            pass

    import kiosk_player
    original_time_module = kiosk_player.time
    kiosk_player.time = FakeTime

    try:
        for step in range(total_steps):
            sim_time[0] = step * dt

            wanderer.update(dt)

            pos_x = max(0, min(map_w - view_w, wanderer.x))
            pos_y = max(0, min(map_h - view_h, wanderer.y))

            visible_ids, _ = _visible_and_margin_tile_ids(
                pos_x, pos_y, view_w, view_h,
                tiles_meta=tiles_meta,
                margin=2, tile_grid=tile_grid,
                grid_cols=grid_cols, grid_rows=grid_rows,
                vel_x=wanderer.heading()[0],
                vel_y=wanderer.heading()[1],
            )
            wanderer.record_visits(visible_ids)

            if step % sample_interval == 0:
                positions.append((pos_x, pos_y))
                blank = wanderer._blank_ratio(pos_x, pos_y)
                blank_at_pos.append(blank)
    finally:
        kiosk_player.time = original_time_module

    # Build per-tile visit grid
    for (r, c), count in wanderer.visit_counts.items():
        visit_grid[r, c] = count

    return {
        "positions": positions,
        "blank_at_pos": blank_at_pos,
        "visit_grid": visit_grid,
        "fully_viewed": set(wanderer.fully_viewed),
        "animated_tiles": set(wanderer.animated_tiles),
        "waypoints_picked": wanderer.waypoints_picked,
        "grid_rows": grid_rows,
        "grid_cols": grid_cols,
        "map_w": map_w,
        "map_h": map_h,
        "view_w": view_w,
        "view_h": view_h,
        "sim_time_s": total_steps * dt,
        "tiles_meta": tiles_meta,
    }


def render_heatmap_png(results, output_path, downsample=8):
    """Render the overview heatmap as a PNG.

    downsample: factor to reduce image size (8 = 1/8 resolution).
    The full map is 10160×8932, so downsample=8 → ~1270×1116.
    """
    grid_rows = results["grid_rows"]
    grid_cols = results["grid_cols"]
    tiles_meta = results["tiles_meta"]

    map_w = results["map_w"]
    map_h = results["map_h"]

    img_w = map_w // downsample
    img_h = map_h // downsample

    # Create RGBA canvas
    canvas = np.zeros((img_h, img_w, 4), dtype=np.uint8)

    # ── Draw tiles ──
    tile_mask = {}
    for tid, info in tiles_meta["tiles"].items():
        r, c = info["row"], info["col"]
        tile_mask[(r, c)] = info.get("animated", False)

    for r in range(grid_rows):
        for c in range(grid_cols):
            x0 = (c * SPACING_W) // downsample
            y0 = (r * SPACING_H) // downsample
            x1 = ((c + 1) * SPACING_W) // downsample
            y1 = ((r + 1) * SPACING_H) // downsample
            x0, x1 = max(0, min(img_w, x0)), max(0, min(img_w, x1))
            y0, y1 = max(0, min(img_h, y0)), max(0, min(img_h, y1))

            is_anim = tile_mask.get((r, c), False)
            if is_anim:
                # Animated tiles: warm base color (dark orange-brown)
                canvas[y0:y1, x0:x1] = (60, 40, 20, 255)
            else:
                # Blank tiles: dark blue-gray
                canvas[y0:y1, x0:x1] = (15, 15, 25, 255)

    # ── Draw viewport visit trail as heatmap ──
    positions = results["positions"]
    if positions:
        # Build a low-res visit accumulator
        trail_w = img_w
        trail_h = img_h
        trail = np.zeros((trail_h, trail_w), dtype=np.float32)

        vw = results["view_w"] // downsample
        vh = results["view_h"] // downsample

        for (px, py) in positions:
            ix = int(px // downsample)
            iy = int(py // downsample)
            x0 = max(0, ix)
            y0 = max(0, iy)
            x1 = min(trail_w, ix + vw)
            y1 = min(trail_h, iy + vh)
            trail[y0:y1, x0:x1] += 1.0

        max_trail = trail.max()
        if max_trail > 0:
            trail_norm = trail / max_trail
        else:
            trail_norm = trail

        # Overlay trail with heat colors (transparent → yellow → red)
        for y in range(trail_h):
            for x in range(trail_w):
                v = trail_norm[y, x]
                if v < 0.01:
                    continue
                # Inferno-like gradient
                if v < 0.15:
                    r, g, b = 40, 20, 80
                    a = int(100 * v / 0.15)
                elif v < 0.35:
                    t = (v - 0.15) / 0.20
                    r = int(40 + 160 * t)
                    g = int(20 + 30 * t)
                    b = int(80 - 20 * t)
                    a = 100 + int(80 * t)
                elif v < 0.65:
                    t = (v - 0.35) / 0.30
                    r = int(200 + 40 * t)
                    g = int(50 + 90 * t)
                    b = int(60 - 20 * t)
                    a = 180 + int(50 * t)
                else:
                    t = (v - 0.65) / 0.35
                    r = 255
                    g = int(140 + 100 * t)
                    b = int(40 + 60 * t)
                    a = 230 + int(25 * t)

                # Blend with existing pixel
                bg = canvas[y, x]
                alpha = a / 255.0
                canvas[y, x] = (
                    int(r * alpha + bg[0] * (1 - alpha)),
                    int(g * alpha + bg[1] * (1 - alpha)),
                    int(b * alpha + bg[2] * (1 - alpha)),
                    255,
                )

    # ── Draw fully-viewed tile borders (green) ──
    fully = results["fully_viewed"]
    for (r, c) in fully:
        x0 = (c * SPACING_W) // downsample
        y0 = (r * SPACING_H) // downsample
        x1 = ((c + 1) * SPACING_W) // downsample
        y1 = ((r + 1) * SPACING_H) // downsample
        # Green border, 2px
        thickness = max(1, 2)
        canvas[y0:y0+thickness, x0:x1] = (80, 220, 80, 255)
        canvas[y1-thickness:y1, x0:x1] = (80, 220, 80, 255)
        canvas[y0:y1, x0:x0+thickness] = (80, 220, 80, 255)
        canvas[y0:y1, x1-thickness:x1] = (80, 220, 80, 255)

    # ── Draw tile grid lines ──
    grid_color = (40, 40, 50, 80)
    for c in range(grid_cols + 1):
        x = (c * SPACING_W) // downsample
        if x < img_w:
            canvas[:, x] = grid_color
    for r in range(grid_rows + 1):
        y = (r * SPACING_H) // downsample
        if y < img_h:
            canvas[y, :] = grid_color

    # Encode PNG
    png_data = _encode_rgba_png(canvas)
    with open(output_path, 'wb') as f:
        f.write(png_data)
    return img_w, img_h


def print_stats(results):
    """Print summary statistics."""
    sim_h = results["sim_time_s"] / 3600
    animated = results["animated_tiles"]
    fully = results["fully_viewed"]
    visits = results["visit_grid"]
    blanks = results["blank_at_pos"]

    print(f"\n{'='*60}")
    print(f"Wanderer Heat-Map Simulation: {sim_h:.1f}h simulated")
    print(f"View: {results['view_w']}x{results['view_h']}, "
          f"Speed: {DEFAULT_WANDER_SPEED}px/s")
    print(f"Waypoints picked: {results['waypoints_picked']}")
    print(f"{'='*60}")

    # Coverage
    total_anim = len(animated)
    viewed = len(fully & animated)
    print(f"\nCoverage: {viewed}/{total_anim} tiles fully viewed "
          f"({viewed/total_anim*100:.1f}%)")

    # Visit stats
    anim_visits = [visits[r, c] for (r, c) in animated]
    if anim_visits:
        print(f"Visit counts (animated tiles):")
        print(f"  min={min(anim_visits):.0f}  max={max(anim_visits):.0f}  "
              f"avg={sum(anim_visits)/len(anim_visits):.1f}")
        print(f"  ratio max/min = {max(anim_visits)/max(1,min(anim_visits)):.1f}:1")

    # Blank ratio stats
    if blanks:
        avg_blank = sum(blanks) / len(blanks)
        max_blank = max(blanks)
        min_blank = min(blanks)
        zero_blank = sum(1 for b in blanks if b < 0.01) / len(blanks)
        print(f"\nBlank ratio at viewport:")
        print(f"  avg={avg_blank:.3f}  max={max_blank:.3f}  "
              f"min={min_blank:.3f}")
        print(f"  positions with <1% blank: {zero_blank*100:.1f}%")

    # Position trail stats
    positions = results["positions"]
    if positions:
        xs = [p[0] for p in positions]
        ys = [p[1] for p in positions]
        cx = sum(x + results["view_w"]/2 for x in xs) / len(xs)
        cy = sum(y + results["view_h"]/2 for y in ys) / len(ys)
        print(f"\nViewport center: avg=({cx:.0f}, {cy:.0f})")
        print(f"  map center=({results['map_w']/2:.0f}, "
              f"{results['map_h']/2:.0f})")
        print(f"  X range: {min(xs):.0f}-{max(xs):.0f}")
        print(f"  Y range: {min(ys):.0f}-{max(ys):.0f}")

    # Per-tile visit grid (text)
    print(f"\nVisit grid (counts per animated tile):")
    print("    " + " ".join(f"{c:5d}" for c in range(results["grid_cols"])))
    print("    " + "------" * results["grid_cols"])
    for r in range(results["grid_rows"]):
        row_str = f"r{r:2d} "
        for c in range(results["grid_cols"]):
            if (r, c) in animated:
                v = int(visits[r, c])
                row_str += f" {v:4d}"
            else:
                row_str += "    ."
        print(row_str)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Wanderer heat-map simulation with PNG output")
    parser.add_argument("--hours", type=float, default=1.0,
                        help="Simulated hours (default: 1)")
    parser.add_argument("--width", type=int, default=1920,
                        help="Viewport width (default: 1920)")
    parser.add_argument("--height", type=int, default=1080,
                        help="Viewport height (default: 1080)")
    parser.add_argument("--speed", type=float, default=DEFAULT_WANDER_SPEED,
                        help=f"Wander speed (default: {DEFAULT_WANDER_SPEED})")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed (default: 42)")
    parser.add_argument("--output", type=str,
                        default="wander_heatmap.png",
                        help="Output PNG filename")
    parser.add_argument("--downsample", type=int, default=8,
                        help="Image downsample factor (default: 8)")
    args = parser.parse_args()

    meta_path = os.path.join(BASE_DIR, "tiles_meta.json")
    if not os.path.exists(meta_path):
        print(f"Error: {meta_path} not found")
        sys.exit(1)

    print(f"Simulating {args.hours}h of wandering...")
    t0 = time.time()
    results = run_simulation(
        meta_path, args.width, args.height,
        sim_hours=args.hours, speed=args.speed, seed=args.seed,
    )
    sim_elapsed = time.time() - t0
    print(f"Simulation done in {sim_elapsed:.1f}s "
          f"({results['sim_time_s']/3600:.1f}h simulated)")

    print_stats(results)

    output_path = os.path.join(BASE_DIR, args.output)
    print(f"\nRendering heatmap to {output_path}...")
    img_w, img_h = render_heatmap_png(results, output_path, args.downsample)
    print(f"PNG: {img_w}x{img_h}px")

    print(f"\nDone. Output: {output_path}")
