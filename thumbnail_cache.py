#!/usr/bin/env python3
"""
ThumbnailCache — fetches, resizes, and caches thumbnails for the
object highlighter.

Link types handled:
  - Direct images (floor796.com/data/misc/*.jpg, imgur, etc.)
  - YouTube videos (watch?v= or youtu.be) → mqdefault thumbnail
  - Compound links (image_url||play-loop://audio.mp3) → image part
  - img:// relative paths → prepend floor796.com base URL
  - Everything else → no thumbnail (returns None)

Thumbnails are cached to disk as 320px-wide JPEGs (~10-20KB each).
On cache hit, loading is instant from disk; on miss, a background
thread fetches and processes the image.
"""

import hashlib
import io
import logging
import os
import re
import threading
import urllib.parse
import urllib.request
from collections import OrderedDict

import pygame

log = logging.getLogger("floor796")

BASE_URL = "https://floor796.com"
THUMB_W = 320          # target width in pixels
THUMB_H = 200          # target height in pixels
CACHE_DIR = "thumbs"   # directory for cached thumbnails
MAX_FETCH_WORKERS = 2  # concurrent fetch threads
REQUEST_TIMEOUT = 8    # seconds

# YouTube thumbnail quality: mqdefault = 320×180, hqdefault = 480×360
YT_THUMB_FMT = "https://img.youtube.com/vi/{vid}/mqdefault.jpg"

# Regex to extract YouTube video ID from various URL formats
_YT_PATTERNS = [
    re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]{11})"),
    re.compile(r"youtube\.com/embed/([\w-]{11})"),
]


def classify_link(link):
    """Classify a changelog 'l' field into a link type.

    Returns (link_type, thumb_url) where link_type is one of:
      'youtube', 'image', 'video', 'wiki', 'web', 'interactive', 'none'
    and thumb_url is the URL to fetch for the thumbnail (or None).
    """
    if not link:
        return "none", None

    # Compound links: "image_url||play-loop://audio.mp3"
    # Take the first part as the thumbnail source
    if "||" in link:
        parts = link.split("||")
        img_part = parts[0].strip()
        # Recurse on just the image part
        return classify_link(img_part)

    # Special protocols — interactive://, event://, play-loop://
    if "://" in link and not link.startswith("http"):
        if link.startswith("img://"):
            # img://path → prepend base URL
            path = link[len("img://"):]
            return "image", f"{BASE_URL}/{path}"
        return "interactive", None

    # YouTube
    for pat in _YT_PATTERNS:
        m = pat.search(link)
        if m:
            vid = m.group(1)
            return "youtube", YT_THUMB_FMT.format(vid=vid)

    # Direct video files (mp4/webm)
    if any(link.lower().endswith(ext) for ext in (".mp4", ".webm", ".mov")):
        # Try to find an associated thumbnail by replacing extension
        # floor796 stores video thumbs sometimes, but we can't be sure.
        # Fall back to no thumbnail for videos.
        return "video", None

    # Direct images
    if any(link.lower().endswith(ext) for ext in
           (".jpg", ".jpeg", ".png", ".gif", ".webp")):
        url = link
        if url.startswith("//"):
            url = "https:" + url
        return "image", url

    # Wikipedia
    if "wikipedia.org" in link or "wikireading.ru" in link:
        return "wiki", None

    # Other web links
    if link.startswith("http"):
        return "web", None

    return "none", None


def _cache_key(url):
    """Generate a cache filename from a URL."""
    return hashlib.md5(url.encode()).hexdigest() + ".png"


