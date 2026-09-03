#!/usr/bin/env python3
"""
Floor796 tile manager — download and keep tile assets up to date.

At startup the player calls ``check_and_update()`` which:
  1. Fetches the latest tile matrix and changelog from floor796.com
     (15s timeout each).
  2. Builds a per-tile fingerprint from:
       - the mp4 URL (it embeds a render timestamp, so this catches
         re-renders even when the file size is identical)
       - the expected mp4 size
       - the sorted set of changelog entry ids whose polygon points
         reference the tile.  The author often extends a tile in
         phases; each phase adds a changelog entry, so a change in
         this set means the tile artwork changed and it must be
         re-fetched.
  3. Compares fingerprints against ``assets/tile_state.json`` and
     downloads new tiles plus any tile whose fingerprint drifted.
  4. Regenerates ``tiles_meta.json`` and refreshes the cached
     ``assets/changelog.json`` used by the object highlighter.

Tiles that are re-fetched have their decoded strips removed from
``cache/strips/`` so ``prepare_strips()`` re-decodes them from the new
mp4, and the caller patches the content density mask incrementally
(see ``content_mask.update_tiles()``).

The first run after upgrading to this scheme has no state file and
re-fetches every tile once to establish fingerprints.  This is
intentional: it also repairs any tile that went stale under the old
size-only comparison.

If the network is unavailable, the kiosk silently falls back to the
existing cached tiles so it always boots offline.
"""

import json
import logging
import os
import random
import concurrent.futures
import urllib.request
import urllib.error

from floor796_kiosk import __version__
from floor796_kiosk.paths import (ASSETS_DIR, TILE_DIR, TILE_META_PATH,
                                  CHANGELOG_PATH, STRIP_DIR, ensure_dirs)

log = logging.getLogger("floor796")

MATRIX_URL = "https://floor796.com/data/matrix.json"
CDN_BASE = "https://cdn.floor796.com/data/"
CHANGELOG_URL = "https://floor796.com/data/changelog.json"

NETWORK_TIMEOUT = 15   # metadata fetches
DOWNLOAD_TIMEOUT = 60  # per-tile mp4 fetch
USER_AGENT = f"Floor796-Kiosk/{__version__}"

# State file: per-tile fingerprint of the content we have on disk.
STATE_PATH = os.path.join(ASSETS_DIR, "tile_state.json")


# ── Public API ────────────────────────────────────────────────────────────────

