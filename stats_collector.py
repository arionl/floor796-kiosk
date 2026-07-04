#!/usr/bin/env python3
"""
StatsCollector — thread-safe telemetry for the floor796 kiosk player.

Collects operational and health metrics from the running player:

Operational (8-hour windows):
  - Per-tile visit counts in 1-minute ring buckets (exact)
  - Spatial viewport heatmap with exponential decay (5 windows + all-time)
  - Scalar metrics (blank %, etc.) in 1-second ring buffer

Health (24-hour window):
  - RSS memory, CPU %, FPS at 10-second sampling

Memory budget: ~1.8 MB total
"""

import math
import os
import resource
import threading
import time
from collections import deque

import numpy as np

# ── Ring buffer for time-windowed exact data ──────────────────────────────────

class RingBuffer:
    """Fixed-size circular buffer for time-series samples.

    Stores (timestamp, value) pairs and can query aggregate statistics
    over a recent time window.
    """

    def __init__(self, max_samples, sample_interval=1.0):
        self._buf = deque(maxlen=max_samples)
        self._sample_interval = sample_interval
        self._last_sample_time = 0.0

    def maybe_sample(self, value, now=None):
        """Add a sample if enough time has elapsed since the last one."""
        now = now or time.time()
        if now - self._last_sample_time >= self._sample_interval:
            self._buf.append((now, value))
            self._last_sample_time = now

    def sample(self, value, now=None):
        """Force-add a sample."""
        now = now or time.time()
        self._buf.append((now, value))
        self._last_sample_time = now

    def query(self, window_seconds, now=None):
        """Return all samples within the given time window (most recent first).

        Returns list of (timestamp, value).
        """
        now = now or time.time()
        cutoff = now - window_seconds
        return [(t, v) for t, v in self._buf if t >= cutoff]

    def stats(self, window_seconds, now=None):
        """Return (current, min, max, avg) over the time window."""
        samples = self.query(window_seconds, now)
        if not samples:
            return (None, None, None, None)
        values = [v for _, v in samples]
        return (values[-1], min(values), max(values), sum(values) / len(values))

    def trend(self, window_seconds, now=None):
        """Compare current value to the oldest in the window.

        Returns (current, oldest, delta_pct).
        """
        samples = self.query(window_seconds, now)
        if len(samples) < 2:
            cur = samples[-1][1] if samples else None
            return (cur, cur, 0.0)
        current = samples[-1][1]
        oldest = samples[0][1]
        if oldest != 0:
            delta = (current - oldest) / abs(oldest) * 100.0
        else:
            delta = 0.0
        return (current, oldest, delta)

    def __len__(self):
        return len(self._buf)


# ── Per-tile visit ring buckets ───────────────────────────────────────────────

