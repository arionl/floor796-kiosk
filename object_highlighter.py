#!/usr/bin/env python3
"""
ObjectHighlighter — automatically highlights objects from floor796.com's
changelog as the wanderer moves through the map.

Each object has a title, date, optional media link, and one or more
polygon points encoded in the changelog 'p' field.  Polygon points are
grouped per-tile to produce per-tile bounding boxes — multi-tile objects
get one segment per tile, and the segment closest to the viewport center
is highlighted.

Display modes:
  - 'inline':  bounding box + label text drawn next to the box
  - 'corner':  bounding box outline + info panel in lower-right corner

Toggled by 'O' key.  Label mode switched by 'L' key.
"""

import json
import logging
import math
import os
import time
import urllib.request
from collections import deque

import pygame

from thumbnail_cache import ThumbnailCache, classify_link

log = logging.getLogger("floor796")

# ── Tunable parameters ───────────────────────────────────────────────────────

HIGHLIGHT_DURATION = 10.0  # seconds an object is shown
PAUSE_DURATION = 2.0       # seconds between objects
RECENT_MEMORY = 60         # don't repeat objects in the last N shown
MIN_BBOX_SIZE = 15         # skip tiny objects (pixels), hard to see
EDGE_MARGIN = 0.15         # fraction of viewport — objects within this
                           # fraction of any edge are deprioritized

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

# Pulse animation for drawing attention to the highlight box.
# For the first PULSE_DURATION seconds of each highlight, the border
# pulses with a glow effect — expanding/halos and intensity oscillation.
# After that it settles into a steady outline so it's not distracting.
PULSE_DURATION = 1.8             # seconds of pulsing at start
PULSE_SPEED = 5.0                # Hz — oscillations per second
PULSE_GLOW_MAX = 8               # max glow radius in pixels
PULSE_INTENSITY_MIN = 0.35       # brightness floor (0=dim, 1=full)
PULSE_BOX_ALPHA_MAX = 180        # peak glow surface alpha

# ── Thumbnail panel layout ───────────────────────────────────────────────────
# The corner panel becomes vertical to accommodate a thumbnail image.
# When a thumbnail is present, the panel is taller.  When no thumbnail
# (YouTube-only links still get one via mqdefault), the panel is compact.

THUMB_W = 320             # thumbnail width (matches ThumbnailCache output)
THUMB_H = 200             # thumbnail height
PANEL_MARGIN = 20         # gap from screen edge
PANEL_PADDING = 16        # inner padding
PANEL_BORDER_RADIUS = 0   # square corners (pygame default)

# Panel widths
PANEL_W = THUMB_W + PANEL_PADDING * 2  # 352px
PANEL_W_NO_THUMB = 360                 # compact panel when no image

# Panel heights
PANEL_H_TITLE_BAR = 34   # title line + date line
PANEL_H_THUMB = THUMB_H + 10   # image + small gap
PANEL_H_FOOTER = 28      # link type + progress bar
PANEL_H_WITH_THUMB = (PANEL_H_TITLE_BAR + PANEL_H_THUMB +
                      PANEL_H_FOOTER + PANEL_PADDING)   # ~288
PANEL_H_NO_THUMB = (PANEL_H_TITLE_BAR + PANEL_H_FOOTER +
                    PANEL_PADDING * 2)                   # ~78

# Link type display metadata: (label, color)
LINK_TYPE_META = {
    "youtube":     ("\u25b6 YouTube",       (255, 70, 70)),    # red
    "image":       ("\u25a0 Image",         (100, 200, 100)),  # green
    "video":       ("\u25a0 Video",         (255, 160, 60)),   # orange
    "wiki":        ("\u25a0 Wikipedia",     (80, 160, 240)),   # blue
    "web":         ("\u25a0 Web",           (160, 160, 160)),  # gray
    "interactive": ("\u25a0 Interactive",   (180, 130, 220)),  # purple
    "none":        ("",                     (100, 100, 100)),  # dim
}

# Placeholder animation for loading thumbnails
PLACEHOLDER_PULSE_SPEED = 2.0  # Hz


# ── Data structures ──────────────────────────────────────────────────────────

