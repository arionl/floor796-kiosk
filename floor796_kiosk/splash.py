#!/usr/bin/env python3
"""splash.py — boot splash with floor796's own station animation.

The looping planet/station video (splash-planet.mp4 — floor796's
about-section animation) plays in a centered square at 66% of the
screen height over the splash navy (8,10,16), with the space-themed
porthole frame graphic (splash-frame.png) composited over it and the
boot status/progress underneath.

pygame cannot decode H.264, so the mp4 is ffmpeg-decoded ONCE (same
pipeline as tile strips: scale + tile=1x60 → strip image) into a
frame strip cached in cache/splash/; boots after the first load only
blit.  If the mp4, ffmpeg or the cache are unavailable the splash
degrades to the legacy text-only status screen — boot never breaks.

These two assets are APP CHROME shipped with the deploy (not
floor796.com map content), so the no-bundling rule for site content
does not apply.
"""

import json
import logging
import os
import subprocess
import time

import pygame

from floor796_kiosk.paths import INSTALL_DIR, ASSETS_DIR, CACHE_DIR

log = logging.getLogger("floor796")

SPLASH_ASSET_DIR = os.path.join(ASSETS_DIR, 'splash')
SPLASH_MP4 = os.path.join(SPLASH_ASSET_DIR, 'splash-planet.mp4')
SPLASH_FRAME_PNG = os.path.join(SPLASH_ASSET_DIR, 'splash-frame.png')
SPLASH_CACHE_DIR = os.path.join(CACHE_DIR, 'splash')

SPLASH_FRAMES = 60        # 5 s at …
SPLASH_FPS = 12           # … 12 fps, looped
BG = (8, 10, 16)          # splash background color


def _fingerprint(path, square):
    try:
        st = os.stat(path)
        return {'mtime': st.st_mtime, 'size': st.st_size,
                'square': square}
    except OSError:
        return None


def load_splash_assets(screen):
    """Prepare the splash strip + frame overlay for this screen size.

    Returns (strip_surface_or_None, frame_surface_or_None).  The strip
    is a display-format surface of SPLASH_FRAMES square frames stacked
    vertically (subsurface blits per frame).  Cold path decodes the
    mp4 via ffmpeg (~1-2 s once); warm path loads the cached BMP.
    """
    w, h = screen.get_size()
    square = int(h * 0.66)          # video square = 66% of height
    if square < 64 or not os.path.exists(SPLASH_MP4):
        return None, None

    os.makedirs(SPLASH_CACHE_DIR, exist_ok=True)
    strip_bmp = os.path.join(SPLASH_CACHE_DIR, 'splash_strip.bmp')
    meta_path = os.path.join(SPLASH_CACHE_DIR, 'splash_strip.json')
    fp = _fingerprint(SPLASH_MP4, square)

    strip = None
    meta_ok = False
    if os.path.exists(strip_bmp) and os.path.getsize(strip_bmp) > 1000:
        try:
            with open(meta_path) as f:
                meta_ok = json.load(f) == fp
        except (OSError, ValueError):
            meta_ok = False

    if not meta_ok:
        # Decode once: mp4 → single tall strip PNG via ffmpeg, then
        # down-convert to a 16-bit BMP cache (~60 MB vs ~120 at 32-bit
        # — the per-frame blit conversion during boot is negligible).
        strip_png = os.path.join(SPLASH_CACHE_DIR, 'splash_strip.png')
        cmd = ['ffmpeg', '-y', '-i', SPLASH_MP4,
               '-vf', f'scale={square}:{square}:flags=neighbor,'
                      f'tile=1x{SPLASH_FRAMES}',
               '-frames', '1', '-compression_level', '3', strip_png]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=60)
            if r.returncode == 0 and os.path.exists(strip_png):
                surf = pygame.image.load(strip_png)
                s16 = pygame.Surface(surf.get_size(), 0, 16)
                s16.blit(surf, (0, 0))
                pygame.image.save(s16, strip_bmp)
                os.remove(strip_png)
                with open(meta_path, 'w') as f:
                    json.dump(fp, f)
                strip = s16
                log.info('splash: decoded %d frames (%dpx) from mp4',
                         SPLASH_FRAMES, square)
            else:
                log.warning('splash: ffmpeg decode failed — '
                            'text-only boot screen')
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning('splash: decode error %s — text-only boot '
                        'screen', exc)

    if strip is None and meta_ok:
        try:
            strip = pygame.image.load(strip_bmp)   # stays 16-bit
            log.info('splash: strip cache hit (%d frames, %dpx)',
                     SPLASH_FRAMES, square)
        except pygame.error as exc:
            log.warning('splash: cache load failed %s', exc)
            strip = None

    frame = None
    if os.path.exists(SPLASH_FRAME_PNG):
        try:
            frame = pygame.image.load(SPLASH_FRAME_PNG).convert_alpha()
            if frame.get_size() != (w, h):
                frame = pygame.transform.smoothscale(frame, (w, h))
        except pygame.error as exc:
            log.warning('splash: frame overlay failed %s', exc)

    return strip, frame


