"""webui.py — the settings & debug web page served by the stats server.

Single embedded HTML document, stdlib only, no external assets.  It
talks to the JSON endpoints on the same server (/settings,
/debug/caches, /debug/clear, /debug/restart, /mode, /viewport,
/overlay, /screenshot, /health, /stats).
"""

from floor796_kiosk import paths

# Each clearable cache: key → (label, paths, what rebuilding costs)
CACHE_TARGETS = {
    "strips": ("Tile animation strips", [paths.STRIP_DIR],
               "re-decodes from freshly fetched tile MP4s "
               "(~8 min at next boot)"),
    "content_mask": ("Content density mask", [paths.CONTENT_MASK_PATH],
                     "rebuilds from strips (~2 min)"),
    "splash": ("Boot splash strip", [paths.SPLASH_CACHE_DIR],
               "re-decodes the bundled splash clip (seconds)"),
    "teleport_trans": ("Teleporter transition beats",
                       [paths.TELEPORT_TRANS_DIR],
                       "re-renders all scene transitions in the "
                       "background at next boot"),
    "thumbnails": ("Label thumbnails", [paths.THUMBNAIL_DIR],
                   "re-fetches thumbnails as objects are highlighted"),
    "tiles": ("Tile cache (full cold boot)", [paths.STRIP_DIR,
                                              paths.TILE_DIR,
                                              paths.TILE_META_PATH,
                                              paths.TILE_STATE_PATH],
              "FULL cold boot: re-downloads and re-decodes every tile "
              "one at a time from the site"),
}
CACHE_ORDER = ("strips", "content_mask", "splash", "teleport_trans",
               "thumbnails", "tiles")


def _dir_size(p):
    """(bytes, file_count) for a file or directory tree."""
    import os
    total, count = 0, 0
    if os.path.isfile(p):
        try:
            return os.path.getsize(p), 1
        except OSError:
            return 0, 0
    if not os.path.isdir(p):
        return 0, 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
                count += 1
            except OSError:
                pass
    return total, count


def cache_report():
    """JSON-ready report of every cache target: size + file count."""
    out = []
    for key in CACHE_ORDER:
        label, pathlist, cost = CACHE_TARGETS[key]
        bytes_ = files = 0
        for p in pathlist:
            b, c = _dir_size(p)
            bytes_ += b
            files += c
        out.append({"key": key, "label": label,
                    "bytes": bytes_, "files": files, "rebuild_cost": cost})
    return out


