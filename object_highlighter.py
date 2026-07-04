#!/usr/bin/env python3
"""
ObjectHighlighter — automatically highlights objects from floor796.com's
changelog as the wanderer moves through the map.

Each object has a title, date, optional media link, and one or more
polygon points encoded in the changelog 'p' field.  We compute bounding
boxes in absolute kiosk coordinates and, each frame, select a visible
object near the center of the viewport to highlight.

Display modes:
  - 'inline':  bounding box + label text drawn next to the box
  - 'corner':  bounding box outline + info panel in lower-right corner

Toggled by 'O' key.  Label mode switched by 'L' key.
"""

import json
import logging
import os
import time
import urllib.request
from collections import deque

import pygame

log = logging.getLogger("floor796")

# ── Tunable parameters ───────────────────────────────────────────────────────

HIGHLIGHT_DURATION = 2.0   # seconds an object is shown
PAUSE_DURATION = 2.0       # seconds between objects
RECENT_MEMORY = 60         # don't repeat objects in the last N shown
MIN_BBOX_SIZE = 20         # skip tiny objects (pixels), hard to see
EDGE_MARGIN = 0.15         # fraction of viewport — objects within this
                           # fraction of any edge are deprioritized
CENTER_WEIGHT = 2.0        # how strongly to bias toward center (higher = more)
SIZE_WEIGHT = 0.5          # how strongly to prefer larger objects

CHANGELOG_URL = "https://floor796.com/data/changelog.json"
CHANGELOG_CACHE = "changelog.json"  # local cache filename

# ── Colors ───────────────────────────────────────────────────────────────────

BOX_COLOR = (255, 220, 80)       # bright yellow
BOX_FILL = (255, 220, 80, 35)    # semi-transparent yellow fill
BOX_OUTLINE = 3                   # pixels
LABEL_BG = (15, 15, 20, 220)
LABEL_TEXT = (255, 255, 255)
LABEL_ACCENT = (255, 220, 80)
CORNER_PANEL_BG = (15, 15, 20, 230)
CORNER_PANEL_BORDER = (60, 60, 80)


# ── Data structures ──────────────────────────────────────────────────────────

class MapObject:
    """A single highlightable object with absolute-map bounding box."""

    __slots__ = ('id', 'title', 'date', 'link', 'tiles',
                 'abs_x1', 'abs_y1', 'abs_x2', 'abs_y2',
                 'cx', 'cy', 'width', 'height')

    def __init__(self, obj_id, title, date, link, points, spacing_w, spacing_h):
        """points: list of (tile_ref, local_x, local_y)."""
        self.id = obj_id
        self.title = title
        self.date = date
        self.link = link

        # Compute per-tile bounding boxes, then merge into one absolute bbox
        abs_xs = []
        abs_ys = []
        self.tiles = set()
        for tile_ref, lx, ly in points:
            self.tiles.add(tile_ref)

        # Each point's absolute position depends on the tile it's on.
        # We need tiles_meta to resolve tile_ref -> (row, col).
        # This is done in the factory method below.
        pass

    def set_abs_bbox(self, x1, y1, x2, y2):
        self.abs_x1 = x1
        self.abs_y1 = y1
        self.abs_x2 = x2
        self.abs_y2 = y2
        self.cx = (x1 + x2) / 2
        self.cy = (y1 + y2) / 2
        self.width = x2 - x1
        self.height = y2 - y1


def _parse_position_field(p_str):
    """Parse the 'p' field: 'tileRef,x,y;tileRef,x,y;...'
    Returns list of (tile_ref, local_x, local_y).
    """
    points = []
    if not p_str:
        return points
    for part in p_str.split(';'):
        fields = part.split(',')
        if len(fields) >= 3:
            tile_ref = fields[0]
            try:
                x = int(fields[1])
                y = int(fields[2])
                points.append((tile_ref, x, y))
            except ValueError:
                pass
    return points


