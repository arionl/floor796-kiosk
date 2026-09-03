# Changelog

All notable changes to the Floor796 Kiosk project are documented here.
Tags are cut on `main`; development happens on `dev`.

---

## v2.4.2 — Hologram room fixes (2026-09-03)

### Fixed
- **Periodic empty hologram room** — three compounding causes:
  1. *Animation-clock drift*: the render loop reset its frame accumulator
     instead of carrying the remainder, so at ~30fps the 12fps animation
     clock ran at 10 ticks/s (20% slow). Every hologram cycle stretched
     from 10s to ~12s (confirmed in journal timestamps) and all fade/gap
     timing drifted with it. The accumulator now carries the remainder,
     clamped after long hitches.
  2. *Missed decode deadlines*: the next scene's decode was requested at
     gap start, leaving only ~3.6s (stretched) of lead time. The largest
     scene (1.6 MB, ~7s decode on a Pi 5) chronically missed it, so
     `render()` silently returned an empty room until the scene popped
     in mid-cycle. Requests are now issued at fade_out start (~5s lead),
     and `_tick` refuses to enter fade_in until the scene is fully
     promoted — extending the gap once (bounded) and otherwise skipping
     to the next scene rather than ever showing an empty mid-cycle room.
  3. *Corrupt-cache lockout*: a crash mid-download left a truncated
     `.decoded` cache file that failed decode on every cycle, forever.
     Cache writes are now atomic (`.tmp` + rename), downloads and cached
     files pass a cheap structural validation, and a decode failure
     purges the cache so the next cycle re-downloads.
- **Highlighter showed non-playing holograms** — the changelog contains
  "Hologram #1..#14" label objects, but the website only ever plays
  slots 1–6 (7–14 are unplayable "404" console buttons) and the kiosk
  plays one scene at a time. The highlighter now receives the hologram
  playback state via a provider and only selects "Hologram #N" while
  slot N is the scene currently materialized; an in-progress highlight
  aborts when its hologram dematerializes (gap) but survives fade_out.
- **Stats `holo_scene` always 0** — read a nonexistent `_scene_idx`
  attribute; now reports `current_holo` (the actual scene index).

### Changed
- **Amortized scene promotion** — promoting a decoded scene converted
  all 60 frames to surfaces in one render frame (100+ ms hitch, seen as
  `overlay=119ms` frame spikes). `poll_scenes()` now promotes at most 8
  frames per call; a scene is "ready" only when fully promoted.
- **In-flight decode tracking** — `_request_scene` knows about scenes
  currently being decoded, closing a re-queue race that could decode the
  same scene twice.

### Added
- `HologramOverlay.playback_state()` — live state accessor for other
  subsystems.
- `tools/test_hologram_fixes.py` — 40-check headless suite covering
  cache validation (against the 6 real scene files), gap-gating state
  machine, highlighter gating/abort semantics, and accumulator math.

---

## v2.4.1 — Changelog-driven tile updates (2026-09-03)

### Added
- **Phased tile update detection** — the author of floor796.com often works
  on a tile in phases, adding content over multiple sessions.  Each phase
  adds an entry to his changelog referencing the tile.  The tile manager now
  fetches the changelog alongside the matrix, derives a per-tile signature
  (sorted set of changelog entry ids whose polygons reference the tile), and
  re-downloads any tile whose signature changes.  Previously tiles were only
  refreshed by mp4 file size, which missed phased updates.
- **`assets/tile_state.json`** — per-tile fingerprint file (mp4 URL,
  expected size, changelog entry set, pending flag).  Replaces size-only
  comparison.  The mp4 URL embeds the upstream render timestamp, so
  re-renders are detected even when file size is identical.
- **Failed-download retry** — tiles whose download fails are marked
  ``pending`` in the state file and retried on the next update check,
  instead of being silently skipped until a size change.
- **`content_mask.update_tiles()`** — incremental content-density-mask
  patching.  When tiles are refreshed, only their rows in
  ``content_mask.npz`` are recomputed instead of a full multi-minute
  rebuild.
- **Changelog cache refresh** — ``assets/changelog.json`` (used by the
  object highlighter) is now rewritten after every successful update check,
  so new objects become highlightable.  Previously it was downloaded once
  and never refreshed.
