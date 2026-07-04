#!/usr/bin/env python3
"""
StatsOverlay — alpha-blended telemetry overlay for the kiosk display.

Renders a semi-transparent panel with live stats on top of the tile
rendering.  Zero cost when disabled (the main loop skips it entirely).

Toggled by 'S' key or via POST /overlay on the stats HTTP server.
Time window cycled by 'T' key or POST /overlay/window.

Layout (right ~35% of screen):
  ┌─────────────────────────────┐
  │ FLOOR796 KIOSK    [30min]  │
  │ uptime: 2h 15m              │
  │                             │
  │ ── Performance ──           │
  │ FPS:   29.8 (avg 29.6)      │
  │ Blank:  14% (avg 16%)       │
  │ Mem:   2.4 GB               │
  │ CPU:   120%                 │
  │                             │
  │ ── Tile Cache ──            │
  │ Loaded: 15/15  Pending: 0   │
  │ Total loads: 142            │
  │                             │
  │ ── Wanderer ──              │
  │ Pos: (3240, 2868)           │
  │ Heading: → (15, 0)          │
  │ Target: tile (3,4)          │
  │ Waypoint: 3                 │
  │                             │
  │ ── Coverage [30min] ──      │
  │ Visited: 32/50 (64%)        │
  │ Fully:   28/50              │
  │                             │
  │   . . . . .                 │
  │   . V V V .                 │
  │   V V V V .                 │  ← mini coverage grid
  │   V V V V V                 │
  │   . V V V .                 │
  │                             │
  │ [S] toggle  [T] time window │
  └─────────────────────────────┘
"""

import pygame
import time

# ── Colors ────────────────────────────────────────────────────────────────────

PANEL_BG = (10, 10, 14, 215)      # near-opaque dark
PANEL_BORDER = (60, 60, 80)
TEXT_PRIMARY = (220, 220, 230)
TEXT_SECONDARY = (140, 140, 160)
TEXT_ACCENT = (0, 200, 100)
TEXT_WARN = (255, 180, 0)
TEXT_ERROR = (255, 80, 80)
SECTION_HEADER = (100, 160, 255)

BLANK_GOOD = (0, 200, 100)
BLANK_WARN = (255, 180, 0)
BLANK_BAD = (255, 80, 80)


