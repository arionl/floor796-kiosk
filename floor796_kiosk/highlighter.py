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

import pygame

from floor796_kiosk.thumbnails import ThumbnailCache, classify_link

log = logging.getLogger("floor796")

# ── Tunable parameters ───────────────────────────────────────────────────────

HIGHLIGHT_DURATION = 10.0  # seconds an object is shown
PAUSE_DURATION = 0.5      # brief pause between objects (near-continuous)
MIN_BBOX_SIZE = 15         # skip tiny objects (pixels), hard to see

# Recency selection: the least-recently-displayed object wins.
# Objects within RECENT_BLACKLIST seconds are never re-selected
# (hard cooldown). Must be > HIGHLIGHT + PAUSE to prevent the same
# object being re-picked immediately.
RECENT_BLACKLIST = 12.0    # hard cooldown (> HIGHLIGHT + PAUSE)
MAX_HISTORY_PER_OBJ = 20   # timestamps retained per object for stats

CHANGELOG_URL = "https://floor796.com/data/changelog.json"
CHANGELOG_CACHE = "changelog.json"  # local cache filename

# ── Colors ───────────────────────────────────────────────────────────────────

BOX_COLOR = (255, 20, 20)        # bright red
BOX_FILL = (255, 20, 20, 35)     # semi-transparent red fill
BOX_OUTLINE = 3                   # pixels
LABEL_BG = (15, 15, 20, 220)
LABEL_TEXT = (255, 255, 255)
LABEL_ACCENT = (255, 20, 20)
CORNER_PANEL_BG = (15, 15, 20, 230)
CORNER_PANEL_BORDER = (60, 60, 80)

# Breathing glow animation:
#   An alpha-channel gradient radiates outward from the box for
#   GLOW_RADIUS pixels, gently expanding/contracting and fading in/
#   out at STEADY_SPEED Hz. Runs for the entire highlight duration.
#
# Zoom-on intro:
#   The box smoothly expands from the full viewport bounds to the
#   object's actual bounding box over ZOOM_DURATION seconds with an
#   ease-out cubic curve, then transitions seamlessly into breathing.
ZOOM_DURATION = 0.5             # seconds for zoom to complete

