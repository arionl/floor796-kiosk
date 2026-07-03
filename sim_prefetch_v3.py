#!/usr/bin/env python3
"""
Simulation V3: Accurate model of the real tile cache eviction behavior.

Key difference from V2: properly models that set_needed() IMMEDIATELY
evicts any cached tile not in the needed set (visible + margin). This is
the root cause of late loads after direction changes:

1. Wanderer changes direction (new waypoint picked)
2. heading() instantly points toward new waypoint
3. Directional filter immediately shifts margin ring to new direction
4. Old-direction margin tiles are evicted from cache instantly
5. New-direction margin tiles are queued but take ~1s each to load
6. During the load gap, viewport may scroll toward new-direction tiles
   that aren't loaded yet → late load / blank flash

The proposed fix: on direction change, immediately queue tiles in the
new direction with high priority AND delay eviction of old-direction
tiles until new ones are loaded.
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
DT = 1.0 / 30.0


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


def margin_tile_ids_expanded(pos_x, pos_y, grid, tiles_meta, margin,
                              vel_x=0, vel_y=0, extra_margin=2):
    """Expanded margin ring in the forward direction."""
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
        all_margin = set()
        for r in range(max(0, rs - margin), min(tiles_meta["grid_rows"], re + margin)):
            for c in range(max(0, cs - margin), min(tiles_meta["grid_cols"], ce + margin)):
                tid = grid.get((r, c))
                if tid:
                    all_margin.add(tid)
        all_margin -= vis
        return vis, all_margin
    dx = vel_x / speed
    dy = vel_y / speed
    fwd_margin = set()
    em = extra_margin
    for r in range(max(0, rs - margin - em),
                    min(tiles_meta["grid_rows"], re + margin + em)):
        for c in range(max(0, cs - margin - em),
                        min(tiles_meta["grid_cols"], ce + margin + em)):
            tid = grid.get((r, c))
            if tid:
                fwd_margin.add(tid)
    fwd_margin -= vis
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
            self.x = self.min_x; self.vx = abs(self.vx) * 0.5
            self._pick_new_waypoint(); self.waypoint_start_time = sim_time
        elif self.x >= self.max_x:
            self.x = self.max_x; self.vx = -abs(self.vx) * 0.5
            self._pick_new_waypoint(); self.waypoint_start_time = sim_time
        if self.y <= self.min_y:
            self.y = self.min_y; self.vy = abs(self.vy) * 0.5
            self._pick_new_waypoint(); self.waypoint_start_time = sim_time
        elif self.y >= self.max_y:
            self.y = self.max_y; self.vy = -abs(self.vy) * 0.5
            self._pick_new_waypoint(); self.waypoint_start_time = sim_time


def angle_diff(a1, a2):
    d = a1 - a2
    while d > math.pi:
        d -= 2 * math.pi
    while d < -math.pi:
        d += 2 * math.pi
    return d


class SimTileCacheV3:
    """Models the REAL TileCache behavior accurately.

    - set_needed(): immediately evicts tiles not in needed set
    - Background worker loads one tile at a time at load_latency steps
    - max_tiles capacity enforced with LRU-ish eviction
    """
    def __init__(self, max_tiles=MAX_TILES, load_latency_steps=30):
        self.cache = set()
        self.max_tiles = max_tiles
        self.load_latency = load_latency_steps
        self._pending = {}  # tid -> steps remaining
        self._queue = []    # FIFO load queue
        self.load_count = 0

    def set_needed(self, needed_ids):
        # Queue new tiles
        for tid in needed_ids:
            if tid not in self.cache and tid not in self._pending and tid not in self._queue:
                self._queue.append(tid)
                self._pending[tid] = -1  # queued but not yet loading

        # Evict tiles not in needed set (matches real code behavior)
        to_evict = [t for t in self.cache if t not in needed_ids]
        for t in to_evict:
            self.cache.discard(t)

        # Also cancel pending loads for tiles no longer needed
        self._queue = [t for t in self._queue if t in needed_ids]
        for t in list(self._pending.keys()):
            if t not in needed_ids and self._pending[t] < 0:
                del self._pending[t]

        # Enforce max_tiles
        if len(self.cache) > self.max_tiles:
            excess = list(self.cache)
            while len(self.cache) > self.max_tiles and excess:
                self.cache.discard(excess.pop())

    def set_needed_graceful(self, needed_ids):
        """PROPOSED: don't evict old tiles until new ones are loaded."""
        # Queue new tiles
        for tid in needed_ids:
            if tid not in self.cache and tid not in self._pending and tid not in self._queue:
                self._queue.append(tid)
                self._pending[tid] = -1

        # Don't evict tiles not in needed set — keep them as buffer
        # Only evict if over capacity AND new tiles need room
        if len(self.cache) > self.max_tiles:
            # Evict least-recently-needed tiles that aren't visible
            excess = [t for t in self.cache if t not in needed_ids]
            while len(self.cache) > self.max_tiles and excess:
                self.cache.discard(excess.pop(0))

    def set_needed_priority(self, visible_ids, margin_ids):
        """PROPOSED: graceful eviction + priority queue.

        Visible tiles go to the front of the load queue; margin tiles
        go to the back.  Queued tiles that become visible get promoted
        to the front.  Models the real set_needed() with reprioritization.
        """
        needed = visible_ids | margin_ids

        # Queue new tiles — visible to front, margin to back
        already = self.cache | set(self._pending.keys()) | set(self._queue)
        new_visible = (needed - already) & visible_ids
        new_margin = (needed - already) & margin_ids

        if new_visible:
            self._queue = list(new_visible) + self._queue
        if new_margin:
            self._queue.extend(sorted(new_margin))

        # Cancel pending loads for tiles no longer needed
        self._queue = [t for t in self._queue if t in needed]
        for t in list(self._pending.keys()):
            if t not in needed and self._pending[t] < 0:
                del self._pending[t]

        # Reprioritize: queued tiles that are now visible go to front
        promoted = []
        remaining = []
        for t in self._queue:
            if t in visible_ids:
                promoted.append(t)
            else:
                remaining.append(t)
        if promoted:
            self._queue = promoted + remaining

        # Graceful eviction
        if len(self.cache) > self.max_tiles:
            excess = [t for t in self.cache if t not in needed]
            while len(self.cache) > self.max_tiles and excess:
                self.cache.discard(excess.pop(0))

    def tick(self):
        # Process load queue one at a time
        if self._queue:
            tid = self._queue[0]
            if self._pending.get(tid, -1) < 0:
                # Start loading
                self._pending[tid] = self.load_latency
            self._pending[tid] -= 1
            if self._pending[tid] <= 0:
                self.cache.add(tid)
                del self._pending[tid]
                self._queue.pop(0)
                self.load_count += 1

    def has(self, tid):
        return tid in self.cache


