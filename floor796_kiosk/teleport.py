#!/usr/bin/env python3
"""
Teleport room renderer for the floor796 kiosk player.

Site-exact port of floor796-tvos ``App/Sources/Rooms.swift`` (TeleportRoom /
TeleK) — that implementation was verified against the live site's
render.js and is the semantic authority.  Do not approximate from
memory; re-read Rooms.swift when in doubt.

The teleport room is a 289x412 scene overlay anchored at site point
``t2r4,618,15`` (+ (-4, 1)) near the top-left of the map.  7 teleport
scenes cycle automatically.  Each cycle:

  gap:            4 ticks (~330 ms) — room empty (map art only)
  materialize:    22 beats — held frame f0 at alpha t/21, sparkle
                  column t sweeping the room (slots diffed vs the
                  empty-room bg on a 10px quant grid)
  play:           60 ticks — [f0 x3 (engine counter freeze + decode
                  lag), then f1..f57] — the FULL scene
  dematerialize:  22 beats — ALL beats hold f58 (the site's
                  correctFrameIndex freezes at dematerializationFrame)
                  fading alpha 1-min(1,t/14) with f58 sparkles
  gap -> next teleport

All timing is at 12 fps (one step() per animation tick), matching the
hologram room.  The black hide-triangle (site eraseUnusedImagePart)
masks ONLY the slot-diff baseline frames (raw f0 / f58) — never
displayed frames.

Memory: scenes decode to 60x (289x412x4) ~= 28.5 MB; only the current
and next scene are ever resident (same contract as the hologram).

Content is downloaded and cached on-device from floor796.com — nothing
is bundled (list JSON, teleport-bg.png, teleport.webp sprite sheet,
.f796.br scenes).
"""
import json
import logging
import os
import random
import struct
import threading
import time
from urllib.request import urlopen

import numpy as np
import pygame

from .hologram import decode_f796_br

log = logging.getLogger('floor796')

# ─── Constants (TeleK in Rooms.swift — do not tweak) ────────────────────────

WIDTH = 289
HEIGHT = 412
HIDE_PART_Y = 341

# Site point 't2r4,618,15' is tileRef,Y,X (Y FIRST) — normalized to x/y.
ANCHOR_TILE = 't2r4'
ANCHOR_X = 15            # local X inside the anchor tile
ANCHOR_Y = 618           # local Y inside the anchor tile
OFF_X = -4
OFF_Y = 1

# Room clip polygon (site render slot clipPathPoints), as
# (tile_ref, x, y) absolute-resolved at prepare() time.
CLIP_POINTS = [
    ('t2r4', 62, 629), ('t2r4', 173, 599), ('t2r4', 246, 618),
    ('t2r4', 322, 689), ('t2r4', 334, 756), ('t2r4', 321, 793),
    ('t1r5', 300, 5), ('t1r5', 300, 149), ('t1r5', 157, 264),
    ('t0r4', 1005, 131), ('t2r4', 19, 628),
]

LIST_URL = 'https://floor796.com/addon/teleport-room/list'
BG_URL = 'https://floor796.com/interactive/teleport-room/teleport-bg.png'
SPRITES_URL = 'https://floor796.com/interactive/teleport-room/teleport.webp'
SCENES_BASE = 'https://floor796.com/interactive/teleport-room/scenes/'

MATERIALIZE_FRAMES = 22   # site: beats t=0..21 (image alpha t/21)
SPRITE_COLS = 21          # 21 columns; t=21 is off-sheet -> nothing drawn
FADE_DIV = 14             # alpha 1-min(1,t/14) on the way out
GAP_FRAMES = 4            # delete tick + TIMEOUT 200 ms ~ 3 pure ticks
PLAY_TICKS = 59           # else-ticks pf 0..58; + transition tick = 60
LAST_PLAY_FRAME = 57      # last raw play frame
DEMAT_ENTRY_FRAME = 58    # demat beat-0 source + slot-diff frame
SPRITE_W = 60
SPRITE_H = 100
SPRITE_VARIANTS = 6
QUANT = 10

TILE_SPACING_W = 1016
TILE_SPACING_H = 812

# Sprite anchor inside a 60x100 cell (drawSlots drawImage offset):
# dx = slot.x - 30 + shift, dy = slot.y - 80.
SPRITE_CX = SPRITE_W // 2
SPRITE_CY = SPRITE_H - 20


