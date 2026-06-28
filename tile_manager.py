#!/usr/bin/env python3
"""
Floor796 tile manager — download and keep tile assets up to date.

At startup the player calls ``check_and_update()`` which:
  1. Fetches the latest tile matrix from floor796.com (15s timeout).
  2. Compares it against the local cache.
  3. Downloads any new or changed tiles.
  4. Regenerates ``tiles_meta.json``.

If the network is unavailable, it silently falls back to the existing
cached tiles so the kiosk always boots offline.
"""

import json
import logging
import os
import random
import concurrent.futures
import urllib.request
import urllib.error

log = logging.getLogger("floor796")

MATRIX_URL = "https://floor796.com/data/matrix.json"
CDN_BASE = "https://cdn.floor796.com/data/"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TILE_DIR = os.path.join(BASE_DIR, "tiles")
TILE_META_PATH = os.path.join(BASE_DIR, "tiles_meta.json")

NETWORK_TIMEOUT = 15


# ── Public API ────────────────────────────────────────────────────────────────

def check_and_update(status_callback=None):
    """Check for tile updates; download if available.

    Returns a dict:
        updated (bool)    — True if tiles were added/changed.
        new_tiles (list)  — tile IDs that were downloaded.
        offline (bool)    — True if network was unavailable.
        total_tiles (int) — total tiles after update.
    """
    matrix = _fetch_matrix()
    if matrix is None:
        log.info("Offline or unreachable — using cached tiles.")
        return {"updated": False, "new_tiles": [], "offline": True,
                "total_tiles": _count_cached_tiles()}

    download_list, new_tile_ids = _build_download_list(matrix)

    if not new_tile_ids:
        # Still regenerate metadata in case the grid layout changed.
        _regenerate_metadata(matrix)
        total = len(download_list)
        log.info("Tiles up to date (%d tiles).", total)
        return {"updated": False, "new_tiles": [], "offline": False,
                "total_tiles": total}

    log.info("Downloading %d new/updated tiles...", len(new_tile_ids))
    failed = _download_all(download_list, status_callback)

    _regenerate_metadata(matrix)
    total = len(download_list)

    if failed:
        log.warning("Failed to download %d tiles: %s", len(failed), failed)
    else:
        log.info("Update complete: %d tiles (%d new).", total, len(new_tile_ids))

    return {"updated": True, "new_tiles": new_tile_ids, "offline": False,
            "total_tiles": total, "failed": failed}


def has_cached_tiles():
    """True if tiles_meta.json and at least one tile MP4 exist locally."""
    return (os.path.exists(TILE_META_PATH) and
            os.path.isdir(TILE_DIR) and
            len(os.listdir(TILE_DIR)) > 0)


# ── Internal helpers ─────────────────────────────────────────────────────────

def _fetch_matrix():
    """Fetch matrix.json from the CDN. Returns dict or None on failure."""
    url = f"{MATRIX_URL}?r={random.random()}"
    try:
        with urllib.request.urlopen(url, timeout=NETWORK_TIMEOUT) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.debug("Matrix fetch failed: %s", exc)
        return None
    except json.JSONDecodeError as exc:
        log.warning("Matrix JSON parse error: %s", exc)
        return None


def _build_download_list(matrix):
    """Compare remote matrix with local cache. Return (list, new_ids)."""
    os.makedirs(TILE_DIR, exist_ok=True)
    download_list = []
    new_tile_ids = []

    for row in matrix["mat"]:
        for tile in row:
            if not tile or not tile.get("video", {}).get("mp4"):
                continue
            tile_id = tile["id"]
            mp4_rel = tile["video"]["mp4"]
            mp4_url = CDN_BASE + mp4_rel
            expected_size = tile.get("video_size", {}).get("mp4", 0)
            mp4_path = os.path.join(TILE_DIR, f"{tile_id}.mp4")

            # Skip if file exists and matches expected size.
            if (os.path.exists(mp4_path) and expected_size > 0
                    and os.path.getsize(mp4_path) == expected_size):
                download_list.append((tile_id, mp4_path, mp4_url, expected_size))
                continue

            download_list.append((tile_id, mp4_path, mp4_url, expected_size))
            new_tile_ids.append(tile_id)

    return download_list, new_tile_ids


def _download_tile(args):
    """Download a single tile. Returns (tile_id, success, info)."""
    tile_id, mp4_path, mp4_url, expected_size = args
    try:
        urllib.request.urlretrieve(mp4_url, mp4_path)
        actual = os.path.getsize(mp4_path)
        if expected_size == 0 or actual == expected_size:
            return (tile_id, True, actual)
        return (tile_id, False, f"size mismatch {actual}/{expected_size}")
    except Exception as exc:
        return (tile_id, False, str(exc))


def _download_all(download_list, status_callback=None):
    """Download tiles in parallel. Returns list of failed tile IDs."""
    total = len(download_list)
    done = 0
    failed = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_download_tile, d): d[0]
                   for d in download_list}
        for future in concurrent.futures.as_completed(futures):
            tile_id, success, info = future.result()
            done += 1
            if not success:
                failed.append(tile_id)
            if status_callback and (done % 10 == 0 or done == total):
                status_callback(done, total, tile_id, success)
    return failed


def _regenerate_metadata(matrix):
    """Write tiles_meta.json from the fresh matrix."""
    grid_rows = len(matrix["mat"])
    grid_cols = len(matrix["mat"][0]) if grid_rows else 0

    tile_meta = {
        "tile_size": [1024, 820],
        "tile_frames": 60,
        "tile_fps": 12,
        "loop_duration_s": 5,
        "grid_rows": grid_rows,
        "grid_cols": grid_cols,
        "tiles": {},
    }
    for row_idx, row in enumerate(matrix["mat"]):
        for col_idx, tile in enumerate(row):
            if tile and tile.get("id"):
                tile_meta["tiles"][tile["id"]] = {
                    "row": row_idx,
                    "col": col_idx,
                    "mp4": f"{tile['id']}.mp4",
                    "animated": tile.get("video_size", {}).get("mp4", 0) > 6000,
                }

    with open(TILE_META_PATH, "w") as f:
        json.dump(tile_meta, f, indent=2)

    log.debug("Metadata regenerated: %dx%d grid, %d tiles.",
              grid_cols, grid_rows, len(tile_meta["tiles"]))


def _count_cached_tiles():
    """Count .mp4 files in the tile directory."""
    if not os.path.isdir(TILE_DIR):
        return 0
    return len([f for f in os.listdir(TILE_DIR) if f.endswith(".mp4")])


# ── CLI entry point ──────────────────────────────────────────────────────────

def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not has_cached_tiles():
        log.info("No cached tiles — performing initial download.")
    result = check_and_update()
    if result["offline"]:
        log.warning("Could not reach floor796.com. Using cached tiles.")
    elif not result["updated"]:
        log.info("All tiles current.")
    else:
        log.info("Downloaded %d new tiles.", len(result["new_tiles"]))


if __name__ == "__main__":
    main()