def load_objects(tiles_meta, spacing_w=1016, spacing_h=812,
                 cache_path=None, data_dir="."):
    """Load and index all highlightable objects from changelog.json.

    Returns a list of MapObject instances with absolute bounding boxes.
    Only objects on tiles present in tiles_meta are included.
    """
    # Build tile_ref -> (row, col) lookup
    tile_rc = {}
    for tid, info in tiles_meta["tiles"].items():
        tile_rc[tid] = (info["row"], info["col"])

    # Load changelog data
    raw_data = None
    cache_full = os.path.join(data_dir, CHANGELOG_CACHE)

    # Try cache first
    if cache_path:
        try:
            with open(cache_path) as f:
                raw_data = json.load(f)
            log.info("ObjectHighlighter: loaded changelog from cache (%s)",
                     cache_path)
        except (IOError, json.JSONDecodeError):
            pass

    if raw_data is None:
        # Try local cache in data_dir
        try:
            with open(cache_full) as f:
                raw_data = json.load(f)
            log.info("ObjectHighlighter: loaded changelog from %s", cache_full)
        except (IOError, json.JSONDecodeError):
            pass

    if raw_data is None:
        # Download from floor796.com
        try:
            log.info("ObjectHighlighter: downloading changelog from %s",
                     CHANGELOG_URL)
            req = urllib.request.Request(CHANGELOG_URL, headers={
                "User-Agent": "Floor796-Kiosk/1.0"
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw_text = resp.read().decode('utf-8')
            raw_data = json.loads(raw_text)
            # Save cache
            try:
                with open(cache_full, 'w') as f:
                    f.write(raw_text)
                log.info("ObjectHighlighter: cached changelog to %s",
                         cache_full)
            except IOError:
                pass
        except Exception as e:
            log.warning("ObjectHighlighter: failed to load changelog: %s", e)
            return []

    # Parse objects
    objects = []
    skipped = 0
    for item in raw_data:
        p_str = item.get("p", "")
        if not p_str:
            skipped += 1
            continue

        points = _parse_position_field(p_str)
        if not points:
            skipped += 1
            continue

        # Compute absolute bounding box across all points
        abs_xs = []
        abs_ys = []
        valid = False
        for tile_ref, lx, ly in points:
            rc = tile_rc.get(tile_ref)
            if rc is None:
                continue  # tile not in our grid
            row, col = rc
            abs_x = col * spacing_w + lx
            abs_y = row * spacing_h + ly
            abs_xs.append(abs_x)
            abs_ys.append(abs_y)
            valid = True

        if not valid or not abs_xs:
            skipped += 1
            continue

        obj = MapObject.__new__(MapObject)
        obj.id = item["id"]
        obj.title = item.get("t", "")
        obj.date = item.get("d", "")
        obj.link = item.get("l", "")
        obj.tiles = set(p[0] for p in points if tile_rc.get(p[0]))
        obj.set_abs_bbox(min(abs_xs), min(abs_ys),
                         max(abs_xs), max(abs_ys))
        objects.append(obj)

    log.info("ObjectHighlighter: %d objects loaded (%d skipped)",
             len(objects), skipped)
    return objects


# ── Spatial index ────────────────────────────────────────────────────────────

class TileObjectIndex:
    """Spatial index: tile (row,col) -> list of MapObject.

    Lets us quickly find all objects overlapping a given viewport.
    """

    def __init__(self, objects, spacing_w=1016, spacing_h=812):
        self._index = {}  # (row, col) -> [MapObject, ...]
        self._all = objects
        self._spacing_w = spacing_w
        self._spacing_h = spacing_h

        for obj in objects:
            # Determine which tiles this object's bbox overlaps
            r1 = int(obj.abs_y1 // spacing_h)
            r2 = int(obj.abs_y2 // spacing_h)
            c1 = int(obj.abs_x1 // spacing_w)
            c2 = int(obj.abs_x2 // spacing_w)
            for r in range(r1, r2 + 1):
                for c in range(c1, c2 + 1):
                    self._index.setdefault((r, c), []).append(obj)

    def query_viewport(self, vp_x1, vp_y1, vp_x2, vp_y2):
        """Return list of objects whose bbox intersects the viewport."""
        r1 = max(0, int(vp_y1 // self._spacing_h))
        r2 = int(vp_y2 // self._spacing_h)
        c1 = max(0, int(vp_x1 // self._spacing_w))
        c2 = int(vp_x2 // self._spacing_w)

        seen = set()
        results = []
        for r in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                for obj in self._index.get((r, c), []):
                    if obj.id in seen:
                        continue
                    seen.add(obj.id)
                    # Precise intersection check
                    if (obj.abs_x2 >= vp_x1 and obj.abs_x1 <= vp_x2 and
                            obj.abs_y2 >= vp_y1 and obj.abs_y1 <= vp_y2):
                        results.append(obj)
        return results


# ── Highlighter ──────────────────────────────────────────────────────────────

STATE_IDLE = "idle"          # waiting to pick next object
STATE_HIGHLIGHT = "highlight"  # showing an object
STATE_PAUSE = "pause"        # between objects

LABEL_INLINE = "inline"
LABEL_CORNER = "corner"


class ObjectHighlighter:
    """Manages the automatic object highlight cycle."""

    def __init__(self, objects, screen_w, screen_h,
                 spacing_w=1016, spacing_h=812):
        self._index = TileObjectIndex(objects, spacing_w, spacing_h)
        self._all_objects = objects
        self._screen_w = screen_w
        self._screen_h = screen_h

        # State machine
        self.enabled = True
        self.label_mode = LABEL_CORNER
        self._state = STATE_IDLE
        self._timer = 0.0
        self._current_obj = None

        # Track recently shown to avoid repetition
        self._recent = deque(maxlen=RECENT_MEMORY)

        # Fonts (lazily initialized when first render is called)
        self._font_title = None
        self._font_body = None
        self._font_small = None
        self._fonts_ready = False

        # Cached corner panel surface
        self._corner_surf = None
        self._corner_obj_id = None

        # Counters for stats
        self.highlights_shown = 0

    def _init_fonts(self):
        if self._fonts_ready:
            return
        self._font_title = pygame.font.Font(None, 22)
        self._font_body = pygame.font.Font(None, 18)
        self._font_small = pygame.font.Font(None, 15)
        self._fonts_ready = True

    def _select_object(self, vp_x1, vp_y1, vp_x2, vp_y2):
        """Select the best object to highlight in the current viewport.

        Prefers objects near the center of the viewport, with reasonable
        size, that haven't been shown recently.
        """
        candidates = self._index.query_viewport(vp_x1, vp_y1, vp_x2, vp_y2)
        if not candidates:
            return None

        vp_cx = (vp_x1 + vp_x2) / 2
        vp_cy = (vp_y1 + vp_y2) / 2
        vp_w = vp_x2 - vp_x1
        vp_h = vp_y2 - vp_y1

        # Edge-safe zone: objects must be at least EDGE_MARGIN inside
        edge_safe_x1 = vp_x1 + vp_w * EDGE_MARGIN
        edge_safe_y1 = vp_y1 + vp_h * EDGE_MARGIN
        edge_safe_x2 = vp_x2 - vp_w * EDGE_MARGIN
        edge_safe_y2 = vp_y2 - vp_h * EDGE_MARGIN

        best_obj = None
        best_score = -1

        for obj in candidates:
            # Skip too-small objects
            if obj.width < MIN_BBOX_SIZE or obj.height < MIN_BBOX_SIZE:
                continue

            # Skip recently shown
            if obj.id in self._recent:
                continue

            # Skip objects that don't fit in the edge-safe zone at all
            if (obj.abs_x2 < edge_safe_x1 or obj.abs_x1 > edge_safe_x2 or
                    obj.abs_y2 < edge_safe_y1 or obj.abs_y1 > edge_safe_y2):
                # Object is too close to viewport edge — skip to avoid
                # scroll-off risk
                continue

            # Score: distance from viewport center (normalized)
            dx = (obj.cx - vp_cx) / vp_w
            dy = (obj.cy - vp_cy) / vp_h
            dist_sq = dx * dx + dy * dy
            center_score = max(0, 1.0 - dist_sq * CENTER_WEIGHT)

            # Size score: prefer medium-large objects (but not too huge)
            size_norm = min(1.0, (obj.width * obj.height) / (200 * 200))
            size_score = size_norm * SIZE_WEIGHT

            # Edge distance: prefer objects whose bbox is fully inside
            # the safe zone
            margin_left = obj.abs_x1 - vp_x1
            margin_right = vp_x2 - obj.abs_x2
            margin_top = obj.abs_y1 - vp_y1
            margin_bottom = vp_y2 - obj.abs_y2
            min_margin = min(margin_left, margin_right,
                             margin_top, margin_bottom)
            margin_score = min(1.0, min_margin / (vp_w * 0.1))

            score = center_score + size_score + margin_score * 0.3

            if score > best_score:
                best_score = score
                best_obj = obj

        return best_obj

    def update(self, dt, pos_x, pos_y):
        """Advance the state machine.  Called once per frame."""
        if not self.enabled:
            return

        vp_x1 = pos_x
        vp_y1 = pos_y
        vp_x2 = pos_x + self._screen_w
        vp_y2 = pos_y + self._screen_h

        self._timer += dt

        if self._state == STATE_IDLE:
            obj = self._select_object(vp_x1, vp_y1, vp_x2, vp_y2)
            if obj:
                self._current_obj = obj
                self._state = STATE_HIGHLIGHT
                self._timer = 0.0
                self.highlights_shown += 1
            # If no object found, stay in IDLE and try next frame

        elif self._state == STATE_HIGHLIGHT:
            if self._timer >= HIGHLIGHT_DURATION:
                self._recent.append(self._current_obj.id)
                self._current_obj = None
                self._state = STATE_PAUSE
                self._timer = 0.0

        elif self._state == STATE_PAUSE:
            if self._timer >= PAUSE_DURATION:
                self._state = STATE_IDLE
                self._timer = 0.0

    def render(self, screen, pos_x, pos_y):
        """Render the current highlight onto the screen."""
        if not self.enabled or self._current_obj is None:
            return

        self._init_fonts()
        obj = self._current_obj

        # Convert absolute map coords to screen coords
        sx1 = obj.abs_x1 - pos_x
        sy1 = obj.abs_y1 - pos_y
        sx2 = obj.abs_x2 - pos_x
        sy2 = obj.abs_y2 - pos_y
        bw = sx2 - sx1
        bh = sy2 - sy1

        if self.label_mode == LABEL_INLINE:
            self._render_inline(screen, obj, sx1, sy1, sx2, sy2, bw, bh)
        else:
            self._render_corner(screen, obj, sx1, sy1, sx2, sy2, bw, bh)

    def _render_inline(self, screen, obj, sx1, sy1, sx2, sy2, bw, bh):
        """Draw bounding box with label text next to it."""

        # Semi-transparent fill
        fill_surf = pygame.Surface((int(bw), int(bh)), pygame.SRCALPHA)
        fill_surf.fill(BOX_FILL)
        screen.blit(fill_surf, (int(sx1), int(sy1)))

        # Bright outline
        pygame.draw.rect(screen, BOX_COLOR,
                         (int(sx1), int(sy1), int(bw), int(bh)),
                         BOX_OUTLINE)

        # Label text — position above the box if space, else below
        title_surf = self._font_title.render(obj.title, True, LABEL_TEXT)
        tw = title_surf.get_width()
        th = title_surf.get_height()

        # Try to place label above the box
        label_y = int(sy1) - th - 8
        if label_y < 5:
            label_y = int(sy2) + 5  # below instead

        # Center label horizontally on the box
        label_x = int(sx1 + bw / 2 - tw / 2)
        # Clamp to screen
        label_x = max(5, min(self._screen_w - tw - 5, label_x))

        # Label background
        pad = 6
        bg_rect = (label_x - pad, label_y - 3, tw + pad * 2, th + 6)
        bg_surf = pygame.Surface((bg_rect[2], bg_rect[3]), pygame.SRCALPHA)
        bg_surf.fill(LABEL_BG)
        screen.blit(bg_surf, (bg_rect[0], bg_rect[1]))

        # Accent line under title
        pygame.draw.rect(screen, LABEL_ACCENT,
                         (bg_rect[0], bg_rect[1] + bg_rect[3] - 2,
                          bg_rect[2], 2))

        screen.blit(title_surf, (label_x, label_y))

        # Date in small text
        if obj.date:
            date_surf = self._font_small.render(obj.date, True, LABEL_ACCENT)
            date_y = label_y + th + 2
            if date_y + date_surf.get_height() < self._screen_h:
                screen.blit(date_surf, (label_x, date_y))

    def _render_corner(self, screen, obj, sx1, sy1, sx2, sy2, bw, bh):
        """Draw bounding box outline + info panel in lower-right corner."""

        # Bright outline only (no fill — the corner panel has the info)
        pygame.draw.rect(screen, BOX_COLOR,
                         (int(sx1), int(sy1), int(bw), int(bh)),
                         BOX_OUTLINE)

        # Cornered corner accents for visibility
        cl = 8  # corner length
        for cx, cy in [(sx1, sy1), (sx2, sy2)]:
            pass  # main rect already draws it
        # Draw small corner brackets for extra emphasis
        for cx, cy, dx, dy in [
            (sx1, sy1, 1, 1), (sx2, sy1, -1, 1),
            (sx1, sy2, 1, -1), (sx2, sy2, -1, -1)
        ]:
            pygame.draw.line(screen, BOX_COLOR,
                             (int(cx), int(cy)),
                             (int(cx + dx * cl), int(cy)), BOX_OUTLINE)
            pygame.draw.line(screen, BOX_COLOR,
                             (int(cx), int(cy)),
                             (int(cx), int(cy + dy * cl)), BOX_OUTLINE)

        # Info panel in lower-right corner
        self._render_corner_panel(screen, obj)

    def _render_corner_panel(self, screen, obj):
        """Draw the lower-right info panel."""

        # Panel dimensions
        panel_w = min(400, self._screen_w // 3)
        panel_h = 100
        panel_x = self._screen_w - panel_w - 20
        panel_y = self._screen_h - panel_h - 20

        # Title text
        title_surf = self._font_title.render(obj.title, True, LABEL_TEXT)
        date_surf = self._font_small.render(
            f"Added: {obj.date}" if obj.date else "", True, LABEL_ACCENT)
        tile_str = ", ".join(sorted(obj.tiles)) if obj.tiles else ""
        tile_surf = self._font_small.render(
            f"Tile: {tile_str}", True, LABEL_ACCENT)

        # If the title is very long, it might need wrapping — for now
        # truncate
        max_title_w = panel_w - 24
        if title_surf.get_width() > max_title_w:
            # Truncate with ellipsis
            truncated = obj.title
            while truncated and title_surf.get_width() > max_title_w:
                truncated = truncated[:-1]
                title_surf = self._font_title.render(
                    truncated + "...", True, LABEL_TEXT)

        # Draw panel background
        panel_surf = pygame.Surface((panel_w, panel_h), pygame.SRCALPHA)
        panel_surf.fill(CORNER_PANEL_BG)
        pygame.draw.rect(panel_surf, CORNER_PANEL_BORDER,
                         (0, 0, panel_w, panel_h), 1)

        # Left accent bar
        pygame.draw.rect(panel_surf, LABEL_ACCENT, (0, 0, 4, panel_h))

        screen.blit(panel_surf, (panel_x, panel_y))

        # Text
        tx = panel_x + 16
        ty = panel_y + 10
        screen.blit(title_surf, (tx, ty))
        ty += title_surf.get_height() + 4
        screen.blit(date_surf, (tx, ty))
        ty += date_surf.get_height() + 2
        screen.blit(tile_surf, (tx, ty))

        # Progress indicator (how much time is left in the highlight)
        progress = min(1.0, self._timer / HIGHLIGHT_DURATION)
        bar_y = panel_y + panel_h - 8
        bar_w = panel_w - 32
        pygame.draw.rect(screen, (40, 40, 50),
                         (panel_x + 16, bar_y, bar_w, 3))
        pygame.draw.rect(screen, LABEL_ACCENT,
                         (panel_x + 16, bar_y, int(bar_w * progress), 3))

    def cycle_label_mode(self):
        """Switch between inline and corner label modes."""
        if self.label_mode == LABEL_INLINE:
            self.label_mode = LABEL_CORNER
        else:
            self.label_mode = LABEL_INLINE
        # Force corner panel rebuild
        self._corner_obj_id = None
        return self.label_mode

    def get_state(self):
        """Return current state info for stats integration."""
        return {
            "hl_state": self._state,
            "hl_current": (self._current_obj.title
                           if self._current_obj else None),
            "hl_current_id": (self._current_obj.id
                              if self._current_obj else None),
            "hl_shown": self.highlights_shown,
            "hl_label_mode": self.label_mode,
            "hl_enabled": self.enabled,
        }