- **`.part` cleanup** — interrupted downloads no longer leave partial
  files in the tiles directory; leftovers from crashed runs are removed at
  the start of every check.
- **`tools/test_tile_update.py`** — 24-check regression suite covering
  first-run download, no-op, phased changelog updates, re-render with
  identical size, entry removal, offline fallback, corrupt-file repair,
  failed-download retry, and atomic state writes.  Runs against a local
  fake HTTP server, no network needed.

### Changed
- **Tile downloads are now atomic** (``.part`` + rename) — a crash or power
  loss mid-download can no longer leave a truncated tile on disk.
- **Status screen wording** — "Tiles updated" now distinguishes "N new"
  from "N refreshed" so you can see when the author extended existing
  tiles.
- **Version reported in User-Agent** — HTTP fetches now send
  ``Floor796-Kiosk/<version>``.
- **First run after upgrade re-fetches once** — with no state file present,
  all tiles are re-downloaded one time to establish fingerprints (and to
  repair any tile left stale by the old size-only scheme).  Subsequent
  checks are no-ops.

### Fixed
- **Stale highlighter labels** — the object highlighter loaded
  ``assets/changelog.json`` from cache and never re-downloaded it, so
  objects added by the author after first boot were never highlighted.
  The cache is now refreshed by the tile manager on every successful
  update check.
- **Stale strips after tile refresh** — re-downloading a tile left the old
  decoded strip in ``cache/strips/`` and ``prepare_strips()`` would skip
  re-decoding it, so the kiosk kept playing the old animation forever.
  Refreshed tiles now have their strips deleted and are re-decoded.

---

## v2.4 — 4K optimization, screenshot endpoint, OOM fix (2026-07-29)

### Added
- **In-process `/screenshot` HTTP endpoint** — captures the live viewport
  (scene + highlighter overlay) as a PNG via a thread-safe handshake between
  the HTTP server and the main render loop.  The frame is copied before
  `display.flip()`, ensuring a clean grab without external tools.  Works under
  KMSDRM where `scrot` and other X11 screenshot utilities cannot.
- **Resolution-aware UI scaling** — all highlighter dimensions (fonts,
  thumbnails, panel geometry, outlines, glow radius, padding) scale
  proportionally with display height (`screen_height / 1080.0`).  At 4K the
  UI is rendered at 2.0×, at 1440p at 1.33×, at 1080p at 1.0×.  Ensures crisp,
  readable text and panels at any resolution without manual tuning.
- **Highlighter panel occlusion filter** — objects whose bounding box overlaps
  the info panel footprint are excluded from selection, preventing highlights
  hidden behind the panel.
- **Edge viewing buffer** — candidates must maintain 5% clearance from all
  screen edges at the end of the 10-second highlight (predicted via wander
  velocity).  Prevents objects from scrolling off-screen mid-highlight.
- **Frame timing instrumentation** — slow frames (>40 ms) are logged with a
  per-section breakdown: `[FRAME] Xms (avg=Y p50=Z n=N) blit=B overlay=O ss=S
  flip=F pending=P`.  Enables precise diagnosis of performance regressions.
- **Surface caching** — glow layers, scaled thumbnails, and panel backgrounds
  are cached per-frame to eliminate redundant allocations.  Combined with
  the FIFO eviction below, overlay cost dropped to ~7 ms steady-state at 4K.

### Changed
- **GLOW_STEPS reduced 16 → 8** — halves per-frame alpha blits with no
  perceptible visual change (3 px gradient steps at minimum).  Significant
  fill-rate savings at 4K where each glow layer is 4× larger than at 1080p.
- **Per-frame overhead reduced ~8 ms** — `wanderer.heading()` computed once
  per frame (was 3×), `coverage_stats()` called once (was 2×), per-frame
  `dict()` copy of visit counts eliminated, and `tile_cache.set_needed()`
  short-circuits when the visible/margin tile set is unchanged (~99% of
  frames).

