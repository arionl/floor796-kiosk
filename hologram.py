#!/usr/bin/env python3
"""
Hologram overlay system for the Floor796 kiosk.

The floor796.com map has a special "hologram room" in the center of the
animated diamond (tiles t3r1, t4r2, t5r1).  On the website, 6 different
hologram images (movie references) are perspective-warped into a display
area.  This module downloads the images, pre-warps them to their map-space
quad positions, and cycles through them.
"""

import json
import logging
import os
import urllib.request

from PIL import Image, ImageDraw

log = logging.getLogger("floor796")

CDN_BASE = "https://floor796.com/data/misc/"
SPACING_W = 1016
SPACING_H = 812
HOLOGRAM_INTERVAL = 20.0       # seconds between hologram switches

_TILE_POS = {"t3r1": (5, 6), "t4r2": (5, 7), "t5r1": (4, 7)}

# 6 holograms with quad corners (tile_id, x, y) from changelog IDs 415-420.
_HOLOGRAMS = [
    {"name": "2001: A Space Odyssey", "image": "odyssey2001.jpg",
     "corners": [("t3r1", 88, 880), ("t5r1", 709, 105),
                 ("t4r2", 265, 98), ("t4r2", 81, 333)]},
    {"name": "Cube (1997)", "image": "cube.jpg",
     "corners": [("t3r1", 92, 891), ("t5r1", 632, 107),
                 ("t4r2", 274, 103), ("t4r2", 90, 347)]},
    {"name": "Planetes (2003)", "image": "planetes.jpg",
     "corners": [("t3r1", 98, 877), ("t5r1", 624, 106),
                 ("t4r2", 304, 105), ("t4r2", 110, 349)]},
    {"name": "The Matrix (1999)", "image": "matrix.jpg",
     "corners": [("t3r1", 96, 873), ("t5r1", 595, 106),
                 ("t4r2", 302, 96), ("t4r2", 88, 301)]},
    {"name": "Saw (2004)", "image": "saw.jpg",
     "corners": [("t3r1", 97, 922), ("t5r1", 606, 107),
                 ("t4r2", 274, 92), ("t4r2", 64, 380)]},
    {"name": "Hackers (1995)", "image": "hackers.jpg",
     "corners": [("t3r1", 102, 891), ("t5r1", 653, 100),
                 ("t4r2", 269, 82), ("t4r2", 89, 303)]},
]


