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
    _compute_max_tiles, _strip_cost_mb, _compute_tile_budget_mb,
    _static_tile_cost_mb, TileCache, MAX_TILES_BASE)


class TestTileBudgetFn(unittest.TestCase):

    def test_budget_matches_reserve_formula(self):
        # Same reserve as the count budget: 4 GB → 2047 MB spendable.
        self.assertEqual(_compute_tile_budget_mb(4045), 2047)
        self.assertEqual(_compute_tile_budget_mb(8192), 4535)
        self.assertEqual(_compute_tile_budget_mb(2000), 800)

    def test_budget_unknown_memory(self):
        self.assertIsNone(_compute_tile_budget_mb(0))

    def test_static_cost(self):
        self.assertAlmostEqual(_static_tile_cost_mb(32), 3.36, delta=0.05)
        self.assertAlmostEqual(_static_tile_cost_mb(16), 1.68, delta=0.05)


class TestCostWeightedCache(unittest.TestCase):
    """TileCache admission + eviction under an MB budget.

    Uses the set_needed/poll_results plumbing with dummy surfaces —
    costs are metadata-driven (animated_ids), not surface-driven.
    """

    def setUp(self):
        import pygame
        pygame.init()
        self.animated = {"anim1", "anim2", "anim3"}
        self.cache = TileCache(
            "/nonexistent", max_tiles=40, display_depth=32,
            mem_budget_mb=450, animated_ids=self.animated)

    def _fill(self, tids):
        for tid in tids:
            self.cache.cache[tid] = (None, 60 if tid in self.animated else 1)
            self.cache._cost_mb[tid] = self.cache._tile_cost_mb(tid)

    def test_admission_statics_always_animated_gated(self):
        # Visible: 2 animated (402 MB).  Budget 450 → one more animated
        # margin tile (603) must NOT be admitted; statics are.
        vis = {"anim1", "anim2"}
        margin = {"anim3", "stat1", "stat2"}
        self.cache.set_needed(vis, margin)
        queued = self.cache._queue
        self.assertIn("stat1", queued)
        self.assertIn("stat2", queued)
        self.assertNotIn("anim3", queued)

    def test_admission_admits_animated_while_budget_allows(self):
        # Budget 450 with 1 visible animated (201): a second animated
        # margin tile fits (402 ≤ 450) and is admitted.
        self.cache.mem_budget_mb = 450
        self.cache.set_needed({"anim1"}, {"anim2"})
        self.assertIn("anim2", self.cache._queue)

    def test_admission_nothing_when_visible_already_over(self):
        # Visible alone over budget → ring skipped entirely (no churn).
        # The visible tiles themselves are still queued (they're on
        # screen); only the margin admission is gated.
        self.cache.set_needed({"anim1", "anim2", "anim3"}, {"stat1"})
        self.assertNotIn("stat1", self.cache._queue)

    def test_eviction_prefers_expensive_non_visible(self):
        self._fill(["anim1", "anim2", "stat1", "stat2"])
        self.cache._visible_ids = {"anim1"}
        self.cache._needed_ids = {"anim1", "anim2", "stat1", "stat2"}
        # 402 + 6.7 MB cached vs 300 MB budget → the non-visible
        # animated strip (201 MB) goes first, statics survive.
        self.cache.mem_budget_mb = 300
        self.cache._results = {}
        self.cache.poll_results()
        self.assertNotIn("anim2", self.cache.cache)
        self.assertIn("stat1", self.cache.cache)
        self.assertIn("stat2", self.cache.cache)
        self.assertIn("anim1", self.cache.cache)  # visible never evicted

    def test_poll_results_evicts_to_budget(self):
        # Simulate the worker returning an over-budget batch while a
        # visible tile is already cached: non-visible results are
        # evicted (biggest first) down to budget; visible survives.
        self._fill(["anim1"])
        self.cache._visible_ids = {"anim1"}
        self.cache._needed_ids = {"anim1", "anim2", "anim3"}
        self.cache._results = {"anim2": (None, 60), "anim3": (None, 60)}
        self.cache.mem_budget_mb = 300
        self.cache.poll_results()
        self.assertEqual(self.cache._cached_cost_mb(), 201)
        self.assertIn("anim1", self.cache.cache)
        self.assertNotIn("anim2", self.cache.cache)
        self.assertNotIn("anim3", self.cache.cache)


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
