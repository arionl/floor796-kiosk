#!/usr/bin/env python3
"""Test the depth-aware tile-cache memory budget.

Real per-strip costs: 201 MB at 32-bit display format, 100 MB at
16-bit.  The cap must apply on ALL boards (the 4 GB board previously
ran uncapped at 18 × 201 MB ≈ 3.6 GB → 430 MB swapped).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from floor796_kiosk.player import (  # noqa: E402
    _compute_max_tiles, _strip_cost_mb, MAX_TILES_BASE)


class TestMemBudget(unittest.TestCase):

    def test_strip_cost(self):
        self.assertEqual(_strip_cost_mb(32), 201)
        self.assertEqual(_strip_cost_mb(16), 100)

    def test_2gb_board_16bit(self):
        # 2000 MB total: reserve 1200 → 800 MB → 8 tiles.
        self.assertEqual(
            _compute_max_tiles(1920, 1080, 2000, 100), 8)

    def test_2gb_board_32bit_would_not_fit(self):
        # Same board with 32-bit strips: 800/201 = 3 tiles — below the
        # ~6-9 visible tiles at 1080p.  This is why low-RAM boards pin
        # depth=16.
        self.assertEqual(
            _compute_max_tiles(1920, 1080, 2000, 201), 3)

    def test_4gb_board_32bit_capped(self):
        # 4045 MB: reserve 1999 → 2046 MB → 10 tiles (was 18 uncapped
        # = 3.6 GB of strips).
        self.assertEqual(
            _compute_max_tiles(1920, 1080, 4045, 201), 10)

    def test_4gb_board_16bit_gets_full_cache(self):
        self.assertEqual(
            _compute_max_tiles(1920, 1080, 4045, 100),
            MAX_TILES_BASE)

    def test_8gb_board(self):
        # 8192: reserve 3657 → 4535/201 = 22 → area cap 18.
        self.assertEqual(
            _compute_max_tiles(1920, 1080, 8192, 201), MAX_TILES_BASE)

    def test_tiny_board_floor(self):
        # Never below 2 tiles.
        self.assertEqual(
            _compute_max_tiles(1920, 1080, 512, 100), 2)

    def test_unknown_memory(self):
        self.assertEqual(
            _compute_max_tiles(1920, 1080, 0, 201), MAX_TILES_BASE)


if __name__ == '__main__':
    unittest.main(verbosity=2)