class HologramOverlay:
    """Downloads, warps, and cycles the 6 hologram images."""

    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.holograms = []
        self.current_idx = 0
        self.last_switch = 0.0
        self.enabled = False
        os.makedirs(cache_dir, exist_ok=True)

    def prepare(self, status_callback=None):
        """Download and pre-warp all hologram images."""
        total = len(_HOLOGRAMS)
        ready = 0

        for holo in _HOLOGRAMS:
            name = holo["name"]
            img_file = holo["image"]
            warped_path = os.path.join(self.cache_dir,
                                       img_file.replace(".jpg", "_holo.png"))
            meta_path = warped_path.replace(".png", "_meta.json")

            # Use cached warp if available
            if os.path.exists(warped_path) and os.path.exists(meta_path):
                with open(meta_path) as f:
                    meta = json.load(f)
                self.holograms.append({
                    "path": warped_path,
                    "map_x": meta["map_x"],
                    "map_y": meta["map_y"],
                    "w": meta["w"],
                    "h": meta["h"],
                    "name": name,
                })
                ready += 1
                if status_callback:
                    status_callback(ready, total, name, True)
                continue

            # Download source
            src_path = os.path.join(self.cache_dir, img_file)
            if not os.path.exists(src_path):
                try:
                    urllib.request.urlretrieve(CDN_BASE + img_file, src_path)
                    log.info("Downloaded hologram: %s", img_file)
                except Exception as exc:
                    log.warning("Failed to download %s: %s", img_file, exc)
                    continue

            # Compute map-space quad and bounding box
            quad = self._corners_to_map_quad(holo["corners"])
            min_x = min(p[0] for p in quad)
            min_y = min(p[1] for p in quad)
            w = int(max(p[0] for p in quad) - min_x)
            h = int(max(p[1] for p in quad) - min_y)
            local_quad = [(p[0] - min_x, p[1] - min_y) for p in quad]

            # Warp
            try:
                src_img = Image.open(src_path)
                warped = self._warp_perspective(src_img, local_quad, w, h)
                warped.save(warped_path, "PNG")
            except Exception as exc:
                log.warning("Failed to warp %s: %s", img_file, exc)
                continue

            with open(meta_path, "w") as f:
                json.dump({"map_x": int(min_x), "map_y": int(min_y),
                           "w": w, "h": h}, f)

            self.holograms.append({
                "path": warped_path,
                "map_x": int(min_x),
                "map_y": int(min_y),
                "w": w, "h": h,
                "name": name,
            })
            ready += 1
            if status_callback:
                status_callback(ready, total, name, True)

        if self.holograms:
            self.enabled = True
            log.info("Hologram overlay ready: %d/%d images",
                     len(self.holograms), total)
        else:
            log.warning("No hologram images could be loaded.")

    def _corners_to_map_quad(self, corners):
        """Convert tile-relative corners to convex map-space quad.

        Returns 4 points in counterclockwise order (convex polygon),
        suitable for PIL's perspective transform.
        """
        import math
        points = []
        for tile_id, x, y in corners:
            row, col = _TILE_POS[tile_id]
            points.append((col * SPACING_W + x, row * SPACING_H + y))

        # Sort by angle from centroid (counterclockwise = convex ordering)
        cx = sum(p[0] for p in points) / len(points)
        cy = sum(p[1] for p in points) / len(points)
        ccw = sorted(points, key=lambda p: math.atan2(p[1] - cy, p[0] - cx))

        # Assign source image corners: the topmost quad point (smallest y)
        # maps to source TL, then go clockwise assigning TR, BR, BL.
        # Find the index of the topmost point in the CCW list.
        top_idx = min(range(4), key=lambda i: ccw[i][1])
        # Reorder so topmost is first, then follow CCW order
        ordered = [ccw[(top_idx + i) % 4] for i in range(4)]
        return ordered

    def _warp_perspective(self, src_img, quad, out_w, out_h):
        """Perspective-warp src_img into the target quad.

        Solves the 8-coefficient inverse mapping and uses PIL's
        PERSPECTIVE transform.
        """
        src_w, src_h = src_img.size
        src_corners = [(0, 0), (src_w, 0), (src_w, src_h), (0, src_h)]

        # Build 8x8 linear system
        A, b = [], []
        for i in range(4):
            ox, oy = quad[i]
            ix, iy = src_corners[i]
            A.append([ox, oy, 1, 0, 0, 0, -ix * ox, -ix * oy])
            b.append(ix)
            A.append([0, 0, 0, ox, oy, 1, -iy * ox, -iy * oy])
            b.append(iy)

        coeffs = _solve_8x8(A, b)

        result = src_img.transform(
            (out_w, out_h), Image.PERSPECTIVE, coeffs, Image.BILINEAR)

        mask = Image.new("L", (out_w, out_h), 0)
        ImageDraw.Draw(mask).polygon(quad, fill=255)
        result.putalpha(mask)
        return result.convert("RGBA")

    def update(self, now):
        if not self.enabled or not self.holograms:
            return
        if now - self.last_switch > HOLOGRAM_INTERVAL:
            self.current_idx = (self.current_idx + 1) % len(self.holograms)
            self.last_switch = now

    @property
    def current(self):
        if not self.enabled or not self.holograms:
            return None
        return self.holograms[self.current_idx]


def _solve_8x8(A, b):
    """Gaussian elimination for 8x8 system (pure Python, no numpy)."""
    n = 8
    aug = [A[i][:] + [b[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            raise ValueError("Singular matrix")
        aug[col], aug[pivot] = aug[pivot], aug[col]
        pv = aug[col][col]
        for j in range(col, n + 1):
            aug[col][j] /= pv
        for row in range(n):
            if row != col:
                factor = aug[row][col]
                for j in range(col, n + 1):
                    aug[row][j] -= factor * aug[col][j]
    return [aug[i][n] for i in range(n)]
