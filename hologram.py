#!/usr/bin/env python3
"""
Hologram room renderer for floor796 kiosk player.

Downloads .f796.br scene files from floor796.com, decodes them into
RGBA frames, and renders them as clipped overlays on the map.

The hologram room spans tiles t4r0/t5r1/t4r2/t3r1 in the center of the map.
A flat 805×646 image is placed at a fixed map position and clipped to a
4-point polygon defining the visible hologram area.

The 6 holograms cycle automatically. Each hologram cycle (10 seconds):
  - 1s gap:       empty room (no overlay)
  - 2s fade in:   materialize effect (cells fade in with glitch colors)
  - 5s normal:    one full play of the 60-frame scene (frames 0-59)
  - 2s fade out:  dematerialize effect (cells fade out)

All timing is at 12fps (animation frames), including transitions.
Glitch colors are cached per animation frame to avoid 60fps flicker.
"""
import os
import random
import struct
import logging
from urllib.request import urlopen

import pygame
import brotli

log = logging.getLogger('floor796')

# ─── Hologram scene definitions (from hologram-room/render.js) ─────────────

HOLOGRAM_FILES = [
    '1681591909.f796.br',  # 2001: A Space Odyssey
    '1682104496.f796.br',  # Cube
    '1682274269.f796.br',  # Planetes
    '1682865942.f796.br',  # The Matrix
    '1682867247.f796.br',  # Saw
    '1683045493.f796.br',  # Hackers
]

CDN_BASE = 'https://floor796.com/interactive/hologram-room/scenes/'

# Scene dimensions
SCENE_WIDTH = 805
SCENE_HEIGHT = 646
OFFSET_X = -7
OFFSET_Y = 29

# Small position correction
POS_FIX_X = 0
POS_FIX_Y = 0

# Tile grid spacing (must match kiosk_player.py)
TILE_SPACING_W = 1016
TILE_SPACING_H = 812

# Anchor tile: t4r0 at matrix position (row=4, col=6)
ANCHOR_TILE_ROW = 4
ANCHOR_TILE_COL = 6
ANCHOR_LOCAL_Y = 477
ANCHOR_LOCAL_X = 704

HOLO_MAP_X = ANCHOR_TILE_COL * TILE_SPACING_W + ANCHOR_LOCAL_X + OFFSET_X + POS_FIX_X
HOLO_MAP_Y = ANCHOR_TILE_ROW * TILE_SPACING_H + ANCHOR_LOCAL_Y + OFFSET_Y + POS_FIX_Y

# Clip polygon points from render.js clipPathPoints (Y,X format converted to map coords)
CLIP_POINTS = [
    (7 * TILE_SPACING_W + 105, 4 * TILE_SPACING_H + 507),  # t5r1,507,105
    (7 * TILE_SPACING_W + 486, 5 * TILE_SPACING_H + 0),     # t4r2,0,486
    (7 * TILE_SPACING_W + 61, 5 * TILE_SPACING_H + 339),    # t4r2,339,61
    (6 * TILE_SPACING_W + 696, 5 * TILE_SPACING_H + 34),    # t3r1,34,696
]

# ─── Materialization effect constants ───────────────────────────────────────

SURFACE_FADE_LEVELS = 5

GLITCH_COLORS = [
    (255, 204, 255),  # #ffccff
    (204, 248, 255),  # #ccf8ff
    (255, 252, 204),  # #fffccc
]

# Three isometric surfaces forming the hologram floor projection area.
SURFACES = [
    {
        'cols': 14, 'rows': 4,
        'colWidth': 31, 'rowHeight': 57,
        'offsetX': 0, 'offsetY': -24,
        'points': [(429, 5), (4, 344), (429, 207)],
    },
    {
        'cols': 13, 'rows': 4,
        'colWidth': 30, 'rowHeight': 57,
        'offsetX': 0, 'offsetY': -24,
        'points': [(429, 5), (810, 308), (429, 207)],
    },
    {
        'cols': 10, 'rows': 8,
        'colWidth': 30, 'rowHeight': 30,
        'offsetX': 0, 'offsetY': 0,
        'points': [(429, 207), (131, 445), (684, 410)],
    },
]

