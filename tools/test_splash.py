#!/usr/bin/env python3
"""Test splash.py — boot splash with the station animation strip.

Cold path: ffmpeg decodes the mp4 once into a 60-frame strip; warm
path: the cached BMP loads; fallback path: no assets → legacy
text-only layout.  All exercised against a dummy display.
"""

import os
import shutil
import sys
import time
import unittest

import pygame

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from floor796_kiosk import splash as S  # noqa: E402
from floor796_kiosk.splash import (  # noqa: E402
    SplashStatusDisplay, create_status_display, load_splash_assets,
    SPLASH_FRAMES, SPLASH_CACHE_DIR)

pygame.init()
screen = pygame.display.set_mode((8, 8))


class TestSplash(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Real assets from assets/splash/, but an ISOLATED strip cache
        # so the test never clobbers (or depends on the permissions
        # of) the production cache decoded for the real display size.
        import tempfile
        cls._tmp = tempfile.mkdtemp(prefix='f796_splash_test_')
        S.SPLASH_CACHE_DIR = cls._tmp
        cls.square = int(600 * 0.66)
        cls.strip, cls.frame = load_splash_assets(
            pygame.display.set_mode((800, 600)))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_strip_decoded_with_60_frames(self):
        if not os.path.exists(S.SPLASH_MP4):
            self.skipTest('splash mp4 not shipped')
        self.assertIsNotNone(self.strip)
        self.assertEqual(self.strip.get_width(), self.square)
        self.assertEqual(self.strip.get_height(),
                         self.square * SPLASH_FRAMES)

    def test_frame_overlay_loaded(self):
        if not os.path.exists(S.SPLASH_FRAME_PNG):
            self.skipTest('splash frame not shipped')
        self.assertIsNotNone(self.frame)
        self.assertEqual(self.frame.get_size(), (800, 600))

    def test_warm_cache_load(self):
        if self.strip is None:
            self.skipTest('decode unavailable')
        strip2, _ = load_splash_assets(
            pygame.display.set_mode((800, 600)))
        self.assertIsNotNone(strip2)
        self.assertEqual(strip2.get_size(), self.strip.get_size())

    def test_show_renders_and_advances_frames(self):
        if self.strip is None:
            self.skipTest('decode unavailable')
        scr = pygame.display.set_mode((800, 600))
        d = SplashStatusDisplay(scr, self.strip, self.frame)
        d.show('Booting', 'step 1/2', progress=0.5)
        # The video square region must be non-background (animation
        # blitted) at the center.
        cx, cy = 400, 300
        self.assertNotEqual(scr.get_at((cx, cy))[:3], S.BG)
        d.release()
        self.assertIsNone(d.strip)

    def test_ticker_animates_between_show_calls(self):
        """The video must keep animating while boot phases block
        between status updates — no show() calls for a while."""
        if self.strip is None:
            self.skipTest('decode unavailable')
        scr = pygame.display.set_mode((800, 600))
        d = SplashStatusDisplay(scr, self.strip, self.frame)
        try:
            flips = {'n': 0}
            real_flip = pygame.display.flip

            def counting_flip():
                flips['n'] += 1
                real_flip()

            pygame.display.flip = counting_flip
            try:
                d.show('Booting', 'step 1/2', progress=0.5)
                before = flips['n']
                time.sleep(0.6)          # ~7 frames at 12 fps, no show()
                gained = flips['n'] - before
                self.assertGreaterEqual(gained, 3,
                                        f'only {gained} redraws in 0.6 s')
            finally:
                pygame.display.flip = real_flip
        finally:
            d.release()

    def test_release_stops_ticker(self):
        if self.strip is None:
            self.skipTest('decode unavailable')
        scr = pygame.display.set_mode((800, 600))
        d = SplashStatusDisplay(scr, self.strip, self.frame)
        d.show('Booting')
        d.release()
        self.assertTrue(d._stop.is_set())
        self.assertIsNone(d._ticker)
        flips = {'n': 0}
        real_flip = pygame.display.flip

        def counting_flip():
            flips['n'] += 1

        pygame.display.flip = counting_flip
        try:
            time.sleep(0.4)
            self.assertEqual(flips['n'], 0,
                             'ticker still rendering after release()')
        finally:
            pygame.display.flip = real_flip

    def test_fallback_legacy_layout_without_assets(self):
        old_mp4 = S.SPLASH_MP4
        try:
            S.SPLASH_MP4 = '/nonexistent/splash.mp4'
            scr = pygame.display.set_mode((800, 600))
            d = create_status_display(scr)
            self.assertIsNone(d.strip)      # text-only fallback
            d.show('Floor796 Kiosk', 'Starting up...')
        finally:
            S.SPLASH_MP4 = old_mp4


if __name__ == '__main__':
    unittest.main(verbosity=2)
