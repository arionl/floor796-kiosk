# Changelog

All notable changes to the Floor796 Kiosk project are documented here.
Tags are cut on `main`; development happens on `dev`.

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
