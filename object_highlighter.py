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
import random
import time
import urllib.request

import pygame

from thumbnail_cache import ThumbnailCache, classify_link

log = logging.getLogger("floor796")

# ── Tunable parameters ───────────────────────────────────────────────────────

HIGHLIGHT_DURATION = 10.0  # seconds an object is shown
PAUSE_DURATION = 2.0       # seconds between objects
MIN_BBOX_SIZE = 15         # skip tiny objects (pixels), hard to see
EDGE_MARGIN = 0.04         # hard margin — bbox must be at least this
                           # far inside the viewport (4%) to prevent
                           # it from being partially off-screen
EDGE_PENALTY = 0.50        # score reduction for objects near edges

# Panel exclusion: the info panel occupies the bottom-right corner.
# Objects whose bbox falls under the panel footprint are penalized.
# Panel is at most 360px wide + 20px margin, ~340px tall + 20px margin.
PANEL_EXCLUDE_W = 380
PANEL_EXCLUDE_H = 360
PANEL_EXCLUDE_PENALTY = 0.30  # up to 70% score reduction if under panel

# Wander velocity prediction: when the viewport is moving, predict
# where the object will be relative to the viewport at the end of the
# highlight duration.  If it would scroll off, penalize heavily.
WANDER_SPEED_DEFAULT = 15.0   # px/s — fallback if no velocity given

# Recency-weighted selection: prefer objects not recently viewed.
# After RECENCY_HALFLIFE seconds, a previously-viewed object's penalty
# decays by half.  Objects within RECENT_BLACKLIST seconds are never
# re-selected (hard cooldown).  Never-viewed objects get a bonus.
RECENCY_HALFLIFE = 600.0   # 10 minutes
RECENT_BLACKLIST = 45.0    # hard cooldown (> HIGHLIGHT + PAUSE)
RECENT_PENALTY = 0.90      # max score reduction for just-viewed (90%)
NEVER_VIEWED_BONUS = 1.15  # 15% score boost for never-viewed objects

