#!/usr/bin/env python3
"""Regression tests for the storage-contract tile pipeline.

STORAGE CONTRACT under test: MP4s are transient scratch, tiles are
fetched one at a time, the decoded strip is the durable artifact, and
a missing MP4 with a complete strip is up to date.

Runs against a fake floor796 site served from a local HTTP server, in
a gitignored scratch directory — no network access needed.

  1. First run        — plan ONLY (no downloads happen in the plan);
                        3 tiles planned, no mp4s on disk yet
  2. Serial fetch     — fetch_tile() one entry at a time; mp4s land
  3. write_state      — fingerprints persisted after the pass
  4. Warm no-op       — strips exist, mp4s ABSENT → empty plan
                        (missing MP4 + complete strip = up to date)
  5. Strip pass       — player.prepare_strips with a REAL ffmpeg mp4:
                        strip built atomically, MP4 discarded
  6. Changelog phase  — new entry referencing t0r4 → only t0r4 planned,
                        fetch invalidates its strip
  7. Re-render        — t1l1 mp4 URL changes (new timestamp), size
                        identical; still planned
  8. Entry removal    — changelog entry deleted for b2r2 → planned
  9. Offline          — server down; plan reports offline, state
                        untouched
  10. Corrupt tile    — on-disk mp4 truncated, no strip → planned
  11. Failed fetch    — 404 for one tile; write_state marks pending;
                        retried (planned) next check
  12. Sweep           — leftover MP4s whose strips exist are deleted;
                        MP4 without a strip is kept
  13. .part cleanup   — stale .part files removed at plan time

Run:  python3 tools/test_tile_update.py
"""
import json
import os
import shutil
import sys
import threading
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

TEST_ROOT = os.path.abspath("./.test_tile_mgr")
SITE_DIR = os.path.join(TEST_ROOT, "site")
INSTALL_DIR = os.path.join(TEST_ROOT, "install")

MP4_MAGIC = b"\x00\x00\x00\x18ftypmp42"


def make_site():
    """Create the fake floor796 site content."""
    shutil.rmtree(SITE_DIR, ignore_errors=True)
    os.makedirs(os.path.join(SITE_DIR, "scene", "a"), exist_ok=True)
    os.makedirs(os.path.join(SITE_DIR, "scene", "b"), exist_ok=True)

    tiles = {
        # id: (rel_url, size)
        "t0r4": ("scene/a/output_1001.mp4", 8000),
        "t1l1": ("scene/a/output_1002.mp4", 9000),
        "b2r2": ("scene/b/output_1003.mp4", 7000),
    }
    for tid, (rel, size) in tiles.items():
        with open(os.path.join(SITE_DIR, rel), "wb") as f:
            f.write(MP4_MAGIC + b"\x11" * (size - len(MP4_MAGIC)))

    # A REAL decodable mp4 (single frame) for the strip-pass test —
    # ffmpeg generates it; served under the t0r4 URL after phase 5.
    real = os.path.join(SITE_DIR, "scene", "a", "output_real.mp4")
    import subprocess
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=s=64x48:d=0.1",
         "-frames:v", "1", "-pix_fmt", "yuv420p", real],
        capture_output=True, check=True)

    matrix = {
        "ver": 1,
        "mat": [[{"id": "t0r4", "video": {"mp4": tiles["t0r4"][0]},
                  "video_size": {"mp4": tiles["t0r4"][1]}},
                 {"id": "t1l1", "video": {"mp4": tiles["t1l1"][0]},
                  "video_size": {"mp4": tiles["t1l1"][1]}},
                 None],
                [None,
                 {"id": "b2r2", "video": {"mp4": tiles["b2r2"][0]},
                  "video_size": {"mp4": tiles["b2r2"][1]}},
                 None]],
    }
    with open(os.path.join(SITE_DIR, "matrix.json"), "w") as f:
        json.dump(matrix, f)

    changelog = [
        {"id": 1, "d": "2026-01-01", "t": "obj one",
         "p": "t0r4,100,100;t0r4,200,200"},
        {"id": 2, "d": "2026-01-02", "t": "obj two",
         "p": "t1l1,50,50"},
        {"id": 3, "d": "2026-01-03", "t": "obj three",
         "p": "b2r2,10,10;b2r2,20,20;t0r4,300,300"},
    ]
    with open(os.path.join(SITE_DIR, "changelog.json"), "w") as f:
        json.dump(changelog, f)
    return tiles