def erase_hidden(px):
    """Site eraseUnusedImagePart: black out the triangle below
    HIDE_PART_Y (bottom-right corner of the scene canvas).

    Triangle (W,hideY)-(0,H)-(W,H); in-place on an (H,W,4) uint8 array.
    Used ONLY on the slot-diff baseline frames (raw f0 / f58) — never
    on displayed frames.
    """
    h, w = px.shape[:2]
    for y in range(HIDE_PART_Y, h):
        t = (y - HIDE_PART_Y) / (h - HIDE_PART_Y)
        x0 = int((1 - t) * w)
        px[y, max(0, x0):, 0] = 0
        px[y, max(0, x0):, 1] = 0
        px[y, max(0, x0):, 2] = 0
        px[y, max(0, x0):, 3] = 255


def poly_coverage(w, h, poly):
    """Even-odd polygon fill mask (Port of PolyMask.coverage).

    ``poly`` is a list of (x, y) in image coordinates.
    """
    n = len(poly)
    ys = np.arange(h, dtype=np.int64)[:, None]
    xs = np.arange(w, dtype=np.int64)[None, :]
    inside = np.zeros((h, w), dtype=bool)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        # Standard even-odd ray casting, vectorized over the grid.
        cond = ((y1 > ys) != (y2 > ys))
        with np.errstate(divide='ignore', invalid='ignore'):
            xint = np.where(cond, x1 + (ys - y1) * (x2 - x1) / (y2 - y1),
                            np.inf)
        inside ^= cond & (xs < xint)
    return inside


def resolve_point(tiles, ref, x, y, spacing_w=TILE_SPACING_W,
                  spacing_h=TILE_SPACING_H):
    """Resolve a site point (tileRef + local x/y) to absolute map coords.

    Mirrors tvOS TileRefResolver.resolve: (col*spacing_w + x,
    row*spacing_h + y).  Returns None when the tile is unknown.
    """
    entry = tiles.get(ref)
    if entry is None:
        return None
    return (entry['col'] * spacing_w + x, entry['row'] * spacing_h + y)


def load_image_rgba(path):
    """Load a PNG/WebP file as a straight-alpha (H,W,4) uint8 array.

    Prefers Pillow (reliable WebP, straight RGBA); falls back to
    pygame.image.load (SDL_image) when PIL is unavailable.
    """
    try:
        from PIL import Image
        img = Image.open(path).convert('RGBA')
        return np.asarray(img, dtype=np.uint8).copy()
    except ImportError:
        surf = pygame.image.load(path).convert_alpha()
        arr = pygame.surfarray.array3d(surf)          # (W,H,3)
        a = pygame.surfarray.pixels_alpha(surf)       # (W,H)
        out = np.dstack([arr.transpose(1, 0, 2), a]).copy()
        return out


class _Scene:
    """Decoded teleport scene: displayed frames (clipped, straight
    alpha) + raw diff baselines (f0, f58 with the hide-triangle)."""

    __slots__ = ('frames', 'raws')

    def __init__(self, frames, raws):
        self.frames = frames   # list of 60 (H,W,4) uint8 arrays
        self.raws = raws       # {frame_idx: (H,W,4) uint8} diff baselines