def run_sim(label, tiles_meta, grid, content_bounds, duration_s=600,
            use_expanded=False, use_graceful=False, use_priority=False,
            extra_margin=2, load_latency=30, seed=42):
    random.seed(seed)
    wanderer = SimWanderer(tiles_meta, content_bounds)
    cache = SimTileCacheV3(max_tiles=MAX_TILES, load_latency_steps=load_latency)
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
    # Track individual late-load durations
    late_load_durations = []
    # Track when a tile first became visible but wasn't cached
    late_tile_seen = {}  # tid -> step it was first seen missing

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
        if use_priority:
            cache.set_needed_priority(vis, margin)
        elif use_graceful:
            cache.set_needed_graceful(needed)
        else:
            cache.set_needed(needed)

        # Track newly visible tiles
        newly_visible = vis - ever_visible
        for tid in newly_visible:
            total_entered += 1
            if not cache.has(tid):
                missing_on_enter += 1
                late_tile_seen[tid] = step
                if change_cooldown < 10.0:
                    missing_after_change += 1
            if change_cooldown < 10.0:
                total_after_change += 1
        ever_visible = vis

        # Track late load resolution
        for tid in list(late_tile_seen.keys()):
            if cache.has(tid):
                late_load_durations.append((step - late_tile_seen[tid]) * DT)
                del late_tile_seen[tid]

    avg_late = sum(late_load_durations) / max(1, len(late_load_durations))
    max_late = max(late_load_durations) if late_load_durations else 0

    return {
        "label": label,
        "direction_changes": direction_changes,
        "total_entered": total_entered,
        "missing_on_enter": missing_on_enter,
        "miss_rate": missing_on_enter / max(1, total_entered),
        "missing_after_change": missing_after_change,
        "total_after_change": total_after_change,
        "miss_rate_after_change": missing_after_change / max(1, total_after_change),
        "late_loads": len(late_load_durations),
        "avg_late_s": avg_late,
        "max_late_s": max_late,
    }


