#!/usr/bin/env python3
"""
ThumbnailCache — fetches, resizes, and caches thumbnails for the
object highlighter.

Link types handled:
  - Direct images (floor796.com/data/misc/*.jpg, imgur, etc.)
  - YouTube videos (watch?v= or youtu.be) → mqdefault thumbnail
  - Compound links (any part order: image||audio, audio||image,
    event://||no-click||image, ...) → best thumbnail-capable part
  - img:// relative paths → prepend floor796.com base URL
  - tenor.com pages → first media.tenor.com GIF on the page
  - fandom.com wikis → lead image via MediaWiki api.php pageimages
    (page HTML 403s non-browser user agents)
  - Other web pages → og:image / twitter:image / first <img>
  - Wikipedia → REST API summary (thumbnail + extract text)
  - Everything else → no thumbnail (returns None)

classify_link() is the single source of truth for link classification
and thumbnail URL resolution — it never does network I/O; page/API
URLs are prefixed ('page:', 'fandomapi:', 'wiki://api:') and resolved
by the fetch worker.  The prefetch tool uses the same URLs as cache
keys, so prefetched files are found at runtime.

Thumbnails are cached to disk as 320px-wide PNGs.  On cache hit,
loading is instant from disk; on miss, a background thread fetches
and processes the image.
"""

import hashlib
import io
import json
import logging
import os
import re
import threading
import urllib.parse
import urllib.request
from collections import OrderedDict

import pygame

log = logging.getLogger("floor796")

BASE_URL = "https://floor796.com"
THUMB_W = 320          # target width in pixels
THUMB_H = 200          # target height in pixels
from floor796_kiosk.paths import THUMBNAIL_DIR as CACHE_DIR
MAX_FETCH_WORKERS = 2  # concurrent fetch threads
REQUEST_TIMEOUT = 8    # seconds

# YouTube thumbnail quality: mqdefault = 320×180, hqdefault = 480×360
YT_THUMB_FMT = "https://img.youtube.com/vi/{vid}/mqdefault.jpg"

# Regex to extract YouTube video ID from various URL formats
_YT_PATTERNS = [
    re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]{11})"),
    re.compile(r"youtube\.com/embed/([\w-]{11})"),
]


def classify_link(link):
    """Classify a changelog 'l' field into a link type.

    Returns (link_type, thumb_url) where link_type is one of:
      'youtube', 'image', 'video', 'wiki', 'web', 'interactive', 'none'
    and thumb_url is the URL to fetch for the thumbnail (or None).

    For 'web' pages a thumb_url is now returned as a hint: the page URL
    itself (with 'page:' prefix), to be resolved by the fetch worker via
    og:image/first-image extraction.  classify_link itself never does
    network I/O.
    """
    if not link:
        return "none", None

    # Compound links: "a||b[||c...]" — parts in ANY order (audio first,
    # event first, 'no-click' filler, then the image, etc.).  Pick the
    # first part that can produce a thumbnail; remember the type if a
    # later part is richer (image > youtube > video > web).
    if "||" in link:
        parts = [p.strip() for p in link.split("||")]
        best = None
        for part in parts:
            if not part or part == "no-click":
                continue
            lt, tu = classify_link(part)
            if lt == "none":
                continue
            if tu is None and lt != "web":
                # non-image part (audio/event) — nothing fetchable here
                continue
            rank = {"image": 0, "youtube": 1, "video": 2, "web": 3}.get(lt, 9)
            if best is None or rank < best[2]:
                best = (lt, tu, rank)
        if best:
            return best[0], best[1]
        return "none", None

    # Special protocols — interactive://, event://, play-loop://
    if "://" in link and not link.startswith("http"):
        if link.startswith("img://"):
            # img://path → prepend base URL
            path = link[len("img://"):]
            return "image", f"{BASE_URL}/{path}"
        return "interactive", None

    # YouTube
    for pat in _YT_PATTERNS:
        m = pat.search(link)
        if m:
            vid = m.group(1)
            return "youtube", YT_THUMB_FMT.format(vid=vid)

    # Direct video files (mp4/webm) — extract a frame via ffmpeg
    if any(link.lower().endswith(ext) for ext in (".mp4", ".webm", ".mov")):
        url = link
        if url.startswith("//"):
            url = "https:" + url
        return "video", url

    # Direct images
    if any(link.lower().endswith(ext) for ext in
           (".jpg", ".jpeg", ".png", ".gif", ".webp")):
        url = link
        if url.startswith("//"):
            url = "https:" + url
        return "image", url

    # Wikipedia — try to get the lead image via REST API
    if "wikipedia.org" in link:
        thumb_url = _wikipedia_thumb_url(link)
        if thumb_url:
            return "wiki", thumb_url
        return "wiki", None

    # wikireading.ru — no API; og:image extraction at fetch time
    if "wikireading.ru" in link:
        return "web", "page:" + link

    # tenor.com — the main GIF is the first media.tenor.com <img> on the
    # page (no og:image).  Fetched via page-image extraction at fetch time.
    if "tenor.com" in link:
        return "web", "page:" + link

    # Fandom wikis (bleach.fandom.com, etc.) — MediaWiki PageImages gives
    # the curated lead image; page HTML 403s non-browser UAs.  Resolved
    # at fetch time via the api.php pageimages prop.
    if "fandom.com" in link:
        return "wiki", "fandomapi:" + link

    # Other web links — resolve via og:image / first image at fetch time
    if link.startswith("http"):
        return "web", "page:" + link

    return "none", None

