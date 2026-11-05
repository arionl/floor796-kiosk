#!/usr/bin/env python3
"""
Floor796 tile manager — plan tile updates and fetch them one at a time.

STORAGE CONTRACT — MP4s are transient scratch.  The decoded frame strip
in ``cache/strips/<tid>.bmp`` is the durable per-tile artifact; a tile's
source MP4 is deleted the moment its strip is (re)built, and tiles are
fetched one at a time, never in bulk.  At rest ``assets/tiles/`` is
empty — peak disk usage equals the final usage plus one in-flight MP4.

At startup the player calls ``check_and_update()`` which builds an
update PLAN (no tile downloads):

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
     plans a fetch for new tiles plus any tile whose fingerprint
     drifted (reasons: new / re-rendered / changelog / retry / size /
     missing / corrupt).

Under the storage contract a MISSING MP4 is not itself a reason to
fetch: a tile whose fingerprint matches and whose strip exists is up
to date.  ``missing`` fires only when there is no usable copy at all
(no MP4 and no complete strip).

The player then runs the plan inside its strip pass
(``player.prepare_strips``): for each tile without a valid strip it
calls ``fetch_tile()`` (one at a time), decodes the strip, discards
the MP4, and finally ``write_state()`` persists fingerprints for the
pass (failed fetches are marked ``pending`` and retried next boot —
an interrupted pass re-plans the same tiles, and tiles whose strips
already exist are skipped, so a crash mid-pass self-heals).

``sweep_scratch_mp4s()`` deletes any leftover MP4 whose strip already
exists (idempotent; migrates installs from the keep-forever scheme).

The first run after upgrading to this scheme has no state file and
plans every tile once to establish fingerprints.  This is
intentional: it also repairs any tile that went stale under the old
size-only comparison.

If the network is unavailable, the kiosk silently falls back to the
existing cached strips so it always boots offline.
"""

import json
import logging
import os
import random
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

# A strip BMP smaller than this is a stub/partial, not a usable strip
# (a real 16-bit static strip is ~1.7 MB, an animated one ~100 MB).
STRIP_MIN_BYTES = 1000


# ── Public API ────────────────────────────────────────────────────────────────

def check_and_update(status_callback=None):
    """Build the tile update plan. Does NOT download anything.

    Returns a dict:
        updated (bool)    — True if any tile needs a fetch.
        plan (list)       — fetch plan entries: dicts with tid, url,
                            expected_size, reason — processed ONE AT A
                            TIME by the caller (player.prepare_strips).
        new_tiles (list)  — tile IDs planned for fetch (new + changed).
        changed (list)    — tile IDs that had content before (i.e. the
                            author extended/re-rendered them).
        offline (bool)    — True if floor796.com was unreachable.
        total_tiles (int) — total tiles in the matrix.
        records (dict)    — per-tile fingerprint records for the whole
                            matrix, for write_state() after the pass.
        entries (dict)    — tile id → changelog entry ids, for
                            write_state() after the pass.
        changelog_fresh (bool) — True if the live changelog was fetched.
    """
    ensure_dirs()
    _cleanup_part_files()

    matrix = _fetch_json(MATRIX_URL)
    if matrix is None:
        log.info("Offline or unreachable — using cached tiles.")
        return {"updated": False, "plan": [], "new_tiles": [], "changed": [],
                "offline": True, "total_tiles": _count_cached_tiles(),
                "records": {}, "entries": {},
                "changelog_fresh": False}

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

    plan = []
    changed_ids = []
    records = {}
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

            records[tid] = {
                "url": rel_url,
                "size": expected_size,
                "entries": entries,
                "pending": False,
            }

            reason = _needs_download(tid, old_state, rel_url, entries,
                                     expected_size, mp4_path)
            if reason:
                plan.append({"tid": tid, "url": CDN_BASE + rel_url,
                             "expected_size": expected_size,
                             "reason": reason})
                had_content = (os.path.exists(mp4_path) or
                               _strip_complete(tid))
                if had_content:
                    changed_ids.append(tid)
                log.info("Tile %s: %s — will fetch.", tid, reason)

    # tiles_meta.json + changelog cache are metadata-only commits:
    # safe to refresh even if the fetch pass later fails or is
    # interrupted (state, which gates re-fetches, is written only
    # after the pass by write_state()).
    _regenerate_metadata(matrix)
    if changelog_fresh:
        _cache_changelog(changelog)

    if not plan:
        log.info("Tiles up to date (%d tiles).", total)
        return {"updated": False, "plan": [], "new_tiles": [], "changed": [],
                "offline": False, "total_tiles": total, "records": records,
                "entries": tile_entries, "changelog_fresh": changelog_fresh}

    log.info("Planned fetch for %d tile(s): %d changed, %d new.",
             len(plan), len(changed_ids), len(plan) - len(changed_ids))

    return {"updated": True, "plan": plan,
            "new_tiles": [p["tid"] for p in plan], "changed": changed_ids,
            "offline": False, "total_tiles": total, "records": records,
            "entries": tile_entries, "changelog_fresh": changelog_fresh}


