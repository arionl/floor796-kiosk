#!/usr/bin/env python3
"""
Wanderer simulation — runs the Wanderer algorithm for N hours of simulated
time and produces a coverage heatmap to detect directional bias.

Does NOT require pygame or a display.  Imports the Wanderer class directly
and runs it with a fixed timestep, simulating visit recording exactly as
the render loop does.
"""

import json
import math
import sys
import os
import time
from collections import defaultdict

# Add the kiosk directory to the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# We need to stub pygame before importing kiosk_player, since Wanderer
# doesn't use pygame directly but kiosk_player imports it at module level.
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
fake_pygame.FULLSCREEN = 0
fake_pygame.SCALED = 0
fake_pygame.QUIT = 0
fake_pygame.KEYDOWN = 0
fake_pygame.K_ESCAPE = 0
fake_pygame.K_SPACE = 0
fake_pygame.K_v = 0
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

from floor796_kiosk.player import (
    Wanderer, SPACING_W, SPACING_H, TILE_W, TILE_H,
    DEFAULT_WANDER_SPEED, _compute_content_bounds,
    _visible_and_margin_tile_ids,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)


def run_simulation(tiles_meta_path, view_w, view_h, sim_hours=2, speed=15.0,
                   dt=1/30, seed=None):
    """Run the wanderer for sim_hours of simulated time.

    Records:
      - visit_counts: how many times each animated tile was in the viewport
      - position_samples: viewport position at regular intervals
      - waypoint_targets: which tiles were picked as waypoints
    """
    if seed is not None:
        import random as _r
        _r.seed(seed)

    with open(tiles_meta_path) as f:
        tiles_meta = json.load(f)

    grid_rows = tiles_meta["grid_rows"]
    grid_cols = tiles_meta["grid_cols"]
    map_w = grid_cols * SPACING_W
    map_h = grid_rows * SPACING_H

    content_bounds = _compute_content_bounds(tiles_meta)

    # Build tile_grid for visit recording
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
    sample_interval = max(1, total_steps // 500)  # ~500 position samples

    position_samples = []
    waypoint_targets = []
    waypoint_count_at_start = 0

    # Patch time.time() for the wanderer's timeout logic
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

    # Replace time module functions used by Wanderer
    import floor796_kiosk.player as kiosk_player
    original_time_module = kiosk_player.time
    kiosk_player.time = FakeTime

    try:
        for step in range(total_steps):
            sim_time[0] = step * dt

            wanderer.update(dt)

            pos_x = wanderer.x
            pos_y = wanderer.y
            pos_x = max(0, min(map_w - view_w, pos_x))
            pos_y = max(0, min(map_h - view_h, pos_y))

            # Record visible tiles (same logic as render loop)
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
                position_samples.append((pos_x, pos_y))

            if wanderer.waypoints_picked > waypoint_count_at_start:
                waypoint_count_at_start = wanderer.waypoints_picked
                if wanderer.target_rc:
                    waypoint_targets.append(wanderer.target_rc)
    finally:
        kiosk_player.time = original_time_module

    return {
        "visit_counts": dict(wanderer.visit_counts),
        "position_samples": position_samples,
        "waypoint_targets": waypoint_targets,
        "waypoints_picked": wanderer.waypoints_picked,
        "sim_time_s": total_steps * dt,
        "view_w": view_w,
        "view_h": view_h,
        "grid_rows": grid_rows,
        "grid_cols": grid_cols,
        "animated_tiles": sorted(wanderer.animated_tiles),
    }


def print_heatmap(results):
    """Print a text heatmap of visit counts."""
    grid_rows = results["grid_rows"]
    grid_cols = results["grid_cols"]
    visits = results["visit_counts"]
    animated = set(results["animated_tiles"])

    max_visits = max(visits.values()) if visits else 1
    min_visits = min(visits.values()) if visits else 0

    print(f"\n{'='*60}")
    print(f"Wanderer Coverage Heatmap ({results['sim_time_s']/3600:.1f}h simulated)")
    print(f"View: {results['view_w']}x{results['view_h']}, "
          f"Waypoints picked: {results['waypoints_picked']}")
    print(f"Visit range: min={min_visits} max={max_visits} "
          f"ratio={max_visits/max(1,min_visits):.1f}:1")
    print(f"{'='*60}\n")

    print("Grid (row 0 = top, col 0 = left). Numbers = visit counts.")
    print("    " + " ".join(f"{c:5d}" for c in range(grid_cols)))
    print("    " + "------" * grid_cols)

    for r in range(grid_rows):
        row_str = f"r{r:2d} "
        for c in range(grid_cols):
            if (r, c) in animated:
                v = visits.get((r, c), 0)
                # Scale to 0-9
                if max_visits > 0:
                    intensity = int((v / max_visits) * 9)
                else:
                    intensity = 0
                row_str += f" {v:4d}"
            else:
                row_str += "    ."
        print(row_str)

    # Analysis
    print(f"\n{'='*60}")
    print("Bias Analysis")
    print(f"{'='*60}")

    # Quadrant analysis
    mid_row = grid_rows / 2
    mid_col = grid_cols / 2
    quadrants = {"TL": 0, "TR": 0, "BL": 0, "BR": 0}
    quad_counts = {"TL": 0, "TR": 0, "BL": 0, "BR": 0}

    for (r, c), v in visits.items():
        if r < mid_row and c < mid_col:
            q = "TL"
        elif r < mid_row and c >= mid_col:
            q = "TR"
        elif r >= mid_row and c < mid_col:
            q = "BL"
        else:
            q = "BR"
        quadrants[q] += v
        quad_counts[q] += 1

    print("\nQuadrant visits (total / avg per tile):")
    for q in ["TL", "TR", "BL", "BR"]:
        total = quadrants[q]
        count = quad_counts[q]
        avg = total / count if count > 0 else 0
        print(f"  {q}: {total:5d} total, {avg:5.1f} avg ({count} tiles)")

    # Edge analysis - which edges are visited least?
    edge_visits = {"top": [], "bottom": [], "left": [], "right": []}
    for (r, c) in animated:
        v = visits.get((r, c), 0)
        if r == min(r for r, c in animated):
            edge_visits["top"].append(v)
        if r == max(r for r, c in animated):
            edge_visits["bottom"].append(v)
        if c == min(c for r, c in animated):
            edge_visits["left"].append(v)
        if c == max(c for r, c in animated):
            edge_visits["right"].append(v)

    print("\nEdge visit averages:")
    for edge, vals in edge_visits.items():
        if vals:
            print(f"  {edge:6s}: avg={sum(vals)/len(vals):.1f} "
                  f"(tiles: {vals})")

    # Waypoint target distribution
    wp_targets = results["waypoint_targets"]
    if wp_targets:
        print(f"\nWaypoint target distribution ({len(wp_targets)} waypoints):")
        wp_counts = defaultdict(int)
        for rc in wp_targets:
            wp_counts[rc] += 1
        for rc in sorted(wp_counts.keys()):
            r, c = rc
            print(f"  ({r:2d},{c:2d}): {wp_counts[rc]:3d} times")

    # Position samples - where does the viewport spend time?
    pos_samples = results["position_samples"]
    if pos_samples:
        print(f"\nPosition samples ({len(pos_samples)}):")
        xs = [p[0] for p in pos_samples]
        ys = [p[1] for p in pos_samples]
        print(f"  X range: {min(xs):.0f}-{max(xs):.0f} "
              f"(mean={sum(xs)/len(xs):.0f})")
        print(f"  Y range: {min(ys):.0f}-{max(ys):.0f} "
              f"(mean={sum(ys)/len(ys):.0f})")

        # Which quadrant does the viewport center spend time in?
        map_w = grid_cols * SPACING_W
        map_h = grid_rows * SPACING_H
        mid_x = map_w / 2
        mid_y = map_h / 2
        quad_time = {"TL": 0, "TR": 0, "BL": 0, "BR": 0}
        for px, py in pos_samples:
            cx = px + results["view_w"] / 2
            cy = py + results["view_h"] / 2
            if cx < mid_x and cy < mid_y:
                quad_time["TL"] += 1
            elif cx < mid_x and cy >= mid_y:
                quad_time["BL"] += 1
            elif cx >= mid_x and cy < mid_y:
                quad_time["TR"] += 1
            else:
                quad_time["BR"] += 1

        total_samples = len(pos_samples)
        print(f"\nViewport center time by quadrant:")
        for q in ["TL", "TR", "BL", "BR"]:
            pct = quad_time[q] / total_samples * 100
            print(f"  {q}: {pct:5.1f}% ({quad_time[q]}/{total_samples})")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Wanderer bias simulation")
    parser.add_argument("--hours", type=float, default=2.0,
                        help="Simulated hours to run (default: 2)")
    parser.add_argument("--width", type=int, default=1920,
                        help="Viewport width (default: 1920)")
    parser.add_argument("--height", type=int, default=1200,
                        help="Viewport height (default: 1200)")
    parser.add_argument("--speed", type=float, default=15.0,
                        help="Wander speed (default: 15.0)")
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed for reproducibility")
    parser.add_argument("--runs", type=int, default=1,
                        help="Number of runs to average")
    args = parser.parse_args()

    meta_path = os.path.join(PROJECT_ROOT, "assets", "tiles_meta.json")
    if not os.path.exists(meta_path):
        print(f"Error: {meta_path} not found")
        sys.exit(1)

    all_results = []
    for run_idx in range(args.runs):
        seed = args.seed + run_idx if args.seed else None
        print(f"\n{'#'*60}")
        print(f"  Run {run_idx+1}/{args.runs}")
        print(f"{'#'*60}")
        results = run_simulation(
            meta_path, args.width, args.height,
            sim_hours=args.hours, speed=args.speed,
            seed=seed,
        )
        print_heatmap(results)
        all_results.append(results)

    if args.runs > 1:
        print(f"\n{'='*60}")
        print(f"Aggregate across {args.runs} runs")
        print(f"{'='*60}")
        # Average visit counts
        all_rc = set()
        for r in all_results:
            all_rc.update(r["visit_counts"].keys())

        avg_visits = {}
        for rc in all_rc:
            vals = [r["visit_counts"].get(rc, 0) for r in all_results]
            avg_visits[rc] = sum(vals) / len(vals)

        max_v = max(avg_visits.values())
        min_v = min(avg_visits.values())
        print(f"Avg visit range: min={min_v:.1f} max={max_v:.1f} "
              f"ratio={max_v/max(1,min_v):.1f}:1")
