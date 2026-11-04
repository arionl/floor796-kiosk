#!/usr/bin/env python3
"""Test settings.py (resolution logic + settings file) and the web-UI
HTTP endpoints (/, /settings, /debug/caches, /debug/clear,
/debug/restart).

Run headless:  SDL_VIDEODRIVER=dummy python3 tools/test_settings.py
"""

import json
import os
import sys
import tempfile
import threading
import urllib.request
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from floor796_kiosk import settings as S
from floor796_kiosk import paths
from floor796_kiosk.stats import webui

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok  %s" % name)
    else:
        FAIL += 1
        print("FAIL  %s  %s" % (name, detail))  # noqa - detail may be any repr


# ── resolve_resolution ───────────────────────────────────────────────────────

def test_resolve():
    print("resolve_resolution:")
    # auto: native passthrough on a 1080p display, any RAM
    w, h, src, _ = S.resolve_resolution("auto", 1920, 1080, 4000)
    check("auto 1080p native", (w, h, src) == (1920, 1080, "native"))

    # auto: 4K display on an 8 GB board → native 4K
    w, h, src, _ = S.resolve_resolution("auto", 3840, 2160, 8192)
    check("auto 4K/8GB native", (w, h, src) == (3840, 2160, "native"))

    # auto: 4K display on a 4 GB board → capped (the standing rule)
    w, h, src, notes = S.resolve_resolution("auto", 3840, 2160, 4096)
    check("auto 4K/4GB capped", (w, h, src) == (1920, 1080,
                                                "auto-capped-1080p"))
    check("cap note present", any("capped" in n for n in notes), notes)

    # forced 1080p on a 4K display (arion's low-memory case)
    w, h, src, notes = S.resolve_resolution("1080p", 3840, 2160, 4096)
    check("forced 1080p on 4K", (w, h, src) == (1920, 1080, "forced-1080p"))
    check("forced note mentions upscale",
          any("scaler" in n for n in notes), notes)

    # forced 4K on a 4K display, small board: allowed with a warning
    w, h, src, notes = S.resolve_resolution("4k", 3840, 2160, 4096)
    check("forced 4K allowed", (w, h, src) == (3840, 2160, "forced-4k"))
    check("4K RAM warning", any("recommended" in n for n in notes), notes)

    # forced 4K on a 1080p-only display: falls back to auto behavior
    w, h, src, notes = S.resolve_resolution("4k", 1920, 1080, 8192)
    check("4K refused on 1080p display",
          (w, h, src) == (1920, 1080, "native"), (w, h, src))
    check("fallback note", any("falling back" in n for n in notes), notes)

    # no detection at all
    w, h, src, notes = S.resolve_resolution("auto", 0, 0, 4096)
    check("no display → fallback 1080p",
          (w, h, src) == (1920, 1080, "fallback-1080p"))

    # garbage setting behaves as auto
    w, h, src, _ = S.resolve_resolution("bogus", 3840, 2160, 8192)
    check("bogus setting → auto native", (w, h, src) == (3840, 2160,
                                                         "native"))


# ── capabilities ─────────────────────────────────────────────────────────────

def test_capabilities():
    print("resolution_capabilities:")
    caps = {c["value"]: c for c in
            S.resolution_capabilities(3840, 2160, 4096)}
    check("4K available on 4K display", caps["4k"]["available"])
    check("4K not recommended at 4 GB",
          caps["4k"]["recommended"] is False)
    check("1080p recommended at 4 GB", caps["1080p"]["recommended"])
    check("auto recommended by default", caps["auto"]["recommended"])

    caps = {c["value"]: c for c in
            S.resolution_capabilities(3840, 2160, 8192)}
    check("4K recommended at 8 GB", caps["4k"]["recommended"])

    caps = {c["value"]: c for c in
            S.resolution_capabilities(1920, 1080, 4096)}
    check("4K unavailable on 1080p display",
          caps["4k"]["available"] is False)


# ── settings file ────────────────────────────────────────────────────────────

def test_file():
    print("settings file:")
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "settings.json")
        s = S.load_settings(p)
        check("missing file → defaults",
              s == {"resolution": "auto"}, s)
        S.save_settings(p, {"resolution": "1080p"})
        s = S.load_settings(p)
        check("round-trip", s == {"resolution": "1080p"}, s)
        with open(p, "w") as f:
            f.write("{corrupt json")
        s = S.load_settings(p)
        check("corrupt file → defaults",
              s == {"resolution": "auto"}, s)
        S.save_settings(p, {"resolution": "nope"})
        s = S.load_settings(p)
        check("invalid value sanitized", s["resolution"] == "auto", s)
        # atomic: no stray tmp files left behind
        check("no tmp files", [f for f in os.listdir(d)
                               if f.endswith(".tmp")] == [])


# ── cache report / clear ─────────────────────────────────────────────────────