def main():
    tiles_meta = load_meta()
    grid = build_tile_grid(tiles_meta)
    content_bounds = compute_content_bounds(tiles_meta)
    LATENCY = 30  # ~1s per tile load

    results = []
    for seed in [42, 123, 777, 2024, 5555]:
        scenarios = [
            run_sim("CURRENT", tiles_meta, grid, content_bounds,
                    seed=seed, load_latency=LATENCY),
            run_sim("GRACEFUL EVICT", tiles_meta, grid, content_bounds,
                    use_graceful=True, seed=seed, load_latency=LATENCY),
            run_sim("PRIORITY QUEUE", tiles_meta, grid, content_bounds,
                    use_priority=True, seed=seed, load_latency=LATENCY),
        ]
        results.append((seed, scenarios))

    print("=" * 100)
    print("FLOOR796 TILE PREFETCH SIMULATION V3")
    print(f"Layout: {tiles_meta['grid_rows']}x{tiles_meta['grid_cols']}, "
          f"{sum(1 for i in tiles_meta['tiles'].values() if i.get('animated'))} animated tiles")
    print(f"Viewport: {VIEW_W}x{VIEW_H}, margin={CACHE_MARGIN}, max_cache={MAX_TILES}")
    print(f"Speed: {WANDER_SPEED} px/s, sim: 600s/seed, load latency: ~1s/tile")
    print(f"Cache loads ONE tile at a time (serial, like real worker thread)")
    print("=" * 100)

    for seed, scenarios in results:
        print(f"\n--- Seed {seed} ---")
        for r in scenarios:
            print(f"  {r['label']:25s} | dir chg: {r['direction_changes']:3d} | "
                  f"entered: {r['total_entered']:4d} | "
                  f"miss: {r['missing_on_enter']:3d} ({r['miss_rate']*100:5.1f}%) | "
                  f"miss@chg: {r['missing_after_change']:3d}/{r['total_after_change']:4d} "
                  f"({r['miss_rate_after_change']*100:5.1f}%) | "
                  f"late: {r['late_loads']:3d}, avg {r['avg_late_s']:.2f}s, max {r['max_late_s']:.2f}s")

    print("\n" + "=" * 100)
    print("AGGREGATE (5 seeds x 10 min = 50 min each)")
    print("=" * 100)
    labels = ["CURRENT", "GRACEFUL EVICT", "PRIORITY QUEUE"]
    for idx, label in enumerate(labels):
        agg = [s[idx] for _, s in results]
        te = sum(r["total_entered"] for r in agg)
        tm = sum(r["missing_on_enter"] for r in agg)
        ta = sum(r["total_after_change"] for r in agg)
        tma = sum(r["missing_after_change"] for r in agg)
        tc = sum(r["direction_changes"] for r in agg)
        tl = sum(r["late_loads"] for r in agg)
        avg_late = sum(r["avg_late_s"] for r in agg) / len(agg)
        max_late = max(r["max_late_s"] for r in agg)
        print(f"\n  {label}")
        print(f"    Dir changes: {tc} | Tiles entered: {te}")
        print(f"    Missing on enter: {tm} ({tm/max(1,te)*100:.1f}%)")
        print(f"    Missing after change: {tma}/{ta} ({tma/max(1,ta)*100:.1f}%)")
        print(f"    Late loads: {tl}, avg delay: {avg_late:.2f}s, max delay: {max_late:.2f}s")