class FailCapableServer(HTTPServer):
    """HTTPServer with an optional 'fail_tile' filename to force 404s."""

    fail_tile: str | None = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0].lstrip("/")
        full = os.path.join(SITE_DIR, path)
        srv = self.server
        fail_tile = getattr(srv, "fail_tile", None)
        if fail_tile and path.endswith(fail_tile):
            self.send_response(404)
            self.end_headers()
            return
        if os.path.isfile(full):
            with open(full, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type",
                             "video/mp4" if path.endswith(".mp4")
                             else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def start_server():
    srv = FailCapableServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def fresh_install():
    """Point the kiosk package's paths at a scratch install dir."""
    shutil.rmtree(INSTALL_DIR, ignore_errors=True)
    os.makedirs(INSTALL_DIR)
    pkg_src = os.path.abspath(os.path.join(os.path.dirname(__file__), "..",
                                           "floor796_kiosk"))
    shutil.copytree(pkg_src, os.path.join(INSTALL_DIR, "floor796_kiosk"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    # scrub stale bytecode
    for root, dirs, files in os.walk(INSTALL_DIR):
        for fn in files:
            if fn.endswith(".pyc"):
                os.remove(os.path.join(root, fn))
    # drop __pycache__ dirs
    for root, dirs, files in os.walk(INSTALL_DIR, topdown=False):
        for d in dirs:
            if d == "__pycache__":
                os.rmdir(os.path.join(root, d))
    sys.path.insert(0, INSTALL_DIR)


def main():
    tiles = make_site()
    fresh_install()
    srv = start_server()
    base = f"http://127.0.0.1:{srv.server_port}/"

    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

    import floor796_kiosk.tile_manager as tm
    tm.MATRIX_URL = base + "matrix.json"
    tm.CHANGELOG_URL = base + "changelog.json"
    tm.CDN_BASE = base

    assets = os.path.join(INSTALL_DIR, "assets")
    tile_dir = os.path.join(assets, "tiles")
    strip_dir = os.path.join(INSTALL_DIR, "cache", "strips")
    os.makedirs(strip_dir, exist_ok=True)
    state_path = os.path.join(assets, "tile_state.json")

    failures = []

    def check(name, cond, detail=""):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
              + (f" — {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    def strip_for(tid):
        # simulate a previously decoded strip (a real one is >= 1.7 MB;
        # the completeness floor is 1000 bytes)
        p = os.path.join(strip_dir, f"{tid}.bmp")
        with open(p, "wb") as f:
            f.write(b"BM" + b"\x00" * 2000)
        return p

    def run_plan():
        """Plan + serial fetch pass, mirroring the player's boot."""
        r = tm.check_and_update()
        failed = []
        for entry in r.get("plan", []):
            _tid, ok, _err = tm.fetch_tile(entry)
            if not ok:
                failed.append(_tid)
        if r.get("records") and not r.get("offline"):
            tm.write_state(r["records"], r.get("entries", {}),
                           failed_ids=set(failed))
        return r, failed

    print("── 1. First run: plan only, no bulk download ──")
    r = tm.check_and_update()
    check("updated=True", r["updated"] is True)
    check("3 tiles planned", sorted(r["new_tiles"]) ==
          ["b2r2", "t0r4", "t1l1"], str(r["new_tiles"]))
    check("0 changed", r["changed"] == [])
    check("NO mp4s on disk after plan",
          not os.listdir(tile_dir) if os.path.isdir(tile_dir) else True,
          str(os.listdir(tile_dir) if os.path.isdir(tile_dir) else ""))
    check("no state file yet", not os.path.exists(state_path))

    print("── 2. Serial fetch pass (one tile at a time) ──")
    for entry in r["plan"]:
        tid, ok, info = tm.fetch_tile(entry)
        check(f"fetched {tid}", ok, str(info))
    check("all mp4s on disk",
          all(os.path.exists(os.path.join(tile_dir, f"{t}.mp4"))
              for t in tiles))
    tm.write_state(r["records"], r["entries"], failed_ids=set())
    check("state file written", os.path.exists(state_path))
    state = json.load(open(state_path))
    check("state entries for t0r4 = [1,3]",
          state["t0r4"]["entries"] == [1, 3], str(state["t0r4"]["entries"]))

    print("── 3. Sweep: no strips yet → nothing deleted ──")
    removed, _freed = tm.sweep_scratch_mp4s()
    check("sweep removed 0 (no strips)", removed == 0, str(removed))

    print("── 4. Warm no-op: strips exist, mp4s ABSENT → empty plan ──")
    for t in tiles:
        strip_for(t)
        mp4 = os.path.join(tile_dir, f"{t}.mp4")
        if os.path.exists(mp4):
            os.remove(mp4)
    r = tm.check_and_update()
    check("updated=False (missing mp4 + strip = up to date)",
          r["updated"] is False)
    check("total_tiles=3", r["total_tiles"] == 3)
    check("has_cached_tiles (strip-based)", tm.has_cached_tiles() is True)

    print("── 5. Strip pass with a real mp4: build → discard ──")
    import floor796_kiosk.player as player
    # make t0r4 a static tile served as a REAL mp4
    real_rel = "scene/a/output_real.mp4"
    real_size = os.path.getsize(os.path.join(SITE_DIR, real_rel))
    matrix = json.load(open(os.path.join(SITE_DIR, "matrix.json")))
    matrix["mat"][0][0]["video"]["mp4"] = real_rel
    matrix["mat"][0][0]["video_size"]["mp4"] = real_size
    json.dump(matrix, open(os.path.join(SITE_DIR, "matrix.json"), "w"))
    os.remove(os.path.join(strip_dir, "t0r4.bmp"))  # force rebuild
    r, failed = run_plan()
    check("t0r4 planned as re-rendered",
          [p["tid"] for p in r["plan"]] == ["t0r4"], str(r["plan"]))
    check("t0r4 strip invalidated by fetch",
          not os.path.exists(os.path.join(strip_dir, "t0r4.bmp")))
    tiles_meta = json.load(open(os.path.join(assets, "tiles_meta.json")))
    check("t0r4 marked static (size <= 6000)",
          tiles_meta["tiles"]["t0r4"]["animated"] is False)
    built_ids, pass_failed = player.prepare_strips(
        tiles_meta, status=None, display_depth=16,
        fetch_plan=r["plan"], fetch_one=tm.fetch_tile)
    check("t0r4 strip built", built_ids == ["t0r4"], str(built_ids))
    check("t0r4 mp4 DISCARDED after strip build",
          not os.path.exists(os.path.join(tile_dir, "t0r4.mp4")))
    strip_sz = os.path.getsize(os.path.join(strip_dir, "t0r4.bmp"))
    check("strip is a real BMP (>1KB)",
          strip_sz > 1000, str(strip_sz))
    check("no failed tiles in pass", pass_failed == [], str(pass_failed))
    # strip actually loadable
    import pygame
    surf = pygame.image.load(os.path.join(strip_dir, "t0r4.bmp"))
    check("strip decodes at 1024x820",
          surf.get_size() == (1024, 820), str(surf.get_size()))
    # restore fake URL for later phases
    matrix["mat"][0][0]["video"]["mp4"] = tiles["t0r4"][0]
    matrix["mat"][0][0]["video_size"]["mp4"] = tiles["t0r4"][1]
    json.dump(matrix, open(os.path.join(SITE_DIR, "matrix.json"), "w"))

    print("── 6. Changelog phase: new entry on t0r4 ──")
    changelog = json.load(open(os.path.join(SITE_DIR, "changelog.json")))
    changelog.insert(0, {"id": 99, "d": "2026-09-01", "t": "obj new phase",
                         "p": "t0r4,400,400"})
    json.dump(changelog, open(os.path.join(SITE_DIR, "changelog.json"), "w"))
    r = tm.check_and_update()
    check("only t0r4 planned", r["new_tiles"] == ["t0r4"],
          str(r["new_tiles"]))
    check("t0r4 in changed", r["changed"] == ["t0r4"])
    tid, ok, info = tm.fetch_tile(r["plan"][0])
    check("t0r4 refetched", ok, str(info))
    check("t0r4 strip invalidated",
          not os.path.exists(os.path.join(strip_dir, "t0r4.bmp")))
    strip_for("t0r4")
    tm.write_state(r["records"], r["entries"], failed_ids=set())
    state = json.load(open(state_path))
    check("state entries updated to [1,3,99]",
          state["t0r4"]["entries"] == [1, 3, 99],
          str(state["t0r4"]["entries"]))
    check("changelog cache refreshed with 4 entries",
          len(json.load(open(os.path.join(assets, "changelog.json")))) == 4)

    print("── 7. Re-render: same size, new URL for t1l1 ──")
    matrix = json.load(open(os.path.join(SITE_DIR, "matrix.json")))
    new_rel = "scene/a/output_9999.mp4"
    shutil.copy(os.path.join(SITE_DIR, "scene/a/output_1002.mp4"),
                os.path.join(SITE_DIR, new_rel))
    matrix["mat"][0][1]["video"]["mp4"] = new_rel
    json.dump(matrix, open(os.path.join(SITE_DIR, "matrix.json"), "w"))
    r = tm.check_and_update()
    check("t1l1 planned", r["new_tiles"] == ["t1l1"], str(r["new_tiles"]))

    print("── 8. Entry removal on b2r2 ──")
    changelog = [e for e in json.load(
        open(os.path.join(SITE_DIR, "changelog.json"))) if e["id"] != 3]
    json.dump(changelog, open(os.path.join(SITE_DIR, "changelog.json"), "w"))
    r = tm.check_and_update()
    check("b2r2 planned after entry removal",
          "b2r2" in r["new_tiles"], str(r["new_tiles"]))

    print("── 9. Offline: server refuses everything ──")
    tm.MATRIX_URL = "http://127.0.0.1:1/matrix.json"
    state_before = open(state_path).read()
    r = tm.check_and_update()
    check("offline=True", r["offline"] is True)
    check("state untouched", open(state_path).read() == state_before)
    tm.MATRIX_URL = base + "matrix.json"

    print("── 10. Corrupt tile: truncated mp4, no strip ──")
    # sync fingerprints with the site as of phase 8 (fetch + strip +
    # state), then corrupt an on-disk copy
    r, failed = run_plan()
    check("phase-8 changes fetched", failed == [], str(failed))
    for t in ("t0r4", "t1l1", "b2r2"):
        strip_for(t)
    mp4 = os.path.join(tile_dir, "b2r2.mp4")
    os.remove(os.path.join(strip_dir, "b2r2.bmp"))
    shutil.copy(os.path.join(SITE_DIR, "scene/b/output_1003.mp4"), mp4)
    with open(mp4, "r+b") as f:
        f.truncate(100)
    r = tm.check_and_update()
    reasons = {p["tid"]: p["reason"] for p in r["plan"]}
    check("b2r2 planned as corrupt", reasons.get("b2r2") == "corrupt",
          str(reasons))
    tid, ok, info = tm.fetch_tile(
        next(p for p in r["plan"] if p["tid"] == "b2r2"))
    check("b2r2 repaired", ok, str(info))
    check("size restored", os.path.getsize(mp4) == 7000)

    print("── 11. Failed fetch → pending → retry ──")
    matrix = json.load(open(os.path.join(SITE_DIR, "matrix.json")))
    new_rel2 = "scene/b/output_8888.mp4"
    shutil.copy(os.path.join(SITE_DIR, "scene/b/output_1003.mp4"),
                os.path.join(SITE_DIR, new_rel2))
    matrix["mat"][1][1]["video"]["mp4"] = new_rel2
    json.dump(matrix, open(os.path.join(SITE_DIR, "matrix.json"), "w"))
    if os.path.exists(os.path.join(strip_dir, "b2r2.bmp")):
        os.remove(os.path.join(strip_dir, "b2r2.bmp"))
    srv.fail_tile = "output_8888.mp4"
    r, failed = run_plan()
    check("b2r2 fetch failed", failed == ["b2r2"], str(failed))
    state = json.load(open(state_path))
    check("b2r2 marked pending", state["b2r2"].get("pending") is True)
    srv.fail_tile = None
    r = tm.check_and_update()
    reasons = {p["tid"]: p["reason"] for p in r["plan"]}
    check("b2r2 planned as retry", reasons.get("b2r2") == "retry",
          str(reasons))
    tid, ok, info = tm.fetch_tile(r["plan"][0])
    check("b2r2 retried and fetched", ok and tid == "b2r2", str(info))
    strip_for("b2r2")
    tm.write_state(r["records"], r["entries"], failed_ids=set())
    state = json.load(open(state_path))
    check("pending cleared", state["b2r2"].get("pending") is False)

    print("── 12. Sweep: scratch mp4s deleted iff strip exists ──")
    # strip exists for b2r2 but its mp4 lingers → swept
    strip_for("t1l1")
    # t1l1 has NO strip (invalidated in phase 6/7? ensure removed)
    if os.path.exists(os.path.join(strip_dir, "t1l1.bmp")):
        os.remove(os.path.join(strip_dir, "t1l1.bmp"))
    os.remove(os.path.join(strip_dir, "t0r4.bmp"))
    removed, freed = tm.sweep_scratch_mp4s()
    mp4s_left = sorted(f for f in os.listdir(tile_dir) if f.endswith(".mp4"))
    check("swept b2r2 (strip exists)", removed == 1, str(removed))
    check("t1l1 mp4 kept (no strip)", "t1l1.mp4" in mp4s_left,
          str(mp4s_left))
    check("t0r4 mp4 kept (no strip)", "t0r4.mp4" in mp4s_left,
          str(mp4s_left))

    print("── 13. .part cleanup ──")
    open(os.path.join(tile_dir, "junk.mp4.part"), "wb").write(b"x")
    tm.check_and_update()
    check(".part removed",
          not os.path.exists(os.path.join(tile_dir, "junk.mp4.part")))

    print()
    if failures:
        print(f"✗ {len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("✓ All storage-contract tile pipeline checks passed.")


if __name__ == "__main__":
    main()