def test_caches():
    print("cache report + clear:")
    with tempfile.TemporaryDirectory() as d:
        # Fake a strip cache
        strips = os.path.join(d, "strips")
        os.makedirs(strips)
        with open(os.path.join(strips, "a.bmp"), "wb") as f:
            f.write(b"x" * 1000)
        mask = os.path.join(d, "mask.npz")
        with open(mask, "wb") as f:
            f.write(b"y" * 50)
        orig = {k: v for k, v in webui.CACHE_TARGETS.items()}
        webui.CACHE_TARGETS["strips"] = ("strips", [strips], "")
        webui.CACHE_TARGETS["content_mask"] = ("mask", [mask], "")
        try:
            rep = {c["key"]: c for c in webui.cache_report()}
            check("report sizes", rep["strips"]["bytes"] == 1000 and
                  rep["strips"]["files"] == 1, rep["strips"])
            freed, _ = webui.clear_caches(["strips", "content_mask"])
            check("clear frees bytes", freed == 1050, freed)
            check("strips dir gone", not os.path.exists(strips))
            check("mask file gone", not os.path.exists(mask))
            freed, _ = webui.clear_caches(["strips"])
            check("clear of missing is safe", freed == 0)
            freed, rep2 = webui.clear_caches(["nonexistent"])
            check("unknown target reported",
                  any("error" in r for r in rep2), rep2)
        finally:
            webui.CACHE_TARGETS.clear()
            webui.CACHE_TARGETS.update(orig)


# ── HTTP endpoints ───────────────────────────────────────────────────────────

class _FakeCollector:
    def snapshot(self, window_seconds=None):
        return {"uptime": 10, "rss_mb": 100, "rss_min": 90, "rss_max": 110,
                "rss_avg": 100, "fps": 30, "fps_avg": 30, "fps_min": 28,
                "fps_max": 32, "overlay_enabled": False}

    def set_overlay(self, enabled, window=None):
        pass

    overlay_enabled = False
    overlay_window = "1h"


def _req(base, path, method="GET", body=None):
    req = urllib.request.Request(base + path, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_http():
    print("HTTP endpoints:")
    from floor796_kiosk.stats.http_server import StatsHandler, start_stats_server
    from http.server import HTTPServer

    with tempfile.TemporaryDirectory() as d:
        # Point the settings file at the temp dir
        orig = paths.SETTINGS_PATH
        paths.SETTINGS_PATH = os.path.join(d, "settings.json")
        try:
            srv = HTTPServer(("127.0.0.1", 0), StatsHandler)
            srv.collector = _FakeCollector()
            srv.settings_hw = {"total_mem_mb": 4096,
                               "detected_w": 3840, "detected_h": 2160}
            srv.settings_active = {"w": 1920, "h": 1080,
                                   "source": "auto-capped-1080p",
                                   "setting": "auto"}
            restarts = []
            ev = threading.Event()

            def _req_restart():
                if ev.is_set():
                    return False
                ev.set()
                restarts.append(1)
                return True
            srv.request_restart = _req_restart
            port = srv.server_address[1]
            base = "http://127.0.0.1:%d" % port
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                # web UI page
                code, body = _req(base, "/")
                check("GET / serves HTML", code == 200 and
                      body.startswith(b"<!DOCTYPE html>"))
                check("page mentions resolution",
                      b"Resolution" in body)

                # settings read
                code, body = _req(base, "/settings")
                d0 = json.loads(body)
                check("GET /settings", code == 200 and
                      d0["settings"] == {"resolution": "auto"})
                check("caps include 4K", any(
                    c["value"] == "4k" and c["available"]
                    for c in d0["capabilities"]))

                # settings write + read-back
                code, body = _req(base, "/settings", "POST",
                                  {"resolution": "1080p"})
                check("POST /settings 1080p", code == 200)
                code, body = _req(base, "/settings")
                d1 = json.loads(body)
                check("setting persisted",
                      d1["settings"]["resolution"] == "1080p")
                code, body = _req(base, "/settings", "POST",
                                  {"resolution": "nope"})
                check("invalid setting rejected", code == 400)
                code, body = _req(base, "/settings", "POST",
                                  {"resolution": "4k"})
                check("4K accepted (display supports)",
                      code == 200, body)
                # back to 4K-unavailable display → 409
                srv.settings_hw = {"total_mem_mb": 4096,
                                   "detected_w": 1920,
                                   "detected_h": 1080}
                code, body = _req(base, "/settings", "POST",
                                  {"resolution": "4k"})
                check("4K refused on 1080p display", code == 409)

                # caches report
                code, body = _req(base, "/debug/caches")
                dc = json.loads(body)
                check("GET /debug/caches", code == 200 and
                      isinstance(dc["caches"], list) and
                      len(dc["caches"]) == len(webui.CACHE_ORDER))

                # clear (against a fake target)
                orig_targets = dict(webui.CACHE_TARGETS)
                strips = os.path.join(d, "strips")
                os.makedirs(strips)
                with open(os.path.join(strips, "s.bmp"), "wb") as f:
                    f.write(b"z" * 123)
                webui.CACHE_TARGETS["strips"] = ("s", [strips], "")
                try:
                    code, body = _req(base, "/debug/clear", "POST",
                                      {"targets": ["strips"]})
                    dr = json.loads(body)
                    check("POST /debug/clear", code == 200 and
                          dr["freed_bytes"] == 123, body)
                    code, body = _req(base, "/debug/clear", "POST",
                                      {"targets": []})
                    check("empty targets rejected", code == 400)
                finally:
                    webui.CACHE_TARGETS.clear()
                    webui.CACHE_TARGETS.update(orig_targets)

                # restart
                code, body = _req(base, "/debug/restart", "POST")
                check("POST /debug/restart", code == 200 and
                      json.loads(body)["restarting"])
                code, body = _req(base, "/debug/restart", "POST")
                check("second restart → 409", code == 409)
            finally:
                srv.shutdown()
        finally:
            paths.SETTINGS_PATH = orig


if __name__ == "__main__":
    test_resolve()
    test_capabilities()
    test_file()
    test_caches()
    test_http()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