def _fetch_url(url, timeout=REQUEST_TIMEOUT):
    """Fetch raw bytes from a URL."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Floor796-Kiosk)"
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _resize_cover(src_surf, target_w, target_h):
    """Resize a pygame Surface to cover target dimensions, cropping excess."""
    sw, sh = src_surf.get_size()
    if sw == 0 or sh == 0:
        return None

    # Calculate scale to cover
    scale = max(target_w / sw, target_h / sh)
    new_w = int(sw * scale)
    new_h = int(sh * scale)
    scaled = pygame.transform.smoothscale(src_surf, (new_w, new_h))

    # Crop center
    crop_x = (new_w - target_w) // 2
    crop_y = (new_h - target_h) // 2
    cropped = pygame.Surface((target_w, target_h))
    cropped.blit(scaled, (-crop_x, -crop_y))
    return cropped


class ThumbnailCache:
    """Disk + memory cache for thumbnails with background fetching.

    Usage:
        cache = ThumbnailCache(cache_dir="thumbs")
        surface = cache.get(obj_id, link)  # may return None if loading
        # ... next frame ...
        surface = cache.get(obj_id, link)  # now returns the surface
    """

    def __init__(self, cache_dir=CACHE_DIR, max_mem=80):
        self._cache_dir = cache_dir
        self._max_mem = max_mem  # max surfaces in RAM
        self._surfaces = OrderedDict()  # obj_id -> pygame.Surface or None
        self._loading = set()  # obj_ids currently being fetched
        self._failed = set()  # obj_ids that failed to load
        self._lock = threading.Lock()
        self._thread_pool = []

        os.makedirs(cache_dir, exist_ok=True)

    def get(self, obj_id, link):
        """Get a thumbnail surface for an object.

        Returns:
          - pygame.Surface if thumbnail is ready
          - None if still loading, failed, or no thumbnail available
        """
        link_type, thumb_url = classify_link(link)

        # No thumbnail possible for this link type
        if thumb_url is None:
            return None

        with self._lock:
            # Already in memory?
            if obj_id in self._surfaces:
                self._surfaces.move_to_end(obj_id)
                surf, converted = self._surfaces[obj_id]
                if not converted:
                    # Surface was stored by background thread — convert now
                    try:
                        surf = surf.convert_alpha()
                    except pygame.error:
                        pass  # display not ready yet
                    self._surfaces[obj_id] = (surf, True)
                return self._surfaces[obj_id][0]

            # Previously failed?
            if obj_id in self._failed:
                return None

            # Currently loading?
            if obj_id in self._loading:
                return None

        # Check disk cache
        cache_file = os.path.join(self._cache_dir, _cache_key(thumb_url))
        if os.path.exists(cache_file):
            try:
                surf = pygame.image.load(cache_file)
                # Convert on main thread where display is available
                surf = surf.convert_alpha()
                self._store(obj_id, surf, converted=True)
                return surf
            except Exception as e:
                log.debug("ThumbnailCache: failed to load cached %s: %s",
                          cache_file, e)

        # Not cached — start background fetch
        self._start_fetch(obj_id, thumb_url, cache_file)
        return None

    def _store(self, obj_id, surf, converted=False):
        """Store a surface in the memory cache, evicting if needed.

        'converted' tracks whether convert_alpha() has been called yet.
        Background threads store raw surfaces (converted=False); the main
        thread converts lazily on first get().
        """
        with self._lock:
            self._surfaces[obj_id] = (surf, converted)
            self._surfaces.move_to_end(obj_id)
            while len(self._surfaces) > self._max_mem:
                self._surfaces.popitem(last=False)

    def _start_fetch(self, obj_id, url, cache_file):
        """Start a background thread to fetch and process a thumbnail."""
        with self._lock:
            if obj_id in self._loading:
                return
            self._loading.add(obj_id)

        t = threading.Thread(
            target=self._fetch_worker,
            args=(obj_id, url, cache_file),
            daemon=True,
        )
        t.start()
        self._thread_pool.append(t)
        # Clean up dead threads occasionally
        self._thread_pool = [t for t in self._thread_pool if t.is_alive()]

    def _fetch_worker(self, obj_id, url, cache_file):
        """Background fetch: download image, resize, save to disk, cache."""
        try:
            raw = _fetch_url(url)
            src_surf = pygame.image.load(io.BytesIO(raw))

            # Resize to cover 320×200
            thumb = _resize_cover(src_surf, THUMB_W, THUMB_H)
            if thumb is None:
                raise ValueError("resize failed")

            # Save to disk cache
            try:
                pygame.image.save(thumb, cache_file)
            except Exception as e:
                log.debug("ThumbnailCache: save failed for %s: %s", obj_id, e)

            # Store raw surface (no convert_alpha in background thread —
            # it requires the video subsystem and may fail or corrupt
            # state when called from a non-main thread).  convert_alpha
            # is deferred to the main thread in _convert_surface().
            self._store(obj_id, thumb)
            log.debug("ThumbnailCache: fetched thumbnail for obj %s from %s",
                      obj_id, url)

        except Exception as e:
            log.debug("ThumbnailCache: fetch failed for obj %s (%s): %s",
                      obj_id, url, e)
            with self._lock:
                self._failed.add(obj_id)
        finally:
            with self._lock:
                self._loading.discard(obj_id)

    def prefetch(self, obj_id, link):
        """Prefetch a thumbnail without needing the surface yet."""
        self.get(obj_id, link)

    def get_link_type(self, link):
        """Return the link type string for rendering indicators."""
        link_type, _ = classify_link(link)
        return link_type
