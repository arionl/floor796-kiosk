#!/usr/bin/env python3
"""Test explorer.py — selection geometry, flight, cooldown.

Pure geometry (no pygame): the 90°-cone → 180°-half-plane → stay-put
jump rule, entry nearest-center selection, flight timing, cooldown,
and the min-size object filter.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from floor796_kiosk.explorer import (  # noqa: E402
    Explorer, FLIGHT_DURATION, LANDING_COOLDOWN)
from floor796_kiosk.highlighter import ObjectSegment  # noqa: E402


def obj(obj_id, cx, cy, w=40, h=40):
    """A w×h object centered at (cx, cy)."""
    return ObjectSegment(obj_id, f'obj{obj_id}', '2026-01-01',
                         'https://floor796.com/', 't0r0,0,0',
                         cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


class TestPickGeometry(unittest.TestCase):
    # ── pick(): directional jump rule ──────────────────────────────

    def test_pick_nearest_within_cone(self):
        from_seg = obj(0, 0, 0)
        near = obj(1, 500, 0)        # dead ahead, closest
        far = obj(2, 5000, 0)        # dead ahead, far
        side = obj(3, 0, 100)        # 90° off — outside the cone
        got = Explorer.pick(from_seg, 1, 0,
                            [from_seg, near, far, side])
        self.assertEqual(got.obj_id, near.obj_id)

    def test_pick_diagonal_cone_boundary(self):
        from_seg = obj(0, 0, 0)
        # Exactly 45° off-axis is INSIDE the 90° cone (cos45).
        diag = obj(1, 100, 100)
        straight = obj(2, 50, 0)
        self.assertEqual(
            Explorer.pick(from_seg, 1, 0, [from_seg, diag]).obj_id,
            diag.obj_id)
        self.assertEqual(
            Explorer.pick(from_seg, 1, 0,
                          [from_seg, diag, straight]).obj_id,
            straight.obj_id)

    def test_pick_falls_back_to_half_plane(self):
        from_seg = obj(0, 0, 0)
        behind = obj(1, -500, 0)     # behind: outside half-plane
        wide = obj(2, 10, 400)       # ~88° off-axis: half-plane only
        got = Explorer.pick(from_seg, 1, 0, [from_seg, behind, wide])
        self.assertEqual(got.obj_id, wide.obj_id)

    def test_pick_stays_put_when_nothing_ahead(self):
        from_seg = obj(0, 0, 0)
        behind = obj(1, -500, 0)
        self.assertIsNone(
            Explorer.pick(from_seg, 1, 0, [from_seg, behind]))

    def test_pick_excludes_self_and_zero_vector(self):
        from_seg = obj(0, 0, 0)
        other = obj(1, 300, 0)
        self.assertIsNone(Explorer.pick(from_seg, 0, 0,
                                        [from_seg, other]),
                          'zero direction must not jump')
        self.assertIsNone(Explorer.pick(from_seg, 1, 0, [from_seg]),
                          'must never re-select the current object')

    # ── nearest(): entry selection ─────────────────────────────────

    def test_nearest_picks_closest_center(self):
        a, b = obj(1, 100, 100), obj(2, 300, 100)
        self.assertEqual(
            Explorer.nearest(90, 90, [a, b]).obj_id, a.obj_id)
        self.assertEqual(
            Explorer.nearest(290, 100, [a, b]).obj_id, b.obj_id)
        self.assertIsNone(Explorer.nearest(90, 90, [a], excluding=a))


class TestExplorerInstance(unittest.TestCase):
    # ── entry, jump, flight, cooldown ──────────────────────────────

    def test_enter_selects_nearest_and_flies(self):
        ex = Explorer([obj(1, 0, 0), obj(2, 1000, 0)])
        sel = ex.enter(120, 0)
        self.assertEqual(sel.obj_id, 1)
        self.assertTrue(ex.is_flying)
        # Flight advances toward the target and completes in ~duration.
        t, last = 0.0, None
        while True:
            c = ex.update(1.0 / 60.0)
            if c is None:
                break
            last = c
            t += 1.0 / 60.0
            self.assertLess(t, 1.0, 'flight must terminate')
        self.assertAlmostEqual(last[0], 0, delta=0.5)
        self.assertFalse(ex.is_flying)
        self.assertAlmostEqual(t, FLIGHT_DURATION, delta=0.05)

    def test_jump_cooldown_and_direction(self):
        ex = Explorer([obj(1, 0, 0), obj(2, 1000, 0), obj(3, -2000, 0)])
        ex.enter(0, 0)
        # Burn the flight to completion (the landing frame arms the
        # cooldown; it ticks down in update()).
        while ex.update(1.0 / 60.0) is not None:
            pass
        ex.update(1.0 / 60.0)   # advance time into the cooldown
        # During cooldown: no jump.
        self.assertFalse(ex.can_jump())
        self.assertIsNone(ex.jump(1, 0, 0, 0))
        # Burn the cooldown (0.3 s of update calls).
        for _ in range(20):
            ex.update(1.0 / 60.0)
        self.assertTrue(ex.can_jump())
        # Forward jump picks the object ahead.
        self.assertEqual(ex.jump(1, 0, 0, 0).obj_id, 2)
        while ex.update(1.0 / 60.0) is not None:
            pass
        for _ in range(20):
            ex.update(1.0 / 60.0)
        # From obj2, +x has nothing ahead: stays put (obj1 is behind,
        # obj3 far behind).
        self.assertIsNone(ex.jump(1, 0, 1000, 0))
        self.assertEqual(ex.selection.obj_id, 2)

    def test_min_size_filter(self):
        # A sub-minimum speck in both dimensions is not a jump target.
        speck = ObjectSegment(9, 'speck', '', '', 't0r0,0,0',
                              500, 0, 501, 1)
        from_seg = obj(1, 0, 0)
        ex = Explorer([from_seg, speck])
        # Entry still works (nearest selectable = from), but a jump
        # toward the speck finds nothing — it was filtered out.
        self.assertEqual(ex.enter(0, 0).obj_id, from_seg.obj_id)
        while ex.update(1.0 / 60.0) is not None:
            pass
        for _ in range(20):
            ex.update(1.0 / 60.0)
        self.assertIsNone(ex.jump(1, 0, 0, 0),
                          'speck must be filtered out of jump targets')

    def test_viewport_objects_excludes_outside(self):
        inside, outside = obj(1, 500, 500), obj(2, 5000, 500)
        ex = Explorer([inside, outside])
        hits = ex.viewport_objects(0, 0, 1920, 1080)
        self.assertEqual([s.obj_id for s in hits], [inside.obj_id])

    def test_resume_keeps_selection(self):
        ex = Explorer([obj(1, 0, 0), obj(2, 400, 0)])
        ex.enter(0, 0)
        while ex.update(1.0 / 60.0) is not None:
            pass
        ex.cancel_flight()
        # Camera drifted to obj2's position; resume re-flies to obj1.
        sel = ex.resume(400, 0)
        self.assertEqual(sel.obj_id, 1)
        self.assertTrue(ex.is_flying)
        last = None
        while True:
            c = ex.update(1.0 / 60.0)
            if c is None:
                break
            last = c
        self.assertAlmostEqual(last[0], 0, delta=0.5)

    def test_multiple_segments_same_object_all_excluded(self):
        # Kiosk twist: one object spanning tiles has several segments
        # sharing obj_id — a jump must skip ALL of them.
        a1 = obj(1, 0, 0)
        a2 = obj(1, 50, 80)          # same object, another tile box
        b = obj(2, 300, 0)
        ex = Explorer([a1, a2, b])
        ex.enter(0, 0)
        while ex.update(1.0 / 60.0) is not None:
            pass
        for _ in range(20):
            ex.update(1.0 / 60.0)
        self.assertEqual(ex.jump(1, 0, 0, 0).obj_id, 2)
        self.assertAlmostEqual(LANDING_COOLDOWN, 0.3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
