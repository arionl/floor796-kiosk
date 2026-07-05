#!/usr/bin/env python3
"""
Generate a downscaled content-density map for the floor796 kiosk.

For each animated tile, create a low-res mask showing which parts of the
tile have actual pixel-art content vs flat background. This accounts for
the isometric diamond shape — ~66% of each tile is background.

The mask is computed from frame strips using local standard deviation:
flat regions (low stddev) are background; textured regions are content.

Output: content_mask.npz — a dict of {tile_id: 2D float array (0..1)}
"""
import json
import os
import sys
import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = 300000000

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STRIP_DIR = os.path.join(BASE_DIR, "strips")
TILE_W = 1024
TILE_H = 820
MASK_COLS = 32   # content mask resolution
MASK_ROWS = 26   # 820/32 ≈ 31px per cell, 1024/32 = 32px per cell


def build_and_save(tiles_meta, output_path, strip_dir=None, progress_callback=None):
    """Build the content density mask and save to output_path.

    Callable from kiosk_player.py at startup when content_mask.npz
    doesn't exist yet.  Uses strip_dir if provided (defaults to the
    module's own directory).

    If progress_callback is provided, it is called as
    ``progress_callback(done, total, message)`` after each tile is
    processed, so the caller can render a progress bar.
    """
    global STRIP_DIR
    if strip_dir:
        STRIP_DIR = strip_dir
    map_mask, _ = build_map_content_mask(tiles_meta, progress_callback=progress_callback)
    np.savez_compressed(output_path, map_mask=map_mask)
    if progress_callback:
        progress_callback(len(tiles_meta["tiles"]), len(tiles_meta["tiles"]),
                          "Saving content mask...")


def compute_content_mask(strip_path):
    """Compute a MASK_ROWS x MASK_COLS content density (0..1) from a strip."""
    img = Image.open(strip_path)
    img.load()
    arr = np.array(img).astype(np.float32)
    # Use first frame
    frame = arr[:TILE_H, :TILE_W]
    
    bh = TILE_H // MASK_ROWS
    bw = TILE_W // MASK_COLS
    
    mask = np.zeros((MASK_ROWS, MASK_COLS), dtype=np.float32)
    for i in range(MASK_ROWS):
        for j in range(MASK_COLS):
            block = frame[i*bh:(i+1)*bh, j*bw:(j+1)*bw]
            if block.size == 0:
                continue
            # Per-channel stddev, averaged
            if block.ndim == 3:
                std = np.mean([np.std(block[:, :, c]) for c in range(min(3, block.shape[2]))])
            else:
                std = np.std(block)
            # Normalize: stddev > 30 = definitely content, < 5 = flat background
            mask[i, j] = min(1.0, max(0.0, (std - 5.0) / 25.0))
    return mask


def build_map_content_mask(tiles_meta, progress_callback=None):
    """Build a full-map content mask at tile-block resolution.

    Simulates the rendered map: for each position on the map grid,
    determines the content density accounting for tile overlap.

    Returns a numpy array of shape (map_rows * MASK_ROWS, map_cols * MASK_COLS)
    where each cell is 0..1 content density.
    """
    grid_rows = tiles_meta.get("grid_rows", 11)
    grid_cols = tiles_meta.get("grid_cols", 10)

    # First compute per-tile masks
    tile_masks = {}
    tile_items = list(tiles_meta["tiles"].items())
    total = len(tile_items)
    for idx, (tid, info) in enumerate(tile_items):
        if not info.get("animated"):
            tile_masks[tid] = None
        else:
            strip_path = os.path.join(STRIP_DIR, f"{tid}.bmp")
            if not os.path.exists(strip_path):
                strip_path = os.path.join(STRIP_DIR, f"{tid}.png")
            if not os.path.exists(strip_path):
                tile_masks[tid] = None
            else:
                try:
                    tile_masks[tid] = compute_content_mask(strip_path)
                except Exception as e:
                    print(f"  Warning: {tid}: {e}")
                    tile_masks[tid] = None
        if progress_callback:
            progress_callback(idx + 1, total, "Building content mask")
    
    # Build full map mask
    # Each tile occupies MASK_ROWS x MASK_COLS cells
    # Tiles overlap by 8px (SPACING vs TILE), which is ~0.8% — negligible at mask res
    map_mask = np.zeros((grid_rows * MASK_ROWS, grid_cols * MASK_COLS), dtype=np.float32)
    
    for tid, info in tiles_meta["tiles"].items():
        r, c = info["row"], info["col"]
        mask = tile_masks.get(tid)
        if mask is not None:
            map_mask[r*MASK_ROWS:(r+1)*MASK_ROWS, 
                     c*MASK_COLS:(c+1)*MASK_COLS] = mask
    
    return map_mask, tile_masks


def content_ratio_at(map_mask, x, y, view_w, view_h, 
                     spacing_w, spacing_h, tile_w, tile_h):
    """Compute the fraction of the viewport that has actual content.
    
    Uses the precomputed map_mask to measure content density at any
    viewport position. Returns 0..1 where 1 = fully content, 0 = all blank.
    """
    mask_h, mask_w = map_mask.shape
    
    # Convert viewport pixel coords to mask coords
    cell_w = tile_w / MASK_COLS   # pixels per mask cell in x
    cell_h = tile_h / MASK_ROWS   # pixels per mask cell in y
    
    x0 = max(0, int(x / cell_w))
    y0 = max(0, int(y / cell_h))
    x1 = min(mask_w, int((x + view_w) / cell_w) + 1)
    y1 = min(mask_h, int((y + view_h) / cell_h) + 1)
    
    if x1 <= x0 or y1 <= y0:
        return 0.0
    
    region = map_mask[y0:y1, x0:x1]
    return float(np.mean(region))


if __name__ == "__main__":
    meta_path = os.path.join(BASE_DIR, "tiles_meta.json")
    with open(meta_path) as f:
        tiles_meta = json.load(f)
    
    print("Building content density map...")
    map_mask, tile_masks = build_map_content_mask(tiles_meta)
    
    # Save
    output_path = os.path.join(BASE_DIR, "content_mask.npz")
    np.savez_compressed(output_path, map_mask=map_mask)
    print(f"Saved to {output_path}")
    print(f"Map mask shape: {map_mask.shape}")
    print(f"Overall content density: {np.mean(map_mask):.1%}")