def clear_caches(targets):
    """Delete the given cache targets.  Returns (freed_bytes, report).

    On Linux, deleting files the player currently has open is safe
    (open handles keep serving the old content); everything is
    rebuilt from source at the next boot.  That is exactly the
    cold-boot simulation this exists for.
    """
    import os
    import shutil
    freed, report = 0, []
    for key in targets:
        if key not in CACHE_TARGETS:
            report.append({"key": key, "error": "unknown cache target"})
            continue
        label, pathlist, _cost = CACHE_TARGETS[key]
        b = f = 0
        for p in pathlist:
            size, count = _dir_size(p)
            freed += size
            b += size
            f += count
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
            except OSError:
                pass
        report.append({"key": key, "label": label,
                       "freed_bytes": b, "files_removed": f})
    return freed, report


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Floor796 Kiosk — Settings &amp; Debug</title>
<style>
  :root {
    --bg: #0b0e14; --panel: #141a24; --panel2: #1b2331;
    --line: #2a3446; --text: #d8dee9; --dim: #8a94a6;
    --accent: #e8b34b; --ok: #6fd08c; --warn: #e8a34b; --bad: #e86b6b;
    --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  header {
    display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap;
    padding: 18px 24px 12px; border-bottom: 1px solid var(--line);
  }
  header h1 { font-size: 19px; margin: 0; letter-spacing: .5px; }
  header h1 em { color: var(--accent); font-style: normal; }
  #health { color: var(--dim); font-family: var(--mono); font-size: 13px; }
  main { max-width: 880px; margin: 0 auto; padding: 20px 24px 60px; }
  section { margin-bottom: 34px; }
  h2 {
    font-size: 13px; text-transform: uppercase; letter-spacing: 1.5px;
    color: var(--dim); margin: 0 0 12px; border-bottom: 1px solid
    var(--line); padding-bottom: 6px;
  }
  .card { background: var(--panel); border: 1px solid var(--line);
          border-radius: 10px; padding: 16px 18px; margin-bottom: 12px; }
  .radio-row { display: flex; align-items: flex-start; gap: 12px;
               padding: 10px 8px; border-radius: 8px; }
  .radio-row:hover { background: var(--panel2); }
  .radio-row.disabled { opacity: .45; }
  .radio-row input { margin-top: 4px; }
  .radio-row .lbl { font-weight: 600; }
  .radio-row .why { color: var(--dim); font-size: 13px; }
  .badge { display: inline-block; font-size: 11px; padding: 1px 8px;
           border-radius: 10px; margin-left: 8px; vertical-align: 1px; }
  .badge.rec { background: #1d3a2a; color: var(--ok); }
  .badge.warn { background: #3a2e1d; color: var(--warn); }
  .badge.no { background: #3a1d1d; color: var(--bad); }
  .hw { color: var(--dim); font-size: 13px; font-family: var(--mono);
        margin: 8px 2px 14px; }
  button {
    background: var(--panel2); color: var(--text); border: 1px solid
    var(--line); border-radius: 8px; padding: 7px 14px; font: inherit;
    cursor: pointer;
  }
  button:hover { border-color: var(--accent); color: var(--accent); }
  button.primary { background: #2a2313; border-color: var(--accent);
                   color: var(--accent); }
  button.danger:hover { border-color: var(--bad); color: var(--bad); }
  button:disabled { opacity: .4; cursor: default; }
  .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th { text-align: left; color: var(--dim); font-weight: 500;
       font-size: 12px; text-transform: uppercase; letter-spacing: 1px;
       padding: 6px 8px; border-bottom: 1px solid var(--line); }
  td { padding: 8px; border-bottom: 1px solid var(--line); }
  td.num { font-family: var(--mono); text-align: right; white-space: nowrap; }
  #msg { position: fixed; left: 50%; bottom: 26px; transform: translateX(-50%);
         background: var(--panel2); border: 1px solid var(--accent);
         color: var(--text); border-radius: 10px; padding: 10px 20px;
         font-size: 14px; display: none; max-width: 80%; }
  #shot { max-width: 100%; border: 1px solid var(--line);
          border-radius: 8px; display: block; margin-top: 10px; }
  .note { color: var(--dim); font-size: 13px; margin-top: 8px; }
  kbd { font-family: var(--mono); background: var(--panel2); padding: 0 5px;
        border-radius: 4px; border: 1px solid var(--line); font-size: 12px; }
</style>
</head>
<body>
<header>
  <h1>Floor796 <em>Kiosk</em></h1>
  <span id="health">loading…</span>
</header>
<main>

<section>
  <h2>Resolution</h2>
  <div class="hw" id="hwinfo"></div>
  <div class="card" id="resoptions"></div>
  <div class="row">
    <button class="primary" id="saveRes">Save resolution</button>
    <span class="note">applies at next player start — use <b>Restart player</b> below</span>
  </div>
</section>

<section>
  <h2>Debug</h2>
  <div class="card">
    <div class="row">
      <span style="color:var(--dim);width:90px">Camera</span>
      <button data-mode="wandering">Wandering</button>
      <button data-mode="navigating">Navigating</button>
      <button data-mode="exploring">Exploring</button>
      <button id="vpResume">Resume tour</button>
    </div>
    <div class="row" style="margin-top:12px">
      <span style="color:var(--dim);width:90px">Overlay</span>
      <button id="ovToggle">Toggle stats overlay</button>
      <button id="ovCycle">Cycle time window</button>
    </div>
    <div class="row" style="margin-top:12px">
      <span style="color:var(--dim);width:90px">Viewport</span>
      <input id="vpx" type="number" value="7127" style="width:90px;
             background:var(--panel2);color:var(--text);border:1px solid
             var(--line);border-radius:6px;padding:5px 8px">
      <input id="vpy" type="number" value="6307" style="width:90px;
             background:var(--panel2);color:var(--text);border:1px solid
             var(--line);border-radius:6px;padding:5px 8px">
      <button id="vpPin">Pin camera (x, y)</button>
    </div>
  </div>
  <div class="card">
    <div class="row">
      <b style="width:90px">Screenshot</b>
      <button id="shotBtn">Refresh</button>
      <span class="note">live viewport capture</span>
    </div>
    <img id="shot" alt="kiosk viewport">
  </div>
</section>

<section>
  <h2>Caches — cold / warm boot simulation</h2>
  <div class="card">
    <table id="caches"><thead><tr>
      <th>Cache</th><th>Files</th><th>Size</th><th>Rebuild cost</th><th></th>
    </tr></thead><tbody></tbody></table>
    <div class="row" style="margin-top:12px">
      <button id="clearSel">Clear selected</button>
      <button class="danger" id="clearAll">Clear ALL (full cold boot)</button>
      <span class="note">clearing is safe while running; everything
      rebuilds at the next start</span>
    </div>
  </div>
</section>

<section>
  <h2>Player</h2>
  <div class="card">
    <div class="row">
      <button class="primary" id="restart">Restart player</button>
      <span class="note">graceful exit — systemd restarts the service
      in ~5 s (needed after a resolution change or cache clear)</span>
    </div>
  </div>
</section>

</main>
<div id="msg"></div>
<script>
"use strict";
const $ = (s) => document.querySelector(s);
let state = { settings: null, caps: null, active: null };

function toast(t, ms=3800) {
  const m = $("#msg"); m.textContent = t; m.style.display = "block";
  clearTimeout(m._t); m._t = setTimeout(() => m.style.display = "none", ms);
}
async function api(path, opts) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.status);
  return j;
}
const fmtBytes = (b) => {
  if (b <= 0) return "0 B";
  const u = ["B","KB","MB","GB"]; const i = Math.min(
    Math.floor(Math.log(b) / Math.log(1024)), 3);
  return (b / 1024**i).toFixed(i ? 1 : 0) + " " + u[i];
};