# Wikipedia REST API summary endpoint.
# Returns JSON with 'thumbnail' and 'extract' fields.
_WP_API_FMT = "https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}"
_WP_THUMB_SIZE = 320  # request 320px-wide thumbnail


def _wikipedia_thumb_url(wiki_url):
    """Extract the article title and lang from a Wikipedia URL, then
    construct a REST API thumbnail URL.

    Returns the thumbnail source URL, or None if the URL can't be parsed.
    The actual fetch happens later (returns URL only, no network call here).
    """
    # Parse: https://en.wikipedia.org/wiki/Herrerasaurus
    #        https://ru.wikipedia.org/wiki/Ждун (URL-encoded)
    m = re.match(
        r"https?://([a-z]+)\.wikipedia\.org/wiki/(.+)$", wiki_url)
    if not m:
        return None
    lang = m.group(1)
    title = urllib.parse.unquote(m.group(2))
    # Use the REST API to get the thumbnail URL.
    # We return a special URL that _fetch_worker knows to resolve.
    api_url = _WP_API_FMT.format(
        lang=lang,
        title=urllib.parse.quote(title))
    return f"wiki://api:{api_url}"


def _cache_key(url):
    """Generate a cache filename from a URL."""
    return hashlib.md5(url.encode()).hexdigest() + ".png"


def _load_image_bytes(raw):
    """Decode raw image bytes into a pygame Surface.

    Fast path: pygame's SDL_image (JPEG/PNG/GIF/BMP).  Fallback: Pillow
    (WEBP/AVIF and other formats SDL_image can't handle), converted to
    raw RGB(A) bytes.  Returns None if both fail.
    """
    try:
        surf = pygame.image.load(io.BytesIO(raw))
        # Paletted (8-bit) GIFs/PNGs can't be smoothly scaled — promote
        # to 32-bit.  convert(32) with an explicit depth does not
        # reference the display surface.
        if surf.get_bitsize() < 24:
            surf = surf.convert(32)
        return surf
    except pygame.error:
        pass
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw))
        img.load()
        if img.mode in ("P", "L", "LA"):
            img = img.convert("RGBA")
        if img.mode == "RGBA":
            surf = pygame.image.frombytes(img.tobytes(), img.size, "RGBA")
        else:
            if img.mode != "RGB":
                img = img.convert("RGB")
            surf = pygame.image.frombytes(img.tobytes(), img.size, "RGB")
        return surf
    except Exception as e:
        log.debug("ThumbnailCache: image decode failed: %s", e)
        return None


