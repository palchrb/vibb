/* Vibb parent PWA — a thin client of the vibbd API (same origin). */
"use strict";

const $ = (sel) => document.querySelector(sel);

/* The box token. Privileged endpoints (settings, Wi-Fi, Bluetooth,
   shutdown) need it; playback and reads never do, so an unlinked phone
   still controls the music. Provisioned by scanning the QR on the box
   screen — see SECURITY.md. Stored per ORIGIN, so a box reached by a new
   IP (or via the setup hotspot) needs one more scan. */
const TOKEN_KEY = "vibb.token";

/* localStorage can be absent or throw (Safari private browsing, storage
   disabled). Degrade to a session-only token rather than breaking the
   whole app on load — playback must never depend on storage. */
function storeGet(k) {
  try { return localStorage.getItem(k) || ""; } catch (e) { return ""; }
}
function storeSet(k, v) {
  try {
    if (v) localStorage.setItem(k, v); else localStorage.removeItem(k);
  } catch (e) { /* session-only for this tab */ }
}

let TOKEN = storeGet(TOKEN_KEY);

function normToken(raw) {
  return String(raw || "").toUpperCase().replace(/[^0-9A-Z]/g, "")
    .replace(/[IL]/g, "1").replace(/O/g, "0").replace(/U/g, "V");
}

function setToken(raw) {
  TOKEN = normToken(raw);
  storeSet(TOKEN_KEY, TOKEN);
  renderLinkState();
}

function renderLinkState() {
  const el = $("#link-state");
  if (el) {
    el.textContent = TOKEN ? "✓ Linked to the box"
                           : "Not linked — playback only";
  }
  const input = $("#set-token");
  if (input && !input.matches(":focus")) input.value = TOKEN;
}

/* A persistent banner, not a toast: "you must go to the box" is not a
   message to blink for two seconds. */
function linkBanner(show) {
  let el = $("#link-banner");
  if (!show) { if (el) el.remove(); return; }
  if (el) return;
  el = document.createElement("div");
  el.id = "link-banner";
  el.className = "banner";
  el.textContent = "This phone isn't linked to the box — on the box: " +
    "Settings → Link phone";
  document.body.prepend(el);
}

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  // Sent on every request: SAFE endpoints ignore it, and one branch
  // beats keeping a copy of the box's endpoint classification here
  // (which would drift the first time the box adds a route).
  if (TOKEN) headers["X-Vibb-Token"] = TOKEN;
  const r = await fetch(path, {
    ...opts,
    headers: { ...headers, ...(opts.headers || {}) },
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  });
  if (!r.ok) {
    let msg = r.statusText, code = "";
    try {
      const j = await r.json();
      msg = j.error || msg;
      code = j.code || "";
    } catch (e) { /* not json */ }
    if (r.status === 401) {
      // A rotated/stale token is worse than none: drop it so the UI says
      // "link" rather than silently failing every privileged action.
      if (code === "token_invalid") setToken("");
      linkBanner(true);
      const err = new Error("This phone isn't linked to the box");
      err.code = code || "token_required";
      throw err;
    }
    throw new Error(msg);
  }
  linkBanner(false);
  return r.json();
}

function toast(msg, ms = 2500) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.hidden = true; }, ms);
}

/* --- tabs ---------------------------------------------------------------- */

document.querySelectorAll("nav button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach((b) =>
      b.classList.toggle("active", b === btn));
    ["player", "library", "settings"].forEach((t) => {
      $(`#tab-${t}`).hidden = t !== btn.dataset.tab;
    });
    if (btn.dataset.tab === "library") { loadLibrary(); loadStorytelStatus(); }
    if (btn.dataset.tab === "settings") {
      loadSettings(); loadSystem(); loadBt(); loadStorytelStatus();
    }
  });
});

/* --- player -------------------------------------------------------------- */

function fmtTime(s) {
  if (s == null) return "–";
  s = Math.floor(s);
  const m = Math.floor(s / 60) % 60, h = Math.floor(s / 3600);
  const mm = h ? String(m).padStart(2, "0") : m;
  return (h ? `${h}:` : "") + `${mm}:${String(s % 60).padStart(2, "0")}`;
}

let volTouched = 0;

// Last polled playback state; a local 500ms ticker interpolates the
// progress display between the 2s polls so seconds count up smoothly.
let np = { position: null, duration: null, playing: false, at: 0 };

function renderProgress() {
  let pos = np.position;
  if (pos != null && np.playing) pos += (Date.now() - np.at) / 1000;
  if (pos != null && np.duration != null) pos = Math.min(pos, np.duration);
  const frac = pos && np.duration ? Math.min(1, pos / np.duration) : 0;
  $("#np-bar").style.width = `${frac * 100}%`;
  $("#np-pos").textContent = fmtTime(pos);
  $("#np-dur").textContent =
    np.position != null && np.duration == null ? "live" : fmtTime(np.duration);
}

let queueTarget;   // undefined = never loaded; null = no queue
let queueRetried = null;   // target already re-fetched once while pending
let currentTarget = null;  // what /status says is (or would be) playing

async function loadQueue(target) {
  queueTarget = target;
  const card = $("#queue-card");
  const wrap = $("#queue");
  wrap.textContent = "";
  if (!target) {
    card.hidden = true;
    return;
  }
  // Spotify contexts list their songs too (fork v0.1.1 metadata cache,
  // via /expand tracks=1) — same rows, same tap-to-play as podcasts.
  const spot = target.includes("spotify");
  try {
    // Prefer the library entry: /expand?id applies its play order,
    // which is the order the box actually queues in.
    let url = `/expand?target=${encodeURIComponent(target)}`;
    try {
      const lib = await api("/library");
      for (const s of lib.sections) for (const e of s.entries) {
        if (e.target === target) url = `/expand?id=${encodeURIComponent(e.id)}`;
      }
    } catch (e) { /* no library — fall back to target expand */ }
    if (spot) url += "&tracks=1";
    const r = await api(url);
    const eps = r.episodes || [];
    for (const ep of eps) {
      const row = document.createElement("div");
      row.className = "entry queue-ep";
      row.dataset.episode = ep.id || "";
      const info = document.createElement("div");
      info.className = "entry-info";
      const name = document.createElement("strong");
      name.textContent = ep.title || ep.id || "?";
      const sub = document.createElement("small");
      sub.textContent = ep.cached ? "✓ offline" : "";
      info.append(name, sub);
      row.appendChild(info);
      row.addEventListener("click", async () => {
        try {
          await api("/play", { method: "POST",
            body: { target, episode: ep.id } });
          toast(`Starting ${ep.title || "episode"} …`);
        } catch (e) { toast(e.message); }
      });
      wrap.appendChild(row);
    }
    card.hidden = eps.length === 0;
    // The daemon's bounded settle can time out on a cold context and
    // hand back a partial/empty listing with pending=true. One delayed
    // re-fetch completes it; without this an empty first load pinned an
    // empty queue card until the target changed. Once per target, so a
    // context that never settles cannot loop.
    if (r.pending && queueRetried !== target) {
      queueRetried = target;
      setTimeout(() => (queueTarget === target ? loadQueue(target) : null),
                 4000);
    } else if (!r.pending) {
      queueRetried = null;
    }
  } catch (e) {
    card.hidden = true;
  }
}

function markQueuePlaying(episodeId) {
  for (const row of document.querySelectorAll(".queue-ep")) {
    row.classList.toggle("playing",
      !!episodeId && row.dataset.episode === episodeId);
  }
}