async function loadHealth() {
  try {
    const h = await api("/health");
    $("#health").textContent =
      `v${h.version || "?"} · up ${Math.floor((h.uptime||0)/60)} min · ` +
      `${h.fps_avg ? h.fps_avg.toFixed(1) : "?"} fps · ` +
      `RSS ${Math.round(h.rss_mb || 0)} MB`;
  } catch (e) { $("#health").textContent = "health unavailable"; }
}

async function loadSettings() {
  try {
    const d = await api("/settings");
    state = d;
    $("#hwinfo").textContent =
      `board RAM ${d.hw.total_mem_mb} MB · display reports ` +
      `${d.hw.detected_w}x${d.hw.detected_h} · rendering ` +
      `${d.active.w}x${d.active.h} (${d.active.source})`;
    const box = $("#resoptions"); box.innerHTML = "";
    d.capabilities.forEach((c) => {
      const row = document.createElement("label");
      row.className = "radio-row" + (c.available ? "" : " disabled");
      const badge = c.recommended
        ? '<span class="badge rec">recommended</span>'
        : (c.value === "4k" && c.available
           ? '<span class="badge warn">RAM below recommendation</span>'
           : (c.available ? "" : '<span class="badge no">unavailable</span>'));
      row.innerHTML =
        `<input type="radio" name="res" value="${c.value}" ` +
        `${c.available ? "" : "disabled"} ` +
        `${d.settings.resolution === c.value ? "checked" : ""}>` +
        `<span><span class="lbl">${c.label}</span>${badge}` +
        `<div class="why">${c.reason} — ${c.detail}</div></span>`;
      box.appendChild(row);
    });
  } catch (e) { toast("settings load failed: " + e.message); }
}

