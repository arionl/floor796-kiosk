#!/usr/bin/env python3
"""
Simulation: tile prefetch behavior during direction changes.

Simulates the wanderer + tile cache with the REAL tile layout from
tiles_meta.json. Measures how many tiles are missing (not yet in
cache) when they scroll into view, focusing on direction-change events.

Runs two scenarios:
  1. CURRENT code: heading() returns instantaneous direction toward
     waypoint, but set_needed() only uses it for directional filtering
     of the margin ring (no extra prefetch depth in the new direction).
  2. PROPOSED: when a direction change is detected, immediately expand
     the margin ring in the new direction to prefetch tiles aggressively
     before the viewport reaches them.
"""

import json
import math
import random
import os

TILE_META_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tiles_meta.json")

SPACING_W = 1016
SPACING_H = 812
TILE_W = 1024
TILE_H = 820
VIEW_W = 1920
VIEW_H = 1200
CACHE_MARGIN = 2
MAX_TILES = 15
WANDER_SPEED = 15.0
TILE_FPS = 12
DT = 1.0 / 30.0  # 30fps render loop


def load_meta():
    with open(TILE_META_PATH) as f:
        return json.load(f)


def compute_content_bounds(tiles_meta):
    min_col, max_col = 999, 0
    min_row, max_row = 999, 0
    for info in tiles_meta["tiles"].values():
        if info.get("animated"):
            min_col = min(min_col, info["col"])
            max_col = max(max_col, info["col"])
            min_row = min(min_row, info["row"])
            max_row = max(max_row, info["row"])
    return (min_col * SPACING_W, min_row * SPACING_H,
            (max_col + 1) * SPACING_W, (max_row + 1) * SPACING_H)


def build_tile_grid(tiles_meta):
    grid = {}
    for tid, info in tiles_meta["tiles"].items():
        grid[(info["row"], info["col"])] = tid
    return grid