async function pollStatus() {
  try {
    const st = await api("/status");
    $("#np-title").textContent = st.title || "Nothing playing";
    const artists = (st.spotify && st.spotify.playing
      ? (st.spotify.artists || []).join(", ") : "");
    const offline = st.spotify_offline && st.source === "spotify";
    renderSpotifyState(st.spotify_state);
    $("#np-sub").textContent = offline
      ? "No internet — Spotify reconnects when it's back"
      : artists || (st.source ? `source: ${st.source}` : "");
    const art = $("#np-art");
    if (st.artwork) {
      const src = st.artwork.startsWith("http")
        ? st.artwork : `/artwork?path=${encodeURIComponent(st.artwork)}`;
      if (art.dataset.src !== src) {
        // decode off-screen and swap only when ready — no blank flash
        art.dataset.src = src;
        const pre = new Image();
        pre.onload = () => {
          if (art.dataset.src === src) { art.src = src; art.hidden = false; }
        };
        pre.src = src;
      } else {
        art.hidden = false;
      }
    } else if (!st.title) {
      // drop the art only when there is genuinely nothing on; a null
      // artwork WITH a title is a transition blip — keep the last image
      art.hidden = true;
      art.dataset.src = "";
    }
    np = { position: st.position, duration: st.duration,
           playing: !!st.playing, at: Date.now() };
    renderProgress();
    $("#btn-play").textContent = st.playing ? "⏸" : "▶";
    currentTarget = st.target || null;
    if (st.target !== queueTarget) loadQueue(st.target);
    markQueuePlaying(st.episode_id ||
      (st.spotify && st.spotify.track_uri));
    $("#btn-shuffle").classList.toggle("on", !!st.shuffle);
    $("#btn-shuffle").dataset.on = st.shuffle ? "1" : "";
    const out = document.querySelector(`input[name=output][value=${st.output}]`);
    if (out) out.checked = true;
  } catch (e) { /* box offline — keep last view */ }
  try {
    if (Date.now() - volTouched > 3000) {
      const v = await api("/volume");
      if (v.volume != null) {
        $("#volume").value = v.volume;
        $("#vol-label").textContent = `${v.volume}%`;
      }
    }
  } catch (e) { /* ignore */ }
}

$("#btn-play").addEventListener("click", () => api("/playpause", { method: "POST", body: {} }).then(pollStatus).catch((e) => toast(e.message)));
$("#btn-next").addEventListener("click", () => api("/next", { method: "POST", body: {} }).catch((e) => toast(e.message)));
$("#btn-prev").addEventListener("click", () => api("/prev", { method: "POST", body: {} }).catch((e) => toast(e.message)));
$("#btn-fresh").addEventListener("click", async () => {
  if (!currentTarget) { toast("Nothing to restart"); return; }
  if (!confirm(
    "Play from the beginning? The saved position is cleared.")) return;
  try {
    await api("/play", { method: "POST",
      body: { target: currentTarget, fresh: true } });
    toast("Starting from the beginning …");
  } catch (e) { toast(e.message); }
});

$("#btn-stop").addEventListener("click", () => api("/stop", { method: "POST", body: {} }).then(pollStatus).catch((e) => toast(e.message)));
$("#btn-shuffle").addEventListener("click", async () => {
  const enable = !$("#btn-shuffle").dataset.on;
  try {
    const r = await api("/shuffle", { method: "POST", body: { enabled: enable } });
    if (r.routed == null) {
      toast("Nothing to shuffle");
    } else {
      toast(enable ? "Shuffle on" : "Shuffle off");
    }
    pollStatus();
  } catch (e) { toast(e.message); }
});

$("#volume").addEventListener("input", () => {
  volTouched = Date.now();
  const v = Number($("#volume").value);
  $("#vol-label").textContent = `${v}%`;
  clearTimeout(window._volTimer);
  window._volTimer = setTimeout(() => {
    api("/volume", { method: "POST", body: { volume: v } })
      .then((r) => { if (r.volume != null && r.volume !== v) {
        $("#volume").value = r.volume;           // clamped by the cap
        $("#vol-label").textContent = `${r.volume}%`;
        toast(`Volume cap: ${r.volume}%`);
      } })
      .catch((e) => toast(e.message));
  }, 250);
});

document.querySelectorAll("input[name=output]").forEach((r) => {
  r.addEventListener("change", async () => {
    try {
      const res = await api("/output", { method: "POST", body: { device: r.value } });
      toast(res.warning || (res.spotify_restarted
        ? "Switching output (Spotify restarts …)" : "Audio output switched"),
        res.warning ? 9000 : 2500);
    } catch (e) { toast(e.message); }
  });
});

/* --- library ------------------------------------------------------------- */

let LIB = { version: 1, sections: [] };

const ORDER_LABEL = {
  auto: "auto", newest_first: "newest first", oldest_first: "oldest first",
};
const CACHE_OPTIONS = [0, 5, 10, 25, 50, -1];  // -1 = keep all offline
const isSpotify = (t) => /open\.spotify\.com|spotify:|spotify\.link\//.test(t);
const isLocal = (t) => t.startsWith("/");
const isStorytel = (t) => t.startsWith("storytel:");

async function loadLibrary() {
  LIB = await api("/library");
  const wrap = $("#sections");
  wrap.textContent = "";
  $("#section-names").textContent = "";
  for (const s of LIB.sections) {
    const opt = document.createElement("option");
    opt.value = s.name;
    $("#section-names").appendChild(opt);

    const card = document.createElement("div");
    card.className = "card";
    const h = document.createElement("h2");
    h.className = "section-head";
    if (s.image) {  // uploaded logo — shown on the box's home screen too
      const im = document.createElement("img");
      im.className = "section-logo";
      im.src = "/artwork?path=" + encodeURIComponent(s.image)
             + "&v=" + Date.now();  // bust the cache after re-upload
      h.appendChild(im);
    }
    h.appendChild(document.createTextNode(s.name));
    if (s.spotify_user) {  // subscription: entries are box-managed
      const tag = document.createElement("small");
      tag.className = "dim follow-tag";
      tag.textContent = ` follows @${s.spotify_user}`;
      h.appendChild(tag);
      const un = document.createElement("button");
      un.className = "logo-btn danger";
      un.textContent = "unfollow";
      un.addEventListener("click", async () => {
        if (!confirm(`Stop following @${s.spotify_user}? ` +
                     `The section and its playlists leave the box.`)) return;
        await saveLibrary((lib) => {
          lib.sections = lib.sections.filter((x) => x.id !== s.id);
        });
        loadLibrary();
      });
      h.appendChild(un);
    }
    const logo = document.createElement("button");
    logo.className = "logo-btn";
    logo.textContent = s.image ? "change logo" : "logo";
    logo.title = "Category picture for the box's home screen";
    logo.addEventListener("click", () => pickSectionLogo(s));
    h.appendChild(logo);
    if (s.image) {
      const rm = document.createElement("button");
      rm.className = "logo-btn";
      rm.textContent = "✕";
      rm.title = "Remove the logo";
      rm.addEventListener("click", async () => {
        LIB = await api("/library/section-logo",
                        { method: "POST", body: { id: s.id, data: null } });
        loadLibrary();
      });
      h.appendChild(rm);
    }
    card.appendChild(h);
    if (s.spotify_user && !s.entries.length) {
      const p = document.createElement("p");
      p.className = "dim";
      p.textContent = "No public playlists on the profile yet — " +
        "the box checks again on every sync.";
      card.appendChild(p);
    }
    for (const e of s.entries) {
      card.appendChild(entryRow(e, s.name, !!s.spotify_user));
    }
    wrap.appendChild(card);
  }
  if (!LIB.sections.length) {
    wrap.innerHTML = "<div class='card'><p>The library is empty — add the first link below.</p></div>";
  }
}

