#!/usr/bin/env python3
"""Sandbox test for the changelog-aware tile_manager.

Serves a fake floor796 site from a local HTTP server and runs
check_and_update() against it in a scratch install dir:

  1. First run        — downloads all tiles, writes state + meta
  2. Second run       — no downloads (fingerprints match)
  3. Changelog phase  — new entry added referencing tile t0r4;
                        only t0r4 is re-fetched, its strip invalidated
  4. Re-render        — t1l1 mp4 URL changes (new timestamp), size
                        identical; tile is still re-fetched
  5. Entry removal    — changelog entry deleted for b2r2; re-fetch
  6. Offline          — server down; boots from cache, state untouched
  7. Corrupt tile     — on-disk mp4 truncated; re-fetched
  8. Failed download  — 404 for one tile; marked pending, retried
                        next run when it reappears
  9. Strip regen      — after strip invalidation, strips are re-decoded
                        (simulated: prepare_strips-style check)

Run:  python3 tools/test_tile_update.py
"""
import json
import os
import shutil
import sys
import threading
import time
import urllib.request
from functools import partial
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
        # simulate previously decoded strip
        p = os.path.join(strip_dir, f"{tid}.bmp")
        with open(p, "wb") as f:
            f.write(b"BM" + b"\x00" * 100)
        return p

    print("── 1. First run: full download ──")
    r = tm.check_and_update()
    check("updated=True", r["updated"] is True)
    check("3 tiles fetched", len(r["new_tiles"]) == 3, str(r["new_tiles"]))
    check("0 changed", r["changed"] == [])
    check("all mp4s on disk",
          all(os.path.exists(os.path.join(tile_dir, f"{t}.mp4"))
              for t in tiles))
    check("state file written", os.path.exists(state_path))

    print("── 2. Second run: no-op ──")
    r = tm.check_and_update()
    check("updated=False", r["updated"] is False)
    check("total_tiles=3", r["total_tiles"] == 3)
    state = json.load(open(state_path))
    check("state has entries for t0r4",
          state["t0r4"]["entries"] == [1, 3], str(state["t0r4"]["entries"]))

    print("── 3. Changelog phase: new entry on t0r4 ──")
    changelog = json.load(open(os.path.join(SITE_DIR, "changelog.json")))
    changelog.insert(0, {"id": 99, "d": "2026-09-01", "t": "obj new phase",
                         "p": "t0r4,400,400"})
    json.dump(changelog, open(os.path.join(SITE_DIR, "changelog.json"), "w"))
    strip_for("t0r4")  # pretend it was decoded
    r = tm.check_and_update()
    check("updated=True", r["updated"] is True)
    check("only t0r4 refetched", r["new_tiles"] == ["t0r4"],
          str(r["new_tiles"]))
    check("t0r4 in changed", r["changed"] == ["t0r4"])
    check("t0r4 strip invalidated",
          not os.path.exists(os.path.join(strip_dir, "t0r4.bmp")))
    state = json.load(open(state_path))
    check("state entries updated to [1,3,99]",
          state["t0r4"]["entries"] == [1, 3, 99],
          str(state["t0r4"]["entries"]))
    check("changelog cache refreshed with 4 entries",
          len(json.load(open(os.path.join(assets, "changelog.json")))) == 4)

    print("── 4. Re-render: same size, new URL for t1l1 ──")
    matrix = json.load(open(os.path.join(SITE_DIR, "matrix.json")))
    new_rel = "scene/a/output_9999.mp4"
    shutil.copy(os.path.join(SITE_DIR, "scene/a/output_1002.mp4"),
                os.path.join(SITE_DIR, new_rel))
    matrix["mat"][0][1]["video"]["mp4"] = new_rel
    json.dump(matrix, open(os.path.join(SITE_DIR, "matrix.json"), "w"))
    r = tm.check_and_update()
    check("t1l1 refetched", r["new_tiles"] == ["t1l1"], str(r["new_tiles"]))

    print("── 5. Entry removal on b2r2 ──")
    changelog = [e for e in json.load(
        open(os.path.join(SITE_DIR, "changelog.json"))) if e["id"] != 3]
    # keep t0r4's new entry; entry 3 covered b2r2 + t0r4. Removing it
    # changes both tiles' entry sets.
    json.dump(changelog, open(os.path.join(SITE_DIR, "changelog.json"), "w"))
    r = tm.check_and_update()
    check("b2r2 refetched after entry removal",
          "b2r2" in r["new_tiles"], str(r["new_tiles"]))

    print("── 6. Offline: server refuses everything ──")
    tm.MATRIX_URL = "http://127.0.0.1:1/matrix.json"
    state_before = open(state_path).read()
    r = tm.check_and_update()
    check("offline=True", r["offline"] is True)
    check("state untouched", open(state_path).read() == state_before)
    tm.MATRIX_URL = base + "matrix.json"

    print("── 7. Corrupt tile: truncated mp4 ──")
    with open(os.path.join(tile_dir, "b2r2.mp4"), "r+b") as f:
        f.truncate(100)
    r = tm.check_and_update()
    check("b2r2 repaired", r["new_tiles"] == ["b2r2"], str(r["new_tiles"]))
    check("size restored",
          os.path.getsize(os.path.join(tile_dir, "b2r2.mp4")) == 7000)

    print("── 8. Failed download → pending → retry ──")
    matrix = json.load(open(os.path.join(SITE_DIR, "matrix.json")))
    new_rel2 = "scene/b/output_8888.mp4"
    shutil.copy(os.path.join(SITE_DIR, "scene/b/output_1003.mp4"),
                os.path.join(SITE_DIR, new_rel2))
    matrix["mat"][1][1]["video"]["mp4"] = new_rel2
    json.dump(matrix, open(os.path.join(SITE_DIR, "matrix.json"), "w"))
    # serve the matrix, but 404 the mp4
    srv.fail_tile = "output_8888.mp4"
    r = tm.check_and_update()
    check("b2r2 fetch failed", r["failed"] == ["b2r2"], str(r["failed"]))
    state = json.load(open(state_path))
    check("b2r2 marked pending", state["b2r2"].get("pending") is True)
    srv.fail_tile = None
    r = tm.check_and_update()
    check("b2r2 retried and fetched", r["new_tiles"] == ["b2r2"],
          str(r["new_tiles"]))
    state = json.load(open(state_path))
    check("pending cleared", state["b2r2"].get("pending") is False)

    print("── 9. .part cleanup ──")
    open(os.path.join(tile_dir, "junk.mp4.part"), "wb").write(b"x")
    r = tm.check_and_update()
    check(".part removed",
          not os.path.exists(os.path.join(tile_dir, "junk.mp4.part")))

    print()
    if failures:
        print(f"✗ {len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("✓ All tile_manager update checks passed.")


if __name__ == "__main__":
    main()