def _fetch_url(url, timeout=REQUEST_TIMEOUT):
    """Fetch raw bytes from a URL."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Floor796-Kiosk)"
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ── Web page image extraction (og:image / twitter:image / first <img>) ──────

_OG_IMAGE_RE = re.compile(
    r'<meta\s+(?:property|name)=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
    re.IGNORECASE)
_OG_IMAGE_RE_REV = re.compile(
    r'<meta\s+content=["\']([^"\']+)["\']\s+(?:property|name)=["\']og:image["\']',
    re.IGNORECASE)
_TWITTER_IMAGE_RE = re.compile(
    r'<meta\s+(?:property|name)=["\']twitter:image["\']\s+content=["\']([^"\']+)["\']',
    re.IGNORECASE)
_TWITTER_IMAGE_RE_REV = re.compile(
    r'<meta\s+content=["\']([^"\']+)["\']\s+(?:property|name)=["\']twitter:image["\']',
    re.IGNORECASE)
_IMG_TAG_RE = re.compile(
    r'<img\s[^>]*src=["\']([^"\']+)["\']', re.IGNORECASE)

# media.tenor.com main-content GIFs; inline JSON also embeds itemurl/imageurl
_TENOR_GIF_RE = re.compile(
    r'https?://media[0-9]?\.tenor\.com/[^"\'\s<>]+?\.gif')


def _tenor_image_url(text):
    """Extract the main GIF URL from a tenor.com view page.

    The page embeds per-item JSON with variant URLs; slashes are
    escaped as \\u002F.  The FIRST 'tinygif' in the document belongs to
    the main item (subsequent ones are recommendations) and is ~220px
    / ~100-500KB — ideal thumbnail source (the full GIF can be 7MB+).
    Falls back to the first media.tenor.com GIF in document order.
    """
    for m in re.finditer(r'tinygif', text):
        seg = text[m.end():m.end() + 250]
        u = re.search(
            r'https?:(?:\\u002F|/)(?:\\u002F|/)'
            r'media[0-9]?\.tenor\.com'
            r'(?:\\u002F|[^"\'\s<>\\])+',
            seg)
        if u:
            return u.group(0).replace("\\u002F", "/")
    m = _TENOR_GIF_RE.search(text)
    return m.group(0) if m else None


def _resolve_relative(url, base):
    """Resolve a possibly-relative image URL against a page URL."""
    if url.startswith("//"):
        return "https:" + url
    if not url.startswith(("http://", "https://")):
        return urllib.parse.urljoin(base.split("?")[0].split("#")[0], url)
    return url


# Analytics/tracking pixel domains — never legitimate thumbnails
_TRACKER_URL_RE = re.compile(
    r'(?:mc\.yandex\.(?:ru|com)|an\.yandex\.(?:ru|com)|'
    r'google-analytics\.com|googletagmanager\.com|'
    r'c\.bing\.net|facebook\.com/tr|px\.ads\.linkedin\.com)',
    re.IGNORECASE)


def _extract_page_image(html, page_url, is_tenor=False):
    """Extract the main image URL from a web page's HTML.

    Order: og:image → twitter:image → (tenor: first media.tenor.com GIF) →
    first non-SVG <img>.  Tracker/analytics pixels are skipped.
    Returns an absolute URL or None.
    """
    try:
        text = html.decode("utf-8", errors="replace")
    except Exception:
        text = html.decode("latin-1", errors="replace")

    for pat in (_OG_IMAGE_RE, _OG_IMAGE_RE_REV,
                _TWITTER_IMAGE_RE, _TWITTER_IMAGE_RE_REV):
        m = pat.search(text)
        if m and m.group(1):
            return _resolve_relative(m.group(1), page_url)

    if is_tenor:
        url = _tenor_image_url(text)
        if url:
            return url

    # Fallback: first non-SVG <img> (site logos/icons are SVGs,
    # analytics pixels live on tracker domains)
    for m in _IMG_TAG_RE.finditer(text):
        src = m.group(1)
        if src.lower().split("?")[0].endswith(".svg"):
            continue
        # data: URIs are not fetchable thumbnails
        if src.startswith("data:"):
            continue
        if _TRACKER_URL_RE.search(src):
            continue
        return _resolve_relative(src, page_url)

    return None


def _fetch_page_image(page_url, timeout=REQUEST_TIMEOUT):
    """Fetch a web page and return the main image's raw bytes, or None."""
    try:
        html = _fetch_url(page_url, timeout=timeout)
    except Exception as e:
        log.debug("ThumbnailCache: page fetch failed for %s: %s", page_url, e)
        return None
    img_url = _extract_page_image(html, page_url,
                                  is_tenor="tenor.com" in page_url)
    if not img_url:
        return None
    try:
        return _fetch_url(img_url, timeout=timeout)
    except Exception as e:
        log.debug("ThumbnailCache: page image fetch failed for %s: %s",
                  img_url, e)
        return None