/* Category logo: pick an image from the phone, downscale it client-side
   (a camera photo is 5-10MB; the box screen shows 56px) and upload. */
function pickSectionLogo(s) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = "image/*";
  input.addEventListener("change", () => {
    const file = input.files && input.files[0];
    if (!file) return;
    const img = new Image();
    img.onload = async () => {
      URL.revokeObjectURL(img.src);
      const size = 300;  // square center-crop, plenty for 56px + PWA
      const c = document.createElement("canvas");
      c.width = c.height = size;
      const scale = size / Math.min(img.width, img.height);
      c.getContext("2d").drawImage(
        img,
        (size - img.width * scale) / 2, (size - img.height * scale) / 2,
        img.width * scale, img.height * scale);
      try {
        LIB = await api("/library/section-logo", {
          method: "POST",
          body: { id: s.id, data: c.toDataURL("image/jpeg", 0.85) },
        });
        toast(`${s.name}: logo updated`);
        loadLibrary();
      } catch (e) {
        toast(`Logo failed: ${e.message}`);
      }
    };
    img.onerror = () => toast("Could not read that image");
    img.src = URL.createObjectURL(file);
  });
  input.click();
}

// \u0000 as an ESCAPE, not a raw byte: a literal NUL in the source
// makes every search tool classify this file as binary and skip it
// silently. Same value at runtime, still uncollidable with a real
// category name.
const NEW_SECTION = "\u0000new";  // sentinel: "move to a new category…"

function entryRow(e, sectionName, locked) {
  const row = document.createElement("div");
  row.className = "entry";

  const info = document.createElement("div");
  info.className = "entry-info";
  const name = document.createElement("strong");
  name.textContent = e.name;
  const target = document.createElement("small");
  target.textContent = e.target;
  info.append(name, target);

  if (locked) {  // follow-section: the sweeper owns these rows, so no
    const play = document.createElement("button");  // edit controls that
    play.textContent = "▶";                         // would be undone
    play.title = "Play now";
    play.addEventListener("click", async () => {
      try {
        await api("/play", { method: "POST", body: { id: e.id } });
        toast(`Playing: ${e.name}`);
      } catch (err) { toast(err.message); }
    });
    const actions = document.createElement("div");
    actions.className = "entry-actions";
    actions.append(play);
    row.append(info, actions);
    return row;
  }

  const order = document.createElement("select");
  for (const [val, label] of Object.entries(ORDER_LABEL)) {
    const o = document.createElement("option");
    o.value = val; o.textContent = label;
    if (e.order === val) o.selected = true;
    order.appendChild(o);
  }
  order.addEventListener("change", async () => {
    e.order = order.value;
    await saveLibrary((lib) => {
      const t = libEntry(lib, e.id); if (t) t.order = e.order;
    });
    toast(`${e.name}: ${ORDER_LABEL[e.order]}`);
  });

  // Per-entry cache — podcasts: keep newest N episodes offline;
  // Spotify (since fork v0.0.3): pre-download the whole playlist/album
  // into go-librespot's disk cache so every skip is instant (on/off —
  // the N has no meaning there). Local folders: already offline.
  let cache = null;
  const spot = isSpotify(e.target);
  const story = isStorytel(e.target);
  if (!isLocal(e.target)) {
    cache = document.createElement("select");
    cache.title = spot ? "Pre-download tracks for instant playback"
                : story ? "Books downloaded to the box"
                : "Episodes kept offline";
    // storytel is download-only, so there is no 'no offline' choice —
    // an entry that keeps nothing offline plays nothing
    for (const n of (spot ? [0, -1] : story ? [-1, 5, 10, 20]
                     : CACHE_OPTIONS)) {
      const o = document.createElement("option");
      o.value = String(n);
      o.textContent = spot ? (n === 0 ? "no pre-cache" : "pre-cache")
                    : story ? (n < 0 ? "download all" : `first ${n}`)
                    : n === 0 ? "no offline" : n < 0 ? "keep all"
                    : `keep ${n}`;
      // spotify: ANY non-zero value means pre-cache is on (the sweep
      // treats n !== 0 as enabled) — an entry holding e.g. 50 must show
      // as 'pre-cache', not fall back to the first option
      if (spot ? ((e.cache || 0) !== 0) === (n !== 0)
               : (e.cache || 0) === n) o.selected = true;
      cache.appendChild(o);
    }
    cache.addEventListener("change", async () => {
      e.cache = Number(cache.value);
      await saveLibrary((lib) => {
        const t = libEntry(lib, e.id); if (t) t.cache = e.cache;
      });
      toast(spot ? (e.cache ? `${e.name}: pre-caching for instant playback`
                            : `${e.name}: no pre-cache`)
            : story ? (e.cache < 0 ? `${e.name}: downloading every book`
                                   : `${e.name}: downloading the first ${e.cache}`)
            : e.cache < 0 ? `${e.name}: keeps every episode offline`
            : e.cache ? `${e.name}: keeps the newest ${e.cache} offline`
                      : `${e.name}: no offline copies`);
    });
  }

  // Resume where you left off, or always start from the beginning — not
  // for Spotify (go-librespot owns its own resume)
  let resume = null;
  if (!isSpotify(e.target)) {
    resume = document.createElement("select");
    resume.title = "Resume or always from the start";
    for (const [val, label] of [["1", "resume"], ["0", "from start"]]) {
      const o = document.createElement("option");
      o.value = val; o.textContent = label;
      if ((e.resume === false ? "0" : "1") === val) o.selected = true;
      resume.appendChild(o);
    }
    resume.addEventListener("change", async () => {
      e.resume = resume.value === "1";
      await saveLibrary((lib) => {
        const t = libEntry(lib, e.id); if (t) t.resume = e.resume;
      });
      toast(e.resume ? `${e.name}: resumes where you left off`
                     : `${e.name}: always starts from the beginning`);
    });
  }

  // Move to another category (reorganise the library)
  const move = document.createElement("select");
  move.title = "Category";
  for (const s of LIB.sections) {
    if (s.spotify_user) continue;  // sweeper-managed — can't hold manual rows
    const o = document.createElement("option");
    o.value = s.name; o.textContent = s.name;
    if (s.name === sectionName) o.selected = true;
    move.appendChild(o);
  }
  const newOpt = document.createElement("option");
  newOpt.value = NEW_SECTION; newOpt.textContent = "New category…";
  move.appendChild(newOpt);
  move.addEventListener("change", async () => {
    let dest = move.value;
    if (dest === NEW_SECTION) {
      dest = (prompt("New category name:") || "").trim();
      if (!dest) { move.value = sectionName; return; }
    }
    if (dest === sectionName) return;
    await saveLibrary((lib) => {
      let moved = null;
      for (const s of lib.sections) {
        const i = s.entries.findIndex((x) => x.id === e.id);
        if (i >= 0) moved = s.entries.splice(i, 1)[0];
      }
      if (!moved) return;
      let d = lib.sections.find(
        (s) => s.name.toLowerCase() === dest.toLowerCase());
      if (d && d.spotify_user) return;  // box-managed — refuse quietly
      if (!d) { d = { name: dest, entries: [] }; lib.sections.push(d); }
      d.entries.push(moved);
      lib.sections = lib.sections.filter(
        (s) => s.entries.length || s.spotify_user);
    });
    loadLibrary();
    toast(`Moved “${e.name}” to ${dest}`);
  });

  const play = document.createElement("button");
  play.textContent = "▶";
  play.title = "Play now";
  play.addEventListener("click", async () => {
    try {
      await api("/play", { method: "POST", body: { id: e.id } });
      toast(`Playing: ${e.name}`);
    } catch (err) { toast(err.message); }
  });

  const del = document.createElement("button");
  del.textContent = "✕";
  del.title = "Remove";
  del.className = "danger";
  del.addEventListener("click", async () => {
    if (!confirm(`Remove “${e.name}” from the library?`)) return;
    await saveLibrary((lib) => {
      for (const s of lib.sections) {
        s.entries = s.entries.filter((x) => x.id !== e.id);
      }
      lib.sections = lib.sections.filter(
        (s) => s.entries.length || s.spotify_user);
    });
    loadLibrary();
  });

  const actions = document.createElement("div");
  actions.className = "entry-actions";
  actions.append(order);
  if (cache) actions.append(cache);
  if (resume) actions.append(resume);
  actions.append(move, play, del);
  row.append(info, actions);
  return row;
}