class ObjectSegment:
    """A per-tile bounding box for one object.

    An object that appears on multiple tiles produces multiple segments.
    Each segment has its own absolute-map bounding box and knows which
    parent object it belongs to.
    """

    __slots__ = ('obj_id', 'title', 'date', 'link', 'tile_ref',
                 'abs_x1', 'abs_y1', 'abs_x2', 'abs_y2',
                 'cx', 'cy', 'width', 'height')

    def __init__(self, obj_id, title, date, link, tile_ref,
                 abs_x1, abs_y1, abs_x2, abs_y2):
        self.obj_id = obj_id
        self.title = title
        self.date = date
        self.link = link
        self.tile_ref = tile_ref
        self.abs_x1 = abs_x1
        self.abs_y1 = abs_y1
        self.abs_x2 = abs_x2
        self.abs_y2 = abs_y2
        self.cx = (abs_x1 + abs_x2) / 2
        self.cy = (abs_y1 + abs_y2) / 2
        self.width = abs_x2 - abs_x1
        self.height = abs_y2 - abs_y1


def _parse_position_field(p_str):
    """Parse the 'p' field: 'tileRef,Y,X;tileRef,Y,X;...'

    NOTE: The floor796.com data format is tileRef,Y,X (Y first, X second),
    confirmed by the website's JS (parsePositionCode assigns i[2] to Y
    axis and i[3] to X axis) and by field range analysis (field 2 max=811
    which fits tile height 820; field 3 max=1015 which fits tile width 1024).

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
                y = int(fields[1])  # first number = Y
                x = int(fields[2])  # second number = X
                points.append((tile_ref, x, y))
            except ValueError:
                pass
    return points


def _trace_polygon_per_tile(abs_verts, spacing_w, spacing_h, tile_rc):
    """Trace a polygon's edges and collect per-tile bounding boxes.

    abs_verts is a list of (tile_ref, abs_x, abs_y) tuples in polygon
    order.  The polygon is traced by connecting consecutive vertices
    (and the last back to the first), sampling points along each edge.

    This correctly handles objects that span multiple tiles where edges
    between vertices in different tiles pass through intermediate tile
    areas — without tracing, those areas would be missing from the
    per-tile bounding boxes.

    tile_rc is the full {tile_ref: (row, col)} lookup from tiles_meta,
    used to assign sampled edge points to the correct tile.

    Returns: { tile_ref: (ax_min, ay_min, ax_max, ay_max) }
    """
    # Build (row, col) -> tile_ref lookup from tiles_meta
    rc_to_tile = {}
    for tile_ref, (row, col) in tile_rc.items():
        rc_to_tile[(row, col)] = tile_ref

    per_tile_points = {}  # tile_ref -> [(ax, ay), ...]

    def add_point(ax, ay):
        r = int(ay // spacing_h)
        c = int(ax // spacing_w)
        tile_ref = rc_to_tile.get((r, c))
        if tile_ref is None:
            return  # tile not in our grid
        per_tile_points.setdefault(tile_ref, []).append((ax, ay))

    # Add vertex points themselves
    for tile_ref, ax, ay in abs_verts:
        add_point(ax, ay)

    # Trace edges between consecutive vertices
    n = len(abs_verts)
    if n >= 2:
        for i in range(n):
            _, x1, y1 = abs_verts[i]
            _, x2, y2 = abs_verts[(i + 1) % n]

            dx = x2 - x1
            dy = y2 - y1
            length = math.sqrt(dx * dx + dy * dy)

            if length < 1:
                continue

            # Sample step: small enough to catch tiles edges pass through
            step = min(spacing_w, spacing_h) / 4  # ~250px
            n_samples = max(2, int(length / step))

            for t in range(1, n_samples):  # skip endpoints (already added)
                frac = t / n_samples
                px = x1 + frac * dx
                py = y1 + frac * dy
                add_point(px, py)

    # Compute bounding box per tile
    result = {}
    for tile_ref, pts in per_tile_points.items():
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        result[tile_ref] = (min(xs), min(ys), max(xs), max(ys))

    return result


def load_objects(tiles_meta, spacing_w=1016, spacing_h=812,
                 cache_path=None, data_dir="."):
    """Load and index all highlightable objects from changelog.json.

    Returns a list of ObjectSegment instances (one per tile per object).
    Only objects on tiles present in tiles_meta are included.
    """
    # Build tile_ref -> (row, col) lookup
    tile_rc = {}
    for tid, info in tiles_meta["tiles"].items():
        tile_rc[tid] = (info["row"], info["col"])

    # Load changelog data
    raw_data = None
    cache_full = os.path.join(data_dir, CHANGELOG_CACHE)

    # Try explicit cache path first
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

    # Parse objects into per-tile segments
    segments = []
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

        # Convert all vertices to absolute coordinates, keeping only
        # those whose tile_ref is in our grid.
        abs_verts = []
        for tile_ref, lx, ly in points:
            rc = tile_rc.get(tile_ref)
            if rc is None:
                continue
            row, col = rc
            abs_verts.append((tile_ref, col * spacing_w + lx,
                              row * spacing_h + ly))

        if not abs_verts:
            skipped += 1
            continue

        obj_id = item["id"]
        title = item.get("t", "")
        date = item.get("d", "")
        link = item.get("l", "")

        # Trace polygon edges and collect all points per-tile.
        # For single-tile objects (most common), this is equivalent to
        # the old approach.  For multi-tile objects like Godzilla, the
        # edges between vertices in different tiles cross through the
        # intermediate tiles, and we need to sample those edges to get
        # accurate per-tile bounding boxes.
        per_tile_points = _trace_polygon_per_tile(
            abs_verts, spacing_w, spacing_h, tile_rc)

        for tile_ref, (ax_min, ay_min, ax_max, ay_max) in per_tile_points.items():
            seg = ObjectSegment(
                obj_id, title, date, link, tile_ref,
                ax_min, ay_min, ax_max, ay_max)
            segments.append(seg)

    log.info("ObjectHighlighter: %d segments from %d objects (%d skipped)",
             len(segments), len(raw_data) - skipped, skipped)
    return segments


# ── Spatial index ────────────────────────────────────────────────────────────

class TileObjectIndex:
    """Spatial index: tile (row,col) -> list of ObjectSegment.

    Lets us quickly find all segments overlapping a given viewport.
    """

    def __init__(self, segments, spacing_w=1016, spacing_h=812):
        self._index = {}  # (row, col) -> [ObjectSegment, ...]
        self._spacing_w = spacing_w
        self._spacing_h = spacing_h

        for seg in segments:
            r1 = int(seg.abs_y1 // spacing_h)
            r2 = int(seg.abs_y2 // spacing_h)
            c1 = int(seg.abs_x1 // spacing_w)
            c2 = int(seg.abs_x2 // spacing_w)
            for r in range(r1, r2 + 1):
                for c in range(c1, c2 + 1):
                    self._index.setdefault((r, c), []).append(seg)

    def query_viewport(self, vp_x1, vp_y1, vp_x2, vp_y2):
        """Return list of segments whose bbox intersects the viewport."""
        r1 = max(0, int(vp_y1 // self._spacing_h))
        r2 = int(vp_y2 // self._spacing_h)
        c1 = max(0, int(vp_x1 // self._spacing_w))
        c2 = int(vp_x2 // self._spacing_w)

        seen = set()
        results = []
        for r in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                for seg in self._index.get((r, c), []):
                    key = (seg.obj_id, seg.tile_ref)
                    if key in seen:
                        continue
                    seen.add(key)
                    if (seg.abs_x2 >= vp_x1 and seg.abs_x1 <= vp_x2 and
                            seg.abs_y2 >= vp_y1 and seg.abs_y1 <= vp_y2):
                        results.append(seg)
        return results


# ── Highlighter ──────────────────────────────────────────────────────────────

STATE_IDLE = "idle"          # waiting to pick next object
STATE_HIGHLIGHT = "highlight"  # showing an object
STATE_PAUSE = "pause"        # between objects

LABEL_INLINE = "inline"
LABEL_CORNER = "corner"


class ObjectHighlighter:
    """Manages the automatic object highlight cycle."""

    def __init__(self, segments, screen_w, screen_h,
                 spacing_w=1016, spacing_h=812):
        self._index = TileObjectIndex(segments, spacing_w, spacing_h)
        self._screen_w = screen_w
        self._screen_h = screen_h

        # State machine
        self.enabled = True
        self.label_mode = LABEL_CORNER
        self._state = STATE_IDLE
        self._timer = 0.0
        self._current_seg = None

        # Track recently shown object IDs to avoid repetition
        self._recent = deque(maxlen=RECENT_MEMORY)

        # Thumbnail cache
        self._thumbs = ThumbnailCache()

        # Fonts (lazily initialized when first render is called)
        self._font_title = None
        self._font_body = None
        self._font_small = None
        self._font_link = None
        self._fonts_ready = False

        # Counters for stats
        self.highlights_shown = 0

    def _init_fonts(self):
        if self._fonts_ready:
            return
        self._font_title = pygame.font.Font(None, 22)
        self._font_body = pygame.font.Font(None, 18)
        self._font_small = pygame.font.Font(None, 16)
        self._font_link = pygame.font.Font(None, 15)
        self._fonts_ready = True

    def _select_segment(self, vp_x1, vp_y1, vp_x2, vp_y2):
        """Select the best segment to highlight in the current viewport.

        Biases toward segments near the center of the viewport.  Objects
        are treated equally regardless of size.  Segments near the edge
        (within EDGE_MARGIN) are skipped to prevent scroll-off.
        """
        candidates = self._index.query_viewport(vp_x1, vp_y1, vp_x2, vp_y2)
        if not candidates:
            return None

        vp_cx = (vp_x1 + vp_x2) / 2
        vp_cy = (vp_y1 + vp_y2) / 2
        vp_w = vp_x2 - vp_x1
        vp_h = vp_y2 - vp_y1

        # Edge-safe zone: segments must be at least EDGE_MARGIN inside
        edge_safe_x1 = vp_x1 + vp_w * EDGE_MARGIN
        edge_safe_y1 = vp_y1 + vp_h * EDGE_MARGIN
        edge_safe_x2 = vp_x2 - vp_w * EDGE_MARGIN
        edge_safe_y2 = vp_y2 - vp_h * EDGE_MARGIN

        best_seg = None
        best_score = -1

        for seg in candidates:
            # Skip too-small segments
            if seg.width < MIN_BBOX_SIZE and seg.height < MIN_BBOX_SIZE:
                continue

            # Skip recently shown (by object ID, not segment)
            if seg.obj_id in self._recent:
                continue

            # Skip segments that aren't within the edge-safe zone
            if (seg.abs_x2 < edge_safe_x1 or seg.abs_x1 > edge_safe_x2 or
                    seg.abs_y2 < edge_safe_y1 or seg.abs_y1 > edge_safe_y2):
                continue

            # Score purely by distance from viewport center
            dx = (seg.cx - vp_cx) / vp_w
            dy = (seg.cy - vp_cy) / vp_h
            dist_sq = dx * dx + dy * dy
            score = 1.0 - dist_sq

            if score > best_score:
                best_score = score
                best_seg = seg

        return best_seg

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
            seg = self._select_segment(vp_x1, vp_y1, vp_x2, vp_y2)
            if seg:
                self._current_seg = seg
                self._state = STATE_HIGHLIGHT
                self._timer = 0.0
                self.highlights_shown += 1
                # Prefetch thumbnail for the new object
                self._thumbs.get(seg.obj_id, seg.link)

        elif self._state == STATE_HIGHLIGHT:
            if self._timer >= HIGHLIGHT_DURATION:
                self._recent.append(self._current_seg.obj_id)
                self._current_seg = None
                self._state = STATE_PAUSE
                self._timer = 0.0

        elif self._state == STATE_PAUSE:
            if self._timer >= PAUSE_DURATION:
                self._state = STATE_IDLE
                self._timer = 0.0

    def render(self, screen, pos_x, pos_y):
        """Render the current highlight onto the screen."""
        if not self.enabled or self._current_seg is None:
            return

        self._init_fonts()
        seg = self._current_seg

        # Convert absolute map coords to screen coords
        sx1 = seg.abs_x1 - pos_x
        sy1 = seg.abs_y1 - pos_y
        sx2 = seg.abs_x2 - pos_x
        sy2 = seg.abs_y2 - pos_y
        bw = sx2 - sx1
        bh = sy2 - sy1

        if self.label_mode == LABEL_INLINE:
            self._render_inline(screen, seg, sx1, sy1, sx2, sy2, bw, bh)
        else:
            self._render_corner(screen, seg, sx1, sy1, sx2, sy2, bw, bh)

    def _pulse_envelope(self):
        """Return (intensity, glow_px) for the current timer position.

        During the first PULSE_DURATION seconds the box pulses to draw
        attention.  After that it settles to a steady outline.  intensity
        is 0..1 (how bright the inner box is), glow_px is how far the
        surrounding glow extends.
        """
        t = self._timer
        if t >= PULSE_DURATION:
            return 1.0, 0
        # Envelope: starts at peak, decays linearly over PULSE_DURATION
        env = 1.0 - (t / PULSE_DURATION)  # 1.0 → 0.0
        # Oscillation: 0..1 sinusoidal at PULSE_SPEED Hz
        osc = (math.sin(t * PULSE_SPEED * 2 * math.pi) + 1) / 2
        # Combined intensity never drops below PULSE_INTENSITY_MIN
        intensity = PULSE_INTENSITY_MIN + (1.0 - PULSE_INTENSITY_MIN) * (
            env * osc + (1 - env))
        glow_px = int(PULSE_GLOW_MAX * env * (0.5 + 0.5 * osc))
        return intensity, glow_px

    def _draw_pulse_glow(self, screen, sx1, sy1, sx2, sy2, intensity, glow_px):
        """Draw expanding glow halos around the highlight box during pulse."""
        if glow_px <= 0:
            return
        bw = sx2 - sx1
        bh = sy2 - sy1
        # Draw 2-3 concentric expanding outlines at decreasing alpha
        for i in range(glow_px, 0, -2):
            alpha = int(PULSE_BOX_ALPHA_MAX * intensity *
                        (1 - i / (glow_px + 1)) ** 2)
            if alpha < 8:
                continue
            pad = i
            gw = int(bw + pad * 2)
            gh = int(bh + pad * 2)
            if gw <= 0 or gh <= 0:
                continue
            glow_surf = pygame.Surface((gw, gh), pygame.SRCALPHA)
            pygame.draw.rect(glow_surf, (*BOX_COLOR, alpha),
                             (0, 0, gw, gh), 2)
            screen.blit(glow_surf, (int(sx1 - pad), int(sy1 - pad)))

    def _box_color_at(self, intensity):
        """Return BOX_COLOR scaled by intensity (toward black)."""
        return (
            int(BOX_COLOR[0] * intensity),
            int(BOX_COLOR[1] * intensity),
            int(BOX_COLOR[2] * intensity),
        )

    def _render_inline(self, screen, seg, sx1, sy1, sx2, sy2, bw, bh):
        """Draw bounding box with label text next to it."""

        intensity, glow_px = self._pulse_envelope()
        box_color = self._box_color_at(intensity)

        # Expanding glow halos during pulse phase
        self._draw_pulse_glow(screen, sx1, sy1, sx2, sy2, intensity, glow_px)

        # Semi-transparent fill
        fill_surf = pygame.Surface((max(1, int(bw)), max(1, int(bh))),
                                    pygame.SRCALPHA)
        fill_alpha = int(BOX_FILL[3] * intensity)
        fill_surf.fill((*BOX_COLOR[:3], fill_alpha))
        screen.blit(fill_surf, (int(sx1), int(sy1)))

        # Bright outline
        pygame.draw.rect(screen, box_color,
                         (int(sx1), int(sy1), int(bw), int(bh)),
                         BOX_OUTLINE)

        # Label text — position above the box if space, else below
        title_surf = self._font_title.render(seg.title, True, LABEL_TEXT)
        tw = title_surf.get_width()
        th = title_surf.get_height()

        label_y = int(sy1) - th - 8
        if label_y < 5:
            label_y = int(sy2) + 5  # below instead

        label_x = int(sx1 + bw / 2 - tw / 2)
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
        if seg.date:
            date_surf = self._font_small.render(seg.date, True, LABEL_ACCENT)
            date_y = label_y + th + 2
            if date_y + date_surf.get_height() < self._screen_h:
                screen.blit(date_surf, (label_x, date_y))

    def _render_corner(self, screen, seg, sx1, sy1, sx2, sy2, bw, bh):
        """Draw bounding box outline + info panel in lower-right corner."""

        intensity, glow_px = self._pulse_envelope()
        box_color = self._box_color_at(intensity)

        # Expanding glow halos during pulse phase
        self._draw_pulse_glow(screen, sx1, sy1, sx2, sy2, intensity, glow_px)

        # Bright outline
        pygame.draw.rect(screen, box_color,
                         (int(sx1), int(sy1), int(bw), int(bh)),
                         BOX_OUTLINE)

        # Corner brackets for extra emphasis
        cl = 8  # corner length
        for cx, cy, dx, dy in [
            (sx1, sy1, 1, 1), (sx2, sy1, -1, 1),
            (sx1, sy2, 1, -1), (sx2, sy2, -1, -1)
        ]:
            pygame.draw.line(screen, box_color,
                             (int(cx), int(cy)),
                             (int(cx + dx * cl), int(cy)), BOX_OUTLINE)
            pygame.draw.line(screen, box_color,
                             (int(cx), int(cy)),
                             (int(cx), int(cy + dy * cl)), BOX_OUTLINE)

        # Info panel in lower-right corner
        self._render_corner_panel(screen, seg)

    def _wrap_title(self, title, font, max_w, max_lines=2):
        """Word-wrap a title to fit within max_w, up to max_lines.

        Returns a list of rendered surfaces.  If the title fits on one
        line, returns a single-element list.
        """
        # Quick check: does it fit on one line?
        single = font.render(title, True, LABEL_TEXT)
        if single.get_width() <= max_w or max_lines <= 1:
            return [single]

        # Word-wrap
        words = title.split()
        lines = []      # list of strings
        current = ""
        word_idx = 0

        for i, word in enumerate(words):
            test = word if not current else current + " " + word
            test_w = font.render(test, True, LABEL_TEXT).get_width()
            if test_w <= max_w:
                current = test
                word_idx = i + 1
            else:
                if current:
                    lines.append(current)
                    current = ""
                # If single word is too long, truncate it
                if font.render(word, True, LABEL_TEXT).get_width() > max_w:
                    truncated = word
                    while truncated and font.render(
                            truncated, True, LABEL_TEXT).get_width() > max_w:
                        truncated = truncated[:-1]
                    lines.append(truncated.rstrip() + "...")
                    word_idx = i + 1
                else:
                    current = word
                    word_idx = i + 1

                # Stop if we've filled max_lines
                if len(lines) >= max_lines:
                    break

        if current and len(lines) < max_lines:
            lines.append(current)

        # If there are remaining words we couldn't fit, add "..." to last line
        has_remaining = word_idx < len(words)
        if has_remaining and lines:
            last_text = lines[-1]
            ellipsis_w = font.render("...", True, LABEL_TEXT).get_width()
            last_w = font.render(last_text, True, LABEL_TEXT).get_width()
            if last_w + ellipsis_w <= max_w:
                lines[-1] = last_text + "..."

        return [font.render(line, True, LABEL_TEXT) for line in lines]

    def _render_corner_panel(self, screen, seg):
        """Draw the lower-right info panel with optional thumbnail."""

        link_type, _ = classify_link(seg.link)
        has_thumb = link_type not in ("none", "video", "interactive", "wiki")
        # YouTube and image links can have thumbnails
        if link_type not in ("youtube", "image", "video"):
            has_thumb = False

        # Try to get the thumbnail surface
        thumb_surf = None
        if has_thumb:
            thumb_surf = self._thumbs.get(seg.obj_id, seg.link)

        # Word-wrap the title (up to 2 lines)
        max_title_w = PANEL_W - PANEL_PADDING * 2
        title_surfaces = self._wrap_title(seg.title, self._font_title,
                                          max_title_w, max_lines=2)
        title_total_h = sum(s.get_height() for s in title_surfaces)

        # Determine panel dimensions
        # Title bar height adapts to whether title is 1 or 2 lines
        title_bar_h = max(PANEL_H_TITLE_BAR, title_total_h + 14)

        if has_thumb:
            panel_w = PANEL_W
            panel_h = title_bar_h + PANEL_H_THUMB + PANEL_H_FOOTER + PANEL_PADDING
        else:
            panel_w = PANEL_W_NO_THUMB
            panel_h = title_bar_h + PANEL_H_FOOTER + PANEL_PADDING

        panel_x = self._screen_w - panel_w - PANEL_MARGIN
        panel_y = self._screen_h - panel_h - PANEL_MARGIN

        date_surf = self._font_small.render(
            f"Added: {seg.date}" if seg.date else "", True, LABEL_ACCENT)

        # ── Draw panel background ──
        panel_surf = pygame.Surface((panel_w, panel_h), pygame.SRCALPHA)
        panel_surf.fill(CORNER_PANEL_BG)
        pygame.draw.rect(panel_surf, CORNER_PANEL_BORDER,
                         (0, 0, panel_w, panel_h), 1)

        # Left accent bar
        pygame.draw.rect(panel_surf, LABEL_ACCENT, (0, 0, 4, panel_h))

        screen.blit(panel_surf, (panel_x, panel_y))

        # ── Title (possibly 2 lines) + date ──
        tx = panel_x + PANEL_PADDING
        ty = panel_y + 8
        for ts in title_surfaces:
            screen.blit(ts, (tx, ty))
            ty += ts.get_height()
        ty += 2
        screen.blit(date_surf, (tx, ty))

        # ── Thumbnail ──
        if has_thumb:
            img_x = tx
            img_y = panel_y + title_bar_h + 4

            if thumb_surf is not None:
                # Draw the thumbnail
                screen.blit(thumb_surf, (img_x, img_y))
            else:
                # Draw loading placeholder
                self._render_placeholder(screen, img_x, img_y, THUMB_W, THUMB_H)

        # ── Footer: link type + progress bar ──
        footer_y = panel_y + panel_h - PANEL_H_FOOTER
        self._render_footer(screen, seg, link_type,
                            panel_x + PANEL_PADDING, footer_y,
                            panel_w - PANEL_PADDING * 2)

    def _render_placeholder(self, screen, x, y, w, h):
        """Draw an animated loading placeholder for the thumbnail."""
        # Dark background
        ph_surf = pygame.Surface((w, h))
        ph_surf.fill((25, 25, 35))
        screen.blit(ph_surf, (x, y))

        # Pulsing border
        t = time.time()
        pulse = (math.sin(t * PLACEHOLDER_PULSE_SPEED * 2 * math.pi) + 1) / 2
        border_color = (
            int(40 + pulse * 60),
            int(40 + pulse * 60),
            int(55 + pulse * 80),
        )
        pygame.draw.rect(screen, border_color, (x, y, w, h), 1)

        # "Loading..." text
        load_surf = self._font_small.render("Loading...", True, (80, 80, 100))
        lx = x + (w - load_surf.get_width()) // 2
        ly = y + (h - load_surf.get_height()) // 2
        screen.blit(load_surf, (lx, ly))

    def _render_footer(self, screen, seg, link_type, fx, fy, fw):
        """Draw link type indicator and progress bar in the footer area."""
        label, color = LINK_TYPE_META.get(link_type, ("", (100, 100, 100)))

        # Link type indicator (left side)
        if label:
            type_surf = self._font_link.render(label, True, color)
            screen.blit(type_surf, (fx, fy))

        # Progress bar (right side, takes remaining width)
        progress = min(1.0, self._timer / HIGHLIGHT_DURATION)
        bar_h = 3
        bar_y = fy + 16
        pygame.draw.rect(screen, (40, 40, 50), (fx, bar_y, fw, bar_h))
        pygame.draw.rect(screen, LABEL_ACCENT,
                         (fx, bar_y, int(fw * progress), bar_h))

    def cycle_label_mode(self):
        """Switch between inline and corner label modes."""
        if self.label_mode == LABEL_INLINE:
            self.label_mode = LABEL_CORNER
        else:
            self.label_mode = LABEL_INLINE
        return self.label_mode

    def get_state(self):
        """Return current state info for stats integration."""
        return {
            "hl_state": self._state,
            "hl_current": (self._current_seg.title
                           if self._current_seg else None),
            "hl_current_id": (self._current_seg.obj_id
                              if self._current_seg else None),
            "hl_shown": self.highlights_shown,
            "hl_label_mode": self.label_mode,
            "hl_enabled": self.enabled,
        }
