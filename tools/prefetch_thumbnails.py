#!/usr/bin/env python3
"""
prefetch_thumbnails.py — pre-populate the player's thumbnail cache.

Iterates through all objects in changelog.json, classifies each link,
and fetches/resizes the thumbnail for every object that has one.
This avoids the lazy background fetch delay on first display.

Usage:
  prefetch_thumbnails.py                    # use default paths
  prefetch_thumbnails.py --install-dir /opt/floor796-kiosk
  prefetch_thumbnails.py --workers 4        # parallel fetch threads
  prefetch_thumbnails.py --force            # re-fetch even if cached
  prefetch_thumbnails.py --dry-run          # show what would be fetched

Run this on the kiosk (or any machine with the repo + deps installed)
before or after the player starts.  Already-cached thumbnails are
skipped by default.  The player will pick up cached files on its next
ThumbnailCache.get() call.
"""

import argparse
import io
import json
import logging
import os
import sys
import time
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


def fetch_thumbnail_bytes(url):
    """Fetch raw image bytes for a thumbnail URL.

    Handles YouTube images, direct images, video frame extraction,
    and Wikipedia REST API lookups.

    Returns raw bytes suitable for pygame.image.load(), or None on failure.
    """
    link_type, _ = classify_link(url)

    if link_type == "video":
        return _extract_video_frame(url)

    if url.startswith("wiki://api:"):
        # Wikipedia: fetch REST API summary, get thumbnail image URL
        api_url = url[len("wiki://api:"):]
        raw = _fetch_url(api_url)
        data = json.loads(raw)
        thumb_data = data.get("thumbnail") or {}
        thumb_src = thumb_data.get("source")
        if not thumb_src:
            return None
        return _fetch_url(thumb_src)

    # Direct image or YouTube thumbnail
    return _fetch_url(url)


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
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    # Resolve paths
    install_dir = args.install_dir
    changelog_path = args.changelog or os.path.join(install_dir, "assets", "changelog.json")
    thumb_dir = args.thumb_dir or os.path.join(install_dir, "cache", "thumbnails")

    log.info("Changelog: %s", changelog_path)
    log.info("Thumbnail dir: %s", thumb_dir)

    # Load changelog
    if not os.path.exists(changelog_path):
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
    for item in data:
        lt, thumb_url = classify_link(item.get("l", ""))
        type_counts[lt] += 1
        if thumb_url:
            fetchable += 1

    log.info("Link type breakdown:")
    for lt, n in type_counts.most_common():
        log.info("  %-15s %4d", lt, n)
    log.info("Fetchable thumbnails: %d / %d", fetchable, len(data))

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
    for item in data:
        lt, thumb_url = classify_link(item.get("l", ""))
        if thumb_url is None:
            stats["no_link"] += 1
            continue
        cache_file = os.path.join(thumb_dir, _cache_key(thumb_url))
        if not args.force and os.path.exists(cache_file) and os.path.getsize(cache_file) > 100:
            stats["cached"] += 1
            continue
        fetch_items.append((item, cache_file, lt))

    log.info("Need to fetch: %d (already cached: %d, no link: %d)",
             len(fetch_items), stats["cached"], stats["no_link"])

    def do_fetch(item_cache_lt):
        item, cache_file, lt = item_cache_lt
        try:
            link = item.get("l", "")
            _, thumb_url = classify_link(link)
            raw_bytes = fetch_thumbnail_bytes(thumb_url)
            if raw_bytes is None:
                return lt, "failed", None, None, item, "no data"
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
            else:
                stats["failed"] += 1
                failed_items.append((item["id"], item.get("t", ""), detail))

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
    print(f"  No link:           {stats['no_link']}")
    print(f"  Failed:            {stats['failed']}")
    if stats["resize_failed"]:
        print(f"  Resize failed:     {stats['resize_failed']}")
    if stats["save_failed"]:
        print(f"  Save failed:       {stats['save_failed']}")
    print(f"  Total in cache:    {final_cached}")
    print(f"  Cache directory:   {thumb_dir}")

    if failed_items:
        print(f"\nFailed items ({len(failed_items)}):")
        for obj_id, title, detail in failed_items[:20]:
            print(f"  obj {obj_id} ({title[:40]}): {detail[:60]}")
        if len(failed_items) > 20:
            print(f"  ... and {len(failed_items) - 20} more")

    pygame.quit()


if __name__ == "__main__":
    main()