async function loadCaches() {
  try {
    const d = await api("/debug/caches");
    const tb = $("#caches tbody"); tb.innerHTML = "";
    d.caches.forEach((c) => {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td><label><input type="checkbox" data-cache="${c.key}"> ` +
        `${c.label}</label></td>` +
        `<td class="num">${c.files}</td>` +
        `<td class="num">${fmtBytes(c.bytes)}</td>` +
        `<td style="color:var(--dim);font-size:13px">${c.rebuild_cost}</td>` +
        `<td><button class="danger" data-clear="${c.key}">Clear</button></td>`;
      tb.appendChild(tr);
    });
    tb.querySelectorAll("[data-clear]").forEach((b) =>
      b.addEventListener("click", () => doClear([b.dataset.clear])));
  } catch (e) { toast("cache report failed: " + e.message); }
}

async function doClear(targets) {
  const names = targets.join(", ");
  if (!confirm(`Clear ${names}?\nEverything rebuilds at the next start.`))
    return;
  try {
    const d = await api("/debug/clear", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({targets})});
    toast(`Cleared ${names} — freed ${fmtBytes(d.freed_bytes)}. ` +
          `Restart the player to rebuild.`);
    loadCaches();
  } catch (e) { toast("clear failed: " + e.message); }
}

$("#saveRes").addEventListener("click", async () => {
  const sel = document.querySelector('input[name="res"]:checked');
  if (!sel) { toast("pick a resolution first"); return; }
  try {
    const d = await api("/settings", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({resolution: sel.value})});
    toast(`Saved: ${d.settings.resolution} — restart the player to apply`);
  } catch (e) { toast("save failed: " + e.message); }
});

document.querySelectorAll("[data-mode]").forEach((b) =>
  b.addEventListener("click", async () => {
    try { await api("/mode", {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({mode: b.dataset.mode})});
      toast("mode → " + b.dataset.mode);
    } catch (e) { toast("mode failed: " + e.message); }
  }));

$("#vpResume").addEventListener("click", () =>
  api("/viewport", {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({resume: true})})
    .then(() => toast("tour resumed")).catch((e) => toast(e.message)));
$("#vpPin").addEventListener("click", () =>
  api("/viewport", {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({x: +$("#vpx").value, y: +$("#vpy").value})})
    .then(() => toast("camera pinned")).catch((e) => toast(e.message)));
$("#ovToggle").addEventListener("click", async () => {
  try {
    const st = await api("/stats");
    const d = await api("/overlay", {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({enabled: !st.overlay_enabled})});
    toast("overlay " + (d.overlay ? "on" : "off"));
  } catch (e) { toast(e.message); }
});
$("#ovCycle").addEventListener("click", () =>
  api("/overlay/window", {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({cycle: true})})
    .then((d) => toast("window: " + d.overlay_window)).catch((e) => toast(e.message)));

$("#shotBtn").addEventListener("click", () =>
  { $("#shot").src = "/screenshot?" + Date.now(); });
$("#shot").src = "/screenshot";

$("#clearSel").addEventListener("click", () => {
  const t = [...document.querySelectorAll("[data-cache]:checked")]
    .map((c) => c.dataset.cache);
  if (!t.length) { toast("select at least one cache"); return; }
  doClear(t);
});
$("#clearAll").addEventListener("click", () =>
  doClear(["strips", "content_mask", "splash", "teleport_trans",
           "thumbnails", "tiles"]));

$("#restart").addEventListener("click", async () => {
  if (!confirm("Restart the player? (display shows the boot splash " +
               "for a few seconds; up to ~1 min on a cold rebuild)"))
    return;
  try {
    await api("/debug/restart", {method: "POST"});
    toast("restart requested — page will reload in 15 s", 12000);
    setTimeout(() => location.reload(), 15000);
  } catch (e) { toast("restart failed: " + e.message); }
});

loadHealth(); loadSettings(); loadCaches();
setInterval(loadHealth, 10000);
</script>
</body>
</html>
"""