# ─── Transition timing (frame-based, at 12fps) ─────────────────────────────
# Full cycle = 10 seconds = 120 frames:
#   1s gap (12f) + 2s fade in (24f) + 5s normal (60f) + 2s fade out (24f)
GAP_FRAMES = 12       # 1 second — empty room
FADE_IN_FRAMES = 24   # 2 seconds — materialize
NORMAL_FRAMES = 60    # 5 seconds — one full scene play (frames 0-59)
FADE_OUT_FRAMES = 24  # 2 seconds — dematerialize


# ─── f796.br decoder ───────────────────────────────────────────────────────

def _rle_decode(src, dst, start, length, pixel_offset=0, index=None):
    """RLE decoder with fragment reference support."""
    pos = pixel_offset
    i = start
    end = start + length

    while i < end:
        b0 = src[i]
        b1 = src[i + 1]
        i += 2

        if b0 == 128 and b1 == 32:  # fragment reference
            if index is not None and i + 5 < len(src):
                frame_idx = src[i]
                frag_len = (src[i + 1] << 8) | src[i + 2]
                frag_offset = (src[i + 3] << 16) | (src[i + 4] << 8) | src[i + 5]
                i += 6
                pos = _rle_decode(src, dst, index[frame_idx]['offset'] + frag_offset,
                                  frag_len, pos, index)
            else:
                i += 6
            continue

        repeat = b0 >> 7
        b0 &= 127

        count = 1
        if repeat:
            count = src[i]
            if count > 1:
                i += 1
            elif count == 0:
                count = (src[i + 1] << 8) | src[i + 2]
                i += 3
            else:
                count = (src[i + 1] << 24) | (src[i + 2] << 16) | (src[i + 3] << 8) | src[i + 4]
                i += 5

        if b0 == 0 and b1 == 0:
            pos += count * 4
            continue

        combined = (b0 << 8) | b1
        r = 7 + ((combined >> 10 & 31) << 3)
        g = 7 + ((combined >> 5 & 31) << 3)
        b = 7 + ((combined & 31) << 3)

        for _ in range(count):
            dst[pos] = r
            dst[pos + 1] = g
            dst[pos + 2] = b
            dst[pos + 3] = 255
            pos += 4

    return pos


def decode_f796_br(raw_data, width=SCENE_WIDTH, height=SCENE_HEIGHT):
    """Decode raw .f796.br bytes into list of RGBA frame byte-arrays."""
    index = []
    offset = 0
    for i in range(61):
        val = struct.unpack('>I', raw_data[i * 4:i * 4 + 4])[0]
        index.append({'offset': offset, 'len': val})
        offset += val

    frame60_raw = raw_data[244:244 + index[60]['len']]
    frame60_rle = brotli.decompress(frame60_raw)
    rest_rle = brotli.decompress(raw_data[244 + index[60]['len']:])

    file_data = bytearray(frame60_rle) + bytearray(rest_rle)

    pixel_count = width * height * 4
    base_frame = bytearray(pixel_count)
    for j in range(3, pixel_count, 4):
        base_frame[j] = 255
    _rle_decode(file_data, base_frame, index[0]['offset'], index[0]['len'], 0, index)

    frames = []
    for frame_no in range(60):
        buf = bytearray(base_frame)
        if frame_no > 0:
            idx = index[frame_no]
            _rle_decode(file_data, buf, idx['offset'], idx['len'], 0, index)
        frames.append(bytes(buf))

    return frames


# ─── Surface matrix (materialization grid) ─────────────────────────────────

