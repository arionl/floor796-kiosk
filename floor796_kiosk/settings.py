"""settings.py — persistent operator settings for the kiosk.

A tiny JSON file (``settings.json`` next to the package) holds
operator-chosen overrides that the player applies at boot.  This is the
kiosk's counterpart of an OS settings bundle: the web UI served by the
stats HTTP server (http://127.0.0.1:8796/) reads and writes these
values.

Current settings:
  resolution : "auto" | "1080p" | "4k"
      auto  — use the display's native mode (with the standing
              <=4 GB-RAM 1080p cap; the default, and what the kiosk
              did before settings existed)
      1080p — always render at 1920x1080, even when the display
              supports more.  KMSDRM negotiates a 1080p mode and the
              TV's scaler upscales — the right choice for a 4K
              display attached to a board without the RAM to feed it.
      4k    — render at 3840x2160.  Only offered when the display
              actually reports a >=4K mode; the UI warns when board
              RAM is under the recommended 8 GB.

Pure logic (no pygame, no I/O beyond the settings file) so it is
unit-testable headless.
"""

import json
import os
import tempfile

# ── Resolution options ───────────────────────────────────────────────────────

RES_AUTO = "auto"
RES_1080P = "1080p"
RES_4K = "4k"
RES_CHOICES = (RES_AUTO, RES_1080P, RES_4K)

# Below this much RAM the budget cannot feed a 4K viewport — the UI
# marks 4K as "not recommended" (still an explicit operator choice).
RES_4K_RECOMMENDED_MEM_MB = 8192
# The standing hard cap from player.py: boards at/below this render
# 1080p in auto mode regardless of the attached display.
RES_1080P_CAP_MEM_MB = 4096

W_1080P, H_1080P = 1920, 1080
W_4K, H_4K = 3840, 2160


def resolve_resolution(setting, detected_w, detected_h, total_mem_mb):
    """Decide the render resolution from the setting + hardware facts.

    Returns ``(w, h, source, notes)`` where *source* is one of
    "native", "auto-capped-1080p", "forced-1080p", "forced-4k",
    "fallback-1080p" and *notes* is a list of human-readable strings
    (empty when nothing noteworthy happened).
    """
    notes = []
    detected_w = detected_w or 0
    detected_h = detected_h or 0
    native_ok = detected_w > 0 and detected_h > 0

    if setting not in RES_CHOICES:
        setting = RES_AUTO

    if setting == RES_4K:
        if native_ok and detected_w >= W_4K and detected_h >= H_4K:
            if 0 < total_mem_mb < RES_4K_RECOMMENDED_MEM_MB:
                notes.append(
                    "4K explicitly selected on a %d MB board "
                    "(recommended >= %d MB) — tile cache will be "
                    "memory-constrained" % (total_mem_mb,
                                            RES_4K_RECOMMENDED_MEM_MB))
            return W_4K, H_4K, "forced-4k", notes
        # Display cannot do 4K — fall through to auto behavior.
        notes.append(
            "4K requested but the display does not report a >=%dx%d "
            "mode (detected %dx%d); falling back to auto" %
            (W_4K, H_4K, detected_w, detected_h))
        setting = RES_AUTO

    if setting == RES_1080P:
        if native_ok and (detected_w > W_1080P or detected_h > H_1080P):
            notes.append(
                "1080p forced on a %dx%d display — KMSDRM negotiates "
                "1920x1080, the display's scaler upscales" %
                (detected_w, detected_h))
        return W_1080P, H_1080P, "forced-1080p", notes

    # auto
    if not native_ok:
        return W_1080P, H_1080P, "fallback-1080p", \
            ["display resolution not detected; using 1920x1080"]
    if 0 < total_mem_mb <= RES_1080P_CAP_MEM_MB and (
            detected_w > W_1080P or detected_h > H_1080P):
        return W_1080P, H_1080P, "auto-capped-1080p", [
            "native %dx%d capped to 1920x1080 (%d MB RAM <= %d MB "
            "threshold); select 4k explicitly to override" %
            (detected_w, detected_h, total_mem_mb, RES_1080P_CAP_MEM_MB)]
    return detected_w, detected_h, "native", notes


def resolution_capabilities(detected_w, detected_h, total_mem_mb):
    """Describe each resolution option for the settings UI.

    Returns a list of dicts: value/label/available/reason/recommended.
    """
    detected_w = detected_w or 0
    detected_h = detected_h or 0
    native_ok = detected_w > 0 and detected_h > 0
    caps = []

    caps.append({
        "value": RES_AUTO,
        "label": "Auto (display native)",
        "available": True,
        "reason": ("native %dx%d" % (detected_w, detected_h)
                   if native_ok else "display not detected"),
        "recommended": True,
        "detail": ("capped to 1080p: %d MB RAM <= %d MB" %
                   (total_mem_mb, RES_1080P_CAP_MEM_MB)
                   if (native_ok and
                       (detected_w > W_1080P or detected_h > H_1080P) and
                       0 < total_mem_mb <= RES_1080P_CAP_MEM_MB)
                   else "uses the display's native mode"),
    })

    caps.append({
        "value": RES_1080P,
        "label": "1080p (1920x1080)",
        "available": True,
        "reason": ("below native %dx%d — TV scaler upscales" %
                   (detected_w, detected_h)
                   if native_ok and
                   (detected_w > W_1080P or detected_h > H_1080P)
                   else "matches native"),
        "recommended": (native_ok and
                        (detected_w > W_1080P or detected_h > H_1080P) and
                        0 < total_mem_mb <= RES_1080P_CAP_MEM_MB),
        "detail": "lowest RAM and bandwidth; always safe",
    })

    can_4k = native_ok and detected_w >= W_4K and detected_h >= H_4K
    rec_4k = can_4k and (total_mem_mb <= 0 or
                         total_mem_mb >= RES_4K_RECOMMENDED_MEM_MB)
    caps.append({
        "value": RES_4K,
        "label": "4K (3840x2160)",
        "available": can_4k,
        "reason": ("native %dx%d" % (detected_w, detected_h) if can_4k
                   else "display reports max %dx%d" %
                   (detected_w, detected_h)),
        "recommended": rec_4k,
        "detail": ("needs >= %d MB RAM for a comfortable tile cache "
                   "(this board: %d MB)" %
                   (RES_4K_RECOMMENDED_MEM_MB, total_mem_mb)
                   if can_4k and not rec_4k else
                   ("full-resolution output" if can_4k
                    else "not supported by this display")),
    })
    return caps


# ── Settings file ────────────────────────────────────────────────────────────

VALID_KEYS = {"resolution"}


def default_settings():
    return {"resolution": RES_AUTO}


def load_settings(path):
    """Load settings JSON; missing/corrupt file → defaults (never
    raises — a bad settings file must not stop a kiosk from booting)."""
    data = default_settings()
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            for k in VALID_KEYS:
                if k in loaded:
                    data[k] = loaded[k]
    except (OSError, ValueError):
        pass
    # Sanitize
    if data["resolution"] not in RES_CHOICES:
        data["resolution"] = RES_AUTO
    return data


def save_settings(path, data):
    """Atomically write settings JSON (tmp file + rename)."""
    clean = {k: data.get(k, default_settings()[k]) for k in VALID_KEYS}
    if clean["resolution"] not in RES_CHOICES:
        clean["resolution"] = RES_AUTO
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(clean, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return clean