### Fixed
- **OOM crash on OrangePi 5 Max (4K)** — the 4K overlay caches
  (`_scaled_thumb_cache`, `_panel_bg_cache`) had no eviction policy, leaking
  ~2.0 GB of surfaces over 24 h as the highlighter cycled through all 804
  objects.  Fixed with a 24-entry FIFO cap on both caches (~60 MB ceiling).
  Combined with the tile cache (~4 GB) and hologram scenes (~360 MB), total
  memory is now bounded at ~4.4 GB on the 8 GB OrangePi.
- **Glow cache artifacts** — glow surfaces were cached with absolute screen
  positions computed at build time, but the wanderer scrolls every frame,
  causing ghosting/misalignment.  Fixed: cache stores only the surface and
  padding; blit position is computed fresh from current viewport coordinates
  each frame.

### Performance
- **OrangePi 5 Max (4K)**: ~17 fps at 3840×2160 (was crashing within 24 h).
  Breakdown: blit=26 ms, flip=9 ms, overlay=7 ms, overhead=18 ms.  The blit
  is memory-bandwidth bound (~33 MB pixel data per frame).  17 fps exceeds
  the 12 fps source animation rate, so no frames are dropped.

---

## v2.3 — KMSDRM unification, highlighter rewrite, multi-board hardening (2026-07-24)

### Added
- **KMSDRM unified rendering** — Raspberry Pi 5 now uses the same KMSDRM code
  path as the OrangePi 5 Max (via V3D driver instead of X11).  Both board types
  boot directly to KMSDRM with no X11 overhead.  X11 is retained only as a
  fallback for generic/unknown boards.
- **Overscan support** — `KIOSK_OVERSCAN_MARGIN` environment variable (read by
  `kiosk-launch.sh`, passed as `--overscan-margin`) insets all content and UI by
  N pixels per side for displays with overscan.  The highlighter, stats overlay,
  and memory warning banner all respect the margin.
- **Low-memory warning banner** — on boards below 3900 MB RAM, a semi-transparent
  amber banner appears at startup advising that 4 GB+ is recommended.  Threshold
  configurable via `LOW_MEM_THRESHOLD_MB`.
- **Thumbnail prefetch tool** (`tools/prefetch_thumbnails.py`) — pre-fetches all
  804 thumbnails offline before deployment.  Supports all link types: YouTube
  (`mqdefault.jpg`), images (direct), video (`ffmpeg` frame extraction), Wikipedia
  (REST API), interactive (`floor796.com/interactive/` og:image), web (HTML
  og:image → twitter:image → first `<img>`).  SVG via `cairosvg`, HEIC/HEIF via
  `pillow-heif`, AVIF native in Pillow 12+ or via `pillow-heif`.  `--types` flag
  to filter by entry type.  Image URL resolution via `urllib.parse.urljoin()`.
- **Image format fallback chain** — pygame fast path → Pillow → format-specific
  plugins (`cairosvg`, `pillow-heif`).  All optional imports guarded with
  try/except for graceful degradation.
- **`install.sh`** now installs `libcairo2` (apt), `cairosvg` + `pillow-heif`
  (pip) for full image format support.