if __name__ == "__main__":
    main()


def angle_diff(a1, a2):
    d = a1 - a2
    while d > math.pi:
        d -= 2 * math.pi
    while d < -math.pi:
        d += 2 * math.pi
    return d


class SimTileCacheV3:
    """Models the REAL TileCache behavior accurately.

    - set_needed(): immediately evicts tiles not in needed set
    - Background worker loads one tile at a time at load_latency steps
    - max_tiles capacity enforced with LRU-ish eviction
    """
    def __init__(self, max_tiles=MAX_TILES, load_latency_steps=30):
        self.cache = set()
        self.max_tiles = max_tiles
        self.load_latency = load_latency_steps
        self._pending = {}
        self._queue = []
        self.load_count = 0

    def set_needed(self, needed_ids):
        for tid in needed_ids:
            if tid not in self.cache and tid not in self._pending and tid not in self._queue:
                self._queue.append(tid)
                self._pending[tid] = -1
        to_evict = [t for t in self.cache if t not in needed_ids]
        for t in to_evict:
            self.cache.discard(t)
        self._queue = [t for t in self._queue if t in needed_ids]
        for t in list(self._pending.keys()):
            if t not in needed_ids and self._pending[t] < 0:
                del self._pending[t]
        if len(self.cache) > self.max_tiles:
            excess = list(self.cache)
            while len(self.cache) > self.max_tiles and excess:
                self.cache.discard(excess.pop())

    def set_needed_graceful(self, needed_ids):
        """PROPOSED: dont evict old tiles until new ones are loaded."""
        for tid in needed_ids:
            if tid not in self.cache and tid not in self._pending and tid not in self._queue:
                self._queue.append(tid)
                self._pending[tid] = -1
        if len(self.cache) > self.max_tiles:
            excess = [t for t in self.cache if t not in needed_ids]
            while len(self.cache) > self.max_tiles and excess:
                self.cache.discard(excess.pop(0))

    def tick(self):
        if self._queue:
            tid = self._queue[0]
            if self._pending.get(tid, -1) < 0:
                self._pending[tid] = self.load_latency
            self._pending[tid] -= 1
            if self._pending[tid] <= 0:
                self.cache.add(tid)
                del self._pending[tid]
                self._queue.pop(0)
                self.load_count += 1

    def has(self, tid):
        return tid in self.cache


def run_sim(label, tiles_meta, grid, content_bounds, duration_s=600,
            use_expanded=False, use_graceful=False, use_priority=False,
            extra_margin=2, load_latency=30, seed=42):
    random.seed(seed)
    wanderer = SimWanderer(tiles_meta, content_bounds)
    cache = SimTileCacheV3(max_tiles=MAX_TILES, load_latency_steps=load_latency)
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
    late_load_durations = []
    late_tile_seen = {}

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
        if use_priority:
            cache.set_needed_priority(vis, margin)
        elif use_graceful:
            cache.set_needed_graceful(needed)
        else:
            cache.set_needed(needed)

        newly_visible = vis - ever_visible
        for tid in newly_visible:
            total_entered += 1
            if not cache.has(tid):
                missing_on_enter += 1
                late_tile_seen[tid] = step
                if change_cooldown < 10.0:
                    missing_after_change += 1
            if change_cooldown < 10.0:
                total_after_change += 1
        ever_visible = vis

        for tid in list(late_tile_seen.keys()):
            if cache.has(tid):
                late_load_durations.append((step - late_tile_seen[tid]) * DT)
                del late_tile_seen[tid]

    avg_late = sum(late_load_durations) / max(1, len(late_load_durations))
    max_late = max(late_load_durations) if late_load_durations else 0

    return {
        "label": label,
        "direction_changes": direction_changes,
        "total_entered": total_entered,
        "missing_on_enter": missing_on_enter,
        "miss_rate": missing_on_enter / max(1, total_entered),
        "missing_after_change": missing_after_change,
        "total_after_change": total_after_change,
        "miss_rate_after_change": missing_after_change / max(1, total_after_change),
        "late_loads": len(late_load_durations),
        "avg_late_s": avg_late,
        "max_late_s": max_late,
    }