def visible_tile_ids(pos_x, pos_y, grid):
    cs = max(0, int(pos_x // SPACING_W))
    ce = int((pos_x + VIEW_W) // SPACING_W) + 1
    rs = max(0, int(pos_y // SPACING_H))
    re = int((pos_y + VIEW_H) // SPACING_H) + 1
    result = set()
    for r in range(rs, re):
        for c in range(cs, ce):
            tid = grid.get((r, c))
            if tid:
                result.add(tid)
    return result


def margin_tile_ids(pos_x, pos_y, grid, tiles_meta, margin, vel_x=0, vel_y=0):
    """Replicate _visible_and_margin_tile_ids from kiosk_player.py."""
    cs = max(0, int(pos_x // SPACING_W))
    ce = min(tiles_meta["grid_cols"], int((pos_x + VIEW_W) // SPACING_W) + 1)
    rs = max(0, int(pos_y // SPACING_H))
    re = min(tiles_meta["grid_rows"], int((pos_y + VIEW_H) // SPACING_H) + 1)

    vis = set()
    for r in range(rs, re):
        for c in range(cs, ce):
            tid = grid.get((r, c))
            if tid:
                vis.add(tid)

    all_margin = set()
    for r in range(max(0, rs - margin), min(tiles_meta["grid_rows"], re + margin)):
        for c in range(max(0, cs - margin), min(tiles_meta["grid_cols"], ce + margin)):
            tid = grid.get((r, c))
            if tid:
                all_margin.add(tid)
    all_margin -= vis

    speed = math.hypot(vel_x, vel_y)
    if speed < 1.0:
        return vis, all_margin

    vcx = pos_x + VIEW_W / 2
    vcy = pos_y + VIEW_H / 2
    dx = vel_x / speed
    dy = vel_y / speed

    forward_margin = set()
    for tid in all_margin:
        info = tiles_meta["tiles"].get(tid)
        if not info:
            continue
        tcx = info["col"] * SPACING_W + SPACING_W / 2
        tcy = info["row"] * SPACING_H + SPACING_H / 2
        forward = (tcx - vcx) * dx + (tcy - vcy) * dy
        if forward > 0:
            forward_margin.add(tid)

    return vis, forward_margin


def margin_tile_ids_expanded(pos_x, pos_y, grid, tiles_meta, margin, vel_x=0, vel_y=0,
                              extra_margin=2):
    """PROPOSED: expand margin ring further in the travel direction."""
    cs = max(0, int(pos_x // SPACING_W))
    ce = min(tiles_meta["grid_cols"], int((pos_x + VIEW_W) // SPACING_W) + 1)
    rs = max(0, int(pos_y // SPACING_H))
    re = min(tiles_meta["grid_rows"], int((pos_y + VIEW_H) // SPACING_H) + 1)

    vis = set()
    for r in range(rs, re):
        for c in range(cs, ce):
            tid = grid.get((r, c))
            if tid:
                vis.add(tid)

    speed = math.hypot(vel_x, vel_y)
    if speed < 1.0:
        # Full ring with normal margin
        all_margin = set()
        for r in range(max(0, rs - margin), min(tiles_meta["grid_rows"], re + margin)):
            for c in range(max(0, cs - margin), min(tiles_meta["grid_cols"], ce + margin)):
                tid = grid.get((r, c))
                if tid:
                    all_margin.add(tid)
        all_margin -= vis
        return vis, all_margin

    # Determine forward-biased expanded margin
    dx = vel_x / speed
    dy = vel_y / speed

    # Base margin ring
    base_margin = set()
    for r in range(max(0, rs - margin), min(tiles_meta["grid_rows"], re + margin)):
        for c in range(max(0, cs - margin), min(tiles_meta["grid_cols"], ce + margin)):
            tid = grid.get((r, c))
            if tid:
                base_margin.add(tid)
    base_margin -= vis

    # Expanded ring in forward direction
    fwd_margin = set()
    for r in range(max(0, rs - margin - extra_margin),
                    min(tiles_meta["grid_rows"], re + margin + extra_margin)):
        for c in range(max(0, cs - margin - extra_margin),
                        min(tiles_meta["grid_cols"], ce + margin + extra_margin)):
            tid = grid.get((r, c))
            if tid:
                fwd_margin.add(tid)
    fwd_margin -= vis

    # Filter to forward-only tiles in the expanded set
    vcx = pos_x + VIEW_W / 2
    vcy = pos_y + VIEW_H / 2
    result = set()
    for tid in fwd_margin:
        info = tiles_meta["tiles"].get(tid)
        if not info:
            continue
        tcx = info["col"] * SPACING_W + SPACING_W / 2
        tcy = info["row"] * SPACING_H + SPACING_H / 2
        forward = (tcx - vcx) * dx + (tcy - vcy) * dy
        if forward > 0:
            result.add(tid)

    return vis, result


class SimWanderer:
    """Simplified wanderer that mirrors the real state machine."""
    def __init__(self, tiles_meta, content_bounds):
        self.speed = WANDER_SPEED
        cx_min, cy_min, cx_max, cy_max = content_bounds
        self.min_x = cx_min
        self.max_x = max(cx_min + 1, cx_max - VIEW_W)
        self.min_y = cy_min
        self.max_y = max(cy_min + 1, cy_max - VIEW_H)
        self.x = (self.min_x + self.max_x) / 2
        self.y = (self.min_y + self.max_y) / 2
        self.angle = random.uniform(0, 2 * math.pi)
        self.vx = math.cos(self.angle) * self.speed
        self.vy = math.sin(self.angle) * self.speed
        self.current_waypoint = None
        self.target_rc = None
        self.recent_targets = []
        self.waypoint_start_time = 0.0
        self.waypoint_timeout = 90.0
        self.visit_counts = {}
        self.animated_tiles = set()
        self.tile_id_to_rc = {}
        for tid, info in tiles_meta["tiles"].items():
            rc = (info["row"], info["col"])
            self.tile_id_to_rc[tid] = rc
            if info.get("animated"):
                self.animated_tiles.add(rc)
                self.visit_counts[rc] = 0
        self.waypoints_picked = 0
        self._pick_new_waypoint()

    def _pick_new_waypoint(self):
        if not self.animated_tiles:
            self.current_waypoint = (
                random.uniform(self.min_x, self.max_x),
                random.uniform(self.min_y, self.max_y))
            return
        max_visits = max(self.visit_counts.values()) if self.visit_counts else 1
        scored = []
        for rc in self.animated_tiles:
            row, col = rc
            vp_x = col * SPACING_W + SPACING_W // 2 - VIEW_W // 2
            vp_y = row * SPACING_H + SPACING_H // 2 - VIEW_H // 2
            norm = self.visit_counts.get(rc, 0) / max(1, max_visits)
            score = norm
            if rc in self.recent_targets:
                score += 0.3
            score += random.uniform(0, 0.05)
            scored.append((score, rc))
        weights = [1.0 / (s + 0.01) for s, _ in scored]
        total_w = sum(weights)
        r = random.uniform(0, total_w)
        cumulative = 0.0
        target_rc = scored[-1][1]
        for (score, rc), w in zip(scored, weights):
            cumulative += w
            if r <= cumulative:
                target_rc = rc
                break
        row, col = target_rc
        tx = col * SPACING_W + SPACING_W // 2 - VIEW_W // 2
        ty = row * SPACING_H + SPACING_H // 2 - VIEW_H // 2
        tx += random.uniform(-TILE_W * 0.3, TILE_W * 0.3)
        ty += random.uniform(-TILE_H * 0.3, TILE_H * 0.3)
        tx = max(self.min_x, min(self.max_x, tx))
        ty = max(self.min_y, min(self.max_y, ty))
        self.current_waypoint = (tx, ty)
        self.target_rc = target_rc
        self.recent_targets = list(self.recent_targets[-3:]) + [target_rc]
        self.waypoint_start_time = 0.0
        self.waypoints_picked += 1

    def heading(self):
        if self.current_waypoint:
            wx, wy = self.current_waypoint
            dx, dy = wx - self.x, wy - self.y
            dist = math.hypot(dx, dy)
            if dist > 0.5:
                return (dx / dist * self.speed, dy / dist * self.speed)
        return (self.vx, self.vy)

    def update(self, dt, sim_time):
        if self.current_waypoint is None:
            self._pick_new_waypoint()
        wx, wy = self.current_waypoint
        dx, dy = wx - self.x, wy - self.y
        dist = math.hypot(dx, dy)
        arrived = dist < max(TILE_W, TILE_H) * 0.5
        timed_out = sim_time - self.waypoint_start_time > self.waypoint_timeout
        if arrived or timed_out:
            self._pick_new_waypoint()
            self.waypoint_start_time = sim_time
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
        if self.x <= self.min_x:
            self.x = self.min_x
            self.vx = abs(self.vx) * 0.5
            self._pick_new_waypoint()
            self.waypoint_start_time = sim_time
        elif self.x >= self.max_x:
            self.x = self.max_x
            self.vx = -abs(self.vx) * 0.5
            self._pick_new_waypoint()
            self.waypoint_start_time = sim_time
        if self.y <= self.min_y:
            self.y = self.min_y
            self.vy = abs(self.vy) * 0.5
            self._pick_new_waypoint()
            self.waypoint_start_time = sim_time
        elif self.y >= self.max_y:
            self.y = self.max_y
            self.vy = -abs(self.vy) * 0.5
            self._pick_new_waypoint()
            self.waypoint_start_time = sim_time

import sys

class SimTileCache:
    """Simplified tile cache that simulates load latency."""
    def __init__(self, max_tiles=MAX_TILES, load_latency_steps=6):
        self.cache = set()
        self.max_tiles = max_tiles
        self.load_latency = load_latency_steps
        self._pending = {}
        self.load_count = 0

    def set_needed(self, needed_ids):
        for tid in needed_ids:
            if tid not in self.cache and tid not in self._pending:
                self._pending[tid] = self.load_latency
        excess = [t for t in self.cache if t not in needed_ids]
        while len(self.cache) > self.max_tiles and excess:
            self.cache.discard(excess.pop(0))

    def tick(self):
        ready = []
        for tid in list(self._pending.keys()):
            self._pending[tid] -= 1
            if self._pending[tid] <= 0:
                ready.append(tid)
                del self._pending[tid]
                self.cache.add(tid)
                self.load_count += 1

    def has(self, tid):
        return tid in self.cache


def angle_diff(a1, a2):
    d = a1 - a2
    while d > math.pi:
        d -= 2 * math.pi
    while d < -math.pi:
        d += 2 * math.pi
    return d


def run_simulation(label, tiles_meta, grid, content_bounds, duration_s=600,
                   use_expanded=False, extra_margin=2, seed=42):
    random.seed(seed)
    wanderer = SimWanderer(tiles_meta, content_bounds)
    cache = SimTileCache(max_tiles=MAX_TILES, load_latency_steps=6)
    sim_time = 0.0
    steps = int(duration_s / DT)
    prev_heading_angle = None
    direction_changes = 0
    missing_on_enter = 0
    total_entered = 0
    missing_after_change = 0
    total_after_change = 0
    change_cooldown = 999.0
    ever_visible = set()

    for step in range(steps):
        sim_time += DT
        wanderer.update(DT, sim_time)
        cache.tick()
        hx, hy = wanderer.heading()
        heading_angle = math.atan2(hy, hx)
        is_direction_change = False
        if prev_heading_angle is not None:
            ad = abs(angle_diff(heading_angle, prev_heading_angle))
            if ad > math.radians(60):
                direction_changes += 1
                is_direction_change = True
                change_cooldown = 0.0
        prev_heading_angle = heading_angle
        change_cooldown += DT
        pos_x = max(0, min(tiles_meta["grid_cols"] * SPACING_W - VIEW_W, wanderer.x))
        pos_y = max(0, min(tiles_meta["grid_rows"] * SPACING_H - VIEW_H, wanderer.y))
        if use_expanded:
            vis, margin = margin_tile_ids_expanded(
                pos_x, pos_y, grid, tiles_meta, CACHE_MARGIN,
                vel_x=hx, vel_y=hy, extra_margin=extra_margin)
        else:
            vis, margin = margin_tile_ids(
                pos_x, pos_y, grid, tiles_meta, CACHE_MARGIN,
                vel_x=hx, vel_y=hy)
        cache.set_needed(vis | margin)
        newly_visible = vis - ever_visible
        for tid in newly_visible:
            total_entered += 1
            if not cache.has(tid):
                missing_on_enter += 1
                if change_cooldown < 10.0:
                    missing_after_change += 1
            if change_cooldown < 10.0:
                total_after_change += 1
        ever_visible = vis

    return {
        "label": label,
        "duration_s": duration_s,
        "direction_changes": direction_changes,
        "total_entered": total_entered,
        "missing_on_enter": missing_on_enter,
        "miss_rate": missing_on_enter / max(1, total_entered),
        "missing_after_change": missing_after_change,
        "total_after_change": total_after_change,
        "miss_rate_after_change": missing_after_change / max(1, total_after_change),
    }


def main():
    tiles_meta = load_meta()
    grid = build_tile_grid(tiles_meta)
    content_bounds = compute_content_bounds(tiles_meta)
    results = []
    for seed in [42, 123, 777, 2024, 5555]:
        r1 = run_simulation("CURRENT", tiles_meta, grid, content_bounds,
                            duration_s=600, use_expanded=False, seed=seed)
        r2 = run_simulation("PROPOSED (+2 fwd)", tiles_meta, grid, content_bounds,
                            duration_s=600, use_expanded=True, extra_margin=2, seed=seed)
        r3 = run_simulation("PROPOSED (+3 fwd)", tiles_meta, grid, content_bounds,
                            duration_s=600, use_expanded=True, extra_margin=3, seed=seed)
        results.append((seed, r1, r2, r3))

    print("=" * 80)
    print("FLOOR796 TILE PREFETCH SIMULATION")
    print(f"Layout: {tiles_meta['grid_rows']}x{tiles_meta['grid_cols']} grid, "
          f"{sum(1 for i in tiles_meta['tiles'].values() if i.get('animated'))} animated tiles")
    print(f"Viewport: {VIEW_W}x{VIEW_H}, margin={CACHE_MARGIN}, max_cache={MAX_TILES}")
    print(f"Speed: {WANDER_SPEED} px/s, sim duration: 600s (10 min) per seed")
    print(f"Load latency: 6 steps (~0.2s) per tile")
    print("=" * 80)

    for seed, r1, r2, r3 in results:
        print(f"\n--- Seed {seed} ---")
        for r in [r1, r2, r3]:
            print(f"  {r['label']:20s} | dir changes: {r['direction_changes']:3d} | "
                  f"tiles entered: {r['total_entered']:4d} | "
                  f"missing: {r['missing_on_enter']:3d} ({r['miss_rate']*100:.1f}%) | "
                  f"missing after change: {r['missing_after_change']:3d}/{r['total_after_change']:4d} "
                  f"({r['miss_rate_after_change']*100:.1f}%)")

    print("\n" + "=" * 80)
    print("AGGREGATE (5 seeds x 10 min = 50 min simulated each)")
    print("=" * 80)
    for idx, label in enumerate(["CURRENT", "PROPOSED (+2 fwd)", "PROPOSED (+3 fwd)"]):
        agg = [rs[idx] for _, *rs in results]
        total_entered = sum(r["total_entered"] for r in agg)
        total_missing = sum(r["missing_on_enter"] for r in agg)
        total_after = sum(r["total_after_change"] for r in agg)
        total_missing_after = sum(r["missing_after_change"] for r in agg)
        total_changes = sum(r["direction_changes"] for r in agg)
        print(f"\n  {label}")
        print(f"    Direction changes:     {total_changes}")
        print(f"    Total tiles entered:   {total_entered}")
        print(f"    Missing on enter:      {total_missing} ({total_missing/max(1,total_entered)*100:.1f}%)")
        print(f"    Tiles entering within 10s of direction change: {total_after}")
        print(f"    Missing after change:  {total_missing_after} ({total_missing_after/max(1,total_after)*100:.1f}%)")


if __name__ == "__main__":
    main()


def run_simulation_v2(label, tiles_meta, grid, content_bounds, duration_s=600,
                      use_expanded=False, extra_margin=2, seed=42,
                      load_latency_steps=30, keep_old_direction=False):
    """V2: More realistic load latency (30 steps = ~1s) and eviction model.

    keep_old_direction: if True, don't evict tiles from old direction on
    direction change — keep them cached until new tiles are loaded.
    """
    random.seed(seed)
    wanderer = SimWanderer(tiles_meta, content_bounds)
    cache = SimTileCache(max_tiles=MAX_TILES, load_latency_steps=load_latency_steps)
    sim_time = 0.0
    steps = int(duration_s / DT)
    prev_heading_angle = None
    direction_changes = 0
    missing_on_enter = 0
    total_entered = 0
    missing_after_change = 0
    total_after_change = 0
    change_cooldown = 999.0
    ever_visible = set()
    prev_needed = set()

    for step in range(steps):
        sim_time += DT
        wanderer.update(DT, sim_time)
        cache.tick()
        hx, hy = wanderer.heading()
        heading_angle = math.atan2(hy, hx)
        is_direction_change = False
        if prev_heading_angle is not None:
            ad = abs(angle_diff(heading_angle, prev_heading_angle))
            if ad > math.radians(60):
                direction_changes += 1
                is_direction_change = True
                change_cooldown = 0.0
        prev_heading_angle = heading_angle
        change_cooldown += DT
        pos_x = max(0, min(tiles_meta["grid_cols"] * SPACING_W - VIEW_W, wanderer.x))
        pos_y = max(0, min(tiles_meta["grid_rows"] * SPACING_H - VIEW_H, wanderer.y))

        if use_expanded:
            vis, margin = margin_tile_ids_expanded(
                pos_x, pos_y, grid, tiles_meta, CACHE_MARGIN,
                vel_x=hx, vel_y=hy, extra_margin=extra_margin)
        else:
            vis, margin = margin_tile_ids(
                pos_x, pos_y, grid, tiles_meta, CACHE_MARGIN,
                vel_x=hx, vel_y=hy)

        needed = vis | margin

        if keep_old_direction and is_direction_change:
            # Don't evict on direction change — keep old tiles until new
            # ones are loaded. Only queue new tiles.
            new_tiles = needed - cache.cache - set(cache._pending.keys())
            for tid in new_tiles:
                cache._pending[tid] = cache.load_latency
            # Don't evict — just let natural overflow happen later
            # when cache exceeds max_tiles on next normal set_needed
        else:
            cache.set_needed(needed)

        newly_visible = vis - ever_visible
        for tid in newly_visible:
            total_entered += 1
            if not cache.has(tid):
                missing_on_enter += 1
                if change_cooldown < 10.0:
                    missing_after_change += 1
            if change_cooldown < 10.0:
                total_after_change += 1
        ever_visible = vis
        prev_needed = needed

    return {
        "label": label,
        "duration_s": duration_s,
        "direction_changes": direction_changes,
        "total_entered": total_entered,
        "missing_on_enter": missing_on_enter,
        "miss_rate": missing_on_enter / max(1, total_entered),
        "missing_after_change": missing_after_change,
        "total_after_change": total_after_change,
        "miss_rate_after_change": missing_after_change / max(1, total_after_change),
    }


def main_v2():
    tiles_meta = load_meta()
    grid = build_tile_grid(tiles_meta)
    content_bounds = compute_content_bounds(tiles_meta)

    # Realistic load latency: 30 steps = ~1s (96MB BMP from SD card)
    LATENCY = 30

    results = []
    for seed in [42, 123, 777, 2024, 5555]:
        r1 = run_simulation_v2("CURRENT (1s load)", tiles_meta, grid, content_bounds,
                               duration_s=600, use_expanded=False, seed=seed,
                               load_latency_steps=LATENCY)
        r2 = run_simulation_v2("EXPANDED +2 (1s load)", tiles_meta, grid, content_bounds,
                               duration_s=600, use_expanded=True, extra_margin=2, seed=seed,
                               load_latency_steps=LATENCY)
        r3 = run_simulation_v2("KEEP-OLD on change", tiles_meta, grid, content_bounds,
                               duration_s=600, use_expanded=False, seed=seed,
                               load_latency_steps=LATENCY, keep_old_direction=True)
        r4 = run_simulation_v2("EXPANDED +2 + KEEP-OLD", tiles_meta, grid, content_bounds,
                               duration_s=600, use_expanded=True, extra_margin=2, seed=seed,
                               load_latency_steps=LATENCY, keep_old_direction=True)
        results.append((seed, r1, r2, r3, r4))

    print("=" * 90)
    print("FLOOR796 TILE PREFETCH SIMULATION V2 (realistic 1s load latency)")
    print(f"Layout: {tiles_meta['grid_rows']}x{tiles_meta['grid_cols']} grid, "
          f"{sum(1 for i in tiles_meta['tiles'].values() if i.get('animated'))} animated tiles")
    print(f"Viewport: {VIEW_W}x{VIEW_H}, margin={CACHE_MARGIN}, max_cache={MAX_TILES}")
    print(f"Speed: {WANDER_SPEED} px/s, sim duration: 600s (10 min) per seed")
    print(f"Load latency: {LATENCY} steps (~1.0s) per tile")
    print("=" * 90)

    for seed, r1, r2, r3, r4 in results:
        print(f"\n--- Seed {seed} ---")
        for r in [r1, r2, r3, r4]:
            print(f"  {r['label']:28s} | dir chg: {r['direction_changes']:3d} | "
                  f"entered: {r['total_entered']:4d} | "
                  f"miss: {r['missing_on_enter']:3d} ({r['miss_rate']*100:5.1f}%) | "
                  f"miss after chg: {r['missing_after_change']:3d}/{r['total_after_change']:4d} "
                  f"({r['miss_rate_after_change']*100:5.1f}%)")

    print("\n" + "=" * 90)
    print("AGGREGATE (5 seeds x 10 min = 50 min simulated each)")
    print("=" * 90)
    labels = ["CURRENT (1s load)", "EXPANDED +2 (1s load)", "KEEP-OLD on change", "EXPANDED +2 + KEEP-OLD"]
    for idx, label in enumerate(labels):
        agg = [rs[idx] for _, *rs in results]
        te = sum(r["total_entered"] for r in agg)
        tm = sum(r["missing_on_enter"] for r in agg)
        ta = sum(r["total_after_change"] for r in agg)
        tma = sum(r["missing_after_change"] for r in agg)
        tc = sum(r["direction_changes"] for r in agg)
        print(f"\n  {label}")
        print(f"    Direction changes:     {tc}")
        print(f"    Total tiles entered:   {te}")
        print(f"    Missing on enter:      {tm} ({tm/max(1,te)*100:.1f}%)")
        print(f"    After change: {tma}/{ta} ({tma/max(1,ta)*100:.1f}%)")

if __name__ == "__main__":
    main_v2()