# ── Fandom wiki lead image via MediaWiki api.php (page HTML 403s bots) ──────

def _fetch_fandom_image(fandom_url, timeout=REQUEST_TIMEOUT):
    """Fetch a Fandom wiki page's lead image via the MediaWiki API.

    Fandom serves MediaWiki, so api.php?action=query&prop=pageimages
    returns the curated lead image ("most relevant") at a requested
    size.  The wiki page itself returns 403 to non-browser user agents,
    so the API is the reliable route.
    """
    try:
        parsed = urllib.parse.urlparse(fandom_url)
        wiki = f"{parsed.scheme}://{parsed.netloc}"
        title = urllib.parse.unquote(parsed.path.split("/wiki/")[-1])
        if not title or title == parsed.path:
            return None
        q = urllib.parse.urlencode({
            "action": "query", "format": "json", "formatversion": "2",
            "titles": title, "prop": "pageimages", "pithumbsize": "640",
        })
        data = json.loads(_fetch_url(f"{wiki}/api.php?{q}", timeout=timeout))
        pages = data.get("query", {}).get("pages", [])
        if not pages:
            return None
        thumb = pages[0].get("thumbnail") or {}
        src = thumb.get("source")
        if not src:
            return None
        return _fetch_url(src, timeout=timeout)
    except Exception as e:
        log.debug("ThumbnailCache: fandom api fetch failed for %s: %s",
                  fandom_url, e)
        return None


def _extract_video_frame(url, timeout=REQUEST_TIMEOUT):
    """Fetch a thumbnail from a video URL using ffmpeg.

    Downloads the video to a temp file, extracts a representative frame,
    and returns it as raw image bytes (PNG).
    """
    import subprocess
    import tempfile

    # Download video to temp file
    raw = _fetch_url(url, timeout=timeout * 2)
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name

    try:
        out_path = tmp_path.replace(".mp4", "_frame.png")

        # Strategy: seek to ~1 second in and grab a frame.
        # Seeking first (-ss before -i) is fast (no full decode).
        # If that fails (e.g., video <1s), fall back to first frame.
        for seek_args in [
            ["-ss", "00:00:01", "-i", tmp_path],
            ["-i", tmp_path],  # no seek — first frame
        ]:
            try:
                result = subprocess.run(
                    ["ffmpeg", "-y"] + seek_args +
                    ["-frames:v", "1", "-vf", "scale=320:-1",
                     "-f", "image2", out_path],
                    capture_output=True, timeout=10
                )
                if result.returncode == 0 and os.path.exists(out_path):
                    break
            except subprocess.TimeoutExpired:
                continue

        if os.path.exists(out_path):
            with open(out_path, "rb") as f:
                return f.read()
        return None
    except Exception as e:
        log.debug("ThumbnailCache: ffmpeg frame extraction failed for %s: %s",
                  url, e)
        return None
    finally:
        for p in (tmp_path, tmp_path.replace(".mp4", "_frame.png")):
            try:
                os.remove(p)
            except OSError:
                pass