def main():
    tiles_meta = load_meta()
    grid = build_tile_grid(tiles_meta)
    content_bounds = compute_content_bounds(tiles_meta)
    LATENCY = 30

    results = []
    for seed in [42, 123, 777, 2024, 5555]:
        scenarios = [
            run_sim("CURRENT", tiles_meta, grid, content_bounds,
                    seed=seed, load_latency=LATENCY),
            run_sim("EXPANDED +2", tiles_meta, grid, content_bounds,
                    use_expanded=True, extra_margin=2, seed=seed, load_latency=LATENCY),
            run_sim("GRACEFUL EVICT", tiles_meta, grid, content_bounds,
                    use_graceful=True, seed=seed, load_latency=LATENCY),
            run_sim("EXP+2+GRACEFUL", tiles_meta, grid, content_bounds,
                    use_expanded=True, extra_margin=2, use_graceful=True,
                    seed=seed, load_latency=LATENCY),
        ]
        results.append((seed, scenarios))

    print("=" * 100)
    print("FLOOR796 TILE PREFETCH SIMULATION V3")
    print(f"Layout: {tiles_meta['grid_rows']}x{tiles_meta['grid_cols']}, "
          f"{sum(1 for i in tiles_meta['tiles'].values() if i.get('animated'))} animated tiles")
    print(f"Viewport: {VIEW_W}x{VIEW_H}, margin={CACHE_MARGIN}, max_cache={MAX_TILES}")
    print(f"Speed: {WANDER_SPEED} px/s, sim: 600s/seed, load latency: ~1s/tile")
    print(f"Cache loads ONE tile at a time (serial, like real worker thread)")
    print("=" * 100)

    for seed, scenarios in results:
        print(f"\n--- Seed {seed} ---")
        for r in scenarios:
            print(f"  {r['label']:20s} | dir chg: {r['direction_changes']:3d} | "
                  f"entered: {r['total_entered']:4d} | "
                  f"miss: {r['missing_on_enter']:3d} ({r['miss_rate']*100:5.1f}%) | "
                  f"miss@chg: {r['missing_after_change']:3d}/{r['total_after_change']:4d} "
                  f"({r['miss_rate_after_change']*100:5.1f}%) | "
                  f"late: {r['late_loads']:3d}, avg {r['avg_late_s']:.2f}s, max {r['max_late_s']:.2f}s")

    print("\n" + "=" * 100)
    print("AGGREGATE (5 seeds x 10 min = 50 min each)")
    print("=" * 100)
    labels = ["CURRENT", "GRACEFUL EVICT", "PRIORITY QUEUE"]
    for idx, label in enumerate(labels):
        agg = [s[idx] for _, s in results]
        te = sum(r["total_entered"] for r in agg)
        tm = sum(r["missing_on_enter"] for r in agg)
        ta = sum(r["total_after_change"] for r in agg)
        tma = sum(r["missing_after_change"] for r in agg)
        tc = sum(r["direction_changes"] for r in agg)
        tl = sum(r["late_loads"] for r in agg)
        avg_late = sum(r["avg_late_s"] for r in agg) / len(agg)
        max_late = max(r["max_late_s"] for r in agg)
        print(f"\n  {label}")
        print(f"    Dir changes: {tc} | Tiles entered: {te}")
        print(f"    Missing on enter: {tm} ({tm/max(1,te)*100:.1f}%)")
        print(f"    Missing after change: {tma}/{ta} ({tma/max(1,ta)*100:.1f}%)")
        print(f"    Late loads: {tl}, avg delay: {avg_late:.2f}s, max delay: {max_late:.2f}s")


if __name__ == "__main__":
    main()