async function saveLibrary(mutate) {
  // Read-modify-write: EVERY save re-fetches the server's current
  // document and applies only THIS change (keyed by stable ids),
  // instead of PUTting this tab's whole in-memory copy. A client with
  // a stale copy (old cached app.js, a second device, a suspended
  // phone PWA) otherwise wiped every edit made elsewhere since it
  // loaded — the field case was a pre-cache flag that never stuck no
  // matter how many times it was toggled (2026-07-19).
  const fresh = await api("/library");
  if (mutate) mutate(fresh);
  LIB = await api("/library", { method: "PUT", body: fresh });
}

function libEntry(lib, id) {
  for (const s of lib.sections) for (const en of s.entries)
    if (en.id === id) return en;
  return null;
}

$("#add-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const sectionName = $("#add-section").value.trim();
  const entry = {
    name: $("#add-name").value.trim(),
    target: $("#add-target").value.trim(),
    order: $("#add-order").value,
    cache: Number($("#add-cache").value),
    resume: $("#add-resume").value === "1",
  };
  let sec = LIB.sections.find(
    (s) => s.name.toLowerCase() === sectionName.toLowerCase());
  if (sec && sec.spotify_user) {
    toast(`“${sec.name}” follows @${sec.spotify_user} — the box manages ` +
          "its contents. Pick another section.");
    return;
  }
  try {
    await saveLibrary((lib) => {
      let s2 = lib.sections.find(
        (s) => s.name.toLowerCase() === sectionName.toLowerCase());
      if (s2 && s2.spotify_user) return;  // warned above
      if (!s2) { s2 = { name: sectionName, entries: [] }; lib.sections.push(s2); }
      s2.entries.push(entry);
    });
    $("#add-name").value = $("#add-target").value = "";
    toast(`Added “${entry.name}”`);
    loadLibrary();
  } catch (e) {
    toast(e.message);
    loadLibrary(); // reload clean state
  }
});

/* Follow a Spotify profile: validate against the box (which owns the API
   credentials), then save a section the sweeper keeps in sync. */
$("#follow-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const raw = $("#follow-user").value.trim();
  const sectionName = $("#follow-section").value.trim();
  const m = raw.match(/user\/([^/?#]+)/);
  const user = m ? decodeURIComponent(m[1]) : raw.replace(/^spotify:user:/, "");
  let preview;
  try {
    preview = await api(`/spotify/profile?user=${encodeURIComponent(user)}`);
  } catch (e) {
    toast(e.message, 8000);
    return;
  }
  let sec = LIB.sections.find(
    (s) => s.name.toLowerCase() === sectionName.toLowerCase());
  if (sec && !sec.spotify_user) {
    toast(`“${sec.name}” already exists with its own content — ` +
          "pick a new section name for the follow.");
    return;
  }
  if (!sec) {
    sec = { name: sectionName, entries: [] };
    LIB.sections.push(sec);
  }
  try {
    await saveLibrary((lib) => {
      let s2 = lib.sections.find(
        (s) => s.name.toLowerCase() === sec.name.toLowerCase());
      if (!s2) { s2 = { name: sec.name, entries: [] }; lib.sections.push(s2); }
      s2.spotify_user = preview.user;
      s2.entries = [];  // the box's sweeper fills these in seconds
    });
    $("#follow-user").value = "";
    toast(`Following @${preview.user} — ${preview.playlists.length} public ` +
          "playlist(s) on the way to the box");
    loadLibrary();
    setTimeout(loadLibrary, 4000);  // show the sweeper's fill-in
  } catch (e) {
    toast(e.message, 6000);
    loadLibrary();
  }
});

/* --- Storytel: an account on the box, and a shelf picker -------------------
   The account lives on the box (download-only), the phone just curates.
   The picker reads the shelf, groups it into series, and each checked
   series becomes a normal library entry the sweeper downloads. */
let STORYTEL_SHELF = [];

async function loadStorytelStatus() {
  let s;
  try { s = await api("/storytel/status"); } catch (e) { return; }
  const cur = $("#storytel-current");
  if (cur) {
    cur.textContent = s.configured
      ? (s.queued ? `Connected — ${s.queued} position(s) waiting to sync`
                  : "Connected")
      : "No account yet";
  }
  const sync = $("#storytel-sync");
  if (sync) sync.checked = !!s.sync;
  const logout = $("#btn-storytel-logout");
  if (logout) logout.hidden = !s.configured;
  const libStatus = $("#storytel-lib-status");
  if (libStatus && s.configured) {
    libStatus.textContent = "Tap “Show my audiobooks” to pick what goes on the box.";
  }
}

// The Spotify engine's own state (soloistd: needs-key | needs-pair |
// expired | bad-key | audio-unbound | offline | ok). The key form only
// exists for the soloist engine — go-librespot boxes never show it.
const SPOTIFY_STATE_TEXT = {
  "needs-key": "Needs a Soloist API key — paste it below.",
  "bad-key": "Spotify rejected the API key — paste a new one.",
  "needs-pair": "Key saved. Pick the box under Devices in the Spotify app to pair.",
  "expired": "Spotify's build has expired — the box is fetching a new one.",
  "audio-unbound": "Spotify could not bind its audio output — check the audio stack.",
  "offline": "No internet — Spotify reconnects when it's back.",
};
function renderSpotifyState(state) {
  const p = $("#spotify-state");
  if (!p) return;
  const text = SPOTIFY_STATE_TEXT[state];
  p.hidden = !text;
  p.textContent = text || "";
  // The key form shows for the soloist engine only: it is the one that
  // reports the key states. Remembered, so a green 'ok' keeps it reachable.
  if (state && state !== "ok" && state !== "offline") p.dataset.soloist = "1";
  const show = p.dataset.soloist === "1";
  if ($("#soloist-key-form")) $("#soloist-key-form").hidden = !show;
  if ($("#soloist-key-help")) $("#soloist-key-help").hidden = !show;
  if ($("#soloist-pair")) $("#soloist-pair").hidden = state !== "needs-pair";
  if ($("#soloist-update")) $("#soloist-update").hidden = state !== "expired";
}

if ($("#btn-soloist-update")) {
  $("#btn-soloist-update").addEventListener("click", async () => {
    try {
      const r = await api("/soloist/update", { method: "POST" });
      toast(r.kicked ? "Fetching the new Spotify build…" : "Already fetching — give it a minute", 6000);
    } catch (e) { toast(e.message, 8000); }
  });
}

if ($("#btn-soloist-pair")) {
  $("#btn-soloist-pair").addEventListener("click", async () => {
    try {
      await api("/soloist/pair", { method: "POST" });
      toast("Open the Spotify app on this Wi-Fi and pick the box under Devices", 10000);
    } catch (e) { toast(e.message, 8000); }
  });
}

if ($("#soloist-key-form")) {
  $("#soloist-key-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const key = $("#soloist-key").value.trim();
    if (!key) { toast("Paste the API key first"); return; }
    /* Shown ONCE, locally, before it leaves the phone: the box stores it
       0600 and never sends it back over the LAN (the backup page's rule). */
    if (!confirm("Save this key in your password manager now:\n\n  " + key
                 + "\n\nThe box keeps it but never shows it again. Use this key?")) return;
    try {
      await api("/soloist/configure", { method: "POST", body: { api_key: key } });
      $("#soloist-key").value = "";
      toast("Key saved — Spotify engine restarting");
    } catch (e) { toast(e.message, 8000); }
  });
  $("#btn-soloist-key-forget").addEventListener("click", async () => {
    if (!confirm("Forget the Soloist API key? Spotify stops until a new one is saved.")) return;
    try {
      await api("/soloist/configure", { method: "POST", body: { api_key: "" } });
      toast("Key forgotten");
    } catch (e) { toast(e.message, 8000); }
  });
}

if ($("#storytel-form")) {
  $("#storytel-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const email = $("#storytel-email").value.trim();
    const password = $("#storytel-password").value;
    if (!email || !password) { toast("E-mail and password, please"); return; }
    try {
      const r = await api("/storytel/credentials",
                          { method: "POST", body: { email, password } });
      $("#storytel-password").value = "";
      toast(`Storytel connected — ${r.series} series on your shelf`);
      loadStorytelStatus();
    } catch (e) { toast(e.message, 8000); }
  });
}