def _invalidate_strips(tile_id):
    """Delete decoded strips so the strip pass re-decodes the new mp4.

    Without this, the pass would see the cached .bmp and keep playing
    the old animation forever.
    """
    for ext in (".bmp", ".png"):
        p = os.path.join(STRIP_DIR, f"{tile_id}{ext}")
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError as exc:
                log.warning("Could not remove stale strip %s: %s", p, exc)


def fetch_tile(entry):
    """Fetch ONE tile MP4 atomically (storage contract: serial).

    ``entry`` is a plan dict from check_and_update() (tid / url /
    expected_size).  Returns (tile_id, success, info).  On success the
    tile's stale strip is invalidated so the caller re-decodes from the
    fresh MP4; on failure nothing on disk changes.
    """
    tid = entry["tid"]
    mp4_path = os.path.join(TILE_DIR, f"{tid}.mp4")
    tmp_path = mp4_path + ".part"
    try:
        req = urllib.request.Request(entry["url"], headers={
            "User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp, \
                open(tmp_path, "wb") as f:
            f.write(resp.read())
        actual = os.path.getsize(tmp_path)
        if entry["expected_size"] > 0 and actual != entry["expected_size"]:
            return (tid, False,
                    f"size mismatch {actual}/{entry['expected_size']}")
        os.replace(tmp_path, mp4_path)
        _invalidate_strips(tid)
        return (tid, True, actual)
    except Exception as exc:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return (tid, False, str(exc))


def write_state(records, entries, failed_ids):
    """Persist per-tile fingerprints after a fetch pass.

    Written for every tile in the matrix; tiles whose fetch failed are
    marked ``pending`` so the next check retries them.
    """
    for tid in failed_ids:
        if tid in records:
            records[tid]["pending"] = True
    _atomic_json_write(STATE_PATH, records)
    failed_n = len(failed_ids)
    if failed_n:
        log.warning("%d tile fetch(es) failed — marked pending, will "
                    "retry next check.", failed_n)


def sweep_scratch_mp4s(tiles_meta=None):
    """Delete every MP4 whose strip already exists (idempotent).

    The strip is the durable artifact; the MP4 is scratch.  Returns
    (count, bytes_freed).  Never deletes an MP4 whose strip is missing
    or partial — that tile still needs it for a (re)decode.
    """
    removed = 0
    freed = 0
    if not os.path.isdir(TILE_DIR):
        return (removed, freed)
    for name in os.listdir(TILE_DIR):
        if not name.endswith(".mp4"):
            continue
        tid = name[:-len(".mp4")]
        if _strip_complete(tid):
            p = os.path.join(TILE_DIR, name)
            try:
                freed += os.path.getsize(p)
                os.remove(p)
                removed += 1
            except OSError as exc:
                log.warning("Could not remove scratch mp4 %s: %s", p, exc)
    if removed:
        log.info("mp4 sweep: discarded %d scratch MP4(s), freed %.1f MB.",
                 removed, freed / 1e6)
    return (removed, freed)


def has_cached_tiles():
    """True if tiles_meta.json and at least one decoded strip exist."""
    return (os.path.exists(TILE_META_PATH) and
            os.path.isdir(STRIP_DIR) and
            _count_cached_tiles() > 0)


def _strip_complete(tid):
    """True if a usable strip BMP exists for the tile."""
    p = os.path.join(STRIP_DIR, f"{tid}.bmp")
    try:
        return os.path.getsize(p) > STRIP_MIN_BYTES
    except OSError:
        return False


# ── Fingerprint comparison ────────────────────────────────────────────────────

def _needs_download(tid, old_state, rel_url, entries, expected_size,
                    mp4_path):
    """Decide whether a tile must be (re)fetched. Returns reason or None.

    STORAGE CONTRACT: a missing MP4 is fine as long as the tile's
    strip exists — only a tile with neither a usable MP4 nor a strip
    is ``missing``.
    """
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
        if _strip_complete(tid):
            return None
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
    """Count decoded strip BMPs (the durable per-tile artifacts)."""
    if not os.path.isdir(STRIP_DIR):
        return 0
    return len([f for f in os.listdir(STRIP_DIR)
                if f.endswith(".bmp")
                and os.path.getsize(os.path.join(STRIP_DIR, f)) > STRIP_MIN_BYTES])


# ── CLI entry point ──────────────────────────────────────────────────────────

def main():
    """Report the update plan (the player performs the fetch pass)."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not has_cached_tiles():
        log.info("No cached strips yet — first boot will fetch tiles.")
    result = check_and_update()
    if result["offline"]:
        log.warning("Could not reach floor796.com. Using cached tiles.")
    elif not result["updated"]:
        log.info("All tiles current.")
    else:
        log.info("Planned fetch for %d tile(s): %d changed, %d new. "
                 "The fetch runs one tile at a time during the "
                 "player's strip pass at boot.",
                 len(result["new_tiles"]), len(result["changed"]),
                 len(result["new_tiles"]) - len(result["changed"]))


if __name__ == "__main__":
    main()
