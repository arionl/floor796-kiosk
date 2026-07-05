#!/usr/bin/env python3
"""
kiosk_status.py — CLI tool for querying floor796 kiosk telemetry.

Usage:
  kiosk_status.py                         # one-shot summary
  kiosk_status.py --watch                 # continuous refresh (2s default)
  kiosk_status.py --watch 5               # refresh every 5 seconds
  kiosk_status.py --health                # health metrics with trends
  kiosk_status.py --overlay on|off        # toggle monitor overlay
  kiosk_status.py --overlay-window 30m    # set overlay time window
  kiosk_status.py --heatmap heat.png      # save heatmap PNG
  kiosk_status.py --window 30m            # filter stats to time window
  kiosk_status.py --json                  # raw JSON output

Runs on the Pi (queries 127.0.0.1:8796) or remotely via SSH.
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8796


def fetch(host, port, path, method="GET", data=None):
    """Fetch from the stats server."""
    url = f"http://{host}:{port}{path}"
    if data:
        req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                      method=method)
        req.add_header("Content-Type", "application/json")
    else:
        req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.headers.get("Content-Type", "").startswith("image/"):
                return resp.read()
            return json.loads(resp.read())
    except urllib.error.URLError as e:
        print(f"Error: cannot connect to {url}: {e}", file=sys.stderr)
        sys.exit(1)


def fmt_uptime(seconds):
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.0f}m"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h {m}m"


def fmt_mb(mb):
    if mb is None:
        return "—"
    if mb >= 1024:
        return f"{mb/1024:.2f} GB"
    return f"{mb:.0f} MB"


def fmt_pct(ratio):
    if ratio is None:
        return "—"
    return f"{ratio*100:.0f}%"


def color_code(text, code):
    """ANSI color."""
    return f"\033[{code}m{text}\033[0m"


def print_summary(stats, window_label="all"):
    """Print a formatted one-shot stats summary."""
    print("=" * 60)
    print(f"  FLOOR796 KIOSK STATUS  [{window_label}]")
    print("=" * 60)

    uptime = stats.get("uptime", 0)
    print(f"\n  Uptime:     {fmt_uptime(uptime)}")
    print(f"  Display:    {stats.get('render_w',0)}x{stats.get('render_h',0)} "
          f"({stats.get('scale_mode','native')})")
    print(f"  Overlay:    {'ON' if stats.get('overlay_enabled') else 'off'} "
          f"[{stats.get('overlay_window','all')}]")

    # Performance
    print(f"\n  {'── Performance ──':<40}")
    fps = stats.get("fps", 0)
    fps_avg = stats.get("fps_avg") or fps
    print(f"  FPS:        {fps:.1f}  (avg {fps_avg:.1f})")

    blank = stats.get("blank_ratio", 0)
    blank_avg = stats.get("blank_avg")
    blank_str = fmt_pct(blank)
    if blank_avg is not None:
        blank_str += f"  (avg {fmt_pct(blank_avg)})"
    if blank < 0.20:
        blank_str = color_code(blank_str, "32")  # green
    elif blank < 0.40:
        blank_str = color_code(blank_str, "33")  # yellow
    else:
        blank_str = color_code(blank_str, "31")  # red
    print(f"  Blank:      {blank_str}")

    rss = stats.get("rss_mb")
    print(f"  Memory:     {fmt_mb(rss)}")
    cpu = stats.get("cpu_pct")
    if cpu is not None:
        print(f"  CPU:        {cpu:.0f}%")

    # Tile Cache
    print(f"\n  {'── Tile Cache ──':<40}")
    loaded = stats.get("cache_loaded", 0)
    max_c = stats.get("cache_max", 0)
    pending = stats.get("cache_pending", 0)
    loads = stats.get("cache_total_loads", 0)
    status = "OK" if loaded == max_c and pending == 0 else "LOADING"
    print(f"  Cache:      {loaded}/{max_c}  pending={pending}  "
          f"total_loads={loads}  [{status}]")

    # Wanderer
    print(f"\n  {'── Wanderer ──':<40}")
    px = stats.get("pos_x", 0)
    py = stats.get("pos_y", 0)
    print(f"  Position:   ({px:.0f}, {py:.0f})")
    vx = stats.get("heading_vx", 0)
    vy = stats.get("heading_vy", 0)
    print(f"  Heading:    ({vx:.0f}, {vy:.0f})")
    target = stats.get("current_target")
    if target:
        print(f"  Target:     tile {target}")
    print(f"  Waypoint:   #{stats.get('waypoints_picked', 0)}")

    # Coverage
    print(f"\n  {'── Coverage':<40} [{window_label}]")
    visited = stats.get("tiles_visited", 0)
    total = stats.get("tiles_total", 0)
    fully = stats.get("tiles_fully_viewed", 0)
    pct = f" ({visited*100//total}%)" if total > 0 else ""
    print(f"  Visited:    {visited}/{total}{pct}")
    print(f"  Fully:      {fully}/{total}")
    print(f"  Blank avg:  {fmt_pct(stats.get('blank_avg'))}")
    print(f"  Blank min:  {fmt_pct(stats.get('blank_min'))}")
    print(f"  Blank max:  {fmt_pct(stats.get('blank_max'))}")

    # Coverage grid
    visits = stats.get("visit_counts", {})
    if visits and total > 0:
        print(f"\n  Coverage grid ({window_label}):")
        visit_map = {}
        for key, count in visits.items():
            try:
                r, c = map(int, key.split(","))
                visit_map[(r, c)] = count
            except (ValueError, TypeError):
                pass

        # Determine grid dimensions from visits
        max_r = max((r for r, c in visit_map), default=0)
        max_c = max((c for r, c in visit_map), default=0)

        for r in range(max_r + 1):
            row = "  "
            for c in range(max_c + 1):
                count = visit_map.get((r, c), 0)
                if count > 0:
                    row += color_code("██", "32")
                else:
                    row += "··"
            print(row)

    print()


def print_health(stats):
    """Print health metrics with trends."""
    print("=" * 60)
    print("  FLOOR796 KIOSK — HEALTH (24h trends)")
    print("=" * 60)

    uptime = stats.get("uptime", 0)
    print(f"\n  Uptime: {fmt_uptime(uptime)}")

    rss = stats.get("rss_mb")
    rss_8h = stats.get("rss_8h_ago")
    trend = stats.get("rss_trend_pct")

    print(f"\n  {'── Memory (RSS) ──':<40}")
    print(f"  Current:    {fmt_mb(rss)}")
    print(f"  8h ago:     {fmt_mb(rss_8h)}")
    if trend is not None:
        if trend > 10:
            trend_str = color_code(f"+{trend:.1f}%", "31")
        elif trend < -5:
            trend_str = color_code(f"{trend:.1f}%", "32")
        else:
            trend_str = f"{trend:+.1f}%"
        print(f"  Trend:      {trend_str}")
    print(f"  Range:      {fmt_mb(stats.get('rss_min'))} – {fmt_mb(stats.get('rss_max'))}")

    print(f"\n  {'── CPU ──':<40}")
    cpu = stats.get("cpu_pct")
    cpu_avg = stats.get("cpu_avg")
    if cpu is not None:
        print(f"  Current:    {cpu:.0f}%")
        print(f"  Average:    {cpu_avg:.0f}%" if cpu_avg else "")

    print(f"\n  {'── FPS ──':<40}")
    fps = stats.get("fps", 0)
    fps_avg = stats.get("fps_avg", fps)
    fps_min = stats.get("fps_min")
    fps_max = stats.get("fps_max")
    print(f"  Current:    {fps:.1f}")
    print(f"  Average:    {fps_avg:.1f}" if fps_avg else "")
    if fps_min is not None:
        print(f"  Range:      {fps_min:.1f} – {fps_max:.1f}")

    print()


def main():
    parser = argparse.ArgumentParser(
        description="Floor796 kiosk status and telemetry")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"Stats server host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Stats server port (default: {DEFAULT_PORT})")
    parser.add_argument("--watch", nargs="?", const=2, type=float,
                        metavar="SECS",
                        help="Continuous refresh (default: every 2 seconds)")
    parser.add_argument("--window", default=None,
                        help="Time window: 10m, 30m, 1h, 4h, 8h, all")
    parser.add_argument("--health", action="store_true",
                        help="Show health metrics with trends")
    parser.add_argument("--json", action="store_true",
                        help="Raw JSON output")
    parser.add_argument("--overlay", choices=["on", "off"],
                        help="Toggle on-screen overlay")
    parser.add_argument("--overlay-window", metavar="WINDOW",
                        help="Set overlay time window (10m, 30m, 1h, etc.)")
    parser.add_argument("--heatmap", metavar="PATH",
                        help="Save heatmap PNG to file")
    args = parser.parse_args()

    # Handle commands that don't need --watch
    if args.overlay:
        enabled = args.overlay == "on"
        result = fetch(args.host, args.port, "/overlay", method="POST",
                       data={"enabled": enabled})
        print(f"Overlay: {'ON' if result.get('overlay') else 'OFF'} "
              f"[{result.get('window', 'all')}]")
        return

    if args.overlay_window:
        result = fetch(args.host, args.port, "/overlay/window", method="POST",
                       data={"window": args.overlay_window})
        print(f"Overlay window: {result.get('overlay_window')}")
        return

    if args.heatmap:
        window = args.window or "all"
        png_data = fetch(args.host, args.port,
                         f"/heatmap?window={window}")
        with open(args.heatmap, "wb") as f:
            f.write(png_data)
        print(f"Heatmap saved to {args.heatmap} ({len(png_data)} bytes, "
              f"window={window})")
        return

    # Continuous or one-shot
    window_param = f"?window={args.window}" if args.window else ""

    def do_query():
        if args.health:
            stats = fetch(args.host, args.port, "/health")
            print_health(stats)
        else:
            stats = fetch(args.host, args.port, f"/stats{window_param}")
            if args.json:
                print(json.dumps(stats, indent=2, default=str))
            else:
                wl = args.window or "all"
                print_summary(stats, wl)

    if args.watch:
        try:
            while True:
                os.system("clear" if os.name != "nt" else "cls")
                do_query()
                print(f"  (refreshing every {args.watch}s — Ctrl+C to stop)")
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        do_query()


if __name__ == "__main__":
    main()