### Changed
- **Highlighter selection rewrite** — replaced weighted random sampling (5-factor
  scoring with `score³` temperature) with deterministic LRU.  Candidates are
  filtered (in viewport, large enough, won't scroll off, not in cooldown), then
  the oldest `last_shown` object is selected.  Never-shown objects have priority.
  Guarantees every object is shown before any repeats.  Net −130 lines.
- **Highlighter visual effects** — new zoom-in intro (0.5s ease-out cubic,
  viewport bounds → object bbox), followed by single-phase breathing glow (16
  filled-rect layers, 24px max radius, quadratic alpha falloff, painter's
  algorithm, box interior cutout, 0.6 Hz breathing, color `(255, 20, 20)`).
  Glow skipped during zoom to prevent 170MB/frame surface allocation.  Outline
  6px during zoom for visibility.  `PAUSE_DURATION` 2.0s → 0.5s.
- **Content-aware scroll limits** — viewport scroll range now derived from
  per-tile content bounds (tight pixel-art bounding boxes from the density mask)
  rather than raw tile-grid dimensions.  50px margin past content edge.  Prevents
  wandering into blank isometric-diamond triangles at tile borders.
- **Resolution cap** — boards with ≤4096 MB RAM (`MAX_RES_1080P_MEM_MB`) are
  capped at 1080p regardless of the display's native resolution.  Prevents OOM
  from excessive tile cache at 1440p/4K.  `SDL_SCALED` is dropped when resolution
  is capped to prevent GLES upscale-tearing.
- **Priority-aware tile eviction** — `poll_results()` evicts in order: unneeded
  → margin → never visible.  Adaptive margin sizing shrinks `CACHE_MARGIN` to fit
  the cache budget on memory-constrained boards.

### Fixed
- **OOM on 2GB Pi 5** — two memory bugs in `TileCache`: (1) late loads after
  direction changes triggered unnecessary tile loads, causing OOM; (2) eviction
  order didn't prioritize truly unneeded tiles.  Fixed with priority-aware
  eviction + adaptive margin.
- **Tearing below native resolution** — `SDL_SCALED` caused GLES upscale blit
  that tore at 1440p→1080p.  Fixed by dropping `SDL_SCALED` when resolution is
  capped.
- **Tile eviction churn** — three fixes for constrained-cache boards: (1)
  directional prefetch loads only tiles ahead of movement, (2) adaptive margin
  shrinks to fit budget, (3) late loads after direction change are skipped if
  tile is no longer needed.
- **prefetch image bugs** — (1) relative URL resolution used string concat
  instead of `urljoin`, breaking paths like `index-v1.html?2/./disk-prop1.png`;
  (2) `<img>` fallback picked up SVG logos (e.g. Tenor); (3) WEBP images failed
  to decode in pygame (now falls back to Pillow).

### Performance
- **2GB Pi 5**: stable at 1080p with 8-tile cache.  ~1.5 GB RSS, ~450 MB swap.
  Low-memory banner shown.  Verified over extended uptime.
- **4GB Pi 5**: stable at 1080p (capped from 1440p).  ~2.4 GB RSS, 0 swap.
- **OrangePi 5 Max**: 60 FPS at native resolution via KMSDRM + Panthor.

---

## v2.2 — Multi-board support: OrangePi 5 Max + Raspberry Pi 5 (2026-07-07)

### Added
- **Centralized board detection** (`floor796_kiosk/board_detect.py`) — a single
  module that detects the embedded board type and selects the appropriate
  rendering code path:
  - **OrangePi 5 Max** (RK3588 + Mali-G610) → KMSDRM + Mesa Panthor, no X11
  - **Raspberry Pi 5** (BCM2712 + VideoCore VII) → X11 + Mesa V3D
  - **Generic / unknown** → X11 fallback (works on most Linux desktops)
  - Detection methods: `/proc/device-tree/model` (primary), GPU driver in DRM
    render node uevent files (fallback 1), `/proc/device-tree/compatible`
    (fallback 2). All read-only, no special permissions.
  - CLI interface for shell scripts: `python3 -m floor796_kiosk.board_detect
    --shell` prints `BOARD_TYPE`, `GPU_DRIVER`, `RENDER_BACKEND`, `NEEDS_X11`,
    `RUNS_AS_ROOT`, `SUPPORTS_4K_NATIVE`, `TOTAL_MEM_MB`. Also supports
    `--json` for structured output.
- **Board-aware install script** — `deploy/install.sh` now detects the board
  type at install time and installs the correct system packages:
  - OrangePi: `libgbm1`, `libgl1-mesa-dri`, `mesa-va-drivers` (KMSDRM/Mesa)
  - Raspberry Pi / generic: `xserver-xorg`, `xinit`, `x11-xserver-utils`
    (X11/Mesa)
  - OrangePi install skips X11 packages entirely (no desktop manager needed).
  - Board-specific kiosk configuration: OrangePi uses console blanking
    disable; Raspberry Pi uses `config.txt` HDMI settings.

### Changed
- **Player GPU detection refactored** — `player.py` no longer has inline
  Panthor detection logic. Instead, it imports `detect_board()` and
  `get_render_config()` from `floor796_kiosk.board_detect`. The inline
  `has_panthor` scan of `/sys/class/drm/renderD*/device/uevent` is replaced
  by a single function call. `SDL_VIDEODRIVER` is set based on the board's
  `RenderConfig.sdl_driver` field.
- **`deploy/run.sh` refactored** — uses `python3 -m floor796_kiosk.board_detect
  --shell` for board detection instead of inline shell loop scanning for
  Panthor render nodes. The X11-vs-KMSDRM decision is now driven by the
  `NEEDS_X11` variable from the Python module, ensuring the shell scripts and
  player code use identical detection logic.
- **`deploy/kiosk-launch.sh` refactored** — uses `RUNS_AS_ROOT` from
  `board_detect` to decide whether to run as root (KMSDRM/OrangePi) or
  `runuser -u kiosk` (X11/Pi 5). Eliminates the duplicated Panthor detection
  loop that was previously in this script.
- **Comments updated** — references to "libmali" in code comments corrected to
  "Panthor" (the proprietary libmali driver was replaced by Mesa Panthor in
  v2.1.3).
- **Player docstring** updated to mention both Raspberry Pi 5 and OrangePi 5
  Max as supported platforms.

### Fixed
- **OrangePi/Pi 5 code path isolation** — the 4K and X11-specific logic in
  `player.py` (xrandr mode switching, `pygame.display.quit()` re-init) is
  correctly skipped on KMSDRM (OrangePi) via the `using_kmsdrm` check, which
  now uses the board detection module's `sdl_driver` value. The Raspberry Pi 5
  X11 path is unaffected by the OrangePi KMSDRM changes.
- **Consistent detection** — previously, `run.sh`, `kiosk-launch.sh`, and
  `player.py` each had their own copy of the Panthor detection loop with
  slightly different ranges (128-132 vs 128-140). Now all three use the same
  `board_detect` module, eliminating detection inconsistencies.
- **OrangePi + 1080p display support** — verified that the OrangePi 5 Max
  works correctly when connected to a 1080p display (not just 4K). KMSDRM
  detects the display's native mode and renders at 1920×1080. The 4K downscale
  block is skipped because `args.width` (1920) is not > 3000. Tile cache is
  sized for 1080p (15 tiles), same as a Pi 5 at 1080p. Also added a new code
  path for the edge case of an OrangePi with only 4 GB RAM on a 4K display
  (can't use xrandr on KMSDRM, so it renders native 4K with a memory-constrained
  tile cache and a warning log).

---

## v2.1 — Package refactor & self-sufficient install (2026-07-05)

### Changed
- **Full project restructure** into a proper Python package layout:
  - `floor796_kiosk/` — importable package with `__main__.py` entry point
    (`python -m floor796_kiosk`)
  - `assets/` — all files downloaded from floor796.com (tiles, tiles_meta.json,
    changelog.json, holograms), cached locally and gitignored
  - `cache/` — all files generated at runtime (decoded strips, content_mask.npz,
    thumbnails), fully rebuildable and gitignored
  - `tools/` — simulation scripts and CLI utilities
  - `deploy/` — installation scripts, systemd service, boot wrappers
- **Centralized path management** (`floor796_kiosk/paths.py`) — all file paths
  resolved in one module. No module hard-codes directory locations. Relocating
  data directories (e.g. to a RAM disk) only requires editing `paths.py`.
- **Module renames** for clarity:
  - `kiosk_player.py` → `floor796_kiosk/player.py`
  - `build_content_mask.py` → `floor796_kiosk/content_mask.py`
  - `object_highlighter.py` → `floor796_kiosk/highlighter.py`
  - `thumbnail_cache.py` → `floor796_kiosk/thumbnails.py`
  - `stats_collector.py` → `floor796_kiosk/stats/collector.py`
  - `stats_http.py` → `floor796_kiosk/stats/http_server.py`
  - `stats_overlay.py` → `floor796_kiosk/stats/overlay.py`
- **Install script simplified** — no longer downloads tiles during
  installation. The installer only installs code, system packages, and
  configures systemd. All content (tiles, labels, strips, content mask) is
  fetched/generated by the player on first boot with on-screen progress
  messages.
- **Service entry point** changed from `python kiosk_player.py` to
  `python -m floor796_kiosk`.
- Updated `.gitignore` for new directory structure.
- Updated README with new file structure, directory roles, and development
  instructions.

### Removed
- `content_mask.npz` removed from git tracking — auto-generated at startup.
- `changelog.json`, `tiles_meta.json`, `wander_heatmap*.png` removed from git
  tracking — all downloaded/generated at runtime.

---

## v2.0.1 — Screenshots & README (2026-07-05)

### Added
- Main screenshot (`screenshot.png`) — highlighter in action with YouTube
  thumbnail (Sonic the Hedgehog #320).
- Stats overlay screenshot (`stats_overlay_screenshot.png`) — cropped view of
  the on-screen telemetry panel with all sections visible.
- README documentation for object highlighter, telemetry API endpoints,
  thumbnail types, on-screen overlay, and updated keyboard controls.

---

## v2.0 — Object highlighter (2026-07-05)

### Added
- **Automatic object highlighter** (`object_highlighter.py`) — identifies
  and labels objects from floor796.com's changelog as the wanderer moves
  through the map. 804 objects indexed with bounding boxes, titles, dates,
  and optional media links.
  - **Weighted random selection** — candidates scored by spatial proximity,
    edge safety, panel exclusion, velocity prediction, and recency. Sampling
    proportional to score³ (temperature=3) prevents the same first/second/
    third object on every boot while still strongly preferring well-positioned
    candidates.
  - **Recency-weighted rotation** — exponential decay (10-min half-life).
    Never-viewed objects get 15% bonus. 45-second hard cooldown prevents
    immediate repeats.
  - **Soft edge scoring** — 4% hard margin for pixel clipping only; objects
    in the 4–20% zone get up to 50% score penalty but remain selectable.
    Relaxed 1.5% clip margin ensures edge-of-map objects (e.g. #383) are
    reachable. All 804 objects (100%) are reachable.
  - **Velocity prediction** — predicts object position at end of highlight
    duration based on wander speed/direction. Skips objects that would scroll
    off-screen. Objects ahead of viewport direction get 10% bonus.
  - **Panel exclusion** — objects overlapping the bottom-right info panel
    footprint get up to 30% score penalty.
  - **Thumbnail support** (`thumbnail_cache.py`) — images, YouTube
    (mqdefault), video frame extraction via ffmpeg, and Wikipedia REST API
    (thumbnail + text extract). Background fetching with animated placeholder.
  - **Pulse animation** — 1.8s expanding glow halos at highlight start to
    draw attention, settling into a steady outline.
  - **Corner info panel** — title (2-line word-wrap), date, thumbnail,
    Wikipedia extract (3-line), link type indicator, and progress bar.
  - **Single bounding box per object** — computed from min/max of all polygon
    vertices, avoiding per-tile fragmentation.
- **Highlighter telemetry** —
  - `GET /objects` — full per-object stats (id, title, views, last_shown).
  - `GET /objects/recent?n=20` — N most recently highlighted.
  - `GET /objects/summary?window=30m&limit=10` — windowed summary with
    most_viewed, recent, and coverage stats. Customizable limit (1–100).
- **Label stats in overlay** — Top (5 most viewed) and Last (10 most recent)
  sections with dynamic pixel-based title truncation and right-aligned
  counters/time values. Windowed to match overlay's time window selection.
- **Wanderer start jitter** (±200px) — different viewport position each boot
  for highlighter variety.

### Changed
- Stats overlay moved to left side (`panel_x = 0`) to avoid overlap with
  the highlighter's bottom-right info panel.
- Label stats titles pass through full (untruncated) from the data layer;
  the overlay renderer handles pixel-based truncation with ellipsis.

### Fixed
- **Deterministic selection** — pure argmax meant same first/third/fifth
  object on every boot. Replaced with weighted random sampling.
- **1.5% clip margin** — edge-of-map objects that barely overflowed the 4%
  scoring margin at their only reachable viewport positions are now
  selectable (was: permanently unreachable).

---

## v1.4 — Telemetry & stats overlay (2026-07-04)

### Added
- **In-process telemetry & stats service** (`stats_collector.py`,
  `stats_http.py`, `stats_overlay.py`) — live querying of internal state
  via HTTP on `127.0.0.1:8796`, no external dependencies (stdlib only).
  - Endpoints: `/stats` (JSON), `/health` (24h memory/CPU/FPS trends),
    `/heatmap` (PNG), `/overlay` (POST toggle).
  - On-screen alpha-blended overlay toggled with `S` key, time window
    cycled with `T` key. Defaults to 30-minute window on toggle.
  - Time-dimension design: per-tile visit ring buffer (8h exact),
    decaying spatial heatmap (10m/30m/1h/4h/8h + all-time), scalar ring
    buffers for blank%/FPS (8h, 1s samples), health metrics (24h, 10s
    samples). Total memory overhead ~1.8 MB.
  - `kiosk_status.py` CLI tool: `--watch`, `--health`, `--overlay on/off`,
    `--heatmap`, `--window`, `--json`.

### Changed
- Section headers use a filled background bar with left accent stripe
  instead of Unicode box-drawing characters (which rendered as missing-
  glyph boxes on the Pi's default font).
- Coverage mini-grid uses a blue → green → yellow → red heat gradient
  normalized to max visit count, replacing flat green-only coloring.

### Fixed
- **FPS drop when overlay enabled** — panel content is rebuilt at most
  every 500ms (2 Hz); cached surface is blitted between rebuilds so
  per-frame cost is a single blit, not dozens of `font.render()` calls.
- **Question-mark boxes in section headers** — replaced Unicode
  box-drawing dashes (`──`) with styled background bars. Unicode
  directional arrows (→↓↘ etc.) are retained and render correctly.

---

## v1.3 — Edge-hugging wanderer + content density mask + 4K display (2026-06-27)

### Added
- **Edge-hugging wanderer algorithm** — viewport never goes past content
  boundary; moves along edges toward next waypoint. Content-dense tour
  ordering (CORE → EDGE → TIP). Eliminated the 76% blank-space spike
  that occurred during tip-tile excursions.
- **Pixel-level content density mask** (`content_mask.npz`, 46 KB) —
  generated offline to avoid OOM on the Pi. Avg density 26.8%. Interior
  tiles 8–18% blank; diamond tip tiles 60–90%+.
- **Content-bounds viewing** — tiles are "viewed" when all actual
  pixel-art content is in the viewport, not when the full tile bounding
  rectangle is centered. Isometric diamonds are only ~34% pixel-art.
- **4K display support** — auto-detects displays wider than 3000px,
  switches X to 1920×1080 via xrandr so the monitor hardware upscales
  to 3840×2160. No software scaling, vsync preserved.
- **Font re-initialization** after `pygame.display.quit()` /
  re-init cycle to fix garbled startup text on 4K.

### Changed
- Coverage threshold lowered to 0.30 (from 0.60) to restrict wandering
  to content-dense interior regions. 1913 safe cells, 25 normal +
  25 tip tiles.

### Performance
- Coverage: 48/50 tiles fully content-viewed. Blank ratio stays 9–22%
  throughout the entire tour (previously spiked to 76%).

---

## v1.2 — Background hologram decoding + graceful tile eviction (2026-06-24)

### Added
- **Background hologram decoding** — hologram video frames decoded in a
  separate thread, eliminating startup stall.
- **Graceful tile eviction** — late tile loads after direction changes
  no longer cause visual artifacts.
- **Priority queue tile loading** — visible tiles load before margin
  tiles.
- **Directional tile prefetch** refinement.

---

## v1.1 — Performance & smoothness (2026-06-22)

### Added
- **16-bit surface conversion** and BMP pre-conversion to eliminate SD
  card swap thrashing.
- **Directional tile prefetch** — loads only tiles ahead of movement
  direction.
- **Pre-applied hologram clip masks** — zero per-frame allocation.
- **System tuning** — swappiness=1, performance CPU governor.

### Performance
- VmSwap=0, 1.5 GB headroom on 4 GB Pi 5.
- Frame timing: p50=33ms, zero spikes above 34ms.
- 30fps render with zero frame spikes.

---

## v1.0 — Initial release (2026-06-20)

### Added
- **Auto-updating tile system** with offline cache fallback.
- **6 hologram scenes** with materialization transitions (12fps).
- **Bare X11 kiosk architecture** — no desktop environment, systemd
  auto-start on cold boot.
- **Display sleep prevention**, journal-only logging.
- **Tested fresh install** on Raspberry Pi 5 (Debian 13 Trixie, 4GB RAM).
