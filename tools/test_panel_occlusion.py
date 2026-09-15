#!/usr/bin/env python3
"""Headless tests for highlighter panel-occlusion fixes.

Covers:
  A. Exclusion rect sizing - PANEL_EXCLUDE_* must cover the true
     worst-case panel footprint (2-line title + date + thumbnail +
     3-line wiki extract + footer + margins).
  B. Selection-time occlusion - objects overlapping the panel rect
     are never selected, including the previously under-excluded
     ~42px band (screen y in [screen_h-402, screen_h-360]).
  C. Predicted (velocity-aware) occlusion - objects that would drift
     into the panel by the END of the highlight are skipped at
     selection time.
  D. Mid-highlight panel abort - an object that drifts behind the
     panel mid-highlight is aborted (highlight ends, state moves on)
     instead of lingering hidden for the rest of the 10s.
  E. Regression guards - objects in clear space are still selected,
     and the normal scroll-off abort still works.

Run:  SDL_VIDEODRIVER=dummy python3 tools/test_panel_occlusion.py
"""
import os
import sys

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO)

import pygame  # noqa: E402
pygame.init()
pygame.display.set_mode((64, 64))

from floor796_kiosk.highlighter import (  # noqa: E402
    ObjectHighlighter, ObjectSegment,
    PANEL_EXCLUDE_W, PANEL_EXCLUDE_H,
    PANEL_W, PANEL_W_NO_THUMB, PANEL_MARGIN,
    THUMB_H, PANEL_H_FOOTER, PANEL_PADDING,
    STATE_HIGHLIGHT, STATE_IDLE, STATE_PAUSE)

failures = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def seg_at(obj_id, abs_x1, abs_y1, w, h, title="Object"):
    return ObjectSegment(
        obj_id=obj_id, title=title, date="2026-01-01", link="",
        tile_ref="0,0",
        abs_x1=abs_x1, abs_y1=abs_y1,
        abs_x2=abs_x1 + w, abs_y2=abs_y1 + h)


W, H = 1920, 1080

print("-- A. exclusion rect sizing --")
worst_panel_h = (2 * 17 + 12 + 16 + THUMB_H + 10
                 + 3 * 12 + 10 + PANEL_H_FOOTER + PANEL_PADDING)
worst_box_w = max(PANEL_W, PANEL_W_NO_THUMB) + 2 * PANEL_MARGIN
worst_box_h = worst_panel_h + 2 * PANEL_MARGIN
check("PANEL_EXCLUDE_H >= worst-case box height",
      PANEL_EXCLUDE_H >= worst_box_h,
      f"{PANEL_EXCLUDE_H} vs {worst_box_h}")
check("PANEL_EXCLUDE_W >= worst-case box width",
      PANEL_EXCLUDE_W >= worst_box_w,
      f"{PANEL_EXCLUDE_W} vs {worst_box_w}")

print("-- B. selection-time occlusion --")
hl = ObjectHighlighter([], W, H)
panel_x1, panel_y1, panel_x2, panel_y2 = hl._panel_rect()
print(f"  panel rect: x[{panel_x1:.0f},{panel_x2:.0f}] y[{panel_y1:.0f},{panel_y2:.0f}]")

safe = seg_at(1, 500, 400, 60, 40, "Safe Object")
occluded = seg_at(2, panel_x1 + 20, panel_y1 + 90, 60, 40, "Behind Panel")
hl2 = ObjectHighlighter([safe, occluded], W, H)
sel = hl2._select_segment(0, 0, W, H, 0, 0)
check("occluded object never selected", sel is None or sel.obj_id == 1,
      f"selected {sel.obj_id if sel else None}")

print("-- C. predicted (velocity-aware) occlusion --")
# Screen position of an object moves OPPOSITE to viewport velocity
# (screen_x = abs_x - vp_x), so a viewport drifting up-left
# (vel=-15,-15) slides objects down-right -- INTO the corner panel.
hl3 = ObjectHighlighter([], W, H)
px1, py1, px2, py2 = hl3._panel_rect()
# Clear of the panel now; predicted +150px down-right lands inside it.
drifting = seg_at(3, px1 - 150, py1 - 50, 60, 40, "Drifting Into Panel")
clear = seg_at(4, 400, 300, 60, 40, "Clear Object")
hl3 = ObjectHighlighter([drifting, clear], W, H)
sel = hl3._select_segment(0, 0, W, H, vel_x=-15, vel_y=-15)
check("object predicted to drift into panel skipped",
      sel is None or sel.obj_id == 4,
      f"selected {sel.obj_id if sel else None}")

# Same spot, viewport drifting down-right (vel=+15,+15): object slides
# up-left, away from the panel -- must remain selectable.
away = seg_at(5, px1 - 150, py1 - 50, 60, 40, "Drifting Away")
hl4 = ObjectHighlighter([away], W, H)
sel = hl4._select_segment(0, 0, W, H, vel_x=15, vel_y=15)
check("object drifting away from panel still selectable",
      sel is not None and sel.obj_id == 5,
      f"selected {sel.obj_id if sel else None}")

print("-- D. mid-highlight panel abort --")
target = seg_at(6, 600, 400, 60, 40, "Will Be Panelled")
hl5 = ObjectHighlighter([target], W, H)
hl5.update(0.1, 0, 0, 0, 0)
check("highlight started", hl5._state == STATE_HIGHLIGHT and
      hl5._current_seg is not None and hl5._current_seg.obj_id == 6)
vp_x = target.abs_x1 - (panel_x1 + 10)
vp_y = target.abs_y1 - (panel_y1 + 10)
hl5.update(0.1, vp_x, vp_y, 0, 0)
check("panel occlusion aborts highlight",
      hl5._current_seg is None and hl5._state in (STATE_IDLE, STATE_PAUSE),
      f"state={hl5._state} seg={hl5._current_seg}")

print("-- E. regression guards --")
t7 = seg_at(7, 500, 400, 60, 40, "Lifecycle")
hl6 = ObjectHighlighter([t7], W, H)
hl6.update(0.1, 0, 0, 0, 0)
check("normal highlight still starts", hl6._state == STATE_HIGHLIGHT)
hl6.update(10.1, 0, 0, 0, 0)
check("normal highlight completes after duration",
      hl6._state != STATE_HIGHLIGHT,
      f"state={hl6._state}")
t8 = seg_at(8, 500, 400, 60, 40, "Scroll Off")
hl7 = ObjectHighlighter([t8], W, H)
hl7.update(0.1, 0, 0, 0, 0)
check("scroll-off case: highlight starts", hl7._state == STATE_HIGHLIGHT)
hl7.update(0.1, 5000, 5000, 0, 0)
check("scroll-off abort still works",
      hl7._current_seg is None and hl7._state != STATE_HIGHLIGHT,
      f"state={hl7._state}")

print()
if failures:
    print(f"FAILED: {len(failures)} test(s): {failures}")
    sys.exit(1)
print("All panel-occlusion tests passed.")
