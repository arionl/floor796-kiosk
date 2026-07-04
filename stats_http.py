#!/usr/bin/env python3
"""
StatsHTTPServer — lightweight HTTP telemetry endpoint.

Runs in a background thread inside the player process on 127.0.0.1:8796.
Provides JSON snapshots, heatmap PNGs, and overlay toggle control.

Endpoints:
  GET  /stats[?window=30m]     JSON telemetry snapshot
  GET  /health                 JSON health metrics with 24h trends
  GET  /heatmap[?window=1h]    PNG image of viewport visit heatmap
  GET  /coverage[?window=30m]  JSON tile coverage grid
  POST /overlay                {"enabled": true} or {"enabled": false}
  PATCH/POST /overlay/window   {"window": "30min"} cycle overlay time window

Uses only stdlib — no external dependencies.
"""

import io
import json
import struct
import threading
import zlib
from http.server import HTTPServer, BaseHTTPRequestHandler

import numpy as np

# ── PNG encoder (minimal, no Pillow needed) ───────────────────────────────────

def encode_png(grid, cmap="inferno", max_val=None):
    """Encode a 2D float numpy array as a PNG image.

    Uses a simple colormap (inferno-like) and zlib compression.
    Returns PNG bytes.
    """
    h, w = grid.shape
    if max_val is None:
        max_val = float(grid.max())
    if max_val <= 0:
        max_val = 1.0

    # Normalize
    norm = np.clip(grid / max_val, 0, 1)

    # Inferno-like colormap (5 control points, linear interpolation)
    # Maps [0,1] → RGBA
    inferno_stops = [
        (0.00, (0,   0,   4,   0)),    # transparent black
        (0.05, (20,  10,  40,  120)),  # dark purple, semi-transparent
        (0.25, (80,  20,  80,  180)),
        (0.50, (190, 50,  60,  220)),
        (0.75, (240, 140, 40,  240)),
        (1.00, (255, 240, 120, 255)),  # bright yellow
    ]

    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    flat = norm.flatten()
    for i, v in enumerate(flat):
        # Find color stops
        for j in range(len(inferno_stops) - 1):
            s0, c0 = inferno_stops[j]
            s1, c1 = inferno_stops[j + 1]
            if s0 <= v <= s1:
                t = (v - s0) / max(s1 - s0, 1e-9)
                r = int(c0[0] + (c1[0] - c0[0]) * t)
                g = int(c0[1] + (c1[1] - c0[1]) * t)
                b = int(c0[2] + (c1[2] - c0[2]) * t)
                a = int(c0[3] + (c1[3] - c0[3]) * t)
                y, x = divmod(i, w)
                rgba[y, x] = (r, g, b, a)
                break

    # Encode as PNG
    return _encode_rgba_png(rgba)


def _encode_rgba_png(rgba):
    """Encode an (H,W,4) uint8 array as PNG bytes."""
    h, w = rgba.shape[:2]
    raw = rgba.tobytes()

    # PNG row format: each row prefixed with filter byte (0 = none)
    row_size = w * 4
    raw_rows = b""
    for y in range(h):
        raw_rows += b"\x00"  # filter: none
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


# ── HTTP handler ──────────────────────────────────────────────────────────────

def parse_window(w):
    """Parse a window string like '30m', '1h', '4h', '8h' → seconds."""
    if not w or w == "all":
        return None
    w = w.strip().lower()
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if w[-1] in multipliers:
        try:
            return int(w[:-1]) * multipliers[w[-1]]
        except ValueError:
            return None
    try:
        return int(w)
    except ValueError:
        return None


class StatsHandler(BaseHTTPRequestHandler):
    """HTTP request handler for stats queries."""

    def log_message(self, *args):
        pass  # silence default logging

    def _send_json(self, data, code=200):
        body = json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_png(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        collector = self.server.collector

        # Parse path and query
        path = self.path.split("?")[0]
        params = {}
        if "?" in self.path:
            for pair in self.path.split("?")[1].split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    params[k] = v

        if path == "/stats":
            window = parse_window(params.get("window"))
            snap = collector.snapshot(window)
            self._send_json(snap)

        elif path == "/health":
            snap = collector.snapshot()
            health = {
                "uptime": snap["uptime"],
                "rss_mb": snap["rss_mb"],
                "rss_min": snap["rss_min"],
                "rss_max": snap["rss_max"],
                "rss_avg": snap["rss_avg"],
                "rss_8h_ago": snap.get("rss_8h_ago"),
                "rss_trend_pct": snap.get("rss_trend_pct"),
                "cpu_pct": snap["cpu_pct"],
                "cpu_avg": snap["cpu_avg"],
                "fps": snap["fps"],
                "fps_avg": snap["fps_avg"],
                "fps_min": snap["fps_min"],
                "fps_max": snap["fps_max"],
            }
            self._send_json(health)

        elif path == "/heatmap":
            window = params.get("window", "all")
            grid, res, shape = collector.get_heatmap(window)
            png_data = encode_png(grid)
            self._send_png(png_data)

        elif path == "/coverage":
            window = parse_window(params.get("window"))
            snap = collector.snapshot(window)
            visits = snap.get("visit_counts", {})

            # Build coverage grid
            tiles_total = snap.get("tiles_total", 0)
            tiles_visited = snap.get("tiles_visited", 0)
            tiles_fully = snap.get("tiles_fully_viewed", 0)

            # Per-tile detail
            tile_details = {}
            for rc_str, count in visits.items():
                tile_details[str(rc_str)] = count

            self._send_json({
                "tiles_visited": tiles_visited,
                "tiles_total": tiles_total,
                "tiles_fully_viewed": tiles_fully,
                "blank_ratio": snap.get("blank_ratio", 0),
                "blank_avg": snap.get("blank_avg", 0),
                "visits": {str(k): v for k, v in visits.items()},
            })

        elif path == "/windows":
            self._send_json({"windows": collector.get_heatmap_windows()})

        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        collector = self.server.collector
        path = self.path.split("?")[0]

        # Read body
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len > 0 else b""

        if path == "/overlay":
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                self._send_json({"error": "invalid JSON"}, 400)
                return
            enabled = data.get("enabled")
            if enabled is not None:
                collector.set_overlay(bool(enabled),
                                      data.get("window"))
                self._send_json({"overlay": collector.overlay_enabled,
                                 "window": collector.overlay_window})
            else:
                self._send_json({"error": "missing 'enabled'"}, 400)

        elif path == "/overlay/window":
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                self._send_json({"error": "invalid JSON"}, 400)
                return
            # Either cycle or set explicit
            if data.get("cycle"):
                window = collector.cycle_overlay_window()
                self._send_json({"overlay_window": window})
            elif data.get("window"):
                collector.set_overlay(collector.overlay_enabled,
                                      data["window"])
                self._send_json({"overlay_window": collector.overlay_window})
            else:
                self._send_json({"error": "missing 'window' or 'cycle'"}, 400)
        else:
            self._send_json({"error": "not found"}, 404)


def start_stats_server(collector, host="127.0.0.1", port=8796):
    """Start the HTTP stats server in a background thread.

    Returns the server instance (call shutdown() to stop).
    """
    server = HTTPServer((host, port), StatsHandler)
    server.collector = collector
    server.daemon_threads = True

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