if ($("#btn-storytel-logout")) {
  $("#btn-storytel-logout").addEventListener("click", async () => {
    if (!confirm("Forget the Storytel account? Downloaded books stay on " +
                 "the box until you remove their tiles.")) return;
    try {
      await api("/storytel/logout", { method: "POST" });
      toast("Storytel account forgotten");
      loadStorytelStatus();
    } catch (e) { toast(e.message); }
  });
}

if ($("#storytel-sync")) {
  $("#storytel-sync").addEventListener("change", async (ev) => {
    try {
      await api("/settings",
                { method: "PUT", body: { storytel_sync: ev.target.checked ? 1 : 0 } });
      toast(ev.target.checked ? "Positions will back up to Storytel"
                              : "Positions stay on the box");
    } catch (e) { toast(e.message); loadStorytelStatus(); }
  });
}

if ($("#btn-storytel-load")) {
  $("#btn-storytel-load").addEventListener("click", async () => {
    $("#btn-storytel-load").disabled = true;
    try {
      const r = await api("/storytel/shelf", { method: "POST" });
      STORYTEL_SHELF = r.series || [];
      renderStorytelShelf();
    } catch (e) { toast(e.message, 8000); }
    finally { $("#btn-storytel-load").disabled = false; }
  });
}

if ($("#storytel-kids")) {
  $("#storytel-kids").addEventListener("change", renderStorytelShelf);
}

function renderStorytelShelf() {
  const box = $("#storytel-shelf");
  if (!box) return;
  box.textContent = "";
  const kidsOnly = $("#storytel-kids").checked;
  const shown = STORYTEL_SHELF.filter((g) => !kidsOnly || g.kids);
  if (!shown.length) {
    const p = document.createElement("p");
    p.className = "dim small";
    p.textContent = STORYTEL_SHELF.length
      ? "No kids' books on the shelf — untick the filter to see all."
      : "Nothing on your Storytel shelf yet.";
    box.append(p);
    return;
  }
  for (const g of shown) {
    // 'in_library' = the parent picked it; 'downloaded' = books actually
    // on disk. A series can be added but not yet (or never) downloaded,
    // so the two are shown separately — checked+disabled only once it is
    // fully on the box, so a stalled download stays re-checkable.
    // Count ALL books, not just unlocked ones: isLockedContent does not
    // reliably mean inaccessible (a Premium account plays them), so the
    // download attempts every book and the server decides.
    const total = g.books.length;
    const done = g.downloaded || 0;
    const complete = g.in_library && done >= total && total > 0;
    const row = document.createElement("label");
    row.className = "entry";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.dataset.target = g.target;
    cb.checked = g.in_library;
    cb.disabled = complete;
    const info = document.createElement("div");
    info.className = "entry-info";
    const name = document.createElement("strong");
    name.textContent = g.name;
    const sub = document.createElement("small");
    let state = "";
    if (complete) state = " · on the box";
    else if (g.in_library) state = ` · downloading ${done}/${total}`;
    sub.textContent = `${g.books.length} book(s)` + state;
    info.append(name, sub);
    row.append(cb, info);
    box.append(row);
  }
  const add = document.createElement("button");
  add.type = "button";
  add.textContent = "Add checked to the box";
  add.addEventListener("click", addCheckedStorytel);
  box.append(add);
}

async function addCheckedStorytel() {
  const section = $("#storytel-section").value.trim() || "Audiobooks";
  const picks = [...document.querySelectorAll("#storytel-shelf input:checked")]
    .filter((cb) => !cb.disabled)
    .map((cb) => STORYTEL_SHELF.find((g) => g.target === cb.dataset.target))
    .filter(Boolean);
  if (!picks.length) { toast("Check a series first"); return; }
  let added = 0;
  try {
    await saveLibrary((lib) => {
      // Dedup against the WHOLE library, not just the chosen section:
      // the server derives entry ids from the target, globally unique —
      // and a still-downloading series renders checked+enabled (so a
      // stalled download stays kickable), so it rides along in every
      // save. Section-scoped dedup let it into a second section, and
      // the server 400'd the whole save, new series included (field
      // 2026-08-16: "duplicate entry id" until Kokosbananas finished
      // downloading and its row went disabled).
      const have = new Set();
      for (const s of lib.sections)
        for (const e of s.entries) have.add(e.target);
      let sec = lib.sections.find(
        (s) => s.name.toLowerCase() === section.toLowerCase());
      if (sec && sec.spotify_user) { sec = null; }  // don't touch a follow
      if (!sec) { sec = { name: section, entries: [] }; lib.sections.push(sec); }
      added = 0;  // saveLibrary may retry the mutate on a fresh copy
      for (const g of picks) {
        if (have.has(g.target)) continue;
        sec.entries.push({ name: g.name, target: g.target,
                           order: "oldest_first", cache: -1, resume: true });
        added += 1;
      }
    });
    toast(added
      ? `Added ${added} series — downloading to the box`
      : "Already on the box — download nudged");
    await loadLibrary();
    renderStorytelShelf();
  } catch (e) { toast(e.message, 6000); }
}

/* --- settings + system ---------------------------------------------------- */

async function loadSettings() {
  const s = await api("/settings");
  $("#set-screen").value = String(s.screen_timeout_s);
  $("#set-brightness").value = String(s.screen_brightness);
  $("#set-cap").value = String(s.volume_cap);
  $("#set-localcap").value = String(s.local_fallback_cap);
  $("#set-idle").value = String(s.idle_shutdown_min);
  $("#set-spotcache").value = String(s.spotify_cache_gb);
  $("#set-bitrate").value = String(s.spotify_bitrate || 160);
  $("#set-resume").value = String(s.resume_window_h);
  $("#set-kidnav").value = String(s.simple_nav || 0);
  $("#set-wifioff").value = String(s.wifi_auto_off_min);
  $("#set-wifiprobe").value = String(s.wifi_probe);
  $("#set-psbt").value = String(s.wifi_ps_bt_off || 0);
}