class TeleportRoom:
    """Teleport room overlay — download/decode/step/render.

    Lifecycle mirrors HologramOverlay: ``prepare()`` (or the threaded
    decoder) downloads base assets + first scenes, ``update()`` is
    called once per animation tick (12 fps), ``render()`` once per
    frame, ``poll_scenes()`` from the main loop.
    """

    GAP, MATERIALIZE, PLAY, DEMATERIALIZE = ('gap', 'materialize', 'play',
                                             'dematerialize')

    def __init__(self, cache_dir, tiles_meta=None):
        self.cache_dir = os.path.join(cache_dir, 'teleport')
        os.makedirs(self.cache_dir, exist_ok=True)
        self.tiles = (tiles_meta or {}).get('tiles', {})
        self.origin_x = None
        self.origin_y = None
        self.clip_mask = None       # (H,W) bool
        self.bg_px = None           # (H,W,4) uint8, hide-triangle erased
        self.sprites = None         # (600,1260,4) uint8
        self.items = []             # [{'file','title_en','src'}]

        self.scenes = {}
        self._loading = set()
        self._scene_requests = []
        self._scene_lock = threading.Lock()
        self._decode_thread = None
        self._stop_event = threading.Event()

        self.phase = self.GAP
        self.phase_frame = 0
        self.slot_frame = 0
        self.held_frame = 0
        self.current = 0
        self.slots = []             # [(x, y, variant, shift)]
        self._staging = None        # composite buffer (H,W,4)
        self.ready = False
        self.prepare_failed = False

    # ─── prepare ───────────────────────────────────────────────────────

    def prepare(self):
        """Resolve geometry, download + cache base assets, decode the
        first two scenes (blocking).  Sets ready (or prepare_failed)."""
        try:
            anchor = resolve_point(self.tiles, ANCHOR_TILE, ANCHOR_X,
                                   ANCHOR_Y)
            if anchor is None or any(
                    resolve_point(self.tiles, ref, x, y) is None
                    for ref, x, y in CLIP_POINTS):
                log.info('teleport-room: anchor/clip tiles missing — '
                         'disabled')
                return
            self.origin_x = anchor[0] + OFF_X
            self.origin_y = anchor[1] + OFF_Y
            poly = []
            for ref, x, y in CLIP_POINTS:
                px, py = resolve_point(self.tiles, ref, x, y)
                poly.append((px - self.origin_x, py - self.origin_y))
            self.clip_mask = poly_coverage(WIDTH, HEIGHT, poly)

            self._fetch_list()
            self.bg_px = self._fetch_image(
                BG_URL, 'teleport-bg.png', (HEIGHT, WIDTH))
            self.sprites = self._fetch_image(
                SPRITES_URL, 'teleport.webp',
                (SPRITE_H * SPRITE_VARIANTS, SPRITE_W * SPRITE_COLS))
            erase_hidden(self.bg_px)

            self.ready = True
            log.info('teleport-room: ready at %.0f,%.0f (%d teleports)',
                     self.origin_x, self.origin_y, len(self.items))
            self._decode_scene(0)
            self._decode_scene(1 % max(1, len(self.items)))
        except Exception as e:
            self.prepare_failed = True
            log.warning('teleport-room: prepare failed: %s', e)

    def _download(self, url, name):
        """Download to cache (tmp+rename), return local path."""
        path = os.path.join(self.cache_dir, name)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
        tmp = path + '.tmp'
        log.info('teleport-room: downloading %s...', name)
        with open(tmp, 'wb') as f:
            f.write(urlopen(url, timeout=30).read())
        os.replace(tmp, path)
        return path

    def _fetch_image(self, url, name, expect_hw):
        arr = load_image_rgba(self._download(url, name))
        if arr.shape[:2] != expect_hw:
            raise ValueError('%s is (H,W)=%s, expected %s' %
                             (name, arr.shape[:2], expect_hw))
        return arr

    def _fetch_list(self):
        path = self._download(LIST_URL, 'list.json')
        with open(path) as f:
            data = json.load(f)
        self.items = [{'file': it['file'],
                       'title_en': it.get('titleEn'),
                       'src': it.get('src')} for it in data]
        if not self.items:
            raise ValueError('empty teleport list')

    # ─── scene decode (background worker, hologram pattern) ────────────

    def start_decoder(self):
        if self._decode_thread is None:
            self._decode_thread = threading.Thread(
                target=self._decode_worker_loop, daemon=True,
                name='teleport-decode')
            self._decode_thread.start()

    def stop_decoder(self):
        self._stop_event.set()
        if self._decode_thread is not None:
            self._decode_thread.join(timeout=5)
            self._decode_thread = None

    def _decode_worker_loop(self):
        while not self._stop_event.is_set():
            idx = None
            with self._scene_lock:
                if self._scene_requests:
                    idx = self._scene_requests.pop(0)
                    if idx in self.scenes or idx in self._loading:
                        idx = None
            if idx is None:
                time.sleep(0.05)
                continue
            self._decode_scene(idx)

    def _request_scene(self, idx):
        with self._scene_lock:
            if idx not in self.scenes and idx not in self._loading:
                self._scene_requests.append(idx)

    def poll_scenes(self):
        """Drop finished scenes is unnecessary; request-ahead happens in
        step().  Kept for API symmetry with HologramOverlay."""
        return None

    def _decode_scene(self, idx):
        if not self.items or idx >= len(self.items) \
                or idx in self.scenes or idx in self._loading:
            return
        self._loading.add(idx)
        try:
            name = self.items[idx]['file']
            path = self._download(SCENES_BASE + name, name)
            with open(path, 'rb') as f:
                raw = f.read()
            frames_raw = decode_f796_br(raw, width=WIDTH, height=HEIGHT)
            frames = []
            raws = {}
            for i, buf in enumerate(frames_raw):
                px = np.frombuffer(buf, dtype=np.uint8).reshape(
                    HEIGHT, WIDTH, 4).copy()
                if i == 0 or i == DEMAT_ENTRY_FRAME:
                    diff = px.copy()
                    erase_hidden(diff)
                    raws[i] = diff
                # Clip to the room polygon (alpha pass, straight alpha).
                px[:, :, 3] = np.where(self.clip_mask, px[:, :, 3], 0)
                frames.append(px)
            self.scenes[idx] = _Scene(frames, raws)
            log.info('teleport-room: scene %d ready (%s)', idx + 1, name)
        except Exception as e:
            log.warning('teleport-room: scene %d failed: %s', idx + 1, e)
        finally:
            self._loading.discard(idx)

    # ─── phase machine (site-exact; one call per 12fps tick) ───────────

    def update(self, _anim_frame=None):
        """Advance the teleport cycle by one animation tick."""
        if not self.ready or not self.items:
            return
        if self.phase == self.GAP:
            self.phase_frame += 1
            if self.phase_frame >= GAP_FRAMES:
                if self.current in self.scenes:
                    # Engine counter frozen at -1 through materialize —
                    # worker keeps decoding f0; beat 0 = sparkles only.
                    self.slot_frame = 0
                    self.phase = self.MATERIALIZE
                    self.phase_frame = 1   # beat 0 displayed below
                    self.held_frame = 0
                    self.generate_slots(0)
                    self._composite(0.0, 0)
                    self._request_scene(
                        (self.current + 1) % len(self.items))
                elif self.phase_frame >= GAP_FRAMES * 8:
                    self.current = (self.current + 1) % len(self.items)
                    self.phase_frame = 0
        elif self.phase == self.MATERIALIZE:
            if self.phase_frame >= MATERIALIZE_FRAMES:
                # Past last beat (t=21 displayed on the previous tick);
                # become play; this tick displays f0 (#1).
                log.info('teleport: mat->play (scene %d)', self.current + 1)
                self.phase = self.PLAY
                self.slot_frame = 0
                self.phase_frame = 0   # next else-tick n=1 -> f0 (#2)
            else:
                self._composite(
                    self.phase_frame / (MATERIALIZE_FRAMES - 1),
                    self.phase_frame)
                self.phase_frame += 1
        elif self.phase == self.PLAY:
            if self.phase_frame >= PLAY_TICKS:
                # f57 displayed on the previous tick -> dematerialize;
                # display demat beat 0 (f58, alpha 1) now.
                log.info('teleport: play->demat (scene %d)',
                         self.current + 1)
                self.phase = self.DEMATERIALIZE
                self.phase_frame = 1   # beat 0 displayed below
                self.held_frame = DEMAT_ENTRY_FRAME
                self.generate_slots(DEMAT_ENTRY_FRAME)
                self._composite(1.0, 0)
            else:
                # Live play shows the FULL scene: [f0 x3, f1..f57].
                self.slot_frame = 0 if self.phase_frame <= 1 \
                    else self.phase_frame - 1
                if self.slot_frame > LAST_PLAY_FRAME:
                    self.slot_frame = LAST_PLAY_FRAME
                self.phase_frame += 1
        elif self.phase == self.DEMATERIALIZE:
            if self.phase_frame >= MATERIALIZE_FRAMES:
                # t=21 displayed on the previous tick; scene over.
                log.info('teleport: demat->gap (scene %d done)',
                         self.current + 1)
                self.scenes.pop(self.current, None)
                self.current = (self.current + 1) % len(self.items)
                self.phase = self.GAP
                self.phase_frame = 0
            else:
                # ALL beats hold f58 under alpha 1-min(1,t/14).
                a = 1 - min(1, self.phase_frame / FADE_DIV)
                self._composite(a, self.phase_frame)
                self.phase_frame += 1

    # ─── sparkle slots ─────────────────────────────────────────────────

    def generate_slots(self, frame_idx):
        """Diff held frame vs empty-room bg on the quant grid."""
        scene = self.scenes.get(self.current)
        if scene is None or self.bg_px is None:
            self.slots = []
            return
        px = scene.raws.get(min(frame_idx, 59))
        if px is None:
            px = scene.frames[min(frame_idx, 59)]
        half = QUANT // 2
        ys = np.arange(half, HEIGHT, QUANT)
        xs = np.arange(half, WIDTH, QUANT)
        diff = np.abs(self.bg_px[ys][:, xs, :3].astype(np.int16)
                      - px[ys][:, xs, :3].astype(np.int16)) >= 16
        yy, xx = np.nonzero(diff.any(axis=2))
        self.slots = [(int(xs[j]), int(ys[i]),
                       random.randrange(SPRITE_VARIANTS),
                       random.randint(-3, 3))
                      for i, j in zip(yy, xx)]

    # ─── compositing (canvas drawSlots semantics, straight alpha) ──────

    def _composite(self, alpha, t):
        """Held frame at ramping alpha + overlay-blend sparkles."""
        scene = self.scenes.get(self.current)
        if scene is None:
            return
        src = scene.frames[min(self.held_frame, 59)]
        t0 = time.perf_counter()
        self._staging = _composite_pixels(
            src, alpha, t, self.slots, self.sprites, self.clip_mask)
        self._last_composite_ms = (time.perf_counter() - t0) * 1000.0

    # ─── render / queries ──────────────────────────────────────────────

    def render(self, screen, pos_x, pos_y):
        """Blit the room at its map position (camera at pos_x, pos_y)."""
        if not self.ready:
            return
        scene = self.scenes.get(self.current)
        if self.phase in (self.MATERIALIZE, self.DEMATERIALIZE) \
                and self._staging is not None:
            surf = _surface(self._staging)
        elif self.phase == self.PLAY and scene is not None:
            surf = _surface(scene.frames[min(self.slot_frame, 59)])
        else:
            return
        screen.blit(surf, (self.origin_x - pos_x, self.origin_y - pos_y))

    def live_index(self):
        """Highlighter gating: teleport N is selectable while it plays
        live (post-materialize, pre-dematerialize)."""
        if self.ready and self.phase == self.PLAY:
            return self.current
        return None

    @property
    def is_ready_for_draw(self):
        return self.ready and bool(self.scenes)