def create_surface_matrix(surface):
    """Create a random fade matrix for a surface. Values 0 to FADE_LEVELS//2-1."""
    cols, rows = surface['cols'], surface['rows']
    return [[random.randint(0, SURFACE_FADE_LEVELS // 2 - 1)
             for _ in range(cols)] for _ in range(rows)]


def update_surface_matrix(mat, direction=1):
    """Increment or decrement all cells in the matrix."""
    for row in mat:
        for c in range(len(row)):
            if direction > 0:
                if row[c] < SURFACE_FADE_LEVELS:
                    row[c] += 1
            else:
                if row[c] > 0:
                    row[c] -= 1


def draw_surface_quads(surface, mat, mask_surf, glitch_cache=None):
    """Draw the surface grid cells onto the mask surface.

    Cells with value 0 are skipped. Higher values = more opaque.
    At max value, cells are solid white. Lower values use glitch colors.
    glitch_cache: optional dict {(r,c): color} to keep colors stable across
    render frames within the same animation frame (12fps, not 60fps).
    """
    p1, p2, p3 = surface['points']
    x_dir = 1 if p2[0] >= p1[0] else -1
    if p2[0] == p1[0]:
        x_dir = 0
    y_dir = 1 if p3[1] >= p1[1] else -1
    if p3[1] == p1[1]:
        y_dir = 0

    dx = abs(p2[0] - p1[0])
    dy_p2 = abs(p2[1] - p1[1])
    y_step = dy_p2 / dx if dx > 0 else 0

    dx_p3 = abs(p3[0] - p1[0])
    dy_p3 = abs(p3[1] - p1[1])
    x_step = dx_p3 / dy_p3 if dy_p3 > 0 else 0

    cw = surface['colWidth']
    rh = surface['rowHeight']
    ox = surface['offsetX']
    oy = surface['offsetY']

    for r in range(len(mat)):
        for c in range(len(mat[r])):
            val = mat[r][c]
            if val == 0:
                continue

            x0 = c * cw
            y0 = r * rh
            x1 = x0 + cw
            y1 = y0 + rh

            sx0 = x0 * x_dir
            sx1 = x1 * x_dir
            sy0 = y0 * y_dir
            sy1 = y1 * y_dir

            y_by_x0 = abs(sx0) * y_step
            y_by_x1 = abs(sx1) * y_step
            x_by_y0 = abs(sy0) * x_step
            x_by_y1 = abs(sy1) * x_step

            alpha = int(255 * val / SURFACE_FADE_LEVELS)
            if val == SURFACE_FADE_LEVELS:
                color = (255, 255, 255, 255)
            else:
                if glitch_cache is not None:
                    key = (r, c)
                    if key not in glitch_cache:
                        glitch_cache[key] = random.choice(GLITCH_COLORS)
                    gc = glitch_cache[key]
                else:
                    gc = random.choice(GLITCH_COLORS)
                color = (gc[0], gc[1], gc[2], alpha)

            quad = [
                (p1[0] + sx0 + x_by_y0 + ox, p1[1] + sy0 + y_by_x0 + oy),
                (p1[0] + sx1 + x_by_y0 + ox, p1[1] + sy0 + y_by_x1 + oy),
                (p1[0] + sx1 + x_by_y1 + ox, p1[1] + sy1 + y_by_x1 + oy),
                (p1[0] + sx0 + x_by_y1 + ox, p1[1] + sy1 + y_by_x0 + oy),
            ]
            pygame.draw.polygon(mask_surf, color, quad)


# ─── Hologram overlay class ─────────────────────────────────────────────────

class HologramOverlay:
    """Manages hologram scene cycling, transition effects, and rendering.

    The state machine is driven by animation frames (12fps), not render
    frames (60fps). state_frame counts animation frames within the current
    state. The hologram scene plays from its own frame 0 during the 'normal'
    state, independent of the tile animation frame.
    """

    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        # scenes: dict of scene_idx -> [pygame.Surface, ...] (60 frames each)
        # Only 2 scenes are held in memory at a time (current + next) to
        # keep memory usage manageable on 4 GB Pi (each scene ~120 MB).
        self.scenes = {}
        self.clip_mask = None   # pygame.Surface for clipping polygon
        self.current_holo = 0
        self._prev_anim_frame = -1  # detect animation frame changes

        # Transition state: gap -> fade_in -> normal -> fade_out -> gap
        self.state = 'gap'
        self.state_frame = 0
        self.surface_matrices = []
        self._glitch_cache = {}  # stable glitch colors per animation frame

        # Pre-compute clip polygon relative to hologram image top-left
        self.clip_poly_local = []
        for mx, my in CLIP_POINTS:
            lx = mx - HOLO_MAP_X
            ly = my - HOLO_MAP_Y
            self.clip_poly_local.append((lx, ly))

    def prepare(self):
        """Download (if needed) and decode the first 2 hologram scenes.

        Only the current and next scene are held in memory.  Subsequent
        scenes are lazy-loaded during the gap phase (1s of idle time) when
        the previous scene is no longer needed.
        """
        try:
            os.makedirs(self.cache_dir, exist_ok=True)

            # Pre-decode first 2 scenes (current + next)
            for i in range(2):
                self._load_scene(i)

            self._build_clip_mask()
            # Pre-apply clip mask to all loaded frames so normal rendering is a
            # single blit (no per-frame surf.copy() + BLEND_RGBA_MULT).
            self._apply_clip_to_all_frames()
            self.surface_matrices = [create_surface_matrix(s) for s in SURFACES]
            log.info(f'Hologram overlay ready: {len(self.scenes)} scenes loaded')
            return True

        except Exception as e:
            log.warning(f'Hologram overlay unavailable: {e}')
            return False

    def _load_scene(self, idx):
        """Download (if needed), decode, and store scene idx in self.scenes."""
        if idx in self.scenes:
            return
        holo_file = HOLOGRAM_FILES[idx]
        cache_path = os.path.join(self.cache_dir, holo_file + '.decoded')

        if os.path.exists(cache_path):
            with open(cache_path, 'rb') as f:
                raw = f.read()
            log.info(f'Hologram scene {idx+1}: cached {holo_file}')
        else:
            url = CDN_BASE + holo_file
            log.info(f'Hologram scene {idx+1}: downloading {holo_file}...')
            raw = urlopen(url, timeout=30).read()
            with open(cache_path, 'wb') as f:
                f.write(raw)

        frames = decode_f796_br(raw)
        surfaces = []
        for frame_data in frames:
            surf = pygame.image.frombuffer(
                frame_data, (SCENE_WIDTH, SCENE_HEIGHT), 'RGBA'
            ).convert_alpha()
            surfaces.append(surf)
        self.scenes[idx] = surfaces

        # Apply clip mask to newly loaded frames
        if self.clip_mask is not None:
            for i in range(len(surfaces)):
                surfaces[i].blit(self.clip_mask, (0, 0),
                                 special_flags=pygame.BLEND_RGBA_MULT)

        log.info(f'  Decoded {len(frames)} frames (scene {idx+1})')

    def _build_clip_mask(self):
        """Create a mask surface for the clip polygon."""
        mask = pygame.Surface((SCENE_WIDTH, SCENE_HEIGHT), pygame.SRCALPHA)
        pygame.draw.polygon(mask, (255, 255, 255, 255), self.clip_poly_local)
        self.clip_mask = mask

    def _apply_clip_to_all_frames(self):
        """Pre-multiply clip mask into every frame of every loaded scene.

        After this, normal rendering is a single blit — no per-frame
        surf.copy() + BLEND_RGBA_MULT needed.  This trades extra surface
        copies at startup for zero per-frame allocation.
        """
        for scene in self.scenes.values():
            for i in range(len(scene)):
                scene[i].blit(self.clip_mask, (0, 0),
                              special_flags=pygame.BLEND_RGBA_MULT)

    def update(self, anim_frame):
        """Called every render frame (60fps) by the player.
        Only advances state_frame on animation frame changes (12fps).
        This keeps all timing in sync with the tile animation clock.
        """
        if anim_frame != self._prev_anim_frame:
            self._prev_anim_frame = anim_frame
            self.state_frame += 1
            self._glitch_cache = {}  # new random colors this animation frame
            self._tick()

    def cycle_next(self):
        """Called when the tile animation loop wraps (frame 59 -> 0).
        Not used for state transitions — all timing is frame-count based.
        """
        pass

    def _tick(self):
        """Advance the state machine by one animation frame (12fps)."""
        # Update transition matrices at 12fps, not 60fps
        if self.state in ('fade_in', 'fade_out'):
            self._update_transition_matrices()

        if self.state == 'gap':
            if self.state_frame >= GAP_FRAMES:
                self._enter_fade_in()

        elif self.state == 'fade_in':
            if self.state_frame >= FADE_IN_FRAMES:
                self._enter_normal()

        elif self.state == 'normal':
            if self.state_frame >= NORMAL_FRAMES:
                self._enter_fade_out()

        elif self.state == 'fade_out':
            if self.state_frame >= FADE_OUT_FRAMES:
                self._enter_gap()

    # ─── State transitions ─────────────────────────────────────────────────

    def _enter_gap(self):
        """Transition to empty room gap; advance to next hologram.

        Evict the scene we just finished (2 cycles ago) to keep only
        2 scenes in memory, and lazy-load the upcoming scene.
        """
        self.state = 'gap'
        self.state_frame = 0
        self.current_holo = (self.current_holo + 1) % len(HOLOGRAM_FILES)

        # Evict the scene that is now 2 positions behind (no longer needed).
        # Keep current_holo and current_holo-1 (for fade_out tail).
        # The scene at current_holo-2 can be freed.
        prev_prev = (self.current_holo - 2) % len(HOLOGRAM_FILES)
        if prev_prev in self.scenes and prev_prev != self.current_holo:
            del self.scenes[prev_prev]

        # Pre-load the next scene (current_holo + 1) during the 1s gap.
        next_idx = (self.current_holo + 1) % len(HOLOGRAM_FILES)
        if next_idx not in self.scenes:
            self._load_scene(next_idx)

    def _enter_fade_in(self):
        """Begin materializing the next hologram."""
        self.state = 'fade_in'
        self.state_frame = 0
        self.surface_matrices = [create_surface_matrix(s) for s in SURFACES]

    def _enter_normal(self):
        """Full display — scene plays from frame 0."""
        self.state = 'normal'
        self.state_frame = 0

    def _enter_fade_out(self):
        """Begin dematerializing — start from full opacity."""
        self.state = 'fade_out'
        self.state_frame = 0
        self.surface_matrices = [
            [[SURFACE_FADE_LEVELS for _ in range(s['cols'])]
             for _ in range(s['rows'])] for s in SURFACES
        ]

    def render(self, screen, viewport_x, viewport_y):
        """Render current hologram frame with transition effects."""
        if not self.scenes:
            return

        # During gap, render nothing (empty room shows through)
        if self.state == 'gap':
            return

        # Ensure current scene is loaded
        if self.current_holo not in self.scenes:
            return

        # Position on screen
        screen_x = HOLO_MAP_X - viewport_x
        screen_y = HOLO_MAP_Y - viewport_y

        # Cull if completely off-screen
        if (screen_x + SCENE_WIDTH < 0 or screen_x > screen.get_width() or
                screen_y + SCENE_HEIGHT < 0 or screen_y > screen.get_height()):
            return

        surfaces = self.scenes[self.current_holo]
        num_frames = len(surfaces)

        # Select which hologram frame to show based on state
        if self.state == 'normal':
            # Play from frame 0 to 59 in lockstep with state_frame
            # This ensures exactly one clean playthrough per normal phase
            holo_frame = min(self.state_frame, num_frames - 1)
        elif self.state == 'fade_in':
            # Show the opening frame while materializing
            holo_frame = 0
        elif self.state == 'fade_out':
            # Show the final frame while dematerializing
            holo_frame = num_frames - 1
        else:
            holo_frame = 0

        surf = surfaces[holo_frame]

        if self.state == 'normal':
            # Clip mask pre-applied during prepare() — single blit.
            screen.blit(surf, (screen_x, screen_y))

        else:
            # fade_in or fade_out: render with materialization grid
            # Matrix updates happen in _tick() at 12fps
            self._render_with_transition(surf, screen, screen_x, screen_y)

    def _update_transition_matrices(self):
        """Update surface matrices for the current fade direction.
        Called once per animation frame (12fps) from _tick().
        Spreads the fade evenly across the fade duration."""
        if self.state == 'fade_in':
            direction = 1
            fade_len = FADE_IN_FRAMES
        else:
            direction = -1
            fade_len = FADE_OUT_FRAMES

        # Update every N animation frames to spread SURFACE_FADE_LEVELS steps
        # across the full fade duration
        skip = max(1, fade_len // SURFACE_FADE_LEVELS)
        if self.state_frame % skip == 0:
            for mat in self.surface_matrices:
                update_surface_matrix(mat, direction)

    def _render_with_transition(self, surf, screen, screen_x, screen_y):
        """Render hologram with materialization mask overlay."""
        # Reuse persistent mask surfaces instead of allocating every frame.
        if not hasattr(self, '_transition_mask') or self._transition_mask is None:
            self._transition_mask = pygame.Surface(
                (SCENE_WIDTH, SCENE_HEIGHT), pygame.SRCALPHA)
        if not hasattr(self, '_combined_mask') or self._combined_mask is None:
            self._combined_mask = pygame.Surface(
                (SCENE_WIDTH, SCENE_HEIGHT), pygame.SRCALPHA)

        mask = self._transition_mask
        mask.fill((0, 0, 0, 0))

        for i, surface in enumerate(SURFACES):
            draw_surface_quads(surface, self.surface_matrices[i], mask,
                              self._glitch_cache)

        combined_mask = self._combined_mask
        combined_mask.fill((0, 0, 0, 0))
        combined_mask.blit(self.clip_mask, (0, 0))
        combined_mask.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)

        clipped = surf.copy()
        clipped.blit(combined_mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)

        screen.blit(clipped, (screen_x, screen_y))
