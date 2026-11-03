#!/usr/bin/env python3
"""Test ghost-box rendering (Exploring mode).

Covers _rect_subtract (mask-hole equivalent for outlined boxes) and
_draw_ghost_box: outlined boxes overlapping the highlighted
object's content area are CLIPPED — never painted inside the exclusion
rect — while unclipped boxes draw a complete outline.
"""

import os
import sys
import unittest

import pygame

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from floor796_kiosk.player import _rect_subtract, _draw_ghost_box  # noqa: E402

pygame.init()
pygame.display.set_mode((8, 8), pygame.SRCALPHA)

COLOR = (255, 0, 0)


class TestRectSubtract(unittest.TestCase):

    def test_no_overlap_returns_whole(self):
        self.assertEqual(_rect_subtract((0, 0, 10, 10), (20, 20, 5, 5)),
                         [(0, 0, 10, 10)])

    def test_full_containment_returns_empty(self):
        self.assertEqual(_rect_subtract((0, 0, 10, 10), (0, 0, 10, 10)), [])

    def test_partial_split(self):
        # b cuts the right half of a: left strip remains.
        parts = _rect_subtract((0, 0, 10, 10), (5, 0, 10, 10))
        self.assertEqual(parts, [(0, 0, 5, 10)])

    def test_center_hole_makes_four_strips(self):
        parts = _rect_subtract((0, 0, 10, 10), (3, 3, 4, 4))
        self.assertEqual(len(parts), 4)
        # Pixel-level coverage: a\b painted, b and outside not.
        surf = pygame.Surface((10, 10))
        for r in parts:
            pygame.draw.rect(surf, COLOR, r)
        px = pygame.surfarray.pixels2d(surf)
        import pygame as pg
        rgb = pygame.surfarray.array3d(surf)
        for x in range(10):
            for y in range(10):
                painted = tuple(rgb[x, y]) == COLOR
                inside_b = 3 <= x < 7 and 3 <= y < 7
                inside_a = 0 <= x < 10 and 0 <= y < 10
                if inside_b:
                    self.assertFalse(painted, f'pixel {x},{y} in hole')
                elif inside_a:
                    self.assertTrue(painted, f'pixel {x},{y} missing')
        del px


class TestGhostBox(unittest.TestCase):

    def _painted(self, surf, x, y):
        return tuple(pygame.surfarray.array3d(surf)[x, y]) == COLOR

    def test_clipped_box_never_paints_inside_exclusion(self):
        surf = pygame.Surface((100, 100))
        box = (10, 10, 80, 80)
        excl = pygame.Rect(40, 40, 20, 20)
        _draw_ghost_box(surf, COLOR, box, excl, width=2)
        # Nothing painted inside the exclusion.
        for x in range(40, 60):
            for y in range(40, 60):
                self.assertFalse(self._painted(surf, x, y),
                                 f'painted inside hole at {x},{y}')
        # The parts of the outline outside the hole ARE painted (top
        # edge, left of the hole).
        self.assertTrue(self._painted(surf, 20, 10))
        self.assertTrue(self._painted(surf, 20, 11))
        self.assertTrue(self._painted(surf, 10, 20))
        self.assertTrue(self._painted(surf, 89, 89))

    def test_unclipped_box_draws_full_outline(self):
        surf = pygame.Surface((100, 100))
        box = (20, 20, 60, 60)
        _draw_ghost_box(surf, COLOR, box, None, width=2)
        # Edge midpoints painted; interior and exterior not.
        self.assertTrue(self._painted(surf, 50, 20))
        self.assertTrue(self._painted(surf, 50, 79))
        self.assertTrue(self._painted(surf, 20, 50))
        self.assertTrue(self._painted(surf, 79, 50))
        self.assertFalse(self._painted(surf, 50, 50))
        self.assertFalse(self._painted(surf, 5, 5))

    def test_disjoint_exclusion_draws_full_outline(self):
        surf = pygame.Surface((100, 100))
        box = (20, 20, 60, 60)
        _draw_ghost_box(surf, COLOR, box, pygame.Rect(0, 0, 5, 5),
                        width=2)
        self.assertTrue(self._painted(surf, 50, 20))
        self.assertTrue(self._painted(surf, 20, 50))


if __name__ == '__main__':
    unittest.main(verbosity=2)