# ─── module-level helpers (unit-testable, pygame-free) ─────────────────────

def _composite_pixels(src, alpha, t, slots, sprites, clip_mask):
    """Pure composite math (site drawSlots semantics) in STRAIGHT alpha.

    1) image pass — canvas drawImage at globalAlpha over a cleared
       canvas: alpha scales, colors stay straight (unlike the tvOS
       premultiplied storage, pygame blits are straight-alpha);
    2) sprite pass — 'overlay' composite at FULL opacity (the site
       restores globalAlpha before drawing sparkles), W3C blend with a
       straight-alpha sprite source over the (semi-transparent) canvas,
       clipped to the room polygon.  t >= SPRITE_COLS draws nothing.

    Perf: sprite cells are mostly transparent (sparkles) — each
    (variant, column) cell is cropped to its opaque bounding box at
    blend time via :func:`_sprite_cell_bbox`, cutting the per-slot
    work ~5-10x (needed: a dense room diffs ~1100 slots per beat).
    """
    out = src.copy()
    out[:, :, 3] = np.clip(
        src[:, :, 3].astype(np.float32) * alpha + 0.5, 0, 255
    ).astype(np.uint8)

    if not slots or t >= SPRITE_COLS or sprites is None:
        return out

    for x, y, variant, shift in slots:
        bx0, by0, bx1, by1 = _sprite_cell_bbox(sprites, variant, t)
        if bx0 >= bx1 or by0 >= by1:
            continue
        dx = x - SPRITE_CX + shift + bx0
        dy = y - SPRITE_CY + by0
        # Destination window (clipped to the canvas).
        x0, x1 = max(0, dx), min(WIDTH, dx + (bx1 - bx0))
        y0, y1 = max(0, dy), min(HEIGHT, dy + (by1 - by0))
        if x0 >= x1 or y0 >= y1:
            continue
        # Source window (sheet cell origin + bbox, matched to the
        # destination clip).
        sxx0 = t * SPRITE_W + bx0 + (x0 - dx)
        syy0 = variant * SPRITE_H + by0 + (y0 - dy)
        w, h = x1 - x0, y1 - y0

        sp = sprites[syy0:syy0 + h, sxx0:sxx0 + w].astype(np.float32) / 255.0
        sa = sp[:, :, 3]
        keep = sa > 0
        if not keep.any():
            continue
        br = out[y0:y1, x0:x1, 3].astype(np.float32) / 255.0
        dr = out[y0:y1, x0:x1, :3].astype(np.float32) / 255.0
        sr, sg, sb = sp[:, :, 0], sp[:, :, 1], sp[:, :, 2]

        ao = sa + br * (1 - sa)

        def ov(cs, cb):
            return np.where(cb <= 0.5, 2 * cs * cb,
                            1 - 2 * (1 - cs) * (1 - cb))

        denom = np.maximum(ao, 1e-6)
        cr = (sa * (1 - br) * sr + sa * br * ov(sr, dr[:, :, 0])
              + (1 - sa) * br * dr[:, :, 0]) / denom
        cg = (sa * (1 - br) * sg + sa * br * ov(sg, dr[:, :, 1])
              + (1 - sa) * br * dr[:, :, 1]) / denom
        cb_ = (sa * (1 - br) * sb + sa * br * ov(sb, dr[:, :, 2])
               + (1 - sa) * br * dr[:, :, 2]) / denom

        # Site clips the ENTIRE work canvas to the room polygon.
        m = keep & clip_mask[y0:y1, x0:x1]
        out[y0:y1, x0:x1, 0] = np.where(m, np.clip(cr * 255 + 0.5, 0, 255),
                                        out[y0:y1, x0:x1, 0])
        out[y0:y1, x0:x1, 1] = np.where(m, np.clip(cg * 255 + 0.5, 0, 255),
                                        out[y0:y1, x0:x1, 1])
        out[y0:y1, x0:x1, 2] = np.where(m, np.clip(cb_ * 255 + 0.5, 0, 255),
                                        out[y0:y1, x0:x1, 2])
        out[y0:y1, x0:x1, 3] = np.where(m, np.clip(ao * 255 + 0.5, 0, 255),
                                        out[y0:y1, x0:x1, 3])
    return out


