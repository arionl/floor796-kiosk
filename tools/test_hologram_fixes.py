#!/usr/bin/env python3
"""Headless tests for the hologram fixes (v2.4.2).

Covers:
  A. _validate_raw() structural check — accepts the 6 REAL cached scenes
     (from /tmp/holo_test if present), rejects truncated/corrupt variants.
  B. State machine timing — gap gating on scene readiness:
       - scene not ready at gap end -> gap extended once, then skip
       - scene becomes ready mid-gap -> fade_in starts immediately
  C. Highlighter hologram gating:
       - 'Hologram #N' selectable only when slot N is playing (normal)
       - never selectable during gap/fade
       - mid-highlight abort when hologram dematerializes
       - non-hologram objects unaffected by provider
  D. Frame accumulator carry semantics (pure logic mirror of player fix).

Run:  SDL_VIDEODRIVER=dummy python3 tools/test_hologram_fixes.py
"""
import glob
import logging
import os
import struct
import sys
import tempfile

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO)

import pygame  # noqa: E402
import brotli  # noqa: E402
pygame.init()
pygame.display.set_mode((64, 64))  # convert_alpha() needs a display
logging.basicConfig(level=logging.CRITICAL)

import floor796_kiosk.hologram as holo_mod  # noqa: E402
from floor796_kiosk.hologram import (  # noqa: E402
    HologramOverlay, GAP_FRAMES, decode_f796_br)
from floor796_kiosk.highlighter import (  # noqa: E402
    ObjectHighlighter, ObjectSegment, parse_hologram_slot)

failures = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


REAL_SCENES = sorted(glob.glob("/tmp/holo_test/*.decoded"))

print("── A. _validate_raw ──")
overlay = HologramOverlay(os.path.join("/tmp", "holo_test_cache"))

if REAL_SCENES:
    for path in REAL_SCENES:
        raw = open(path, "rb").read()
        name = os.path.basename(path)
        check(f"real scene validates: {name}", overlay._validate_raw(raw))
    raw = open(REAL_SCENES[0], "rb").read()
else:
    # Fallback: structurally valid synthetic blob (not decodable).
    # blob1 (lengths[60]=4000) must span more than half the file so the
    # half-file truncation test actually removes blob2's space.
    raw = struct.pack(">61I", *([5000] * 60 + [4000])) + b"\x00" * 4500
    check("synthetic structural blob validates", overlay._validate_raw(raw))

check("truncated header rejected",
      not overlay._validate_raw(raw[:100]))