STEADY_SPEED = 0.6               # Hz — slow breathing (~1.7s/cycle)
GLOW_RADIUS = 24                 # outward gradient extent in pixels
GLOW_STEPS = 20                  # number of concentric rect layers
GLOW_PEAK_ALPHA = 90             # peak alpha at box edge when breathing peaks

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

        # Compute one bounding box for the entire object using all
        # polygon vertices.  We no longer split into per-tile segments
        # because that fragments multi-tile objects (e.g. Pocahontas
        # showed only a 112x134 top strip instead of the full 162x196
        # character).  All objects are small enough to fit in the
        # viewport (largest is ~488x492 = 25% of 1920x1080).
        xs = [v[1] for v in abs_verts]
        ys = [v[2] for v in abs_verts]
        ax_min = min(xs)
        ay_min = min(ys)
        ax_max = max(xs)
        ay_max = max(ys)
        seg_w = ax_max - ax_min
        seg_h = ay_max - ay_min
        if seg_w < 2 and seg_h < 2:
            skipped += 1
            continue
        seg = ObjectSegment(
            obj_id, title, date, link, "all",
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
                 spacing_w=1016, spacing_h=812, overscan_margin=0):
        self._index = TileObjectIndex(segments, spacing_w, spacing_h)
        self._screen_w = screen_w
        self._screen_h = screen_h
        self._overscan_margin = overscan_margin

        # State machine
        self.enabled = True
        self.label_mode = LABEL_CORNER
        self._state = STATE_IDLE
        self._timer = 0.0
        self._current_seg = None

        # Timestamped view history: obj_id -> [timestamps]
        # Used for recency-weighted selection and stats reporting.
        self._view_history = {}  # obj_id -> list of float timestamps
        self._last_shown = {}    # obj_id -> most recent timestamp (cache)

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
        # Track all known object IDs (from segments) for coverage stats
        self._all_obj_ids = set()
        self._obj_titles = {}  # obj_id -> title (for stats)
        for seg in segments:
            self._all_obj_ids.add(seg.obj_id)
            if seg.obj_id not in self._obj_titles:
                self._obj_titles[seg.obj_id] = seg.title

    def _init_fonts(self):
        if self._fonts_ready:
            return
        self._font_title = pygame.font.Font(None, 22)
        self._font_body = pygame.font.Font(None, 18)
        self._font_small = pygame.font.Font(None, 16)
        self._font_link = pygame.font.Font(None, 15)
        self._fonts_ready = True

    def _select_segment(self, vp_x1, vp_y1, vp_x2, vp_y2,
                        vel_x=0, vel_y=0):
        """Select the best segment to highlight in the current viewport.

        Selection is deterministic: pick the LEAST-RECENTLY-DISPLAYED
        object that is fully visible in the viewport and will remain
        visible for the entire highlight duration (accounting for
        wander velocity). Ties are broken by distance from viewport
        center (closer wins).

        Filtering (hard skips):
          - Too-small segments (< MIN_BBOX_SIZE)
          - Bbox partially off-screen (must be fully inside viewport)
          - Would scroll off-screen during HIGHLIGHT_DURATION
          - Currently within RECENT_BLACKLIST cooldown

        From the remaining candidates, returns the one with the
        oldest last-shown timestamp. Objects never shown before get
        timestamp 0 (highest priority).
        """
        candidates = self._index.query_viewport(vp_x1, vp_y1, vp_x2, vp_y2)
        if not candidates:
            return None

        now = time.time()
        vp_w = vp_x2 - vp_x1
        vp_h = vp_y2 - vp_y1
        vp_cx = (vp_x1 + vp_x2) / 2
        vp_cy = (vp_y1 + vp_y2) / 2

        # Hard margin — bbox must be fully inside the viewport (1.5%
        # tolerance to allow edge-of-map objects).
        clip_margin_x = vp_w * 0.015
        clip_margin_y = vp_h * 0.015

        # Wander prediction
        wander_speed = math.hypot(vel_x, vel_y)
        predict_dist = wander_speed * HIGHLIGHT_DURATION

        eligible = []

        for seg in candidates:
            # Skip too-small segments
            if seg.width < MIN_BBOX_SIZE and seg.height < MIN_BBOX_SIZE:
                continue

            # Hard cooldown: skip if shown within RECENT_BLACKLIST seconds
            last = self._last_shown.get(seg.obj_id)
            if last is not None and (now - last) < RECENT_BLACKLIST:
                continue

            # Hard skip: bbox must be fully inside the viewport
            if (seg.abs_x1 < vp_x1 + clip_margin_x or
                    seg.abs_x2 > vp_x2 - clip_margin_x or
                    seg.abs_y1 < vp_y1 + clip_margin_y or
                    seg.abs_y2 > vp_y2 - clip_margin_y):
                continue

            # Velocity prediction: skip objects that would scroll off
            # during the highlight duration
            if predict_dist > 1:
                sx1 = seg.abs_x1 - vp_x1
                sy1 = seg.abs_y1 - vp_y1
                sx2 = seg.abs_x2 - vp_x1
                sy2 = seg.abs_y2 - vp_y1
                future_x1 = sx1 - vel_x * HIGHLIGHT_DURATION
                future_y1 = sy1 - vel_y * HIGHLIGHT_DURATION
                future_x2 = sx2 - vel_x * HIGHLIGHT_DURATION
                future_y2 = sy2 - vel_y * HIGHLIGHT_DURATION
                visible = (future_x2 > clip_margin_x and
                           future_x1 < self._screen_w - clip_margin_x and
                           future_y2 > clip_margin_y and
                           future_y1 < self._screen_h - clip_margin_y)
                if not visible:
                    continue

            # Distance from viewport center (for tie-breaking only)
            dx = (seg.cx - vp_cx) / (vp_w / 2)
            dy = (seg.cy - vp_cy) / (vp_h / 2)
            center_dist = dx * dx + dy * dy

            # last_shown timestamp (0 = never shown = highest priority)
            last_ts = last if last is not None else 0.0

            eligible.append((last_ts, center_dist, seg))

        if not eligible:
            return None

        # Sort by last_shown ascending (oldest first = least-recently shown),
        # then by center distance ascending (closest to center) as tie-breaker
        eligible.sort(key=lambda t: (t[0], t[1]))
        return eligible[0][2]

    def update(self, dt, pos_x, pos_y, vel_x=0, vel_y=0):
        """Advance the state machine.  Called once per frame.

        vel_x, vel_y: viewport velocity in pixels/sec (from wanderer).
        Used to predict whether candidate objects will stay visible
        for the full highlight duration.
        """
        if not self.enabled:
            return

        vp_x1 = pos_x
        vp_y1 = pos_y
        vp_x2 = pos_x + self._screen_w
        vp_y2 = pos_y + self._screen_h

        self._timer += dt

        if self._state == STATE_IDLE:
            seg = self._select_segment(vp_x1, vp_y1, vp_x2, vp_y2,
                                        vel_x, vel_y)
            if seg:
                self._current_seg = seg
                self._state = STATE_HIGHLIGHT
                self._timer = 0.0
                self.highlights_shown += 1
                # Record view timestamp
                now = time.time()
                last = self._last_shown.get(seg.obj_id)
                ago = f"first time" if last is None else f"{now - last:.0f}s ago"
                log.info("Highlighting obj %d '%s' (last shown: %s, %d total in history)",
                         seg.obj_id, seg.title[:40], ago, len(self._last_shown))
                self._last_shown[seg.obj_id] = now
                history = self._view_history.setdefault(seg.obj_id, [])
                history.append(now)
                # Cap history length for memory
                if len(history) > MAX_HISTORY_PER_OBJ:
                    del history[:-MAX_HISTORY_PER_OBJ]
                # Prefetch thumbnail for the new object
                self._thumbs.get(seg.obj_id, seg.link)

        elif self._state == STATE_HIGHLIGHT:
            # Mid-highlight check: if the current object has scrolled
            # off-screen (e.g. wander direction changed), abort early
            # and immediately pick a new one.
            if self._current_seg is not None:
                seg = self._current_seg
                clip_margin_x = self._screen_w * 0.015
                clip_margin_y = self._screen_h * 0.015
                if (seg.abs_x2 < vp_x1 + clip_margin_x or
                        seg.abs_x1 > vp_x2 - clip_margin_x or
                        seg.abs_y2 < vp_y1 + clip_margin_y or
                        seg.abs_y1 > vp_y2 - clip_margin_y):
                    # Scrolled off — immediately select next
                    log.info("Scroll-off abort: obj %d '%s' left viewport at %.1fs",
                             seg.obj_id, seg.title[:30], self._timer)
                    self._current_seg = None
                    self._state = STATE_IDLE
                    self._timer = 0.0
                    return

            if self._timer >= HIGHLIGHT_DURATION:
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
        box_sx1 = seg.abs_x1 - pos_x
        box_sy1 = seg.abs_y1 - pos_y
        box_sx2 = seg.abs_x2 - pos_x
        box_sy2 = seg.abs_y2 - pos_y

        # Zoom-on intro: interpolate from full viewport bounds to box bounds
        t = self._timer
        if t < ZOOM_DURATION:
            raw = t / ZOOM_DURATION
            # Ease-out cubic: fast start, gentle settle
            eased = 1.0 - (1.0 - raw) ** 3
            # Start from full viewport bounds, zoom into box bounds
            vp_sx1 = 0
            vp_sy1 = 0
            vp_sx2 = self._screen_w
            vp_sy2 = self._screen_h
            sx1 = vp_sx1 + (box_sx1 - vp_sx1) * eased
            sy1 = vp_sy1 + (box_sy1 - vp_sy1) * eased
            sx2 = vp_sx2 + (box_sx2 - vp_sx2) * eased
            sy2 = vp_sy2 + (box_sy2 - vp_sy2) * eased
        else:
            sx1, sy1 = box_sx1, box_sy1
            sx2, sy2 = box_sx2, box_sy2

        bw = sx2 - sx1
        bh = sy2 - sy1

        if self.label_mode == LABEL_INLINE:
            self._render_inline(screen, seg, sx1, sy1, sx2, sy2, bw, bh)
        else:
            self._render_corner(screen, seg, sx1, sy1, sx2, sy2, bw, bh)

    def _pulse_envelope(self):
        """Return (intensity, glow_radius) for the current timer position.

        Single-phase breathing glow that runs for the entire highlight.
        Returns (1.0, radius) where radius oscillates between 40% and
        100% of GLOW_RADIUS at STEADY_SPEED Hz.
        """
        t = self._timer
        osc = (math.sin(t * STEADY_SPEED * 2 * math.pi) + 1) / 2
        radius = int(GLOW_RADIUS * (0.4 + 0.6 * osc))
        return 1.0, radius

    def _draw_breathing_glow(self, screen, sx1, sy1, sx2, sy2, glow_radius):
        """Draw a smooth alpha-gradient glow radiating outward from the box.

        Draws GLOW_STEPS filled rectangles from outermost (largest, lowest
        alpha) to innermost (box edge, highest alpha), each covering the
        previous. The box interior is cut out of each layer so only the
        outward ring area receives glow. This painter's-algorithm approach
        produces a continuous gradient with no gaps or banding.
        """
        bw = sx2 - sx1
        bh = sy2 - sy1
        steps = min(GLOW_STEPS, glow_radius)
        if steps < 2:
            return

        for i in range(steps, 0, -1):
            frac = i / steps  # 1.0 at outer edge → 0 at box edge
            alpha = int(GLOW_PEAK_ALPHA * (1.0 - frac) ** 1.5)
            if alpha < 2:
                continue
            pad = max(1, int(glow_radius * frac))
            gw = int(bw + pad * 2)
            gh = int(bh + pad * 2)
            if gw <= 0 or gh <= 0:
                continue
            glow_surf = pygame.Surface((gw, gh), pygame.SRCALPHA)
            glow_surf.fill((*BOX_COLOR, alpha))
            # Cut out the box interior so glow only covers the ring area
            if int(bw) > 0 and int(bh) > 0:
                glow_surf.fill((0, 0, 0, 0), (pad, pad, int(bw), int(bh)))
            screen.blit(glow_surf, (int(sx1 - pad), int(sy1 - pad)))

    def _render_inline(self, screen, seg, sx1, sy1, sx2, sy2, bw, bh):
        """Draw bounding box with label text next to it."""

        _intensity, glow_radius = self._pulse_envelope()

        self._draw_breathing_glow(screen, sx1, sy1, sx2, sy2, glow_radius)

        # Semi-transparent fill
        fill_surf = pygame.Surface((max(1, int(bw)), max(1, int(bh))),
                                    pygame.SRCALPHA)
        fill_alpha = int(BOX_FILL[3])
        fill_surf.fill((*BOX_COLOR[:3], fill_alpha))
        screen.blit(fill_surf, (int(sx1), int(sy1)))

        # Bright outline
        pygame.draw.rect(screen, BOX_COLOR,
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

        _intensity, glow_radius = self._pulse_envelope()

        self._draw_breathing_glow(screen, sx1, sy1, sx2, sy2, glow_radius)

        # Bright outline
        pygame.draw.rect(screen, BOX_COLOR,
                         (int(sx1), int(sy1), int(bw), int(bh)),
                         BOX_OUTLINE)

        # Corner brackets for extra emphasis
        cl = 8  # corner length
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
        return self._wrap_text(title, font, max_w, max_lines, LABEL_TEXT)

    def _wrap_text(self, text, font, max_w, max_lines=3, color=None):
        """Word-wrap arbitrary text into rendered surfaces.

        Returns list of pygame surfaces, at most max_lines long.
        """
        if color is None:
            color = (180, 180, 190)

        words = text.split()
        lines = []
        current = ""

        for word in words:
            test = word if not current else current + " " + word
            test_w = font.render(test, True, color).get_width()
            if test_w <= max_w:
                current = test
            else:
                if current:
                    lines.append(current)
                    current = ""
                # Handle single word too long for the line
                if font.render(word, True, color).get_width() > max_w:
                    truncated = word
                    while truncated and font.render(
                            truncated, True, color).get_width() > max_w:
                        truncated = truncated[:-1]
                    lines.append(truncated.rstrip() + "...")
                else:
                    current = word
                if len(lines) >= max_lines:
                    break

        if current and len(lines) < max_lines:
            lines.append(current)

        # Add ellipsis to last line if text was truncated
        total_words = len(words)
        used_words = sum(len(l.split()) for l in lines)
        if used_words < total_words and lines:
            last = lines[-1]
            ellipsis_w = font.render("...", True, color).get_width()
            last_w = font.render(last, True, color).get_width()
            if last_w + ellipsis_w <= max_w:
                lines[-1] = last + "..."

        return [font.render(line, True, color) for line in lines]

    def _render_corner_panel(self, screen, seg):
        """Draw the lower-right info panel with optional thumbnail."""

        link_type, _ = classify_link(seg.link)
        # All link types that can produce a visual thumbnail:
        # images, YouTube, video frame captures, and Wikipedia images
        has_thumb = link_type in ("image", "youtube", "video", "wiki")

        # Try to get the thumbnail surface
        thumb_surf = None
        if has_thumb:
            thumb_surf = self._thumbs.get(seg.obj_id, seg.link)

        # Wikipedia extract text (if available)
        extract_text = None
        if link_type == "wiki":
            extract_text = self._thumbs.get_extract(seg.obj_id)

        # Word-wrap the title (up to 2 lines)
        max_title_w = PANEL_W - PANEL_PADDING * 2
        title_surfaces = self._wrap_title(seg.title, self._font_title,
                                          max_title_w, max_lines=2)
        title_total_h = sum(s.get_height() for s in title_surfaces)

        # Determine panel dimensions
        # Title bar includes title lines + date line + padding.
        date_h = self._font_small.get_height()
        title_bar_h = title_total_h + date_h + 16

        # Wrap extract text to compute its height
        extract_lines = []
        extract_h = 0
        if extract_text:
            extract_lines = self._wrap_text(
                extract_text, self._font_small,
                max_title_w, max_lines=3)
            extract_h = sum(s.get_height() for s in extract_lines) + 10

        if has_thumb:
            panel_w = PANEL_W
            panel_h = (title_bar_h + PANEL_H_THUMB + PANEL_H_FOOTER +
                       PANEL_PADDING + extract_h)
        else:
            panel_w = PANEL_W_NO_THUMB
            panel_h = title_bar_h + PANEL_H_FOOTER + PANEL_PADDING

        panel_x = self._screen_w - panel_w - PANEL_MARGIN - self._overscan_margin
        panel_y = self._screen_h - panel_h - PANEL_MARGIN - self._overscan_margin

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

        # ── Wikipedia extract text (below thumbnail) ──
        if extract_lines:
            ex_y = panel_y + title_bar_h + PANEL_H_THUMB + 4
            for line_surf in extract_lines:
                screen.blit(line_surf, (tx, ex_y))
                ex_y += line_surf.get_height()

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

    def get_object_stats(self):
        """Return per-object view statistics for telemetry.

        Returns a dict with:
          - total_objects: count of all known objects
          - viewed_objects: count of objects viewed at least once
          - never_viewed: count of objects never viewed
          - coverage_pct: percentage of objects viewed at least once
          - objects: list of {id, title, views, last_shown, last_shown_ago}
        """
        now = time.time()
        objects = []
        viewed = 0
        for obj_id in sorted(self._all_obj_ids):
            history = self._view_history.get(obj_id, [])
            views = len(history)
            last = self._last_shown.get(obj_id)
            if views > 0:
                viewed += 1
            objects.append({
                "id": obj_id,
                "title": self._obj_titles.get(obj_id, ""),
                "views": views,
                "last_shown": last,
                "last_shown_ago": (now - last) if last else None,
            })

        total = len(self._all_obj_ids)
        return {
            "total_objects": total,
            "viewed_objects": viewed,
            "never_viewed": total - viewed,
            "coverage_pct": (viewed / total * 100) if total else 0,
            "objects": objects,
        }

    def get_windowed_summary(self, window_seconds, limit=5):
        """Return highlighter stats scoped to a time window.

        Filters view histories to only counts within the window, then
        computes:
          - viewed_in_window: unique objects shown
          - total_views: total highlight events
          - coverage_pct: viewed_in_window / total_objects
          - most_viewed: top  objects by view count (with titles)
          - least_viewed: bottom  viewed objects
          - recent: last 3 highlighted objects (within window)
        """
        now = time.time()
        cutoff = now - window_seconds if window_seconds else 0

        # Per-object view counts within window
        window_counts = {}  # obj_id -> count
        window_last = {}    # obj_id -> most recent timestamp in window
        for obj_id, history in self._view_history.items():
            in_window = [t for t in history if t >= cutoff]
            if in_window:
                window_counts[obj_id] = len(in_window)
                window_last[obj_id] = max(in_window)

        total = len(self._all_obj_ids)
        viewed = len(window_counts)
        total_views = sum(window_counts.values())

        # Most/least viewed (only among objects that were viewed)
        sorted_by_count = sorted(window_counts.items(),
                                 key=lambda x: x[1], reverse=True)
        most = []
        for oid, cnt in sorted_by_count[:limit]:
            most.append({
                "id": oid,
                "title": self._obj_titles.get(oid, ""),
                "views": cnt,
            })

        least = []
        for oid, cnt in reversed(sorted_by_count[-limit:]):
            least.append({
                "id": oid,
                "title": self._obj_titles.get(oid, ""),
                "views": cnt,
            })

        # Most recent (sorted by last shown time, newest first)
        recent = []
        sorted_by_time = sorted(window_last.items(),
                                key=lambda x: x[1], reverse=True)
        for oid, ts in sorted_by_time[:limit]:
            recent.append({
                "id": oid,
                "title": self._obj_titles.get(oid, ""),
                "ago": now - ts,
            })

        return {
            "viewed_in_window": viewed,
            "total_objects": total,
            "total_views": total_views,
            "coverage_pct": (viewed / total * 100) if total else 0,
            "most_viewed": most,
            "least_viewed": least,
            "recent": recent,
        }

    def get_recent_highlights(self, n=20):
        """Return the N most recently highlighted objects.

        Returns list of {id, views, last_shown_ago} sorted by recency.
        """
        now = time.time()
        recent = sorted(self._last_shown.items(),
                        key=lambda x: x[1], reverse=True)[:n]
        return [{
            "id": oid,
            "views": len(self._view_history.get(oid, [])),
            "last_shown_ago": now - ts,
        } for oid, ts in recent]