class TileVisitTracker:
    """Tracks per-tile visit counts in 1-minute buckets for exact time windows.

    Memory: 480 buckets × 50 tiles × 4 bytes = 96 KB
    """

    BUCKET_SECONDS = 60.0
    MAX_BUCKETS = 480  # 8 hours

    def __init__(self, num_tiles):
        self._num_tiles = num_tiles
        self._buckets = deque(maxlen=self.MAX_BUCKETS)
        # Each bucket: (bucket_start_time, np.array of visit counts)
        self._current_bucket_start = 0.0
        self._current_counts = None

    def update(self, visit_counts_dict, now=None):
        """Record a snapshot of visit counts for all tiles.

        visit_counts_dict: {tile_rc: count} — we track the delta.
        """
        now = now or time.time()
        bucket_time = int(now // self.BUCKET_SECONDS) * self.BUCKET_SECONDS

        if self._current_bucket_start == 0:
            self._current_bucket_start = bucket_time
            self._current_counts = np.zeros(self._num_tiles, dtype=np.float32)
            self._prev_counts = {k: 0 for k in visit_counts_dict}

        # New bucket?
        if bucket_time != self._current_bucket_start:
            self._buckets.append((self._current_bucket_start,
                                  self._current_counts.copy()))
            self._current_bucket_start = bucket_time
            self._current_counts = np.zeros(self._num_tiles, dtype=np.float32)

        # Accumulate visit deltas into current bucket
        for i, (rc, count) in enumerate(sorted(visit_counts_dict.items())):
            delta = count - self._prev_counts.get(rc, 0)
            if delta > 0:
                self._current_counts[i] += delta
            self._prev_counts[rc] = count

    def flush_current(self, now=None):
        """Push the current bucket into the ring (for querying)."""
        if self._current_counts is not None:
            now = now or time.time()
            bucket_time = int(now // self.BUCKET_SECONDS) * self.BUCKET_SECONDS
            if bucket_time != self._current_bucket_start:
                self._buckets.append((self._current_bucket_start,
                                      self._current_counts.copy()))
                self._current_counts = np.zeros(self._num_tiles,
                                                dtype=np.float32)
                self._current_bucket_start = bucket_time

    def query(self, window_seconds, tile_index_map, now=None):
        """Return {tile_rc: visit_count} for the given time window.

        tile_index_map: {tile_rc: index} matching the order used in update().
        """
        self.flush_current(now)
        now = now or time.time()
        cutoff = now - window_seconds

        result = {rc: 0 for rc in tile_index_map}
        index_to_rc = {v: k for k, v in tile_index_map.items()}
        for bucket_time, counts in self._buckets:
            if bucket_time >= cutoff:
                for i, cnt in enumerate(counts):
                    rc = index_to_rc.get(i)
                    if rc and cnt > 0:
                        result[rc] += int(cnt)
        # Include current (unflushed) bucket
        if self._current_counts is not None and self._current_bucket_start >= cutoff:
            for i, cnt in enumerate(self._current_counts):
                rc = index_to_rc.get(i)
                if rc and cnt > 0:
                    result[rc] += int(cnt)
        return result


# ── Decaying spatial heatmap ──────────────────────────────────────────────────

class DecayHeatmap:
    """Spatial viewport heatmap with exponential decay for multiple windows.

    Each window decays independently: heat *= (1 - dt/window) each frame.
    A 6th grid stores all-time (non-decaying) visits.

    Memory: 6 grids × ~26K cells × 4 bytes = ~624 KB
    """

    WINDOWS = [
        ("10min", 600),
        ("30min", 1800),
        ("1h", 3600),
        ("4h", 14400),
        ("8h", 28800),
    ]

    def __init__(self, max_x, max_y, resolution=50):
        self._res = resolution
        self._gx = int(max_x / resolution) + 1
        self._gy = int(max_y / resolution) + 1
        self._decay_grids = {}
        for name, secs in self.WINDOWS:
            self._decay_grids[name] = np.zeros((self._gy, self._gx),
                                                dtype=np.float32)
        self._all_time = np.zeros((self._gy, self._gx), dtype=np.float32)
        self._last_time = time.time()
        self._max_heat = 1.0  # for normalization

    def update(self, x, y, now=None):
        """Record a viewport position visit."""
        now = now or time.time()
        dt = min(now - self._last_time, 1.0)
        self._last_time = now

        gx = max(0, min(self._gx - 1, int(x / self._res)))
        gy = max(0, min(self._gy - 1, int(y / self._res)))

        # Decay each window grid, then add current position
        for name, secs in self.WINDOWS:
            grid = self._decay_grids[name]
            decay = max(0.0, 1.0 - dt / secs)
            grid *= decay
            grid[gy, gx] += 1.0

        self._all_time[gy, gx] += 1.0
        if self._all_time[gy, gx] > self._max_heat:
            self._max_heat = self._all_time[gy, gx]

    def get_grid(self, window_name):
        """Return the heatmap grid for the named window (or 'all')."""
        if window_name == "all":
            return self._all_time
        return self._decay_grids.get(window_name, self._all_time)

    def get_window_names(self):
        return [name for name, _ in self.WINDOWS] + ["all"]

    @property
    def resolution(self):
        return self._res

    @property
    def shape(self):
        return (self._gy, self._gx)


# ── StatsCollector ────────────────────────────────────────────────────────────

class StatsCollector:
    """Central telemetry collector.  Thread-safe snapshot access.

    The main loop calls update() once per frame.  The HTTP server and
    overlay call snapshot() to get a consistent view of all metrics.
    """

    # Health ring buffer: 24 hours at 10-second intervals
    HEALTH_SAMPLES = 8640  # 24h / 10s
    HEALTH_INTERVAL = 10.0

    # Scalar ring buffer: 8 hours at 1-second intervals
    SCALAR_SAMPLES = 28800  # 8h / 1s
    SCALAR_INTERVAL = 1.0

    def __init__(self, tile_rc_list, map_w, map_h):
        # Sorted tile list for consistent indexing
        self._tiles = sorted(tile_rc_list)
        self._tile_index = {rc: i for i, rc in enumerate(self._tiles)}
        self._lock = threading.Lock()

        # Current frame reference values (updated by main loop)
        self._frame = {}
        self._start_time = time.time()

        # Per-tile visit tracker
        self._tile_visits = TileVisitTracker(len(self._tiles))

        # Decaying spatial heatmap
        self._heatmap = DecayHeatmap(map_w, map_h)

        # Scalar ring buffers (1-second, 8-hour)
        self._blank_buf = RingBuffer(self.SCALAR_SAMPLES, self.SCALAR_INTERVAL)
        self._fps_buf = RingBuffer(self.SCALAR_SAMPLES, self.SCALAR_INTERVAL)

        # Health ring buffers (10-second, 24-hour)
        self._rss_buf = RingBuffer(self.HEALTH_SAMPLES, self.HEALTH_INTERVAL)
        self._cpu_buf = RingBuffer(self.HEALTH_SAMPLES, self.HEALTH_INTERVAL)

        # Overlay toggle (atomic-ish)
        self.overlay_enabled = False
        self.overlay_window = "all"

        # CPU sampling
        self._last_cpu_time = None

    @property
    def start_time(self):
        return self._start_time

    def update(self, frame_data):
        """Called once per frame by the main loop.

        frame_data is a dict with keys:
          x, y, vx, vy, pos_x, pos_y, fps,
          cache_loaded, cache_max, cache_pending, cache_total_loads,
          cache_evictions,
          tiles_visited, tiles_total, tiles_fully_viewed,
          visit_counts, blank_ratio, current_target, waypoints_picked,
          frame_idx, anim_fps, holo_scene,
          render_w, render_h, physical_w, physical_h, scale_mode
        """
        now = time.time()

        with self._lock:
            self._frame = dict(frame_data)

            # Update tile visit ring buffer
            if "visit_counts" in frame_data:
                self._tile_visits.update(frame_data["visit_counts"], now)

            # Update spatial heatmap
            if "x" in frame_data and "y" in frame_data:
                self._heatmap.update(frame_data["x"], frame_data["y"], now)

            # Sample scalar metrics
            if "blank_ratio" in frame_data:
                self._blank_buf.maybe_sample(frame_data["blank_ratio"], now)
            if "fps" in frame_data:
                self._fps_buf.maybe_sample(frame_data["fps"], now)

            # Sample health metrics
            self._sample_health(now)

    def _sample_health(self, now):
        """Sample RSS memory and CPU usage."""
        try:
            # RSS memory (KB on Linux)
            rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # On Linux ru_maxrss is in KB; on macOS it's in bytes
            if rss_kb > 10_000_000:  # likely bytes (macOS)
                rss_mb = rss_kb / 1024.0
            else:
                rss_mb = rss_kb / 1024.0
            self._rss_buf.maybe_sample(rss_mb, now)
        except Exception:
            pass

        # CPU% — approximate from process time delta
        try:
            cpu_time = time.process_time()
            if self._last_cpu_time is not None:
                wall = now - getattr(self, "_last_health_time", now)
                if wall > 0:
                    cpu_pct = (cpu_time - self._last_cpu_time) / wall * 100.0
                    self._cpu_buf.maybe_sample(cpu_pct, now)
            self._last_cpu_time = cpu_time
            self._last_health_time = now
        except Exception:
            pass

    def snapshot(self, window_seconds=None):
        """Return a consistent snapshot of all metrics.

        Args:
          window_seconds: if provided, tile visits and scalar stats
            are computed over this window.  If None, uses all-time.
        """
        with self._lock:
            now = time.time()
            uptime = now - self._start_time
            f = dict(self._frame)

            # Tile visits for the window
            if window_seconds:
                visits = self._tile_visits.query(window_seconds,
                                                  self._tile_index, now)
            else:
                visits = f.get("visit_counts", {}).copy()

            visited_count = sum(1 for v in visits.values() if v > 0)

            # Stringify tuple keys for JSON serialization
            visits_json = {f"{r},{c}": v for (r, c), v in visits.items()}

            # Scalar stats for the window
            if window_seconds:
                blank_stats = self._blank_buf.stats(window_seconds, now)
                fps_stats = self._fps_buf.stats(window_seconds, now)
            else:
                blank_stats = self._blank_buf.stats(uptime, now)
                fps_stats = self._fps_buf.stats(uptime, now)

            # Health stats (always 24h window)
            rss_stats = self._rss_buf.stats(86400, now)
            cpu_stats = self._cpu_buf.stats(86400, now)

            result = {
                # Current values
                "timestamp": now,
                "uptime": uptime,
                "overlay_enabled": self.overlay_enabled,
                "overlay_window": self.overlay_window,

                # Display
                "render_w": f.get("render_w", 0),
                "render_h": f.get("render_h", 0),
                "physical_w": f.get("physical_w", 0),
                "physical_h": f.get("physical_h", 0),
                "scale_mode": f.get("scale_mode", "native"),
                "fps": f.get("fps", 0),
                "fps_avg": fps_stats[3],
                "fps_min": fps_stats[1],
                "fps_max": fps_stats[2],

                # Tile cache
                "cache_loaded": f.get("cache_loaded", 0),
                "cache_max": f.get("cache_max", 0),
                "cache_pending": f.get("cache_pending", 0),
                "cache_total_loads": f.get("cache_total_loads", 0),

                # Wanderer
                "pos_x": f.get("x", 0),
                "pos_y": f.get("y", 0),
                "heading_vx": f.get("vx", 0),
                "heading_vy": f.get("vy", 0),
                "current_target": f.get("current_target", None),
                "waypoints_picked": f.get("waypoints_picked", 0),
                "wander_speed": f.get("wander_speed", 0),

                # Coverage
                "tiles_visited": visited_count,
                "tiles_total": f.get("tiles_total", 0),
                "tiles_fully_viewed": f.get("tiles_fully_viewed", 0),
                "blank_ratio": f.get("blank_ratio", 0),
                "blank_avg": blank_stats[3],
                "blank_min": blank_stats[1],
                "blank_max": blank_stats[2],
                "visit_counts": visits_json,

                # Animation
                "frame_idx": f.get("frame_idx", 0),
                "anim_fps": f.get("anim_fps", 0),
                "holo_scene": f.get("holo_scene", 0),

                # Health (24h window)
                "rss_mb": rss_stats[0],
                "rss_min": rss_stats[1],
                "rss_max": rss_stats[2],
                "rss_avg": rss_stats[3],
                "cpu_pct": cpu_stats[0],
                "cpu_avg": cpu_stats[3],
            }

            # Memory trend over 8h
            rss_trend = self._rss_buf.trend(28800, now)
            if rss_trend[0] is not None and rss_trend[1] is not None:
                result["rss_8h_ago"] = rss_trend[1]
                result["rss_trend_pct"] = rss_trend[2]

            return result

    def get_heatmap(self, window_name="all"):
        """Return (grid, resolution, shape) for the named window."""
        with self._lock:
            return (self._heatmap.get_grid(window_name).copy(),
                    self._heatmap.resolution,
                    self._heatmap.shape)

    def get_heatmap_windows(self):
        return self._heatmap.get_window_names()

    def set_overlay(self, enabled, window=None):
        with self._lock:
            self.overlay_enabled = enabled
            if window:
                self.overlay_window = window

    def cycle_overlay_window(self):
        with self._lock:
            windows = self._heatmap.get_window_names()
            try:
                idx = windows.index(self.overlay_window)
                self.overlay_window = windows[(idx + 1) % len(windows)]
            except ValueError:
                self.overlay_window = windows[0]
            return self.overlay_window