class StatsOverlay:
    """Alpha-blended stats overlay rendered on the pygame screen.

    Performance: the panel is rebuilt at most every 500ms (2 Hz) and the
    cached surface is blitted in between, so the per-frame cost when the
    overlay is on is a single blit rather than dozens of font.render() calls.
    """

    REBUILD_INTERVAL = 0.5  # seconds between full panel rebuilds

    def __init__(self, screen_w, screen_h, grid_rows, grid_cols):
        self.screen_w = screen_w
        self.screen_h = screen_h
        self.grid_rows = grid_rows
        self.grid_cols = grid_cols

        # Panel dimensions (right side of screen)
        self.panel_w = min(420, screen_w // 3)
        self.panel_h = screen_h
        self.panel_x = screen_w - self.panel_w
        self.panel_y = 0

        # Fonts (created lazily — caller must ensure pygame.font is init'd)
        self._font_title = pygame.font.Font(None, 24)
        self._font_section = pygame.font.Font(None, 19)
        self._font_data = pygame.font.Font(None, 18)
        self._font_grid = pygame.font.Font(None, 14)
        self._font_hint = pygame.font.Font(None, 15)

        # Throttle: cache the rendered panel and only rebuild periodically.
        self._cached_panel = None       # pygame.Surface or None
        self._last_rebuild = 0.0        # monotonic timestamp
        self._last_window = None        # detect window change to force rebuild

        # Cache for grid rendering
        self._grid_tile_w = 0
        self._grid_tile_h = 0

    def _blank_color(self, blank_ratio):
        if blank_ratio < 0.20:
            return BLANK_GOOD
        elif blank_ratio < 0.40:
            return BLANK_WARN
        return BLANK_BAD

    def _heading_arrow(self, vx, vy):
        """Simple ASCII arrow representation for wanderer heading."""
        speed = (vx ** 2 + vy ** 2) ** 0.5
        if speed < 0.5:
            return "*"
        import math
        angle = math.atan2(vy, vx)
        # 8-direction arrows (ASCII-safe)
        deg = math.degrees(angle)
        if -22.5 <= deg < 22.5:
            return "->"
        elif 22.5 <= deg < 67.5:
            return "v"
        elif 67.5 <= deg < 112.5:
            return "v"
        elif 112.5 <= deg < 157.5:
            return "<-"
        elif deg >= 157.5 or deg < -157.5:
            return "<-"
        elif -157.5 <= deg < -112.5:
            return "<-"
        elif -112.5 <= deg < -67.5:
            return "^"
        else:
            return "^"

    def _format_uptime(self, seconds):
        if seconds < 60:
            return f"{seconds:.0f}s"
        if seconds < 3600:
            return f"{seconds/60:.0f}m"
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h {m}m"

    def _format_mb(self, mb):
        if mb is None:
            return "n/a"
        if mb >= 1024:
            return f"{mb/1024:.1f} GB"
        return f"{mb:.0f} MB"

    def render(self, screen, snapshot):
        """Render the overlay onto the screen surface.

        The panel content is rebuilt at most every REBUILD_INTERVAL seconds;
        in between, the cached panel is blitted directly (cheap).

        Args:
          screen: the main pygame Surface (will be blitted onto)
          snapshot: dict from StatsCollector.snapshot()
        """
        now = time.monotonic()
        cur_window = snapshot.get("overlay_window", "all")

        # Force a rebuild if the window changed (so 'T' is responsive)
        force = cur_window != self._last_window

        if force or self._cached_panel is None or \
           (now - self._last_rebuild) >= self.REBUILD_INTERVAL:
            self._build_panel(snapshot)
            self._last_rebuild = now
            self._last_window = cur_window

        screen.blit(self._cached_panel, (self.panel_x, self.panel_y))

    def _build_panel(self, snapshot):
        """Rebuild the full panel content onto self._cached_panel."""
        if self._cached_panel is None:
            self._cached_panel = pygame.Surface(
                (self.panel_w, self.panel_h), pygame.SRCALPHA)
        self._cached_panel.fill((0, 0, 0, 0))

        # Draw panel background
        pygame.draw.rect(self._cached_panel, PANEL_BG,
                         (0, 0, self.panel_w, self.panel_h))
        pygame.draw.rect(self._cached_panel, PANEL_BORDER,
                         (0, 0, self.panel_w, self.panel_h), 1)

        y = 12
        x = 14

        # Title
        title = self._font_title.render("FLOOR796 KIOSK", True, TEXT_PRIMARY)
        self._cached_panel.blit(title, (x, y))
        y += title.get_height() + 2

        window_label = snapshot.get("overlay_window", "all")
        win_surf = self._font_hint.render(f"[{window_label}]", True, TEXT_ACCENT)
        self._cached_panel.blit(win_surf, (x, y))
        y += win_surf.get_height() + 4

        uptime = self._format_uptime(snapshot.get("uptime", 0))
        up_surf = self._font_data.render(f"uptime: {uptime}", True, TEXT_SECONDARY)
        self._cached_panel.blit(up_surf, (x, y))
        y += up_surf.get_height() + 8

        # Performance
        y = self._draw_section("Performance", x, y)
        fps = snapshot.get("fps", 0)
        fps_avg = snapshot.get("fps_avg") or fps
        self._draw_row(x, y, "FPS", f"{fps:.1f}", f"avg {fps_avg:.1f}")
        y += 18

        blank = snapshot.get("blank_ratio", 0)
        blank_avg = snapshot.get("blank_avg")
        blank_str = f"{blank*100:.0f}%"
        if blank_avg is not None:
            blank_str += f"  avg {blank_avg*100:.0f}%"
        self._draw_row(x, y, "Blank", blank_str,
                       color=self._blank_color(blank))
        y += 18

        rss = snapshot.get("rss_mb")
        self._draw_row(x, y, "Memory", self._format_mb(rss))
        y += 18

        cpu = snapshot.get("cpu_pct")
        if cpu is not None:
            cpu_color = TEXT_WARN if cpu > 200 else TEXT_PRIMARY
            self._draw_row(x, y, "CPU", f"{cpu:.0f}%", color=cpu_color)
        else:
            self._draw_row(x, y, "CPU", "n/a")
        y += 18 + 8

        # Tile Cache
        y = self._draw_section("Tile Cache", x, y)
        loaded = snapshot.get("cache_loaded", 0)
        max_c = snapshot.get("cache_max", 0)
        pending = snapshot.get("cache_pending", 0)
        total_loads = snapshot.get("cache_total_loads", 0)
        cache_color = TEXT_ACCENT if loaded == max_c and pending == 0 else TEXT_WARN
        self._draw_row(x, y, "Loaded", f"{loaded}/{max_c}",
                       f"pending: {pending}", color=cache_color)
        y += 18
        self._draw_row(x, y, "Loads", f"{total_loads}")
        y += 18 + 8

        # Wanderer
        y = self._draw_section("Wanderer", x, y)
        px = snapshot.get("pos_x", 0)
        py = snapshot.get("pos_y", 0)
        self._draw_row(x, y, "Pos", f"({px:.0f}, {py:.0f})")
        y += 18

        vx = snapshot.get("heading_vx", 0)
        vy = snapshot.get("heading_vy", 0)
        arrow = self._heading_arrow(vx, vy)
        self._draw_row(x, y, "Heading", f"{arrow} ({vx:.0f}, {vy:.0f})")
        y += 18

        target = snapshot.get("current_target")
        target_str = f"({target[0]},{target[1]})" if target else "n/a"
        self._draw_row(x, y, "Target", target_str)
        y += 18

        wpt = snapshot.get("waypoints_picked", 0)
        self._draw_row(x, y, "Waypoint", f"#{wpt}")
        y += 18 + 8

        # Coverage
        win_tag = f" [{window_label}]"
        y = self._draw_section(f"Coverage{win_tag}", x, y)
        visited = snapshot.get("tiles_visited", 0)
        total = snapshot.get("tiles_total", 0)
        pct = f" ({visited*100//total}%)" if total > 0 else ""
        self._draw_row(x, y, "Visited", f"{visited}/{total}{pct}")
        y += 18

        fully = snapshot.get("tiles_fully_viewed", 0)
        self._draw_row(x, y, "Fully", f"{fully}/{total}")
        y += 18 + 6

        # Mini coverage grid
        visits = snapshot.get("visit_counts", {})
        grid_area_h = self.panel_h - y - 30
        if grid_area_h > 40 and total > 0:
            self._draw_coverage_grid(x, y, visits)
            y += min(grid_area_h, 150)

        # Hints
        y = self.panel_h - 22
        hint = self._font_hint.render("[S] toggle   [T] time window",
                                       True, TEXT_SECONDARY)
        self._cached_panel.blit(hint, (x, y))

    def _draw_section(self, label, x, y):
        hdr = self._font_section.render(f"== {label} ==", True, SECTION_HEADER)
        self._cached_panel.blit(hdr, (x, y))
        return y + hdr.get_height() + 3

    def _draw_row(self, x, y, label, value, detail="", color=None):
        lbl = self._font_data.render(f"{label}:", True, TEXT_SECONDARY)
        self._cached_panel.blit(lbl, (x, y))

        val_color = color or TEXT_PRIMARY
        val = self._font_data.render(value, True, val_color)
        self._cached_panel.blit(val, (x + 65, y))

        if detail:
            det = self._font_data.render(detail, True, TEXT_SECONDARY)
            self._cached_panel.blit(det, (x + 65 + val.get_width() + 10, y))

    def _draw_coverage_grid(self, x, y, visits):
        """Draw a mini coverage grid.

        visits: dict of "r,c" -> visit count
        """
        # Parse visits
        visit_map = {}
        for key, count in visits.items():
            try:
                r, c = map(int, key.split(","))
                visit_map[(r, c)] = count
            except (ValueError, AttributeError):
                # Already a tuple? (shouldn't be after JSON, but just in case)
                try:
                    r, c = key
                    visit_map[(r, c)] = count
                except Exception:
                    pass

        cell_w = (self.panel_w - 28) // self.grid_cols
        cell_h = 12
        for r in range(self.grid_rows):
            for c in range(self.grid_cols):
                cx = x + c * cell_w
                cy = y + r * cell_h
                count = visit_map.get((r, c), 0)
                if count > 0:
                    # Color intensity by visit count
                    intensity = min(1.0, count / 10.0)
                    green = int(100 + 155 * intensity)
                    color = (0, green, 50)
                    pygame.draw.rect(self._cached_panel, color,
                                     (cx, cy, cell_w - 1, cell_h - 1))
                else:
                    pygame.draw.rect(self._cached_panel, (30, 30, 40),
                                     (cx, cy, cell_w - 1, cell_h - 1))