class SplashStatusDisplay:
    """StatusDisplay-compatible boot screen with the station animation
    looping behind the status text.  API matches StatusDisplay.show()
    so every existing boot call site works unchanged; without assets
    it renders the legacy text-only layout."""

    def __init__(self, screen, strip=None, frame=None):
        self.screen = screen
        self.w = screen.get_width()
        self.h = screen.get_height()
        self.font_lg = pygame.font.Font(None, 48)
        self.font_md = pygame.font.Font(None, 32)
        self.font_sm = pygame.font.Font(None, 24)
        self.strip = strip
        self.frame = frame
        self.square = strip.get_width() if strip else 0
        self._t0 = time.monotonic()

    # ── Layout ──────────────────────────────────────────────────────

    def _video_rect(self):
        v = self.square
        return ((self.w - v) // 2, (self.h - v) // 2)

    def show(self, message, detail='', progress=None):
        self.screen.fill(BG)

        if self.strip is not None:
            # Current animation frame (12 fps loop).
            fi = int((time.monotonic() - self._t0) * SPLASH_FPS) \
                % SPLASH_FRAMES
            vx, vy = self._video_rect()
            v = self.square
            sub = self.strip.subsurface((0, fi * v, v, v))
            self.screen.blit(sub, (vx, vy))
            if self.frame is not None:
                self.screen.blit(self.frame, (0, 0))

            # Status text below the framed video.
            base_y = (self.h + v) // 2 + 34
            text = self.font_md.render(message, True, (235, 235, 240))
            self.screen.blit(
                text, text.get_rect(center=(self.w // 2, base_y)))
            detail_rect = text.get_rect()
            if detail:
                d = self.font_sm.render(detail, True, (140, 140, 150))
                detail_rect = d.get_rect(
                    center=(self.w // 2, base_y + 28))
                self.screen.blit(d, detail_rect)
            if progress is not None:
                self._progress_bar(progress, detail_rect.bottom + 22)
        else:
            # Legacy text-only layout (StatusDisplay parity).
            text = self.font_lg.render(message, True, (235, 235, 240))
            rect = text.get_rect(
                center=(self.w // 2, self.h // 2 - 40))
            self.screen.blit(text, rect)
            detail_rect = rect
            if detail:
                d = self.font_md.render(detail, True, (140, 140, 150))
                detail_rect = d.get_rect(
                    center=(self.w // 2, rect.bottom + 25))
                self.screen.blit(d, detail_rect)
            if progress is not None:
                self._progress_bar(progress, detail_rect.bottom + 30,
                                   large=True)

        pygame.display.flip()

    def _progress_bar(self, progress, y, large=False):
        progress = max(0.0, min(1.0, progress))
        bar_w = min(self.w - 200, 600)
        bar_h = 12 if large else 6
        bar_x = (self.w - bar_w) // 2
        pygame.draw.rect(self.screen, (40, 40, 50),
                         (bar_x, y, bar_w, bar_h), border_radius=3)
        fill_w = int(bar_w * progress)
        if fill_w > 0:
            pygame.draw.rect(self.screen, (170, 200, 235),
                             (bar_x, y, fill_w, bar_h), border_radius=3)

    def release(self):
        """Drop the strip/overlay references (~60 MB) once the main
        loop starts."""
        self.strip = None
        self.frame = None
        log.info('splash: released frame memory')


def create_status_display(screen):
    """StatusDisplay factory used by the boot sequence: splash with the
    station animation when the assets are available, legacy text
    screen otherwise."""
    try:
        strip, frame = load_splash_assets(screen)
    except Exception as exc:            # never block boot
        log.warning('splash: prepare failed %s — text-only', exc)
        strip, frame = None, None
    if strip is not None:
        log.info('splash: station animation active')
        return SplashStatusDisplay(screen, strip, frame)
    return SplashStatusDisplay(screen)   # legacy layout