def _resize_cover(src_surf, target_w, target_h):
    """Resize a pygame Surface to fit target dimensions.

    Uses cover-crop (fill target, crop overflow) when the source aspect
    ratio is close to the target.  When the aspect ratios differ
    significantly, uses letterbox/pillarbox (fit entirely, fill margins
    with black) to avoid chopping important content from very wide or
    very tall images.
    """
    sw, sh = src_surf.get_size()
    if sw == 0 or sh == 0:
        return None

    src_ratio = sw / sh
    target_ratio = target_w / target_h

    if abs(src_ratio - target_ratio) < 0.2:
        # Close enough — cover-crop
        scale = max(target_w / sw, target_h / sh)
        new_w = int(sw * scale)
        new_h = int(sh * scale)
        scaled = pygame.transform.smoothscale(src_surf, (new_w, new_h))
        crop_x = (new_w - target_w) // 2
        crop_y = (new_h - target_h) // 2
        result = pygame.Surface((target_w, target_h))
        result.blit(scaled, (-crop_x, -crop_y))
        return result
    else:
        # Aspect ratios differ significantly — letterbox
        scale = min(target_w / sw, target_h / sh)
        new_w = max(1, int(sw * scale))
        new_h = max(1, int(sh * scale))
        scaled = pygame.transform.smoothscale(src_surf, (new_w, new_h))
        result = pygame.Surface((target_w, target_h))
        result.fill((15, 15, 20))  # dark background matching panel
        offset_x = (target_w - new_w) // 2
        offset_y = (target_h - new_h) // 2
        result.blit(scaled, (offset_x, offset_y))
        return result


