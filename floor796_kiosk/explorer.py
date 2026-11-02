#!/usr/bin/env python3
"""explorer.py — Exploring mode's selection engine.

1:1 port of floor796-tvos App/Sources/Explorer.swift (spec 2026-11-01):
object-to-object navigation on the fixed-scale map.  The camera
re-centers on a selected object with a short eased flight.  One flick
(here: one arrow KEYDOWN) jumps to the nearest object whose center lies
within a 90° cone around the flick direction; if none, the nearest
within the 180° half-plane; if still none, the selection stays.  Input
is ignored while a flight is in progress and for a short cooldown after
landing.

Objects are the kiosk's ObjectSegments (highlighter.py) — one per tile
bounding box, with absolute-map cx/cy.  Pure logic: no pygame.

Ghost boxes: tvOS additionally draws gray ghost boxes for viewport
objects while exploring; the kiosk skips them for now (per-frame cost
on the Pi was the concern) — revisit if wanted.
"""

import math

from floor796_kiosk.highlighter import MIN_BBOX_SIZE, TileObjectIndex

FLIGHT_DURATION = 0.4      # s, camera flight to the selected object
LANDING_COOLDOWN = 0.3     # s, input lock after landing
CONE_HALF_ANGLE_COS = 0.7071067811865476   # cos(45°) — half of the 90° cone


class Explorer:
    """Exploring mode engine (port of tvOS Explorer)."""

    def __init__(self, segments, spacing_w=1016, spacing_h=812):
        # Same minimum-size rule as the highlighter's selection pass —
        # specks nobody can read a callout for are not jump targets.
        self.segments = [
            s for s in segments
            if not (s.width < MIN_BBOX_SIZE and s.height < MIN_BBOX_SIZE)
        ]
        self.index = TileObjectIndex(self.segments, spacing_w, spacing_h)
        self.selection = None

        # Flight state (camera-CENTER coordinates, map px).
        self._flight_from = None
        self._flight_to = None
        self._flight_t = 0.0
        # Seconds since the current selection was made (drives the red
        # box's pulsation phase, mirroring the highlighter timer).
        self.selection_age = 0.0
        # Post-landing input cooldown, counted down by update(dt).
        self._cooldown_remaining = 0.0

    # ── Entry ────────────────────────────────────────────────────────

    def enter(self, cam_x, cam_y):
        """Select the object whose center is nearest the camera center
        and begin a flight to it.  Returns the new selection (None when
        there are no objects — caller should treat Exploring as
        unavailable).  Mode entry ALWAYS re-anchors (tvOS rule:
        never snap back to a previous stint's selection)."""
        first = self.nearest(cam_x, cam_y, self.segments)
        if first is None:
            return None
        self._select(first, cam_x, cam_y)
        return first

    def resume(self, cam_x, cam_y):
        """Re-enter with a previous selection kept: fly the camera from
        its current position back onto the selection (it may have
        drifted, e.g. under Wandering)."""
        if self.selection is None:
            return self.enter(cam_x, cam_y)
        self._select(self.selection, cam_x, cam_y)
        return self.selection

    # ── Jumping ──────────────────────────────────────────────────────

    @property
    def is_flying(self):
        return self._flight_to is not None

    def can_jump(self):
        """True when a flick may start a new flight (no flight running,
        landing cooldown elapsed)."""
        return not self.is_flying and self._cooldown_remaining <= 0

    def jump(self, dx, dy, cam_x, cam_y):
        """Directional jump.  (dx, dy) is the flick direction in screen
        axes (y down), which match map axes 1:1.  Returns the newly
        selected segment, or None (stay put / input locked)."""
        if not self.can_jump():
            return None
        cur = self.selection
        if cur is None:
            return None
        nxt = self.pick(cur, dx, dy, self.segments)
        if nxt is None:
            return None
        self._select(nxt, cam_x, cam_y)
        return nxt

    def _select(self, seg, from_x, from_y):
        self.selection = seg
        self.selection_age = 0.0
        self._flight_from = (from_x, from_y)
        self._flight_to = (seg.cx, seg.cy)
        self._flight_t = 0.0

    def update(self, dt):
        """Advance the flight.  Returns the camera CENTER (x, y) to
        apply this frame, or None when no flight is running."""
        self.selection_age += dt
        if self._cooldown_remaining > 0:
            self._cooldown_remaining = max(0.0,
                                           self._cooldown_remaining - dt)
        if self._flight_from is None or self._flight_to is None:
            return None
        self._flight_t += dt
        raw = min(1.0, self._flight_t / FLIGHT_DURATION)
        # Ease-out cubic — fast start, gentle landing.
        eased = 1.0 - (1.0 - raw) ** 3
        x = self._flight_from[0] + (self._flight_to[0]
                                    - self._flight_from[0]) * eased
        y = self._flight_from[1] + (self._flight_to[1]
                                    - self._flight_from[1]) * eased
        if raw >= 1.0:
            self._flight_from = None
            self._flight_to = None
            self._cooldown_remaining = LANDING_COOLDOWN
        return (x, y)

    def cancel_flight(self):
        """Stop any in-flight camera motion (mode exit).  The selection
        is kept so re-entering Exploring can resume from it."""
        self._flight_from = None
        self._flight_to = None

    def viewport_objects(self, vp_x, vp_y, vp_w, vp_h):
        """Objects intersecting the viewport — for gray ghost boxes."""
        return self.index.query_viewport(vp_x, vp_y,
                                         vp_x + vp_w, vp_y + vp_h)

    # ── Pure selection geometry (unit-tested) ────────────────────────

    @staticmethod
    def pick(from_seg, dx, dy, segments):
        """Nearest object center strictly within a 90° cone around the
        unit direction (dx, dy); falls back to the 180° half-plane;
        None when nothing lies ahead.  `from_seg` itself (by obj_id)
        is excluded."""
        length = math.hypot(dx, dy)
        if length <= 0:
            return None
        ux, uy = dx / length, dy / length

        best_cone = None    # (distance, seg)
        best_half = None
        for seg in segments:
            if seg.obj_id == from_seg.obj_id:
                continue
            vx = seg.cx - from_seg.cx
            vy = seg.cy - from_seg.cy
            d = math.hypot(vx, vy)
            if d <= 0:
                continue
            cos_a = (vx * ux + vy * uy) / d
            if cos_a >= CONE_HALF_ANGLE_COS:
                if best_cone is None or d < best_cone[0]:
                    best_cone = (d, seg)
            elif cos_a > 0:
                if best_half is None or d < best_half[0]:
                    best_half = (d, seg)
        if best_cone is not None:
            return best_cone[1]
        return best_half[1] if best_half is not None else None

    @staticmethod
    def nearest(x, y, segments, excluding=None):
        """Object with the center nearest (x, y).  `excluding` skips an
        already-selected object (by obj_id)."""
        best = None         # (distance², seg)
        for seg in segments:
            if excluding is not None and seg.obj_id == excluding.obj_id:
                continue
            d = (seg.cx - x) ** 2 + (seg.cy - y) ** 2
            if best is None or d < best[0]:
                best = (d, seg)
        return best[1] if best is not None else None
