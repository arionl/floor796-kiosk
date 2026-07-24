#!/usr/bin/env python3
"""
prefetch_thumbnails.py — pre-populate the player's thumbnail cache.

Iterates through all objects in changelog.json, classifies each link,
and fetches/resizes the thumbnail for every object that has one.
This avoids the lazy background fetch delay on first display.

Supports ALL entry types:
  - youtube:     YouTube video → mqdefault thumbnail from img.youtube.com
  - image:       Direct images (.jpg/.png/.gif/.webp) → fetch + resize
  - video:       Video files (.mp4/.webm) → ffmpeg frame extraction
  - wiki:        Wikipedia articles → REST API thumbnail; wikireading.ru → og:image
  - web:         Web pages → og:image / twitter:image meta tag extraction
  - interactive: interactive:// pages → floor796.com HTML og:image;
                 compound links (play-loop://audio||image_url) → image part;
                 event:// → no thumbnail (skipped)
  - none:        No link → skipped

Usage:
  prefetch_thumbnails.py                    # use default paths
  prefetch_thumbnails.py --install-dir /opt/floor796-kiosk
  prefetch_thumbnails.py --workers 4        # parallel fetch threads
  prefetch_thumbnails.py --force            # re-fetch even if cached
  prefetch_thumbnails.py --dry-run          # show what would be fetched
  prefetch_thumbnails.py --types youtube,image  # only fetch specific types

Run this on the kiosk (or any machine with the repo + deps installed)
before or after the player starts.  Already-cached thumbnails are
skipped by default.  The player will pick up cached files on its next
ThumbnailCache.get() call.
"""

import argparse
import hashlib
import io
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Bootstrap package paths ──────────────────────────────────────────────────
# Allow running from repo root or from tools/ directory.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import pygame  # noqa: E402

from floor796_kiosk.paths import (  # noqa: E402
    INSTALL_DIR, CHANGELOG_PATH, THUMBNAIL_DIR,
)
from floor796_kiosk.thumbnails import (  # noqa: E402
    classify_link, _cache_key, _fetch_url, _extract_video_frame,
    _resize_cover, THUMB_W, THUMB_H,
)

log = logging.getLogger("prefetch_thumbnails")

BASE_URL = "https://floor796.com"
REQUEST_TIMEOUT = 12

# og:image / twitter:image extraction regexes
_OG_IMAGE_RE = re.compile(
    r'<meta\s+(?:property|name)=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
    re.IGNORECASE)
_OG_IMAGE_RE_REV = re.compile(
    r'<meta\s+content=["\']([^"\']+)["\']\s+(?:property|name)=["\']og:image["\']',
    re.IGNORECASE)
_TWITTER_IMAGE_RE = re.compile(
    r'<meta\s+(?:property|name)=["\']twitter:image["\']\s+content=["\']([^"\']+)["\']',
    re.IGNORECASE)
_TWITTER_IMAGE_RE_REV = re.compile(
    r'<meta\s+content=["\']([^"\']+)["\']\s+(?:property|name)=["\']twitter:image["\']',
    re.IGNORECASE)

# <img> tag extraction (fallback when no og:image)
_IMG_TAG_RE = re.compile(
    r'<img\s[^>]*src=["\']([^"\']+)["\']',
    re.IGNORECASE)

ALL_TYPES = ("youtube", "image", "video", "wiki", "web", "interactive", "none")


def _extract_og_image(html_bytes):
    """Extract og:image or twitter:image URL from HTML page source.

    Tries og:image first, then twitter:image, then first <img> tag as
    a last resort. Returns absolute URL or None.
    """
    # Decode HTML (try utf-8, fall back to latin-1 for weird encodings)
    try:
        html = html_bytes.decode("utf-8", errors="replace")
    except Exception:
        html = html_bytes.decode("latin-1", errors="replace")

    # Try og:image (both attribute orderings)
    for pat in (_OG_IMAGE_RE, _OG_IMAGE_RE_REV):
        m = pat.search(html)
        if m:
            return m.group(1)

    # Try twitter:image
    for pat in (_TWITTER_IMAGE_RE, _TWITTER_IMAGE_RE_REV):
        m = pat.search(html)
        if m:
            return m.group(1)

    # Fallback: first <img> tag src (useful for floor796 pages without og:image)
    m = _IMG_TAG_RE.search(html)
    if m:
        return m.group(1)

    return None