class ThumbnailCache:
    """Disk + memory cache for thumbnails with background fetching.

    Usage:
        cache = ThumbnailCache(cache_dir="thumbs")
        surface = cache.get(obj_id, link)  # may return None if loading
        # ... next frame ...
        surface = cache.get(obj_id, link)  # now returns the surface
    """

    def __init__(self, cache_dir=CACHE_DIR, max_mem=80):
        self._cache_dir = cache_dir
        self._max_mem = max_mem  # max surfaces in RAM
        self._surfaces = OrderedDict()  # obj_id -> (pygame.Surface, converted)
        self._loading = set()  # obj_ids currently being fetched
        self._failed = set()  # obj_ids that failed to load
        self._lock = threading.Lock()
        self._thread_pool = []
        self._extracts = {}  # obj_id -> Wikipedia extract text

        os.makedirs(cache_dir, exist_ok=True)

    def get(self, obj_id, link):
        """Get a thumbnail surface for an object.

        Returns:
          - pygame.Surface if thumbnail is ready
          - None if still loading, failed, or no thumbnail available
        """
        link_type, thumb_url = classify_link(link)

        # No thumbnail possible for this link type
        if thumb_url is None:
            return None

        with self._lock:
            # Already in memory?
            if obj_id in self._surfaces:
                self._surfaces.move_to_end(obj_id)
                surf, converted = self._surfaces[obj_id]
                if not converted:
                    # Surface was stored by background thread — convert now
                    try:
                        surf = surf.convert_alpha()
                    except pygame.error:
                        pass  # display not ready yet
                    self._surfaces[obj_id] = (surf, True)
                return self._surfaces[obj_id][0]

            # Previously failed?
            if obj_id in self._failed:
                return None

            # Currently loading?
            if obj_id in self._loading:
                return None

        # Check disk cache
        cache_file = os.path.join(self._cache_dir, _cache_key(thumb_url))
        if os.path.exists(cache_file):
            try:
                surf = pygame.image.load(cache_file)
                # Convert on main thread where display is available
                surf = surf.convert_alpha()
                self._store(obj_id, surf, converted=True)
                return surf
            except Exception as e:
                log.debug("ThumbnailCache: failed to load cached %s: %s",
                          cache_file, e)

        # Not cached — start background fetch
        self._start_fetch(obj_id, thumb_url, cache_file)
        return None

    def _store(self, obj_id, surf, converted=False):
        """Store a surface in the memory cache, evicting if needed.

        'converted' tracks whether convert_alpha() has been called yet.
        Background threads store raw surfaces (converted=False); the main
        thread converts lazily on first get().
        """
        with self._lock:
            self._surfaces[obj_id] = (surf, converted)
            self._surfaces.move_to_end(obj_id)
            while len(self._surfaces) > self._max_mem:
                self._surfaces.popitem(last=False)

    def _start_fetch(self, obj_id, url, cache_file):
        """Start a background thread to fetch and process a thumbnail."""
        with self._lock:
            if obj_id in self._loading:
                return
            self._loading.add(obj_id)

        t = threading.Thread(
            target=self._fetch_worker,
            args=(obj_id, url, cache_file),
            daemon=True,
        )
        t.start()
        self._thread_pool.append(t)
        # Clean up dead threads occasionally
        self._thread_pool = [t for t in self._thread_pool if t.is_alive()]

    def _fetch_worker(self, obj_id, url, cache_file):
        """Background fetch: download image, resize, save to disk, cache."""
        # Pin this thread to slow cores on big.LITTLE SoCs (OrangePi 5 Max).
        # No-op on homogeneous SoCs like the Raspberry Pi 5.
        from floor796_kiosk.cpu_affinity import pin_background_thread
        pin_background_thread("thumb_fetch")

        try:
            link_type, _ = classify_link(url)

            if link_type == "video":
                # Extract a frame from the video using ffmpeg
                frame_bytes = _extract_video_frame(url)
                if frame_bytes is None:
                    raise ValueError("video frame extraction failed")
                src_surf = pygame.image.load(io.BytesIO(frame_bytes))

            elif url.startswith("page:"):
                # Web page (tenor.com and general links) — og:image /
                # first-image extraction
                img_raw = _fetch_page_image(url[len("page:"):])
                if img_raw is None:
                    raise ValueError("no page image found")
                src_surf = _load_image_bytes(img_raw)

            elif url.startswith("fandomapi:"):
                # Fandom wiki — lead image via MediaWiki api.php
                img_raw = _fetch_fandom_image(url[len("fandomapi:"):])
                if img_raw is None:
                    raise ValueError("no fandom image found")
                src_surf = _load_image_bytes(img_raw)

            elif url.startswith("wiki://api:"):
                # Wikipedia: fetch REST API summary, get thumbnail + extract
                api_url = url[len("wiki://api:"):]
                raw = _fetch_url(api_url)
                data = json.loads(raw)

                # Store the text extract for this object
                extract = data.get("extract", "")
                if extract:
                    with self._lock:
                        self._extracts[obj_id] = extract

                # Get the thumbnail image URL
                thumb_data = data.get("thumbnail") or {}
                thumb_src = thumb_data.get("source")
                if not thumb_src:
                    raise ValueError("no Wikipedia thumbnail")

                img_raw = _fetch_url(thumb_src)
                src_surf = pygame.image.load(io.BytesIO(img_raw))

            else:
                raw = _fetch_url(url)
                src_surf = pygame.image.load(io.BytesIO(raw))

            # Resize to fit 320×200
            thumb = _resize_cover(src_surf, THUMB_W, THUMB_H)
            if thumb is None:
                raise ValueError("resize failed")

            # Save to disk cache
            try:
                pygame.image.save(thumb, cache_file)
            except Exception as e:
                log.debug("ThumbnailCache: save failed for %s: %s", obj_id, e)

            # Store raw surface (no convert_alpha in background thread —
            # it requires the video subsystem and may fail or corrupt
            # state when called from a non-main thread).  convert_alpha
            # is deferred to the main thread in get().
            self._store(obj_id, thumb)
            log.debug("ThumbnailCache: fetched thumbnail for obj %s from %s",
                      obj_id, url)

        except Exception as e:
            log.debug("ThumbnailCache: fetch failed for obj %s (%s): %s",
                      obj_id, url, e)
            with self._lock:
                self._failed.add(obj_id)
        finally:
            with self._lock:
                self._loading.discard(obj_id)

    def prefetch(self, obj_id, link):
        """Prefetch a thumbnail without needing the surface yet."""
        self.get(obj_id, link)

    def get_extract(self, obj_id):
        """Return cached Wikipedia extract text for an object, or None."""
        with self._lock:
            return self._extracts.get(obj_id)

    def has_failed(self, obj_id):
        """True if a fetch was attempted for this object and failed."""
        with self._lock:
            return obj_id in self._failed

    def get_link_type(self, link):
        """Return the link type string for rendering indicators."""
        link_type, _ = classify_link(link)
        return link_type
