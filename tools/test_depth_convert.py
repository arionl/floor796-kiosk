#!/usr/bin/env python3
"""Test TileCache._ensure_display_depth (numpy 24->32 fast path).

Covers: pixel-exact conversion, surface NOT left locked (numpy views
keep the BufferProxy alive — a locked surface raises on every blit),
no-op when depths already match, and fallback path correctness."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402
pygame.init()
pygame.display.set_mode((8, 8))

import numpy as np  # noqa: E402

from floor796_kiosk.player import TileCache  # noqa: E402


def make_cache():
    return TileCache('/tmp/nonexistent-strips', max_tiles=4,
                     display_depth=pygame.display.get_surface()
                     .get_bitsize())


def make_strip(w=64, h=40):
    s = pygame.Surface((w, h), 0, 24)
    rng = np.random.default_rng(7)
    arr = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
    px = pygame.surfarray.pixels3d(s)
    px[...] = np.transpose(arr, (1, 0, 2))
    del px
    return s, arr


class TestDepthConvert(unittest.TestCase):

    def test_convert_is_pixel_exact(self):
        cache = make_cache()
        self.assertEqual(cache.display_depth, 32)
        src, arr = make_strip()
        tgt = cache._ensure_display_depth(src)
        self.assertEqual(tgt.get_bitsize(), 32)
        for x, y in ((0, 0), (31, 19), (63, 39), (5, 7)):
            c = tgt.get_at((x, y))
            self.assertEqual((c.r, c.g, c.b), tuple(arr[y, x]),
                             f'pixel {(x, y)}')

    def test_converted_surface_is_not_locked(self):
        # The numpy fast path writes through get_view buffers, which
        # lock the surface; every reference must be dropped before the
        # surface is returned or every blit raises.
        cache = make_cache()
        src, _ = make_strip()
        tgt = cache._ensure_display_depth(src)
        self.assertFalse(tgt.get_locked())
        sc = pygame.Surface((128, 128), 0, 32)
        sc.blit(tgt, (0, 0))          # must not raise
        self.assertEqual(sc.get_at((5, 5))[:3],
                         tgt.get_at((5, 5))[:3])

    def test_same_depth_is_noop(self):
        cache = make_cache()
        s = pygame.Surface((16, 16), 0, 32)
        self.assertIs(cache._ensure_display_depth(s), s)

    def test_repeated_conversions_are_stable(self):
        cache = make_cache()
        for _ in range(3):
            src, arr = make_strip()
            tgt = cache._ensure_display_depth(src)
            self.assertFalse(tgt.get_locked())
            self.assertEqual(tgt.get_at((10, 10)).r, int(arr[10, 10, 0]))


if __name__ == '__main__':
    unittest.main(verbosity=2)