_bbox_cache = {}


def _sprite_cell_bbox(sprites, variant, col):
    """Opaque bounding box (x0, y0, x1, y1) inside a 60x100 sprite cell
    — sparkle cells are mostly transparent, so blending just the box is
    5-10x cheaper than the full cell."""
    key = (id(sprites), variant, col)
    hit = _bbox_cache.get(key)
    if hit is not None and hit[0] is sprites:
        return hit[1]
    y0 = variant * SPRITE_H
    x0 = col * SPRITE_W
    cell = sprites[y0:y0 + SPRITE_H, x0:x0 + SPRITE_W, 3]
    rows = np.any(cell > 0, axis=1)
    cols = np.any(cell > 0, axis=0)
    if not rows.any():
        box = (0, 0, 0, 0)
    else:
        ys = np.nonzero(rows)[0]
        xs = np.nonzero(cols)[0]
        box = (int(xs[0]), int(ys[0]), int(xs[-1]) + 1, int(ys[-1]) + 1)
    if len(_bbox_cache) > 256:
        _bbox_cache.clear()
    _bbox_cache[key] = (sprites, box)
    return box


_surf_cache = {}


def _surface(arr):
    """(H,W,4) uint8 array -> pygame surface.

    Cached per array identity; entries keep a reference to the source
    array so ids cannot be recycled while cached.
    """
    key = id(arr)
    hit = _surf_cache.get(key)
    if hit is not None and hit[0] is arr:
        return hit[1]
    surf = pygame.image.frombuffer(arr.tobytes(), (WIDTH, HEIGHT),
                                   'RGBA').convert_alpha()
    if len(_surf_cache) > 8:
        _surf_cache.clear()
    _surf_cache[key] = (arr, surf)
    return surf