def check_and_update(status_callback=None):
    """Check for tile updates; download if available.

    Returns a dict:
        updated (bool)    — True if tiles were added/changed.
        new_tiles (list)  — tile IDs that were fetched (new + changed).
        changed (list)    — tile IDs that replaced an existing file
                            (i.e. the author extended/re-rendered them).
        offline (bool)    — True if floor796.com was unreachable.
        total_tiles (int) — total tiles after update.
        failed (list)     — tile IDs whose download failed (retried on
                            the next check via the ``pending`` flag).
    """
    ensure_dirs()
    _cleanup_part_files()

    matrix = _fetch_json(MATRIX_URL)
    if matrix is None:
        log.info("Offline or unreachable — using cached tiles.")
        return {"updated": False, "new_tiles": [], "changed": [],
                "offline": True, "total_tiles": _count_cached_tiles(),
                "failed": []}

    changelog = _fetch_json(CHANGELOG_URL)
    changelog_fresh = changelog is not None
    if not changelog_fresh:
        # Fall back to the cached changelog for fingerprinting so a
        # temporarily unreachable changelog doesn't trigger a mass
        # redownload ("entries changed" vs an empty set).
        log.warning("Changelog unreachable — using cached changelog for "
                    "freshness checks.")
        changelog = _load_cached_changelog() or []

    old_state = _load_state()
    tile_entries = _changelog_tile_entries(changelog)

    to_download = []
    changed_ids = []
    total = 0

    for row in matrix["mat"]:
        for tile in row:
            if not tile or not tile.get("video", {}).get("mp4"):
                continue
            total += 1
            tid = tile["id"]
            rel_url = tile["video"]["mp4"]
            expected_size = tile.get("video_size", {}).get("mp4", 0)
            entries = tile_entries.get(tid, [])
            mp4_path = os.path.join(TILE_DIR, f"{tid}.mp4")

            reason = _needs_download(tid, old_state, rel_url, entries,
                                     expected_size, mp4_path)
            if reason:
                to_download.append((tid, mp4_path, CDN_BASE + rel_url,
                                    expected_size))
                if os.path.exists(mp4_path):
                    changed_ids.append(tid)
                log.info("Tile %s: %s — fetching.", tid, reason)

    if not to_download:
        _regenerate_metadata(matrix)
        _save_state(matrix, tile_entries, failed_ids=set())
        if changelog_fresh:
            _cache_changelog(changelog)
        log.info("Tiles up to date (%d tiles).", total)
        return {"updated": False, "new_tiles": [], "changed": [],
                "offline": False, "total_tiles": total, "failed": []}

    log.info("Fetching %d tile(s): %d changed, %d new.",
             len(to_download), len(changed_ids),
             len(to_download) - len(changed_ids))
    failed = _download_all(to_download, status_callback)

    _regenerate_metadata(matrix)
    _save_state(matrix, tile_entries, failed_ids=set(failed))
    if changelog_fresh:
        _cache_changelog(changelog)

    if failed:
        log.warning("Failed to fetch %d tile(s): %s — will retry next "
                    "check.", len(failed), failed)
    log.info("Update complete: %d tiles (%d fetched, %d refreshed).",
             total, len(to_download), len(changed_ids))

    return {"updated": True, "new_tiles": [t[0] for t in to_download],
            "changed": changed_ids, "failed": failed,
            "offline": False, "total_tiles": total}


def has_cached_tiles():
    """True if tiles_meta.json and at least one tile MP4 exist locally."""
    return (os.path.exists(TILE_META_PATH) and
            os.path.isdir(TILE_DIR) and
            len(os.listdir(TILE_DIR)) > 0)


# ── Fingerprint comparison ────────────────────────────────────────────────────

def _needs_download(tid, old_state, rel_url, entries, expected_size,
                    mp4_path):
    """Decide whether a tile must be (re)fetched. Returns reason or None."""
    if tid not in old_state:
        return "new"
    rec = old_state[tid]

    if rec.get("url") != rel_url:
        return "re-rendered"
    if rec.get("entries", []) != entries:
        return "changelog"
    if rec.get("pending"):
        return "retry"
    if rec.get("size", -1) != expected_size:
        return "size"
    if not os.path.exists(mp4_path):
        return "missing"
    if expected_size > 0 and os.path.getsize(mp4_path) != expected_size:
        return "corrupt"
    return None


def _changelog_tile_entries(changelog):
    """Map tile id → sorted list of changelog entry ids referencing it.

    Changelog polygon points live in the ``p`` field, formatted as
    ``t0r4,664,383;t2l2,192,43;...`` — the first token of each point is
    a tile reference.  When the author extends a tile in phases, new
    entries (higher ids) appear that reference the same tile, so the
    entry-id set per tile is a reliable content signature.
    """
    result = {}
    for entry in changelog:
        entry_id = entry.get("id")
        p_str = entry.get("p", "") or ""
        if entry_id is None or not p_str:
            continue
        for part in p_str.split(";"):
            tile_ref = part.split(",")[0] if part else ""
            if tile_ref:
                result.setdefault(tile_ref, set()).add(entry_id)
    return {tid: sorted(ids) for tid, ids in result.items()}


# ── State persistence ─────────────────────────────────────────────────────────

