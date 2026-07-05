"""Centralized path management for the Floor796 kiosk.

All file paths used by the player are resolved here so modules never
hard-code directory locations.  This makes it trivial to relocate the
data directories (e.g. to a RAM disk or SD card partition).

Directory layout::

    INSTALL_DIR/                 (e.g. /opt/floor796-kiosk)
    ├── floor796_kiosk/          Python package
    ├── assets/                  Downloaded from floor796.com (cached)
    │   ├── tiles/               Source tile MP4s
    │   ├── tiles_meta.json      Tile grid metadata
    │   ├── changelog.json       Object labels / polygon data
    │   └── holograms/           Hologram scene sources
    └── cache/                   Generated at runtime (rebuildable)
        ├── strips/              Decoded tile animation strips (BMP)
        ├── content_mask.npz     Content density mask
        └── thumbnails/          Resized label thumbnails
"""

import os

# ── Install root ─────────────────────────────────────────────────────────────
# The package lives at  <install_dir>/floor796_kiosk/
# so the install root is two levels up from this file.
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
INSTALL_DIR = os.path.dirname(PKG_DIR)

# ── Assets (downloaded from floor796.com, cached locally) ────────────────────
ASSETS_DIR = os.path.join(INSTALL_DIR, "assets")
TILE_DIR = os.path.join(ASSETS_DIR, "tiles")
TILE_META_PATH = os.path.join(ASSETS_DIR, "tiles_meta.json")
CHANGELOG_PATH = os.path.join(ASSETS_DIR, "changelog.json")
HOLOGRAM_DIR = os.path.join(ASSETS_DIR, "holograms")

# ── Cache (generated at runtime, safe to delete) ─────────────────────────────
CACHE_DIR = os.path.join(INSTALL_DIR, "cache")
STRIP_DIR = os.path.join(CACHE_DIR, "strips")
CONTENT_MASK_PATH = os.path.join(CACHE_DIR, "content_mask.npz")
THUMBNAIL_DIR = os.path.join(CACHE_DIR, "thumbnails")


def ensure_dirs():
    """Create all required directories if they don't exist."""
    for d in (ASSETS_DIR, TILE_DIR, HOLOGRAM_DIR,
              CACHE_DIR, STRIP_DIR, THUMBNAIL_DIR):
        os.makedirs(d, exist_ok=True)