for (const [id, key] of [["#set-screen", "screen_timeout_s"],
                         ["#set-brightness", "screen_brightness"],
                         ["#set-cap", "volume_cap"],
                         ["#set-localcap", "local_fallback_cap"],
                         ["#set-idle", "idle_shutdown_min"],
                         ["#set-spotcache", "spotify_cache_gb"],
                         ["#set-bitrate", "spotify_bitrate"],
                         ["#set-resume", "resume_window_h"],
                         ["#set-kidnav", "simple_nav"],
                         ["#set-wifioff", "wifi_auto_off_min"],
                         ["#set-wifiprobe", "wifi_probe"],
                         ["#set-psbt", "wifi_ps_bt_off"]]) {
  $(id).addEventListener("change", async () => {
    try {
      await api("/settings", { method: "PUT",
        body: { [key]: Number($(id).value) } });
      toast("Saved");
    } catch (e) { toast(e.message); }
  });
}

function fmtBytes(n) {
  if (n == null) return "–";
  for (const u of ["B", "kB", "MB", "GB"]) {
    if (n < 1024) return `${n.toFixed(0)} ${u}`;
    n /= 1024;
  }
  return `${n.toFixed(1)} TB`;
}

async function loadSystem() {
  const sys = await api("/system");
  // keep the header pill in sync — otherwise it shows a reading up to
  // 60s older than the settings row and the two disagree
  renderBatteryPill(sys);
  const rows = [];
  rows.push(["Battery", sys.battery == null ? "unknown"
    : `${Math.round(sys.battery)}%${sys.plugged ? " (charging)" : ""}`]);
  if (sys.battery_v != null) {
    rows.push(["Battery voltage", `${sys.battery_v.toFixed(2)} V`]);
  }
  if (sys.battery_i != null) {
    /* PiSugar signs the current by direction; the plugged flag names it
       so the row reads as a rate, not a mystery sign */
    rows.push([sys.plugged ? "Charge current" : "Power draw",
      `${Math.abs(sys.battery_i).toFixed(2)} A`]);
  }
  if (sys.on_battery_s != null) {
    const h = Math.floor(sys.on_battery_s / 3600);
    const m = Math.floor((sys.on_battery_s % 3600) / 60);
    rows.push(["On battery", h ? `${h} h ${m} min` : `${m} min`]);
  }
  if (sys.disk) {
    rows.push(["SD card free", `${fmtBytes(sys.disk.free)} of ${fmtBytes(sys.disk.total)}`]);
  }
  for (const [k, v] of Object.entries(sys.caches || {})) {
    rows.push([k === "podcasts" ? "Podcast cache" : "Spotify cache", fmtBytes(v)]);
  }
  rows.push(["Wi-Fi", sys.wifi.enabled ? (sys.wifi.ssid || "on (not connected)") : "off"]);
  rows.push(["IP", sys.wifi.ip || "–"]);
  if (sys.cpu_temp != null) rows.push(["CPU temp", `${sys.cpu_temp}°C`]);
  $("#spotify-current").textContent = sys.spotify_open
    ? (sys.spotify_user
        ? `Open for login as ${sys.spotify_user} — locking shortly…`
        : "Open — pick the box under Devices in the Spotify app")
    : (sys.spotify_user
        ? `Locked to ${sys.spotify_user} — no one else can take the box`
        : "Not logged in — tap Switch account to open for login");
  rows.push(["Box", sys.hostname]);
  const dl = $("#sysinfo");
  dl.textContent = "";
  for (const [k, v] of rows) {
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v;
    dl.append(dt, dd);
  }
  $("#btn-wifi").textContent = sys.wifi.enabled ? "Turn Wi-Fi off" : "Turn Wi-Fi on";
  $("#btn-wifi").dataset.enabled = sys.wifi.enabled ? "1" : "";
  $("#wifi-current").textContent = sys.wifi.hotspot
    ? `Setup hotspot active: ${sys.wifi.hotspot_ssid}`
    : sys.wifi.enabled
      ? (sys.wifi.ssid ? `Connected to ${sys.wifi.ssid} (${sys.wifi.ip || "no IP"})`
                       : "On — not connected to any network")
      : "Wi-Fi is off";
  $("#btn-hotspot").textContent = sys.wifi.hotspot
    ? "Stop hotspot" : "Setup hotspot";
  $("#btn-hotspot").dataset.on = sys.wifi.hotspot ? "1" : "";
}

$("#btn-hotspot").addEventListener("click", async () => {
  const enable = !$("#btn-hotspot").dataset.on;
  if (enable && !confirm(
    "Start the setup hotspot? The box LEAVES the current network — connect "
    + "your phone to the hotspot (see the name/password in the next toast) "
    + "to reach this page again.")) return;
  try {
    const r = await api("/wifi/hotspot", { method: "POST", body: { enabled: enable } });
    toast(enable && r.ok
      ? `Hotspot: ${r.ssid} — password: ${r.password}` : enable
        ? (r.output || "Hotspot failed") : "Hotspot stopped", 15000);
    loadSystem();
  } catch (e) {
    toast(enable
      ? "Lost contact — the box is now the hotspot; join it and reload."
      : e.message, 10000);
  }
});

$("#btn-wifi").addEventListener("click", async () => {
  const enable = !$("#btn-wifi").dataset.enabled;
  if (!enable && !confirm(
    "Turn Wi-Fi off? This page loses contact with the box until Wi-Fi is back on (via the screen or a reboot).")) return;
  try {
    await api("/system/wifi", { method: "POST", body: { enabled: enable } });
    toast(enable ? "Wi-Fi on" : "Wi-Fi off");
    loadSystem();
  } catch (e) { toast(e.message); }
});

$("#wifi-add-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const ssid = $("#wifi-add-ssid").value.trim();
  const pass = $("#wifi-add-pass").value;
  if (pass && (pass.length < 8 || pass.length > 63)) {
    toast("WPA password must be 8-63 characters"); return;
  }
  try {
    const r = await api("/wifi/add",
      { method: "POST", body: { ssid, password: pass || undefined } });
    toast(r.output || (r.ok ? "Saved" : "Failed"), 6000);
    if (r.ok) { $("#wifi-add-ssid").value = ""; $("#wifi-add-pass").value = ""; }
  } catch (e) { toast(e.message); }
});

$("#btn-wifi-reconnect").addEventListener("click", async () => {
  try {
    await api("/system/wifi", { method: "POST", body: { enabled: true } });
    toast("Wi-Fi on — reconnecting…");
    setTimeout(loadSystem, 4000);
  } catch (e) { toast(e.message); }
});

$("#btn-spotify-logout").addEventListener("click", async () => {
  if (!confirm(
    "Log the box out of Spotify? Afterwards, pick the box under Devices " +
    "in the Spotify app with the account you want.")) return;
  try {
    await api("/spotify/logout", { method: "POST", body: {} });
    toast("Logged out — pick the box in the Spotify app", 6000);
    setTimeout(loadSystem, 4000);
  } catch (e) { toast(e.message); }
});

$("#btn-shutdown").addEventListener("click", async () => {
  if (!confirm("Shut down the box?")) return;
  await api("/system/shutdown", { method: "POST", body: {} }).catch(() => {});
  toast("Shutting down …", 10000);
});
$("#btn-restart").addEventListener("click", async () => {
  if (!confirm("Restart the box?")) return;
  await api("/system/shutdown", { method: "POST", body: { restart: true } }).catch(() => {});
  toast("Restarting …", 10000);
});

/* --- bluetooth ------------------------------------------------------------- */

