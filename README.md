# Floor796 Kiosk

A self-contained animated pixel-art kiosk that displays the
[floor796.com](https://floor796.com) interactive isometric map on a dedicated
display — designed for Raspberry Pi 5 (4 GB+).

The player boots from cold-start, automatically pans across the full animated
scene ensuring every tile is visited, and keeps the display on 24/7 with no
screensaver or sleep.  When the floor796 author publishes new tiles, they are
automatically downloaded and incorporated on the next boot.

![Floor796 Kiosk](docs/screenshot.png)

---

## Features

- **Full-resolution pixel art** — 1024×820 tiles rendered at native resolution
  (no scaling artifacts).
- **Coverage-weighted wandering** — a visit heat map ensures all 50+ animated
  tiles are toured evenly; a blank-ratio guard keeps the viewport on content.
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

| Component          | Specification                          |
|--------------------|----------------------------------------|
| Hardware           | Raspberry Pi 5 (4 GB minimum)          |
| OS                 | Raspberry Pi OS (Bookworm or Trixie)   |
| Display            | HDMI (1920×1080 or 1920×1200)          |
| Network            | Internet for initial download + updates|
| Storage            | 4 GB free (tiles + decoded strips)    |

---

## Quick Start (Fresh Pi 5)

```bash
# 1. Clone or copy this repo to the Pi
git clone <repo-url> /tmp/floor796-kiosk
cd /tmp/floor796-kiosk

# 2. Run the installer (requires root)
sudo bash install.sh

# 3. Reboot to test cold-boot auto-start
sudo reboot
```

The first boot downloads ~123 MB of tiles and decodes them to frame strips
(~3 minutes).  Subsequent boots take ~20 seconds.

---

## File Structure

```
floor796-kiosk/
├── kiosk_player.py              # Main player (rendering + wandering)
├── tile_manager.py              # Tile download + auto-update logic
├── run.sh                       # Boot wrapper (starts bare X server)
├── install.sh                   # One-shot installer for fresh Raspbian
├── floor796-kiosk.service       # systemd unit (cold-boot auto-start)
├── tiles/                       # Downloaded MP4 tiles (gitignored)
├── strips/                      # Decoded frame strips (gitignored)
├── tiles_meta.json              # Grid metadata (auto-generated)
├── README.md                    # This file
└── .gitignore
```

---

## How It Works

### Tile System

Floor796's map is a grid of 1024×820 pixel tiles.  Most tiles are static
(single-frame), but ~50 tiles in the center are animated (60-frame loops at
12 fps).  The player downloads these as MP4s from the CDN, decodes them to
full-resolution PNG frame strips using ffmpeg, and caches them in `strips/`.

Tiles are spaced at 1016×812 intervals (8 px overlap per axis), matching the
floor796.com front-end.  This overlap is critical for pixel-perfect alignment.

### Wandering Algorithm

The `Wanderer` class implements coverage-weighted waypoint navigation:

1. **Visit heat map** — counts how many frames each animated tile has been
   visible in the viewport.
2. **Waypoint scoring** — least-visited tiles get the lowest score (highest
   priority); anti-oscillation penalties prevent ping-ponging between adjacent
   tiles.
3. **Blank-ratio guard** — positions where the viewport would be more than 25%
   static content get a large penalty, keeping the camera on animated areas.
4. **Smooth steering** — gradual angle interpolation toward the next waypoint
   with momentum blending for natural-looking movement.
5. **Dynamic timeout** — far tiles get longer timeouts based on distance and
   speed (`distance / speed * 1.8`), ensuring the full scene is reachable.

Full coverage of all animated tiles is typically achieved in ~25 minutes.

### Auto-Update

At startup, `tile_manager.py` fetches `matrix.json` from floor796.com to check
for new or changed tiles.  If the network is unavailable, it silently falls
back to the existing cache — the kiosk always boots, online or offline.

---

## Configuration

The player can be configured via command-line arguments (see `run.sh`) or by
editing constants at the top of `kiosk_player.py`:

| Setting             | Default | Description                              |
|---------------------|---------|------------------------------------------|
| `DEFAULT_WIDTH`     | 0       | Display width (0 = auto-detect)          |
| `DEFAULT_HEIGHT`    | 0       | Display height (0 = auto-detect)         |
| `SCALE`             | 1.0     | Tile scale (must be 1.0 — see warnings) |
| `DEFAULT_WANDER_SPEED` | 15.0 | Pan speed in pixels/sec                  |
| `CACHE_MARGIN`      | 2       | Extra tile ring to prefetch              |
| `COVERAGE_LOG_INTERVAL` | 300 | Seconds between coverage log lines     |

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
| Space           | Toggle auto-wandering      |
| Arrow keys      | Pan manually               |
| Mouse drag      | Pan manually               |
| V               | Print coverage stats       |
| ESC             | Quit (service will restart)|
| F               | Toggle fullscreen          |

---

## Troubleshooting

### Black screen on boot

- Check HDMI cable and that the display is powered on.
- Check logs: `journalctl -u floor796-kiosk -b`
- Ensure `hdmi_force_hotplug=1` is set in `/boot/firmware/config.txt`.
- Try a different `hdmi_group` / `hdmi_mode` in config.txt.

### Player crashes / restarts repeatedly

- Check logs: `journalctl -u floor796-kiosk --no-pager -n 100`
- Ensure the venv has pygame installed: `ls /opt/floor796-kiosk/venv/bin/python`
- Check available memory: `free -h` (needs ~2 GB free).
- Ensure tiles are downloaded: `ls /opt/floor796-kiosk/tiles/*.mp4 | wc -l`

### Display goes to sleep

- The installer disables DPMS at the X server level (`-dpms`, `-s 0`).
- Also check `/boot/firmware/config.txt` for `hdmi_blanking=1`.
- Some displays have their own sleep timer — check the monitor's OSD menu.

### Tiles not updating

- The player checks for updates at every startup.  If offline, it uses cache.
- To force a manual update: `sudo -u kiosk /opt/floor796-kiosk/venv/bin/python /opt/floor796-kiosk/tile_manager.py`

---

## Performance

| Metric              | Value (Pi 5, 4 GB)              |
|---------------------|---------------------------------|
| Render rate         | 60 fps (vsync)                 |
| Animation rate      | 12 fps (60-frame, 5s loop)     |
| Memory (RSS)        | ~1.9 GB                        |
| Swap                | 0 MB                           |
| CPU                 | ~83% (one core)                |
| Cold-boot to display| ~20s (warm), ~3 min (first run)|
| Full coverage       | ~25 minutes                    |

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