def _load_state():
    """Load tile_state.json. Returns {} on first run or corruption."""
    try:
        with open(STATE_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (IOError, json.JSONDecodeError):
        pass
    return {}


def _save_state(matrix, tile_entries, failed_ids):
    """Persist per-tile fingerprints.

    Written for every tile in the matrix.  Tiles whose fetch failed are
    marked ``pending`` so the next check retries them.
    """
    records = {}
    for row in matrix["mat"]:
        for tile in row:
            if not tile or not tile.get("video", {}).get("mp4"):
                continue
            tid = tile["id"]
            records[tid] = {
                "url": tile["video"]["mp4"],
                "size": tile.get("video_size", {}).get("mp4", 0),
                "entries": tile_entries.get(tid, []),
                "pending": tid in failed_ids,
            }
    _atomic_json_write(STATE_PATH, records)


def _atomic_json_write(path, data):
    """Write JSON atomically (tmp + rename) so a crash can't corrupt it."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _load_cached_changelog():
    """Load assets/changelog.json if present (highlighter's cache)."""
    try:
        with open(CHANGELOG_PATH) as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except (IOError, json.JSONDecodeError):
        pass
    return None


def _cache_changelog(changelog):
    """Refresh assets/changelog.json so the highlighter sees new objects."""
    try:
        _atomic_json_write(CHANGELOG_PATH, changelog)
        log.debug("Changelog cache refreshed (%d entries).", len(changelog))
    except OSError as exc:
        log.warning("Could not refresh changelog cache: %s", exc)


# ── Downloading ───────────────────────────────────────────────────────────────

def _cleanup_part_files():
    """Remove leftover .part files from an interrupted download."""
    if not os.path.isdir(TILE_DIR):
        return
    for name in os.listdir(TILE_DIR):
        if name.endswith(".part"):
            try:
                os.remove(os.path.join(TILE_DIR, name))
            except OSError:
                pass


def _download_tile(args):
    """Download a single tile atomically. Returns (tile_id, success, info)."""
    tid, mp4_path, mp4_url, expected_size = args
    tmp_path = mp4_path + ".part"
    try:
        req = urllib.request.Request(mp4_url, headers={
            "User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp, \
                open(tmp_path, "wb") as f:
            f.write(resp.read())
        actual = os.path.getsize(tmp_path)
        if expected_size > 0 and actual != expected_size:
            return (tid, False, f"size mismatch {actual}/{expected_size}")
        os.replace(tmp_path, mp4_path)
        _invalidate_strips(tid)
        return (tid, True, actual)
    except Exception as exc:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return (tid, False, str(exc))


def _invalidate_strips(tile_id):
    """Delete decoded strips so prepare_strips() re-decodes the new mp4.

    Without this, prepare_strips() would see the cached .bmp and keep
    playing the old animation forever.
    """
    for ext in (".bmp", ".png"):
        p = os.path.join(STRIP_DIR, f"{tile_id}{ext}")
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError as exc:
                log.warning("Could not remove stale strip %s: %s", p, exc)


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
                log.warning("Tile %s failed: %s", tile_id, info)
            if status_callback and (done % 10 == 0 or done == total):
                status_callback(done, total, tile_id, success)
    return failed


# ── Network / metadata ────────────────────────────────────────────────────────

def _fetch_json(url):
    """Fetch a JSON document with cache-busting. Returns parsed data or None."""
    try:
        req = urllib.request.Request(
            f"{url}?r={random.random()}",
            headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=NETWORK_TIMEOUT) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.debug("Fetch failed for %s: %s", url, exc)
        return None
    except json.JSONDecodeError as exc:
        log.warning("JSON parse error for %s: %s", url, exc)
        return None


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

    _atomic_json_write(TILE_META_PATH, tile_meta)
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
        log.info("Fetched %d tile(s): %d changed, %d new; %d failed.",
                 len(result["new_tiles"]), len(result["changed"]),
                 len(result["new_tiles"]) - len(result["changed"]),
                 len(result["failed"]))


if __name__ == "__main__":
    main()
