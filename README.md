# Floor796 Kiosk

A self-contained animated pixel-art kiosk that displays the
[floor796.com](https://floor796.com) interactive isometric map on a dedicated
display — designed for Raspberry Pi 5 and OrangePi 5 Max.

The player boots from cold-start into a splash screen (floor796's own
station animation), automatically pans across the full animated scene
ensuring every tile is visited, and keeps the display on 24/7 with no
screensaver or sleep.  As objects scroll into view, the highlighter
identifies them from floor796.com's changelog, drawing a bounding box
with a zoom-in animation, breathing glow, and info panel with title,
date, and thumbnail (YouTube, image, video, Wikipedia, or interactive).
The site's teleporter room plays in the corner with site-exact
materialize/play/dematerialize transitions.  A keyboard (or the HTTP
control endpoints) switches between three camera modes: Wandering
(auto tour), Navigating (manual pan), and Exploring (object-to-object
jumps).  When the floor796 author publishes new tiles, they are
automatically downloaded and incorporated on the next boot.

![Floor796 Kiosk — object highlighter](screenshot.png)

---

## Features

- **Full-resolution pixel art** — 1024×820 tiles rendered at native resolution
  (no scaling artifacts).
- **KMSDRM unified rendering** — both Raspberry Pi 5 (V3D) and OrangePi 5 Max
  (Panthor) render directly through KMSDRM with no X11 overhead.  Boards ≤4 GB
  RAM are capped at 1080p to prevent OOM; higher memory boards render at native
  resolution (up to 4K).  The highlighter UI scales proportionally with display
  height (2.0× at 4K, 1.33× at 1440p, 1.0× at 1080p) for crisp text and panels
  at any resolution.
- **Coverage-weighted wandering** — a visit heat map ensures all 50+ animated
  tiles are toured evenly.  Content-aware scroll limits (derived from the
  pixel-level density mask) keep the viewport hugging actual pixel-art content,
  not blank tile-grid borders.  A* pathfinding through a safe-region grid
  ensures transit stays on content.
- **Three camera modes** — `Wandering` (the default self-driving tour),
  `Navigating` (free manual pan), and `Exploring` (object-to-object jumps:
  one arrow keypress flies the camera to the nearest object in that
  direction, with gray ghost boxes marking the other jump targets in view,
  clipped so they never cover the highlighted artwork).  SPACE cycles the
  modes; a transient capsule announces each change.
- **Teleporter room** — a site-exact port of floor796.com's teleport room:
  8 scenes cycle automatically with 22-beat materialize (sparkle columns),
  60-tick play, and 22-beat dematerialize transitions.  All transition beats
  are pre-rendered at boot and cached on disk, so the render loop only blits
  (~1.5 ms/frame).  Objects linked to a teleport pad are highlightable only
  while that pad is playing.
- **Boot splash** — floor796's about-section station animation loops in a
  framed square over the boot status/progress.  The clip is decoded once to
  a cached frame strip; boots after the first only blit.
- **Object highlighter** — automatically identifies and labels 855 objects from
  floor796.com's changelog as they scroll into view.  Each highlight features a
  zoom-in intro animation (viewport bounds contract to the object over 0.5s),
  followed by a breathing glow effect.  Deterministic LRU selection ensures
  every object is shown before any repeats, with scroll-off safety checks.
- **Overscan support** — `KIOSK_OVERSCAN_MARGIN` environment variable insets all
  content and UI for displays with overscan (configured in the systemd unit).
- **Telemetry & stats** — in-process HTTP API on `127.0.0.1:8796` provides live
  metrics: FPS, memory, CPU, tile cache, coverage heatmaps, per-object highlight
  stats, and more.  An on-screen overlay (toggled with `S`) shows real-time
  performance, coverage, and label statistics.
- **Settings & debug web UI** — the same HTTP server serves an operator
  page at `http://127.0.0.1:8796/` (see below): resolution selection
  (Auto / 1080p / 4K, informed by the display's reported mode and board
  RAM), cache clearing for cold/warm boot simulation, camera/overlay
  debug controls, a live screenshot viewer, and a one-click player
  restart.

  ![Stats overlay](stats_overlay_screenshot.png)

- **Auto-updating tiles** — checks floor796.com for new tiles at startup; falls
  back to cached tiles if offline.
- **Cold-boot kiosk** — boots directly into the player via systemd; no desktop
  environment needed.
- **No display sleep** — DPMS, screensaver, and power management are disabled
  at every layer.
- **No log files** — all output goes to the systemd journal (rotated
  automatically); no unbounded files are created.
- **Background tile loading** — tile decode and surface conversion happen in a
  background thread so the render loop stays smooth.

---

## Requirements

| Component          | Specification                                    |
|--------------------|--------------------------------------------------|
| Hardware           | Raspberry Pi 5 (2 GB min, 4 GB+ recommended) or  |
|                    | OrangePi 5 Max (8 GB)                            |
| OS                 | Raspberry Pi OS / Armbian (Bookworm or Trixie)   |
| Display            | HDMI (1920×1080 native, or higher with 4 GB+)    |
| Network            | Internet for initial download + updates          |
| Storage            | 4 GB free (decoded strips; MP4s are transient)   |

> **2 GB boards** (e.g. Raspberry Pi 5 2 GB) run the full feature set with a
> reduced tile cache (8 tiles, 16-bit strips) — ~1.45 GB RSS.  The tile-cache
> budget adapts automatically to board RAM and strip depth; no user-facing
> warnings.  4 GB+ is recommended for headroom.

---

## Quick Start (Fresh Pi 5)

```bash
# 1. Clone or copy this repo to the Pi
git clone <repo-url> /tmp/floor796-kiosk
cd /tmp/floor796-kiosk

# 2. Run the installer (requires root)
sudo bash deploy/install.sh

# 3. Reboot to test cold-boot auto-start
sudo reboot
```

The installer copies the code and configures the system service — it does
**not** download any content.  Tiles, labels, and all other assets are
fetched automatically on the first boot of the player:

- Tile MP4s from floor796.com, fetched ONE AT A TIME and decoded to
  frame strips (each MP4 is deleted the moment its strip is built, so
  only one MP4 is ever on disk at once)
- Object labels (changelog.json)
- Boot splash animation strip (decoded once from the bundled mp4)
- Teleporter room scenes + pre-rendered transitions (~54 MB, rendered in the
  background while the room cycles)
- Decoded frame strips (~8 minutes; the durable per-tile artifacts)
- Content density mask (~2 minutes)

All of this is cached — subsequent boots take ~20 seconds, fetch nothing,
and hold zero tile MP4s.

---

## Project Structure

```
floor796-kiosk/
├── floor796_kiosk/               Python package
│   ├── __init__.py
│   ├── __main__.py               Entry point: python -m floor796_kiosk
│   ├── paths.py                  Centralized path management
│   ├── board_detect.py           Board detection (Pi 5 / OrangePi / generic)
│   ├── player.py                 Main player (rendering, modes, ghost boxes)
│   ├── tile_manager.py           Update planning + one-at-a-time fetching
│   ├── content_mask.py           Content density mask generator
│   ├── hologram.py               Hologram scene overlay
│   ├── teleport.py               Teleporter room overlay (pre-rendered transitions)
│   ├── modes.py                  Camera mode state machine (W/N/E)
│   ├── explorer.py               Exploring-mode object-to-object navigation
│   ├── splash.py                 Boot splash (station animation strip)
│   ├── settings.py               Operator settings (resolution logic, settings.json)
│   ├── highlighter.py            Object highlighter (855 objects, LRU selection)
│   ├── thumbnails.py             Thumbnail fetcher (YouTube, images, video, wiki, tenor, fandom, web)
│   ├── cpu_affinity.py           big.LITTLE CPU core pinning
│   └── stats/
│       ├── __init__.py
│       ├── collector.py          Telemetry collector (ring buffers, heatmaps)
│       ├── http_server.py        HTTP API + settings/debug server (127.0.0.1:8796)
│       ├── webui.py              Settings & debug web page + cache registry
│       └── overlay.py            On-screen alpha-blended stats overlay
├── assets/                       Downloaded from floor796.com (cached, gitignored)
│   ├── tiles/                    TRANSIENT SCRATCH: at most the 1 MP4
│   │                             currently being decoded (empty at rest)
│   ├── tiles_meta.json           Grid metadata
│   ├── tile_state.json           Per-tile download fingerprints (update detection)
│   ├── changelog.json            Object labels / polygon data
│   └── holograms/                Hologram scene sources
├── cache/                        Generated at runtime (rebuildable, gitignored)
│   ├── strips/                   Decoded frame strips (BMP)
│   ├── content_mask.npz          Pixel-level content density mask
│   └── thumbnails/               Resized label thumbnails
├── tools/                        Utilities, tests & simulations
│   ├── kiosk_status.py           [Operator] Query the stats API from CLI
│   ├── prefetch_thumbnails.py    [Operator] Pre-fetch all thumbnails offline (all link types)
│   ├── test_tile_update.py       [Test] Tile update engine regression suite (local fake server)
│   ├── test_hologram_fixes.py    [Test] Hologram/highlighter regression suite (headless)
│   ├── test_teleport.py          [Test] Teleport room: phases, slots, compositing (22 cases)
│   ├── test_modes.py             [Test] Camera mode state machine
│   ├── test_explorer.py          [Test] Exploring-mode selection geometry + flight
│   ├── test_ghost_boxes.py       [Test] Ghost-box clipping (exclusion hole)
│   ├── test_splash.py            [Test] Boot splash strip decode/cache/fallback
│   ├── test_mem_budget.py        [Test] Depth-aware tile-cache memory budget
│   ├── test_depth_convert.py     [Test] Strip 24→32-bit numpy conversion
│   ├── test_panel_occlusion.py   [Test] Highlighter panel occlusion rules
│   ├── test_settings.py          [Test] Settings + web UI endpoints (43 checks)
│   ├── sim_wander.py             [Maintained] Wanderer coverage simulation
│   ├── sim_heatmap.py            [Maintained] Wanderer heatmap simulation + visualization
│   ├── sim_prefetch_v3.py        [Maintained] Tile prefetch/cache eviction simulation
│   ├── sim_prefetch.py           [Legacy] First prefetch sim; wrong eviction model — see v3
│   └── simulate_wander.py        [Legacy] Pre-refactor wanderer sim; superseded by sim_wander/sim_heatmap
├── deploy/                       Installation & systemd
│   ├── install.sh                One-shot installer for fresh Pi/OrangePi
│   ├── run.sh                    Boot wrapper (KMSDRM or X11 fallback)
│   ├── kiosk-launch.sh           Player launcher (env setup, overscan margin)
│   └── floor796-kiosk.service    systemd unit (cold-boot auto-start)
├── screenshot.png                Main screenshot (highlighter)
├── stats_overlay_screenshot.png  Stats overlay screenshot
├── README.md
├── CHANGELOG.md
└── .gitignore
```

### Directory Roles

- **`assets/`** — files downloaded from floor796.com and cached locally.  If
  deleted, the player re-downloads them on next boot.  Safe to delete to force
  a fresh download.
- **`cache/`** — files generated by the player at runtime (decoded strips,
  content mask, thumbnails).  Safe to delete at any time; the player rebuilds
  them automatically (strips take ~8 min, content mask ~2 min).
- **`floor796_kiosk/`** — the Python package.  Entry point is
  `python -m floor796_kiosk`.
- **`tools/`** — offline simulation scripts and CLI utilities, not needed for
  normal operation.
- **`deploy/`** — system integration scripts, used only during installation.

---

## How It Works

### Tile System

Floor796's map is a grid of 1024×820 pixel tiles.  Most tiles are static
(single-frame), but ~50 tiles in the center are animated (60-frame loops at
12 fps).  The player fetches tiles as MP4s from the CDN, decodes them to
full-resolution BMP frame strips using ffmpeg, and caches them in
`cache/strips/`.

Tiles are spaced at 1016×812 intervals (8 px overlap per axis), matching the
floor796.com front-end.  This overlap is critical for pixel-perfect alignment.

#### Storage contract: MP4s are transient scratch

The decoded strip in `cache/strips/<tid>.bmp` is the durable per-tile
artifact.  A tile's source MP4 is deleted the moment its strip is built,
and tiles are processed ONE AT A TIME (fetch → decode → discard) — never
bulk-downloaded.  At rest `assets/tiles/` is empty; peak disk usage equals
final usage plus one in-flight MP4.  Leftover MP4s whose strips already
exist are swept at every boot (idempotent migration for older installs,
~130 MB freed).

#### Content change detection (when does a tile re-encode?)

Change detection is metadata-driven — no per-file HTTP HEAD/ETag probing.
Every boot fetches `floor796.com/data/matrix.json` + `changelog.json` (no
tile downloads) and compares each tile's persisted fingerprint
(`tile_state.json`: mp4 URL, expected size, changelog entry ids, pending
flag) against the live matrix:

| reason | trigger |
|---|---|
| `new` | no fingerprint record for the tile |
| `re-rendered` | MP4 URL changed — the URL embeds the render timestamp, so any re-render gets a new URL |
| `changelog` | the set of changelog entry ids referencing the tile changed (the author extends tiles in phases) |
| `retry` | previous fetch never completed (`pending` flag) |
| `size` | the matrix's declared MP4 size moved |
| `corrupt` | an MP4 exists on disk but its size ≠ the declared size |
| `missing` | no usable copy at all — no MP4 **and** no complete strip |

A missing MP4 is deliberately *not* a trigger by itself: a tile whose
fingerprint matches and whose strip exists is up to date.  Planned fetches
run through the same one-at-a-time strip pass; state is committed only
after the pass, so an interrupted boot re-plans the same tiles (existing
strips are skipped — self-healing) and failed fetches retry via `pending`.

### Wandering Algorithm

The `Wanderer` class in `player.py` implements coverage-weighted waypoint
navigation with content-aware edge clamping:

1. **Content-aware scroll limits** — at startup, per-tile content bounds (tight
   boxes around actual pixel art, derived from the density mask) are aggregated
   into a global content bounding box.  Scroll limits are set to hug this border
   with a 50 px margin, preventing the viewport from wandering into the large
   blank isometric-diamond triangles at tile edges.
2. **Safe-region grid** — a boolean grid of viewport positions where the blank
   ratio ≤ 30%.  Only positions where the viewport is mostly content are
   considered safe for transit.  A* pathfinding routes through this grid.
3. **Optimal viewing positions** — for each animated tile, the viewport position
   that contains all of the tile's content pixels while minimizing blank ratio.
   Tiles whose optimal position is in the safe region are "normal"; others are
   "tip" tiles requiring an excursion.
4. **Content-density tour ordering** — tiles are visited in density order: CORE
   (dense interior) first, EDGE next, sparse TIP tiles last, keeping the viewport
   in low-blank territory for most of the tour.
5. **Smooth steering** — gradual angle interpolation toward each sub-waypoint
   with momentum blending for natural-looking movement.

Full coverage of all animated tiles is typically achieved in ~25 minutes.

### Content Density Mask

The `content_mask.py` module builds a downscaled content-density map from the
decoded tile strips.  For each animated tile, it measures local pixel standard
deviation to distinguish pixel-art content from flat background (the isometric
diamond shape means ~66% of each tile is background).  This mask is used by:

- The wanderer to compute blank ratios and find optimal viewing positions.
- The object highlighter to determine content bounds (tight box around actual
  pixel art, not the full tile rectangle).

The mask is built on first boot (or whenever `cache/content_mask.npz` is
missing) and cached.  A progress bar is shown on-screen during the build.

### Camera Modes

The player has three canonical camera modes, cycled with SPACE:

1. **Wandering** (default) — the self-driving coverage tour.  Arrow keys
   are inert here.
2. **Navigating** — free manual pan with the arrow keys.  Object
   highlighting is off while hand-driving the camera.
3. **Exploring** — object-to-object navigation.  Each arrow keypress
   flies the camera (0.4 s eased flight, 0.3 s landing cooldown) to the
   nearest selectable object in that direction — nearest within a 90°
   cone first, then the 180° half-plane, otherwise stay put.  The
   selected object gets the red box + info panel (no countdown bar —
   it stays until the next jump); every other selectable object in
   view gets a gray ghost box, clipped so its lines never draw over
   the highlighted artwork.  Entering the mode re-anchors to the
   object nearest the view center.

Returning to Wandering resumes the tour from the current camera —
never a snap-back to a previous position.  A transient capsule at the
bottom of the screen announces each mode change.

### Teleporter Room

The site's teleporter room (a 289×412 overlay near the top-left of the
map) cycles through its scenes automatically: a 22-beat materialize
with sparkle columns, a 60-tick play phase, and a 22-beat
dematerialize, then the next scene.  The implementation is site-exact
(verified against the live site's render semantics), including the
clip polygon and the black hide-triangle.

All transition beats are pre-rendered once (first boot, in a
background worker) and persisted to `assets/teleport/`; the render
loop only blits the cached frames (~1.5 ms/frame).  Scene content
downloads and caches on-device like everything else, and the scene
count adapts if the site adds more teleports.

### Boot Splash

During boot, floor796's about-section station animation loops in a
centered, framed square at 66% of screen height, with the boot status
text and progress bar underneath.  The mp4 is ffmpeg-decoded once into
a 60-frame strip (the same pipeline as tile strips) and cached in
`cache/splash/`; the strip is freed before the main render loop
starts.  If the assets or ffmpeg are unavailable, the boot screen
falls back to plain status text.

### Auto-Update

At startup, `tile_manager.py` fetches `matrix.json` from floor796.com to check
for new or changed tiles.  If the network is unavailable, it silently falls
back to the existing cache — the kiosk always boots, online or offline.

### Object Highlighter

The `ObjectHighlighter` class in `highlighter.py` automatically identifies and
labels objects from floor796.com's changelog (855 objects) as the wanderer
brings them into view.

**Selection algorithm** — deterministic LRU (least-recently-used):

1. **Filter** — candidates must be fully in the viewport, large enough to
   highlight, won't scroll off-screen during the 10-second highlight duration,
   not obscured by the info panel footprint, and have comfortable clearance
   from all screen edges (5% edge buffer) so they remain visible throughout
   the highlight.
2. **Select** — the object with the oldest `last_shown` timestamp is picked
   (never-shown objects have timestamp 0 = highest priority).  Ties are broken
   by closeness to viewport center.
3. **Scroll-off safety** — mid-highlight, if the object scrolls out of view
   (due to wanderer movement), the highlight is aborted cleanly.  The same
   abort fires if the object drifts behind the opaque corner info panel,
   and selection additionally predicts both screen-edge and panel overlap
   at the END of the 10-second highlight (velocity-aware), so an object
   that would wander into the panel is never picked in the first place.

This guarantees every reachable object is highlighted before any repeats — no
random sampling, no scoring weights.

**Visual effects:**

- **Zoom-in intro** (0.5s) — the bounding box interpolates from full viewport
  bounds to the object's bounding box with ease-out cubic, like a camera
  focusing.  Outline is 6 px thick during zoom for visibility.
- **Breathing glow** — 8 concentric filled-rect layers radiate outward to 24 px
  max radius, with quadratic alpha falloff.  Painter's algorithm (outer→inner).
  The box interior is cut out so glow doesn't tint the content.  Breathing at
  0.6 Hz.  Color: bright red `(255, 20, 20)`.  Glow, thumbnail, and panel
  surfaces are cached and bounded (24-entry FIFO eviction) to prevent memory
  growth over long uptimes.

**Thumbnail types** — the highlighter fetches and displays:

| Link type | Thumbnail source |
|-----------|-----------------|
| YouTube | `mqdefault.jpg` from `img.youtube.com` |
| Image | Direct download (imgur, etc.) |
| Video | Frame extraction via `ffmpeg` at ~1s timestamp |
| Compound | All `\|\|`-separated parts scanned (any order); best image source picked |
| Wikipedia | REST API (`/api/rest_v1/page/summary/`) returns thumbnail + text extract |
| Fandom wikis | MediaWiki `api.php` `pageimages` lead image (page HTML 403s bots) |
| Tenor | Item's `tinygif` (~220px) from embedded page JSON — the full GIF is 7MB+ |
| Overwatch | Hero-specific splash art (`960_{Hero}.jpg` from Blizzard CMS) — og:image is a generic share image identical for all heroes |
| Interactive | `og:image` from `floor796.com/interactive/` pages |
| Web | HTML `og:image` → `twitter:image` → first non-SVG `<img>` (tracker domains skipped) |
| SVG | Rendered to PNG via `cairosvg` |
| AVIF / HEIC | Decoded via `pillow-heif` or native Pillow 12+ |

Failed web fetches (bot-blocked sites like IMDb) degrade gracefully: the
panel renders at no-thumbnail size rather than showing an eternal
loading placeholder.

Thumbnails are cached in `cache/thumbnails/` and fetched in background threads.

### Settings & Web UI

The stats HTTP server serves an operator page at `http://127.0.0.1:8796/`
(reachable over SSH with `ssh -L 18796:127.0.0.1:8796 <pi>`).  It is
stdlib-only — a single embedded HTML page talking to the JSON endpoints.

**Resolution setting** — three options, each annotated with the hardware
reality (board RAM, the display's reported mode, and what the player is
currently rendering and why):

- **Auto** (default) — the display's native mode, with the standing rule
  that boards with ≤4 GB RAM render at 1080p regardless of the display.
- **1080p** — always 1920×1080.  On a higher-res display, KMSDRM
  negotiates a 1080p mode and the display's scaler upscales — the right
  choice for a 4K TV attached to a board without the RAM to feed it.
- **4K** — 3840×2160, only offered when the display reports a ≥4K mode;
  carries a warning when board RAM is under 8 GB (allowed as an explicit
  operator choice — the tile-cache budget still protects the board).

The setting persists in `settings.json` (install root, gitignored) and
applies at the next player start.

**Cache clearing (cold/warm boot simulation)** — a live report of every
cache (tile strips, content mask, splash strip, teleporter transitions,
thumbnails, downloaded tiles) with file counts, sizes, and rebuild
costs.  Caches can be cleared individually, as a selection, or all at
once (full cold boot: tiles re-download ~123 MB, strips re-decode
~8 min).  Clearing is safe while the player runs — everything rebuilds
at the next start.

**Debug controls** — camera mode buttons (Wandering/Navigating/
Exploring), viewport pin/resume, stats overlay toggle and time window,
a live screenshot viewer, and a **Restart player** button (graceful
exit; systemd restarts the service within ~5 s).

### Telemetry & Stats API

The player runs a lightweight HTTP server on `127.0.0.1:8796` (stdlib only,
no external dependencies).  All endpoints return JSON unless noted.
`GET /` serves the settings & debug web page described above.

| Endpoint | Description |
|----------|-------------|
| `GET /` | Settings & debug web UI (see *Settings & Web UI* above) |
| `GET /settings` | Current settings + resolution options with hardware capabilities |
| `POST /settings` | `{"resolution": "auto" \| "1080p" \| "4k"}` — persists, applies at next start |
| `GET /debug/caches` | Per-cache report: files, bytes, rebuild cost |
| `POST /debug/clear` | `{"targets": ["strips", "content_mask", "splash", "teleport_trans", "thumbnails", "tiles"]}` |
| `POST /debug/restart` | Graceful player exit (systemd restarts within ~5 s) |
| `GET /stats[?window=30m]` | Full telemetry snapshot (FPS, memory, CPU, coverage, wanderer position, highlighter state, label stats) |
| `GET /health` | 24-hour health trends (RSS, CPU, FPS with min/max/avg) |
| `GET /heatmap[?window=1h]` | Viewport visit heatmap as a PNG image |
| `GET /coverage[?window=30m]` | Tile coverage grid with per-tile visit counts |
| `GET /objects` | Per-object highlight stats (id, title, views, last shown) for all 855 objects |
| `GET /objects/recent?n=20` | N most recently highlighted objects |
| `GET /objects/summary?window=30m&limit=10` | Windowed summary: most-viewed, most-recent, coverage % |
| `POST /overlay` | Toggle on-screen overlay: `{"enabled": true}` |
| `POST /overlay/window` | Set overlay time window: `{"window": "1h"}` or `{"cycle": true}` |
| `POST /viewport` | Pin camera to a map position `{"x": .., "y": ..}` (pauses wandering); `{"resume": true}` resumes |
| `POST /mode` | Switch camera mode: `{"mode": "wandering" \| "navigating" \| "exploring"}` |
| `GET /screenshot` | PNG capture of the live viewport (highlighter + scene) |

**On-screen overlay** — press `S` to toggle a semi-transparent stats panel
showing live FPS, memory, CPU, tile cache status, wanderer position/heading,
coverage heatmap grid, and label statistics (Top 5 most-viewed, Last 10
most-recent).  Press `T` to cycle time windows (10min → 30min → 1h → 4h →
8h → all-time).

---

## Configuration

The player can be configured via command-line arguments or by editing constants
in `floor796_kiosk/player.py` and `floor796_kiosk/paths.py`.

All file paths are centralized in `paths.py` — if you need to relocate the
data directories (e.g., to a faster SD card or RAM disk), change them there.

| Setting             | Default | Description                              |
|---------------------|---------|------------------------------------------|
| `DEFAULT_WIDTH`     | 0       | Display width (0 = auto-detect)          |
| `DEFAULT_HEIGHT`    | 0       | Display height (0 = auto-detect)         |
| `SCALE`             | 1.0     | Tile scale (must be 1.0 — see warnings) |
| `DEFAULT_WANDER_SPEED` | 15.0 | Pan speed in pixels/sec                  |
| `CACHE_MARGIN`      | 2       | Extra tile ring to prefetch              |
| `COVERAGE_LOG_INTERVAL` | 300 | Seconds between coverage log lines     |

### Overscan

Some displays (particularly older TVs) crop a few pixels at each edge —
"overscan".  Set `KIOSK_OVERSCAN_MARGIN` in the systemd unit to inset all
content and UI by that many pixels per side:

```ini
# /etc/systemd/system/floor796-kiosk.service
Environment=KIOSK_OVERSCAN_MARGIN=60
```

After changing, run `sudo systemctl daemon-reload && sudo systemctl restart
floor796-kiosk`.  Default is 0 (no inset).

### Display Resolution

The player **auto-detects** the native resolution of the connected display via
pygame's `display.Info()`. No configuration is needed — the code defaults to
`0` (auto-detect), and the launch script passes `--width 0 --height 0` to
enable this.

If auto-detection fails (e.g., no display connected at boot), it falls back to
1920×1080.

To override with a specific resolution, set the `KIOSK_WIDTH` and
`KIOSK_HEIGHT` environment variables, or pass `--width` and `--height` directly:

```bash
# Override to 1920×1200 via environment
KIOSK_WIDTH=1920 KIOSK_HEIGHT=1200
```

---

## Service Management

```bash
# Start / stop / restart
sudo systemctl start floor796-kiosk
sudo systemctl stop floor796-kiosk
sudo systemctl restart floor796-kiosk

# View live logs
journalctl -u floor796-kiosk -f

# Check status
sudo systemctl status floor796-kiosk

# Disable auto-start
sudo systemctl disable floor796-kiosk
```

---

## Manual Controls (for testing)

When a keyboard/mouse is connected during maintenance:

| Key             | Action                     |
|-----------------|----------------------------|
| Space           | Cycle camera mode: Wandering → Navigating → Exploring → Wandering |
| Arrow keys      | Navigating: pan manually.  Exploring: jump to the next object in that direction.  Inert in Wandering. |
| V               | Print coverage stats       |
| O               | Toggle object highlighter  |
| L               | Switch label mode (corner/inline) |
| S               | Toggle stats overlay       |
| T               | Cycle stats time window    |
| ESC             | Quit (service will restart)|

---

## Troubleshooting

### Black screen on boot

- Check HDMI cable and that the display is powered on.
- Check logs: `journalctl -u floor796-kiosk -b`
- Ensure `hdmi_force_hotplug=1` is set in `/boot/firmware/config.txt`.
- Try a different `hdmi_group` / `hdmi_mode` in config.txt.

### Player crashes / restarts repeatedly

- Check logs: `journalctl -u floor796-kiosk --no-pager -n 100`
- Check available memory: `free -h` (needs ~2 GB free).
- Ensure tiles are downloaded: `ls /opt/floor796-kiosk/assets/tiles/*.mp4 | wc -l`

### Display goes to sleep

- KMSDRM mode: DPMS is handled by the DRM driver.  Ensure
  `hdmi_blanking=1` is NOT set in `/boot/firmware/config.txt` (it forces
  blanking).
- X11 fallback mode: the installer disables DPMS at the X server level
  (`-dpms`, `-s 0`).
- Some displays have their own sleep timer — check the monitor's OSD menu.

### Tiles not updating

- The player checks for updates at every startup.  If offline, it uses cache.
- To force a manual update:
  ```bash
  cd /opt/floor796-kiosk
  sudo -u kiosk python3 -m floor796_kiosk.tile_manager
  ```

### Rebuilding the cache

The `cache/` directory is fully rebuildable — the settings web UI's
*Clear selected / Clear ALL* buttons do this without a shell.  The
manual equivalent:

```bash
sudo systemctl stop floor796-kiosk
sudo rm -rf /opt/floor796-kiosk/cache/*
sudo systemctl start floor796-kiosk
```

The player will re-decode strips (~8 min), rebuild the content mask (~2 min),
and re-fetch thumbnails on next boot.

---

## Performance

| Metric              | Pi 5 (4 GB)      | Pi 5 (2 GB)      | OrangePi 5 Max   |
|---------------------|------------------|------------------|------------------|
| Render rate         | 30 fps (vsync)   | 30 fps (vsync)   | ~17 fps (4K)¹    |
| Animation rate      | 12 fps           | 12 fps           | 12 fps           |
| Resolution          | 1080p (capped)   | 1080p (capped)   | native (up to 4K)|
| Memory (RSS)        | ~2.9 GB          | ~1.45 GB         | ~2.7 GB          |
| Swap                | 0 MB             | some (tight headroom) | 0 MB        |
| Tile cache          | 10 tiles @32-bit | 8 tiles @16-bit  | 18+ tiles        |
| CPU                 | ~50% (1 core)    | ~50% (1 core)    | ~30% (1 big core)|
| Cold-boot (warm)    | ~20s             | ~20s             | ~20s             |
| Full coverage       | ~25 min          | ~25 min          | ~25 min          |

> ¹ At 4K (3840×2160), each frame is ~33 MB of pixel data — the render loop is
> memory-bandwidth bound (~26 ms per blit).  17 fps exceeds the 12 fps source
> animation rate, so no animation frames are dropped.  At 1080p the OrangePi
> also achieves 30 fps (vsync).
>
> Boards with ≤4 GB RAM are automatically capped at 1080p to prevent OOM.
> The tile-cache budget prices each slot at the real per-depth strip cost
> (201 MB at 32-bit, 100 MB at 16-bit) and adapts to board RAM: 4 GB boards
> hold ~10 display-format strips with zero swap; boards under 3.9 GB keep
> 16-bit strips (~8 tiles on 2 GB).  All boards use identical KMSDRM
> rendering.

---

## Development

### Running the player directly

```bash
cd floor796-kiosk
python3 -m floor796_kiosk --fullscreen
```

### Tools & simulations

The `tools/` directory holds three kinds of scripts. Each script's module
docstring carries the same status tag as below.

**Operator utilities** — useful to anyone running a kiosk, no display or
simulated time required:

```bash
# Live status of a running kiosk (one-shot, watch mode, health, heatmap)
python3 tools/kiosk_status.py --host 127.0.0.1
python3 tools/kiosk_status.py --watch 2 --health

# Pre-fetch every object thumbnail offline, before deployment
python3 tools/prefetch_thumbnails.py
```

**Test suites** — run before deploying changes; both are self-contained
(fake HTTP server / headless pygame) and exit non-zero on failure:

```bash
python3 tools/test_tile_update.py     # tile update engine (24 checks)
python3 tools/test_hologram_fixes.py  # hologram + highlighter (40 checks)
python3 tools/test_teleport.py        # teleport room (22 cases)
python3 tools/test_modes.py           # camera mode state machine
python3 tools/test_explorer.py        # exploring-mode navigation
python3 tools/test_ghost_boxes.py     # ghost-box clipping
python3 tools/test_splash.py          # boot splash strip
python3 tools/test_mem_budget.py      # tile-cache memory budget
python3 tools/test_depth_convert.py   # strip depth conversion
python3 tools/test_panel_occlusion.py # panel occlusion rules
python3 tools/test_settings.py        # settings + web UI endpoints
```

**Maintained simulations** — import the real classes from the
`floor796_kiosk` package and are kept current with it. Use these for
wanderer/cache work:

```bash
cd floor796-kiosk
python3 tools/sim_wander.py --hours 2            # coverage / directional bias
python3 tools/sim_heatmap.py --hours 1 --output heatmap.png
python3 tools/sim_prefetch_v3.py                 # tile cache eviction model
```

**Legacy simulations** — kept for historical reference only:
`sim_prefetch.py` (first prefetch sim; its eviction model was wrong, which
is why v3 exists) and `simulate_wander.py` (pre-refactor wanderer sim,
superseded by `sim_wander.py`/`sim_heatmap.py`). Don't start new work
from these.

### Building the content mask manually

```bash
cd floor796-kiosk
python3 -m floor796_kiosk.content_mask
```

---

## Credits

- [Floor796](https://floor796.com) — the original interactive isometric
  pixel-art map of the 796th floor.  All tile artwork belongs to the floor796
  project.
- This kiosk is a standalone viewer; it does not modify or redistribute the
  original artwork beyond caching tiles for local display.

## License

The code in this repository is provided as-is for personal use.  The floor796
tile artwork remains the property of its respective creators.