async function loadBt() {
  let bt;
  try { bt = await api("/bt"); } catch (e) { return; }
  const active = bt.devices.find((d) => d.mac === bt.configured);
  $("#bt-current").textContent = bt.configured
    ? `Active: ${active ? active.name : bt.configured}` +
      (active && active.connected ? " (connected)" : " (not connected now)")
    : "No speaker selected yet.";
  const wrap = $("#bt-devices");
  wrap.textContent = "";
  for (const d of bt.devices) {
    const row = document.createElement("div");
    row.className = "entry";
    const info = document.createElement("div");
    info.className = "entry-info";
    const name = document.createElement("strong");
    name.textContent = d.name + (d.connected ? " ●" : "");
    // audio === false is a gamepad/phone/keyboard, not a speaker. It
    // still belongs in the list (you must be able to unpair it), but
    // offering "Connect" would route audio into a controller — the
    // configured-output file and asound.conf would both point at it
    // (field 2026-08-04: a paired Pro Controller showed up here as a
    // connectable speaker).
    const isSpeaker = d.audio !== false;
    const mac = document.createElement("small");
    mac.textContent = d.mac + (d.paired ? " · paired" : "")
      + (isSpeaker ? "" : " · not a speaker");
    info.append(name, mac);
    row.append(info);

    if (isSpeaker) {
      const isActive = d.connected && d.mac === bt.configured;
      const use = document.createElement("button");
      use.textContent = isActive ? "Active" : "Connect";
      use.disabled = isActive;
      use.addEventListener("click", () => btAction("/bt/connect",
        { mac: d.mac }, `Connecting to ${d.name} …`));
      row.append(use);
    }
    if (d.connected && d.mac !== bt.configured) {
      // a device that connected on its own — hang up without forgetting
      const disc = document.createElement("button");
      disc.textContent = "Disconnect";
      disc.addEventListener("click", () =>
        btAction("/bt/disconnect", { mac: d.mac }, "Disconnecting …"));
      row.append(disc);
    }
    const rename = document.createElement("button");
    rename.textContent = "Rename";
    rename.addEventListener("click", () => {
      const next = prompt(
        `Name for this ${isSpeaker ? "speaker" : "device"} `
        + `(blank resets to the factory name):`,
        d.name);
      if (next === null) return;             // cancelled
      btAction("/bt/rename", { mac: d.mac, name: next.trim() },
        "Renaming …");
    });
    row.append(rename);

    const forget = document.createElement("button");
    forget.textContent = "Forget";
    forget.className = "danger";
    forget.addEventListener("click", () => {
      if (confirm(`Forget “${d.name}”? This removes the pairing.`)) {
        btAction("/bt/forget", { mac: d.mac }, "Forgetting …");
      }
    });
    row.append(forget);
    wrap.appendChild(row);
  }
  $("#btn-pair").disabled = bt.pairing;
  $("#btn-visible").disabled = bt.pairing;
}

async function btAction(path, body, busyMsg, busyMs = 60000) {
  toast(busyMsg, busyMs);
  try {
    const r = await api(path, { method: "POST", body });
    toast(r.ok ? "OK" : (r.output || "Failed").split("\n").pop(), r.ok ? 2500 : 8000);
  } catch (e) {
    toast(e.message, 6000);
  }
  loadBt();
}

$("#btn-pair").addEventListener("click", async () => {
  const btn = $("#btn-pair");
  btn.disabled = true;
  btn.textContent = "Pairing …";
  await btAction("/bt/pair", {}, "Scanning and pairing the nearest speaker …");
  btn.disabled = false;
  btn.textContent = "Pair nearest";
});

$("#btn-visible").addEventListener("click", async () => {
  const btn = $("#btn-visible");
  btn.disabled = true;
  btn.textContent = "Visible … (~2 min)";
  const tick = setInterval(loadBt, 5000); // a new bond shows up live
  await btAction("/bt/visible", { secs: 120 },
    "Box is visible — start the pairing from the car’s Bluetooth menu …",
    150000);
  clearInterval(tick);
  btn.disabled = false;
  btn.textContent = "Pair from car";
});

$("#btn-scan").addEventListener("click", async () => {
  const btn = $("#btn-scan");
  btn.disabled = true;
  btn.textContent = "Scanning … (~25 s)";
  const wrap = $("#bt-found");
  wrap.textContent = "";
  try {
    const r = await api("/bt/scan", { method: "POST", body: {} });
    if (!r.found.length) {
      wrap.innerHTML = "<p class='dim'>No new devices found — is the speaker in pairing mode and nearby?</p>";
    }
    for (const d of r.found) {
      const row = document.createElement("div");
      row.className = "entry";
      const info = document.createElement("div");
      info.className = "entry-info";
      const name = document.createElement("strong");
      name.textContent = d.name + (d.audio ? " 🔊" : "");
      const mac = document.createElement("small");
      mac.textContent = d.mac;
      info.append(name, mac);
      const pick = document.createElement("button");
      pick.textContent = "Pair and connect";
      pick.addEventListener("click", async () => {
        wrap.textContent = "";
        await btAction("/bt/connect", { mac: d.mac },
          `Pairing and connecting to ${d.name} …`);
      });
      row.append(info, pick);
      wrap.appendChild(row);
    }
  } catch (e) { toast(e.message, 6000); }
  btn.disabled = false;
  btn.textContent = "Scan for new";
});

/* --- wifi join ---------------------------------------------------------------- */

function signalBars(pct) {
  return pct > 75 ? "▂▄▆█" : pct > 50 ? "▂▄▆" : pct > 25 ? "▂▄" : "▂";
}

$("#btn-wifi-scan").addEventListener("click", async () => {
  const btn = $("#btn-wifi-scan");
  btn.disabled = true;
  btn.textContent = "Scanning …";
  const wrap = $("#wifi-list");
  wrap.textContent = "";
  try {
    const r = await api("/wifi/scan", { method: "POST", body: {} });
    if (!r.ok) {
      toast(r.output || "Scan failed", 6000);
    } else if (!r.networks.length) {
      wrap.innerHTML = "<p class='dim'>No networks found.</p>";
    }
    for (const n of r.networks) {
      const row = document.createElement("div");
      row.className = "entry";
      const info = document.createElement("div");
      info.className = "entry-info";
      const name = document.createElement("strong");
      name.textContent = (n.in_use ? "✓ " : "") + n.ssid + (n.secured ? " 🔒" : "");
      const sub = document.createElement("small");
      sub.textContent = `${signalBars(n.signal)} ${n.signal}%` +
        (n.known ? " · saved" : "");
      info.append(name, sub);
      row.appendChild(info);
      if (!n.in_use) {
        const join = document.createElement("button");
        join.textContent = n.known ? "Connect" : "Join";
        join.addEventListener("click", () => wifiJoin(n));
        row.appendChild(join);
        if (n.known) {
          const forget = document.createElement("button");
          forget.textContent = "Forget";
          forget.className = "danger";
          forget.addEventListener("click", async () => {
            if (!confirm(`Forget the saved network “${n.ssid}”?`)) return;
            await api("/wifi/forget", { method: "POST", body: { ssid: n.ssid } })
              .then((res) => toast(res.ok ? "Forgotten" : res.output, 4000))
              .catch((e) => toast(e.message));
          });
          row.appendChild(forget);
        }
      }
      wrap.appendChild(row);
    }
  } catch (e) { toast(e.message, 6000); }
  btn.disabled = false;
  btn.textContent = "Scan for networks";
});