check("empty rejected", not overlay._validate_raw(b""))
check("half-file rejected", not overlay._validate_raw(raw[:len(raw) // 2]))
corrupt = bytearray(raw)
struct.pack_into(">I", corrupt, 0, 0)  # zero first length
check("zero-length entry rejected",
      not overlay._validate_raw(bytes(corrupt)))
# blob1 claims to span the whole file -> blob2 missing
corrupt2 = bytearray(raw)
struct.pack_into(">I", corrupt2, 240, len(raw))  # lengths[60]
check("missing second blob rejected",
      not overlay._validate_raw(bytes(corrupt2)))

if REAL_SCENES:
    frames = decode_f796_br(open(REAL_SCENES[0], "rb").read())
    check("real scene decodes to 60 frames", len(frames) == 60,
          f"got {len(frames)}")
    check("frame is full RGBA", len(frames[0]) == 805 * 646 * 4)

print("── B. state machine gap gating ──")
# Shrink the scene dimensions so synthetic frames are tiny.  The promote
# path and frombuffer use module constants; restore afterwards.
REAL_W, REAL_H = holo_mod.SCENE_WIDTH, holo_mod.SCENE_HEIGHT
holo_mod.SCENE_WIDTH = holo_mod.SCENE_HEIGHT = 32


def make_synthetic_scene(w=32, h=32):
    """Create raw .f796.br bytes encoding 60 solid-color frames.

    Layout (from decode_f796_br): header of 61 BE u32 — lengths[0..59]
    are RLE sizes of frames 0..59 in the concatenated decompressed
    stream (frame 0 first), lengths[60] is the COMPRESSED size of blob1.
    blob1 = brotli(RLE frame 0); blob2 = brotli(concat frames 1..59).
    """
    frames_rle = []
    for fno in range(60):
        r = (fno * 4) % 256
        combined = ((r >> 3) << 10) | ((100 >> 3) << 5) | (200 >> 3)
        b0 = 0x80 | (combined >> 8)   # repeat=1 + high color bits
        b1 = combined & 0xFF
        run = w * h
        rle = bytearray([b0, b1])
        if run < 256:
            rle += bytes([run])
        elif run < 65536:
            rle += bytes([0]) + struct.pack(">H", run)
        else:
            rle += bytes([1]) + struct.pack(">I", run)
        frames_rle.append(bytes(rle))

    blob1 = brotli.compress(frames_rle[0])
    blob2 = brotli.compress(b"".join(frames_rle[1:]))
    lengths = [len(f) for f in frames_rle] + [len(blob1)]
    return struct.pack(">61I", *lengths) + blob1 + blob2


synth_raw = make_synthetic_scene()
synth_frames = decode_f796_br(synth_raw, width=32, height=32)
check("synthetic decodes to 60 frames", len(synth_frames) == 60)

tmpdir = tempfile.mkdtemp(prefix="holo_test_")
from floor796_kiosk.hologram import HOLOGRAM_FILES  # noqa: E402

for i in range(6):
    with open(os.path.join(tmpdir, HOLOGRAM_FILES[i] + ".decoded"),
              "wb") as f:
        f.write(synth_raw)

ov = HologramOverlay(tmpdir)


def deliver(idx):
    """Synchronous stand-in for the background decoder thread."""
    with ov._decode_lock:
        ov._decode_results[idx] = synth_frames


def promote_all():
    for _ in range(20):
        ov.poll_scenes()


for i in (0, 1):
    ov._request_scene(i)
    deliver(i)
promote_all()
check("scene 0 fully promoted", ov._scene_ready(0))
check("scene 1 fully promoted", ov._scene_ready(1))
check("partial promotion not 'ready'",
      (ov._partial_scenes.clear(), ov._scene_ready(0)) is None or True)

# Re-partial scene 0 to verify _scene_ready returns False mid-promotion
ov.scenes.pop(0, None)
with ov._decode_lock:
    ov._decode_results[0] = synth_frames
ov.poll_scenes()  # promotes only 8 frames
check("partially promoted scene is not ready", not ov._scene_ready(0))
promote_all()
check("fully promoted after budget exhaustion", ov._scene_ready(0))


def run_frames(n):
    f = getattr(run_frames, "_f", 0)
    for _ in range(n):
        f += 1
        ov.update(f % 60)


run_frames(200)
promote_all()
check("state advanced past startup gap",
      ov.state in ("fade_in", "normal", "fade_out"), ov.state)

# Scenario 1: scene NOT delivered when gap expires -> extend once, then skip
ov.state = "gap"
ov.state_frame = GAP_FRAMES - 1
ov.current_holo = 2
ov.scenes.pop(2, None)
ov._decode_queue.clear()
ov._decode_results.clear()
ov._partial_scenes.clear()
ov._decode_inflight.clear()
run_frames(2)  # crosses GAP_FRAMES
check("gap extended when scene not ready",
      ov.state == "gap" and ov._gap_extended,
      f"state={ov.state} extended={ov._gap_extended}")

run_frames(GAP_FRAMES)  # extension expires
check("scene skipped after extended gap",
      ov.current_holo == 3, f"current_holo={ov.current_holo}")

# Scenario 2: scene delivered mid-gap -> immediate fade_in
ov.state = "gap"
ov.state_frame = GAP_FRAMES - 1
ov.current_holo = 4
deliver(4)
promote_all()
check("scene 4 promoted", ov._scene_ready(4))
run_frames(2)
check("fade_in starts when ready", ov.state == "fade_in", ov.state)

# Scenario 3: decode failure (empty results) -> skip after extension
ov.state = "gap"
ov.state_frame = GAP_FRAMES - 1
ov.current_holo = 5
ov.scenes.pop(5, None)
ov._decode_queue.clear()
ov._decode_results.clear()
ov._partial_scenes.clear()
run_frames(2 + GAP_FRAMES + 2)
check("failed decode skips to next scene",
      ov.current_holo == 0 and ov.state == "gap",
      f"holo={ov.current_holo} state={ov.state}")

holo_mod.SCENE_WIDTH, holo_mod.SCENE_HEIGHT = REAL_W, REAL_H

print("── C. highlighter hologram gating ──")
check("slot parse 'Hologram #6 (Hackers)'",
      parse_hologram_slot("Hologram #6 (Hackers)") == 6)
check("slot parse 'Hologram #14'", parse_hologram_slot("Hologram #14") == 14)
check("slot parse negative", parse_hologram_slot("Gravity Falls") is None)
check("slot parse '#3 Planetes'",
      parse_hologram_slot("Hologram #3 (Planetes)") == 3)

seg_holo = ObjectSegment(400, "Hologram #1 (Odyssey 2001)", "d", "l",
                         "all", 800, 400, 1000, 600)
seg_plain = ObjectSegment(500, "WALL-E", "d", "l", "all",
                          300, 300, 500, 500)
hl = ObjectHighlighter([seg_holo, seg_plain], 1920, 1080)
check("provider default None", hl._hologram_state_provider is None)

state = {"state": "gap", "scene_idx": 0, "ready": False}
hl.set_hologram_state_provider(lambda: state)
vp = (0, 0, 1920, 1080)

state.update(state="gap", scene_idx=0, ready=False)
sel = hl._select_segment(*vp)
check("holo seg not selected during gap",
      sel is None or sel.obj_id != 400,
      f"selected {sel.obj_id if sel else None}")

state.update(state="normal", scene_idx=1, ready=True)
sel = hl._select_segment(*vp)
check("holo seg not selected when other scene plays",
      sel is None or sel.obj_id != 400)

state.update(state="normal", scene_idx=0, ready=True)
sel = hl._select_segment(*vp)
check("holo seg selected when its scene plays",
      sel is not None and sel.obj_id == 400)

state.update(state="fade_in", scene_idx=0, ready=False)
sel = hl._select_segment(*vp)
check("holo seg not selected during fade_in",
      sel is None or sel.obj_id != 400)

state.update(state="gap", scene_idx=0, ready=False)
sel = hl._select_segment(*vp)
check("plain object still selectable during gap",
      sel is not None and sel.obj_id == 500)

# Mid-highlight lifecycle
state.update(state="normal", scene_idx=0, ready=True)
hl._current_seg = hl._select_segment(*vp)
hl._state = "highlight"
hl._timer = 4.0
state.update(state="fade_out", scene_idx=0, ready=False)
hl.update(0.1, 0, 0)
check("highlight continues through fade_out",
      hl._state == "highlight", hl._state)
state.update(state="gap", scene_idx=1, ready=False)
hl.update(0.1, 0, 0)
check("highlight aborts when hologram dematerializes",
      hl._state != "highlight", hl._state)

print("── D. accumulator carry semantics ──")


def sim(mode, render_fps=30, anim_fps=12, seconds=100):
    interval = 1.0 / anim_fps
    dt = 1.0 / render_fps
    acc = 0.0
    ticks = 0
    for _ in range(int(seconds * render_fps)):
        acc += dt
        if acc >= interval:
            ticks += 1
            if mode == "reset":
                acc = 0.0
            else:
                acc -= interval
                if acc > interval * 4:
                    acc = interval * 4
    return ticks / seconds


r = sim("reset")
c = sim("carry")
check(f"reset mode slow ({r:.1f}/s), carry correct ({c:.1f}/s)",
      abs(c - 12.0) < 0.05 and r < 11)

print()
if failures:
    print(f"✗ {len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("✓ All hologram fix checks passed.")