def _resolve_url(url, base=None):
    """Resolve a possibly-relative URL against a base URL."""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return (base or BASE_URL).rstrip("/") + url
    if not url.startswith("http"):
        return (base or BASE_URL).rstrip("/") + "/" + url
    return url


def fetch_thumbnail_bytes(url, original_link=None):
    """Fetch raw image bytes for a thumbnail URL.

    Handles ALL entry types:
      - youtube:    YouTube mqdefault thumbnail
      - image:      Direct image fetch
      - video:      ffmpeg frame extraction
      - wiki://api: Wikipedia REST API thumbnail
      - web:        HTML page → og:image / twitter:image extraction
      - interactive: floor796.com HTML page → og:image

    Returns raw bytes suitable for pygame.image.load(), or None on failure.
    """
    link_type, _ = classify_link(url)

    # ── YouTube: already resolved to img.youtube.com thumbnail URL ──
    if link_type == "youtube":
        return _fetch_url(url)

    # ── Video: extract a frame via ffmpeg ──
    if link_type == "video":
        return _extract_video_frame(url)

    # ── Wikipedia REST API ──
    if url.startswith("wiki://api:"):
        api_url = url[len("wiki://api:"):]
        raw = _fetch_url(api_url)
        data = json.loads(raw)
        thumb_data = data.get("thumbnail") or {}
        thumb_src = thumb_data.get("source")
        if not thumb_src:
            return None
        return _fetch_url(thumb_src)

    # ── Direct image ──
    if link_type == "image":
        return _fetch_url(url)

    # ── Web pages: fetch HTML, extract og:image / twitter:image ──
    if link_type == "web":
        try:
            html_bytes = _fetch_url(url, timeout=REQUEST_TIMEOUT)
            og_img = _extract_og_image(html_bytes)
            if og_img:
                og_img = _resolve_url(og_img, base=url)
                return _fetch_url(og_img, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            log.debug("web og:image extraction failed for %s: %s", url, e)
        return None

    # ── Interactive: resolve to floor796.com HTML page ──
    if link_type == "interactive":
        # Try to resolve interactive:// path to a floor796.com HTML page
        page_url = None
        if url.startswith("interactive://"):
            path = url[len("interactive://"):]
            page_url = f"{BASE_URL}/interactive/{path}"
        elif url.startswith("event://"):
            # Pure event triggers have no page or thumbnail
            return None
        else:
            # Could be a compound link part that ended up here
            page_url = url

        if page_url:
            try:
                html_bytes = _fetch_url(page_url, timeout=REQUEST_TIMEOUT)
                og_img = _extract_og_image(html_bytes)
                if og_img:
                    og_img = _resolve_url(og_img, base=page_url)
                    return _fetch_url(og_img, timeout=REQUEST_TIMEOUT)
            except Exception as e:
                log.debug("interactive og:image extraction failed for %s: %s",
                          page_url, e)
        return None

    # ── Wiki (non-Wikipedia, e.g. wikireading.ru): try og:image ──
    if link_type == "wiki":
        # wikireading.ru and other wiki-like sites may have og:image
        if url.startswith("http"):
            try:
                html_bytes = _fetch_url(url, timeout=REQUEST_TIMEOUT)
                og_img = _extract_og_image(html_bytes)
                if og_img:
                    og_img = _resolve_url(og_img, base=url)
                    return _fetch_url(og_img, timeout=REQUEST_TIMEOUT)
            except Exception as e:
                log.debug("wiki og:image extraction failed for %s: %s", url, e)
        return None

    return None


def resolve_thumb_url(link):
    """Resolve a changelog 'l' field to a thumbnail URL for ALL types.

    This extends classify_link by also handling:
      - Compound links: checks ALL ||-separated parts for an image
      - web/interactive/wiki: generates a cache key from the original link

    Returns (link_type, thumb_url, cache_key_source) where cache_key_source
    is the string used for the cache filename hash (thumb_url or original link).
    """
    if not link:
        return "none", None, None

    # Compound links: "image_url||play-loop://audio.mp3" or
    # "play-loop://audio.mp3||image_url"
    # Try ALL parts, return the first one that produces a thumbnail.
    if "||" in link:
        parts = [p.strip() for p in link.split("||")]
        for part in parts:
            lt, tu = classify_link(part)
            if tu:
                return lt, tu, tu
        # No part produced a direct thumbnail URL; try og:image on each
        # http part as a fallback.
        for part in parts:
            lt, _ = classify_link(part)
            if lt in ("web", "interactive", "wiki") and part.startswith("http"):
                return lt, part, part
            if part.startswith("interactive://"):
                return "interactive", part, part
        # All parts are non-image (e.g. event:// or play-loop://audio only)
        # Use the first part's type
        lt, _ = classify_link(parts[0])
        return lt, None, None

    # Single link
    lt, thumb_url = classify_link(link)

    # Types where classify_link already gives us a fetchable URL
    if lt in ("youtube", "image", "video"):
        return lt, thumb_url, thumb_url

    # Wikipedia with REST API URL
    if lt == "wiki" and thumb_url and thumb_url.startswith("wiki://api:"):
        return lt, thumb_url, thumb_url

    # Wikipedia / wikireading.ru without REST API → try og:image on the URL
    if lt == "wiki" and thumb_url is None:
        return lt, link, link

    # web and interactive: thumbnail URL IS the page URL (for og:image fetch)
    if lt in ("web", "interactive"):
        if link.startswith("http"):
            return lt, link, link
        if link.startswith("interactive://"):
            return lt, link, link

    # event://, none, etc.
    return lt, None, None


def main():
    parser = argparse.ArgumentParser(
        description="Pre-populate the floor796 kiosk thumbnail cache.")
    parser.add_argument("--install-dir", default=INSTALL_DIR,
                        help=f"Install directory (default: {INSTALL_DIR})")
    parser.add_argument("--changelog", default=None,
                        help="Path to changelog.json (default: <install-dir>/assets/changelog.json)")
    parser.add_argument("--thumb-dir", default=None,
                        help="Thumbnail cache directory (default: <install-dir>/cache/thumbnails)")
    parser.add_argument("--workers", type=int, default=4,
                        help="Number of parallel fetch workers (default: 4)")
    parser.add_argument("--force", action="store_true",
                        help="Re-fetch even if thumbnail is already cached")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be fetched without downloading")
    parser.add_argument("--max-items", type=int, default=0,
                        help="Stop after N items (0 = all, for testing)")
    parser.add_argument("--types", default=",".join(ALL_TYPES),
                        help=f"Comma-separated link types to fetch (default: all). "
                             f"Choices: {', '.join(ALL_TYPES)}")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    # Parse type filter
    wanted_types = set(t.strip().lower() for t in args.types.split(","))
    unknown = wanted_types - set(ALL_TYPES)
    if unknown:
        log.error("Unknown type(s): %s. Valid: %s", ", ".join(unknown),
                  ", ".join(ALL_TYPES))
        sys.exit(1)

    # Resolve paths
    install_dir = args.install_dir
    changelog_path = args.changelog or os.path.join(install_dir, "assets", "changelog.json")
    thumb_dir = args.thumb_dir or os.path.join(install_dir, "cache", "thumbnails")

    log.info("Changelog: %s", changelog_path)
    log.info("Thumbnail dir: %s", thumb_dir)
    log.info("Types: %s", ", ".join(sorted(wanted_types)))

    # Load changelog
    if not os.path.exists(changelog_path):
        # Try repo-root changelog.json as fallback
        repo_cl = os.path.join(REPO_ROOT, "changelog.json")
        if os.path.exists(repo_cl):
            changelog_path = repo_cl
            log.info("Using changelog: %s", changelog_path)
        else:
            log.error("Changelog not found: %s", changelog_path)
            log.error("Run the player once to download it, or specify --changelog")
            sys.exit(1)

    with open(changelog_path) as f:
        data = json.load(f)

    log.info("Loaded %d objects from changelog", len(data))

    if args.max_items > 0:
        data = data[:args.max_items]
        log.info("Limited to %d items", len(data))

    # Classify all links for summary
    type_counts = Counter()
    fetchable = 0
    item_map = []  # (item, link_type, thumb_url, cache_key_source)
    for item in data:
        link = item.get("l", "")
        lt, thumb_url, key_src = resolve_thumb_url(link)
        type_counts[lt] += 1
        if thumb_url and lt in wanted_types:
            fetchable += 1
        item_map.append((item, lt, thumb_url, key_src))

    log.info("Link type breakdown:")
    for lt, n in type_counts.most_common():
        marker = "✓" if lt in wanted_types else "·"
        log.info("  %s %-15s %4d", marker, lt, n)
    log.info("Fetchable (selected types): %d / %d", fetchable, len(data))

    # Count existing cache
    existing = 0
    if os.path.isdir(thumb_dir):
        existing = len([f for f in os.listdir(thumb_dir)
                        if f.endswith(".png") and os.path.getsize(os.path.join(thumb_dir, f)) > 100])
    log.info("Existing cached thumbnails: %d", existing)

    if args.dry_run:
        log.info("Dry run — no downloads. %d thumbnails would be fetched.",
                 fetchable - existing if not args.force else fetchable)
        return

    # Ensure thumbnail directory exists
    os.makedirs(thumb_dir, exist_ok=True)

    # Initialize pygame for surface operations (no display needed)
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    pygame.init()
    pygame.display.init()
    pygame.display.set_mode((1, 1))

    # ── Fetch phase: download raw bytes in parallel ──────────────────────────
    stats = Counter()
    pending_resizes = []  # list of (raw_bytes, cache_file, obj_id, title)
    failed_items = []
    start_time = time.time()

    # Filter to items with fetchable thumbnails
    fetch_items = []
    for item, lt, thumb_url, key_src in item_map:
        if lt not in wanted_types:
            stats["type_filtered"] += 1
            continue
        if thumb_url is None:
            stats["no_thumb"] += 1
            continue
        cache_file = os.path.join(thumb_dir, _cache_key(key_src))
        if not args.force and os.path.exists(cache_file) and os.path.getsize(cache_file) > 100:
            stats["cached"] += 1
            continue
        fetch_items.append((item, cache_file, lt, thumb_url, key_src))

    log.info("Need to fetch: %d (already cached: %d, no thumb: %d, filtered: %d)",
             len(fetch_items), stats["cached"], stats["no_thumb"], stats["type_filtered"])

    def do_fetch(item_cache_lt_url):
        item, cache_file, lt, thumb_url, key_src = item_cache_lt_url
        try:
            raw_bytes = fetch_thumbnail_bytes(thumb_url, original_link=item.get("l", ""))
            if raw_bytes is None:
                return lt, "failed", None, None, item, "no data returned"
            return lt, "ok", raw_bytes, cache_file, item, thumb_url
        except Exception as e:
            return lt, "error", None, None, item, str(e)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(do_fetch, fc): fc for fc in fetch_items}

        for i, future in enumerate(as_completed(futures), 1):
            lt, status, raw_bytes, cache_file, item, detail = future.result()

            if status == "ok" and raw_bytes is not None:
                pending_resizes.append((raw_bytes, cache_file, item["id"], item.get("t", "")))
                stats["fetched"] += 1
                stats[f"fetched_{lt}"] += 1
            else:
                stats["failed"] += 1
                stats[f"failed_{lt}"] += 1
                failed_items.append((item["id"], item.get("t", ""), lt, detail))

            # Progress
            if i % 50 == 0 or i == len(fetch_items):
                elapsed = time.time() - start_time
                rate = i / elapsed if elapsed > 0 else 0
                eta = (len(fetch_items) - i) / rate if rate > 0 else 0
                log.info("Fetched %d/%d (%.0f%%) — %d ok, %d failed — %.1f/s ETA %.0fs",
                         i, len(fetch_items), 100*i/len(fetch_items),
                         stats["fetched"], stats["failed"], rate, eta)

    # ── Resize + save phase: single-threaded (pygame not thread-safe) ────────
    log.info("Resizing and saving %d thumbnails...", len(pending_resizes))

    for i, (raw_bytes, cache_file, obj_id, title) in enumerate(pending_resizes, 1):
        try:
            src_surf = pygame.image.load(io.BytesIO(raw_bytes))
            # Ensure surface is 24/32-bit before smoothscale (GIFs load
            # as 8-bit palette which smoothscale can't handle)
            if src_surf.get_bitsize() < 24:
                src_surf = src_surf.convert(32)
            thumb = _resize_cover(src_surf, THUMB_W, THUMB_H)
            if thumb is not None:
                pygame.image.save(thumb, cache_file)
            else:
                stats["resize_failed"] += 1
                log.warning("Resize failed for obj %d (%s)", obj_id, title[:40])
        except Exception as e:
            stats["save_failed"] += 1
            log.warning("Save failed for obj %d (%s): %s", obj_id, title[:40], e)

        if i % 100 == 0 or i == len(pending_resizes):
            log.info("Resized %d/%d", i, len(pending_resizes))

    # ── Summary ──────────────────────────────────────────────────────────────
    elapsed = time.time() - start_time
    final_cached = len([f for f in os.listdir(thumb_dir)
                        if f.endswith(".png")
                        and os.path.getsize(os.path.join(thumb_dir, f)) > 100])

    print()
    print("=" * 60)
    print("Thumbnail prefetch complete")
    print("=" * 60)
    print(f"  Time elapsed:      {elapsed:.1f}s")
    print(f"  Already cached:    {stats['cached']}")
    print(f"  Newly fetched:     {stats['fetched']}")
    print(f"  No thumb URL:      {stats['no_thumb']}")
    print(f"  Type filtered:     {stats['type_filtered']}")
    print(f"  Failed:            {stats['failed']}")

    # Per-type breakdown
    type_success = {}
    for lt in ALL_TYPES:
        ok = stats.get(f"fetched_{lt}", 0)
        fail = stats.get(f"failed_{lt}", 0)
        if ok or fail:
            type_success[lt] = (ok, fail)
    if type_success:
        print(f"  Per-type (ok/fail):")
        for lt in ALL_TYPES:
            if lt in type_success:
                ok, fail = type_success[lt]
                print(f"    {lt:15s} {ok:4d} ok / {fail:3d} fail")

    if stats["resize_failed"]:
        print(f"  Resize failed:     {stats['resize_failed']}")
    if stats["save_failed"]:
        print(f"  Save failed:       {stats['save_failed']}")
    print(f"  Total in cache:    {final_cached}")
    print(f"  Cache directory:   {thumb_dir}")

    if failed_items:
        print(f"\nFailed items ({len(failed_items)}):")
        for obj_id, title, lt, detail in failed_items[:20]:
            print(f"  obj {obj_id} [{lt}] ({title[:40]}): {detail[:60]}")
        if len(failed_items) > 20:
            print(f"  ... and {len(failed_items) - 20} more")

    pygame.quit()


if __name__ == "__main__":
    main()