async function wifiJoin(n) {
  let password;
  if (n.secured && !n.known) {
    password = prompt(`Password for “${n.ssid}”:`);
    if (!password) return;
  }
  toast(`Joining ${n.ssid} … the box may move networks (reconnect your phone if this page stops responding)`, 60000);
  try {
    const r = await api("/wifi/connect",
      { method: "POST", body: { ssid: n.ssid, password } });
    toast(r.ok ? `Connected to ${r.ssid} (${r.ip || "getting IP …"})`
               : (r.output || "Failed").split("\n").pop(), r.ok ? 4000 : 8000);
    loadSystem();
  } catch (e) {
    toast("Lost contact — if the box joined the new network, reconnect your phone to it and reload.", 10000);
  }
}

/* --- header battery pill ---------------------------------------------------- */

function renderBatteryPill(sys) {
  const b = $("#battery");
  if (sys.battery == null) {
    b.textContent = "–";
    b.classList.remove("low");
  } else {
    b.textContent = `${Math.round(sys.battery)}%${sys.plugged ? " ⚡" : ""}`;
    b.classList.toggle("low", !sys.plugged && sys.battery <= 15);
  }
}

async function pollBattery() {
  try {
    renderBatteryPill(await api("/system"));
  } catch (e) { /* box offline */ }
}

/* --- boot ------------------------------------------------------------------ */
/* Poll only while the tab is actually visible: a PWA left open on a
   docked phone otherwise wakes the box's wifi radio out of its power-
   save nap every 2s around the clock (review P2). While nothing plays,
   /status slows to 5s — the page has nothing moving to show. */

let statusTimer = null, battTimer = null;

function statusPeriod() {
  return (np.playing ? 2000 : 5000);
}

function schedStatus() {
  clearTimeout(statusTimer);
  statusTimer = setTimeout(async () => {
    if (!document.hidden) await pollStatus();
    schedStatus();
  }, statusPeriod());
}

function startPolling() {
  pollStatus();
  pollBattery();
  schedStatus();
  clearInterval(battTimer);
  battTimer = setInterval(() => { if (!document.hidden) pollBattery(); },
                          60000);
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  startPolling();  // fresh data the moment we're back
  // Re-load the library too, so a resumed tab shows edits made from
  // other devices while it slept. (Saves are safe regardless — each
  // one re-fetches the document and applies only its own keyed change
  // in saveLibrary — but the view would still LOOK stale.)
  const active = document.querySelector("nav button.active");
  if (active && active.dataset.tab === "library") { loadLibrary(); loadMedia(); }
});

/* QR landing: the box screen shows http://<box>/#t=<TOKEN>. The token
   rides in the FRAGMENT, which browsers never send to the server, so it
   can't land in a request line, a log or a Referer. Strip it from the
   URL bar afterwards so it isn't left in history or a screenshot. */
(function claimTokenFromUrl() {
  let m = null;
  try { m = (location.hash || "").match(/[#&]t=([0-9A-Za-z-]+)/); }
  catch (e) { /* no location (non-browser host) */ }
  if (!m) { renderLinkState(); return; }
  setToken(m[1]);
  try {
    history.replaceState(null, "", location.pathname + location.search);
  } catch (e) { /* best effort — the token is already stored */ }
  toast("This phone is now linked to the box");
})();

$("#btn-token-save")?.addEventListener("click", () => {
  const v = normToken($("#set-token").value);
  if (!v) { toast("Enter the token shown on the box screen"); return; }
  setToken(v);
  linkBanner(false);
  toast("This phone is now linked to the box");
});

$("#btn-token-forget")?.addEventListener("click", () => {
  setToken("");
  toast("Token removed from this phone");
});

/* ---- Your own audio: upload + manage ------------------------------- */
/* XHR, not fetch: only XHR reports UPLOAD progress, and an audiobook is
   minutes over wifi to a Zero 2 W — without a bar it looks hung. */
function uploadOne(collection, file, onProgress) {
  return new Promise((resolve, reject) => {
    const url = "/media/upload?collection=" + encodeURIComponent(collection) +
                "&name=" + encodeURIComponent(file.name);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    // octet-stream (not multipart) is what keeps the CSRF guard intact —
    // a form can send multipart, but not this.
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    if (TOKEN) xhr.setRequestHeader("X-Vibb-Token", TOKEN);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch (e) { /* ignore */ }
      if (xhr.status >= 200 && xhr.status < 300) return resolve(body);
      if (xhr.status === 401) { linkBanner(true); }
      reject(new Error(body.error || ("HTTP " + xhr.status)));
    };
    xhr.onerror = () => reject(new Error("network error"));
    xhr.send(file);
  });
}

async function loadMedia() {
  const box = $("#media-list");
  if (!box) return;
  let r;
  try { r = await api("/media"); } catch (e) { box.textContent = ""; return; }
  const gb = (n) => (n / 1e9).toFixed(1) + " GB";
  box.innerHTML = "";
  /* Existing collections as datalist suggestions: adding more files to
     "Ronja" means picking it, not retyping it — a typo ("Ronja ")
     silently created a NEW collection. */
  const dl = $("#collection-names");
  if (dl) {
    dl.innerHTML = "";
    for (const c of r.collections || []) {
      const o = document.createElement("option");
      o.value = c.name;
      dl.appendChild(o);
    }
  }
  for (const c of r.collections || []) {
    const el = document.createElement("div");
    el.className = "entry";
    const n = c.files.length;
    el.innerHTML = `<div class="entry-info"><b></b><small></small></div>`;
    el.querySelector("b").textContent = c.name;
    el.querySelector("small").textContent =
      `${n} file${n === 1 ? "" : "s"} · ${(c.bytes / 1e6).toFixed(0)} MB · ${c.path}`;
    const del = document.createElement("button");
    del.textContent = "Delete";
    del.className = "danger";
    del.onclick = async () => {
      if (!confirm(`Delete the whole "${c.name}" collection from the box?`)) return;
      try {
        await api("/media/delete", { method: "POST", body: { collection: c.name } });
        toast("Deleted");
        loadMedia();
      } catch (e) { toast(e.message); }
    };
    el.appendChild(del);
    box.appendChild(el);
  }
  if (typeof r.free === "number") {
    const p = document.createElement("p");
    p.className = "dim small";
    p.textContent = `${gb(r.free)} free on the box`;
    box.appendChild(p);
  }
}

$("#btn-upload")?.addEventListener("click", async () => {
  const coll = ($("#up-collection").value || "").trim();
  const files = Array.from($("#up-files").files || []);
  if (!coll) { toast("Give the collection a name first"); return; }
  if (!files.length) { toast("Pick one or more files"); return; }
  const wrap = $("#up-progress"), bar = $("#up-bar"), status = $("#up-status");
  const btn = $("#btn-upload");
  btn.disabled = true;
  wrap.hidden = false;
  const total = files.reduce((a, f) => a + f.size, 0);
  let done = 0;
  try {
    for (const [i, f] of files.entries()) {
      status.textContent = `Uploading ${i + 1}/${files.length}: ${f.name}`;
      await uploadOne(coll, f, (frac) => {
        bar.style.width = (100 * (done + f.size * frac) / total).toFixed(1) + "%";
      });
      done += f.size;
      bar.style.width = (100 * done / total).toFixed(1) + "%";
    }
    status.textContent = "Done — add it as a library entry below.";
    $("#up-files").value = "";
    toast(`Uploaded ${files.length} file${files.length === 1 ? "" : "s"}`);
    loadMedia();
  } catch (e) {
    status.textContent = "Failed: " + e.message;
    toast(e.message);
  } finally {
    btn.disabled = false;
    setTimeout(() => { wrap.hidden = true; bar.style.width = "0%"; }, 4000);
  }
});

startPolling();
setInterval(renderProgress, 500);