# Weighted random sampling: instead of always picking the highest-scored
# object (pure argmax), we sample proportional to score^TEMPERATURE.
# Higher TEMPERATURE = more deterministic (always picks best).
# TEMPERATURE = 1.0 = purely proportional to score.
# TEMPERATURE = 3.0 = heavily favours top candidates but still varies.
# This prevents the same object always being picked first on startup
# when all objects have identical recency scores.
SELECTION_TEMPERATURE = 3.0
MAX_HISTORY_PER_OBJ = 20   # timestamps retained per object for stats

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

    def _recency_score(self, obj_id, now):
        """Return a multiplier (0..~1.15) for how preferable this object is.

        - Never viewed: NEVER_VIEWED_BONUS (1.15)
        - Viewed recently: decaying penalty (down to ~0.1 at t=0)
        - After RECENCY_HALFLIFE: penalty halves again each halflife
        """
        last = self._last_shown.get(obj_id)
        if last is None:
            return NEVER_VIEWED_BONUS
        elapsed = now - last
        # Exponential decay: penalty = RECENT_PENALTY * 0.5^(elapsed/halflife)
        penalty = RECENT_PENALTY * (0.5 ** (elapsed / RECENCY_HALFLIFE))
        return max(0.05, 1.0 - penalty)

    def _select_segment(self, vp_x1, vp_y1, vp_x2, vp_y2,
                        vel_x=0, vel_y=0):
        """Select the best segment to highlight in the current viewport.

        Scoring factors:
          1. Spatial proximity to viewport center (primary).
          2. Edge safety: soft penalty for objects near viewport edges.
             Only hard-skips if the bbox would be partially off-screen
             (< EDGE_MARGIN inside).
          3. Panel exclusion: penalize objects under the bottom-right
             panel footprint (where the info panel renders).
          4. Velocity prediction: penalize objects that would scroll
             off-screen during the highlight duration based on wander
             velocity.  Objects ahead of the viewport (will move toward
             center) get a bonus.
          5. Recency: prefer not-recently-viewed objects.

        Objects within RECENT_BLACKLIST seconds are skipped entirely.
        """
        candidates = self._index.query_viewport(vp_x1, vp_y1, vp_x2, vp_y2)
        if not candidates:
            return None

        now = time.time()
        vp_w = vp_x2 - vp_x1
        vp_h = vp_y2 - vp_y1
        vp_cx = (vp_x1 + vp_x2) / 2
        vp_cy = (vp_y1 + vp_y2) / 2

        # Hard margin — bbox must be at least this far inside to be
        # fully visible.  This is small (4%) just to prevent clipping.
        margin_x = vp_w * EDGE_MARGIN
        margin_y = vp_h * EDGE_MARGIN

        # Edge penalty zone: between hard margin and 20% from edge,
        # objects get a progressively heavier penalty.
        edge_zone_x = vp_w * 0.20
        edge_zone_y = vp_h * 0.20

        # Panel footprint in screen coords (bottom-right corner)
        panel_x1 = self._screen_w - PANEL_EXCLUDE_W
        panel_y1 = self._screen_h - PANEL_EXCLUDE_H

        # Wander speed for prediction
        wander_speed = math.hypot(vel_x, vel_y)
        # How far the viewport will move during the highlight
        predict_dist = wander_speed * HIGHLIGHT_DURATION

        scored_candidates = []

        for seg in candidates:
            # Skip too-small segments
            if seg.width < MIN_BBOX_SIZE and seg.height < MIN_BBOX_SIZE:
                continue

            # Hard cooldown: skip if shown within RECENT_BLACKLIST seconds
            last = self._last_shown.get(seg.obj_id)
            if last is not None and (now - last) < RECENT_BLACKLIST:
                continue

            # Hard skip: bbox must be fully inside the viewport.  Use a
            # reduced margin (1.5%) for the actual pixel-clipping check,
            # to allow objects that barely overflow the 4% scoring margin
            # at their only reachable viewport positions (edge-of-map
            # objects like #383).
            clip_margin_x = vp_w * 0.015
            clip_margin_y = vp_h * 0.015
            if (seg.abs_x1 < vp_x1 + clip_margin_x or
                    seg.abs_x2 > vp_x2 - clip_margin_x or
                    seg.abs_y1 < vp_y1 + clip_margin_y or
                    seg.abs_y2 > vp_y2 - clip_margin_y):
                continue

            # ── Spatial score: 1.0 at center, 0.0 at edges ──
            dx = (seg.cx - vp_cx) / (vp_w / 2)
            dy = (seg.cy - vp_cy) / (vp_h / 2)
            dist_sq = dx * dx + dy * dy
            spatial_score = max(0.0, 1.0 - dist_sq)

            # ── Edge proximity penalty (soft) ──
            # Distance from viewport edges (normalized 0..1 where 1=safe)
            seg_right_dist = (vp_x2 - seg.abs_x2) / edge_zone_x
            seg_left_dist = (seg.abs_x1 - vp_x1) / edge_zone_x
            seg_bottom_dist = (vp_y2 - seg.abs_y2) / edge_zone_y
            seg_top_dist = (seg.abs_y1 - vp_y1) / edge_zone_y
            min_edge_dist = min(seg_right_dist, seg_left_dist,
                                seg_bottom_dist, seg_top_dist)
            if min_edge_dist < 1.0:
                edge_mult = 1.0 - EDGE_PENALTY * (1.0 - min_edge_dist)
            else:
                edge_mult = 1.0

            # ── Panel overlap penalty ──
            # Convert bbox to screen coords
            sx1 = seg.abs_x1 - vp_x1
            sy1 = seg.abs_y1 - vp_y1
            sx2 = seg.abs_x2 - vp_x1
            sy2 = seg.abs_y2 - vp_y1
            # Compute overlap fraction with panel footprint
            ox1 = max(sx1, panel_x1)
            oy1 = max(sy1, panel_y1)
            ox2 = min(sx2, self._screen_w)
            oy2 = min(sy2, self._screen_h)
            if ox2 > ox1 and oy2 > oy1:
                overlap_area = (ox2 - ox1) * (oy2 - oy1)
                seg_area = max(1, (sx2 - sx1) * (sy2 - sy1))
                overlap_frac = overlap_area / seg_area
                panel_mult = 1.0 - PANEL_EXCLUDE_PENALTY * overlap_frac
            else:
                panel_mult = 1.0

            # ── Velocity prediction ──
            # At 15px/s over 10s, the viewport moves 150px.
            # An object near the leading edge will move toward center
            # (good).  An object near the trailing edge will scroll off
            # (bad).  We predict where the bbox will be relative to the
            # viewport at t+HIGHLIGHT_DURATION.
            if predict_dist > 1:
                # Object's future position relative to viewport
                # (viewport moves, object stays — so relative to
                # viewport, the object moves backward by predict_dist)
                clip_m = vp_w * 0.015
                future_x1 = sx1 - vel_x * HIGHLIGHT_DURATION
                future_y1 = sy1 - vel_y * HIGHLIGHT_DURATION
                future_x2 = sx2 - vel_x * HIGHLIGHT_DURATION
                future_y2 = sy2 - vel_y * HIGHLIGHT_DURATION

                # Check if the object would still be visible (using
                # the relaxed clip margin)
                visible = (future_x2 > clip_m and
                           future_x1 < self._screen_w - clip_m and
                           future_y2 > clip_m and
                           future_y1 < self._screen_h - clip_m)
                if not visible:
                    # Would scroll off — skip this one
                    continue

                # Bonus for objects ahead of the viewport (moving toward
                # center): the further ahead, the more it'll be centered
                # during the highlight
                future_cx = (future_x1 + future_x2) / 2
                future_cy = (future_y1 + future_y2) / 2
                future_center_dist = math.hypot(
                    (future_cx - self._screen_w / 2) / (self._screen_w / 2),
                    (future_cy - self._screen_h / 2) / (self._screen_h / 2))
                # Objects that will be MORE centered get a small bonus
                vel_mult = 1.0 + 0.10 * max(0, 1.0 - future_center_dist)
            else:
                vel_mult = 1.0

            # ── Recency multiplier ──
            recency = self._recency_score(seg.obj_id, now)

            # ── Combined score ──
            score = (spatial_score *
                     edge_mult *
                     panel_mult *
                     vel_mult *
                     recency)

            scored_candidates.append((score, seg))

        if not scored_candidates:
            return None

        # Weighted random sampling: sample proportional to score^temperature.
        # This ensures variety while still strongly preferring high-scoring
        # objects.  Prevents the same first/third/fifth object on every boot.
        max_score = max(s for s, _ in scored_candidates)
        if max_score <= 0:
            return None

        # Apply temperature: scores are normalized to [0,1] then raised
        # to the power of TEMPERATURE.  At temp=3, the top candidate is
        # ~27x more likely than one at half its score, but still not
        # guaranteed — providing variety across boots.
        weights = []
        for score, _ in scored_candidates:
            normalized = score / max_score
            weights.append(normalized ** SELECTION_TEMPERATURE)

        total_weight = sum(weights)
        if total_weight <= 0:
            # All-zero weights (shouldn't happen) — fall back to uniform
            return random.choice(scored_candidates)[1]

        r = random.random() * total_weight
        cumulative = 0.0
        for weight, (score, seg) in zip(weights, scored_candidates):
            cumulative += weight
            if r <= cumulative:
                return seg

        return scored_candidates[-1][1]

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
                self._last_shown[seg.obj_id] = now
                history = self._view_history.setdefault(seg.obj_id, [])
                history.append(now)
                # Cap history length for memory
                if len(history) > MAX_HISTORY_PER_OBJ:
                    del history[:-MAX_HISTORY_PER_OBJ]
                # Prefetch thumbnail for the new object
                self._thumbs.get(seg.obj_id, seg.link)

        elif self._state == STATE_HIGHLIGHT:
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
