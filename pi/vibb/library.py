"""Library services: the parent-curated link collection, expansion into
playable episode lists, background episode caching, and the artwork-proxy
allowlist. Extracted verbatim from daemon.py."""

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

from vibb import content, spotify, spotify_web, storytel
from vibb import paths as _paths
from vibb import radio as _radio
from vibb.paths import ART_DIR, CACHE_DIR, STATE_DIR
from vibb.spotify import is_spotify

LIB_FILE = os.environ.get("VIBB_LIBRARY", "/etc/vibb/library.json")
ORDERS = ("auto", "newest_first", "oldest_first")


def log(msg):
    print(f"vibbd: {msg}", flush=True)


def state_key(target):
    """The resume-bookmark key for a target (same rule as player.py):
    the podcast slug for NRK links, else a hash of the target."""
    m = re.match(r"https?://radio\.nrk\.no/podkast/([a-z0-9_-]+)", target, re.I)
    if m:
        return m.group(1)
    return hashlib.sha1(target.encode()).hexdigest()[:12]


# --- library (parent-curated named links) --------------------------------------

def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "x"


def normalize_library(obj):
    """Validate and normalize a library document; raises ValueError.
    Fills in missing ids (stable: sha1 of target) so clients can reference
    entries without carrying URLs around."""
    if not isinstance(obj, dict) or not isinstance(obj.get("sections"), list):
        raise ValueError("library must be an object with a 'sections' list")
    out = {"version": 1, "sections": []}
    seen = set()
    for s in obj["sections"]:
        if not isinstance(s, dict):
            raise ValueError("section must be an object")
        name = str(s.get("name") or "").strip()
        if not name:
            raise ValueError("section needs a name")
        sec = {"id": str(s.get("id") or _slug(name)), "name": name, "entries": []}
        image = s.get("image")  # optional section logo (uploaded via PWA)
        if image:
            if not isinstance(image, str) or len(image) > 500:
                raise ValueError("section image must be a short string")
            sec["image"] = image
        user = s.get("spotify_user")  # section follows a Spotify profile:
        if user:                      # its entries are sweeper-managed
            if not isinstance(user, str) or not 0 < len(user.strip()) <= 100:
                raise ValueError("spotify_user must be a short string")
            sec["spotify_user"] = user.strip()
        for e in s.get("entries") or []:
            if not isinstance(e, dict):
                raise ValueError("entry must be an object")
            target = str(e.get("target") or "").strip()
            ename = str(e.get("name") or "").strip()
            if not target or not ename:
                raise ValueError("entry needs a name and a target")
            order = e.get("order") or "auto"
            if order not in ORDERS:
                raise ValueError(f"order must be one of {ORDERS}")
            cache = e.get("cache", _default_cache(e.get("target") or ""))
            # -1 = keep all episodes offline; 0 = none; 1..100 = newest N
            if not isinstance(cache, int) or not (cache == -1 or 0 <= cache <= 100):
                raise ValueError("cache must be -1 (all) or 0-100 (episodes "
                                 "to keep offline)")
            if cache == 0 and storytel.is_storytel(target):
                cache = -1   # storytel is download-only: cache 0 would be an
                #              entry that plays nothing. 'in the library' means
                #              'downloaded'. The PWA still picks all vs first-N.
            resume = e.get("resume", True)  # False = always play from the start
            if not isinstance(resume, bool):
                raise ValueError("resume must be true or false")
            eid = str(e.get("id") or hashlib.sha1(target.encode()).hexdigest()[:8])
            if eid in seen:
                raise ValueError(f"duplicate entry id {eid}")
            seen.add(eid)
            sec["entries"].append(
                {"id": eid, "name": ename, "target": target, "order": order,
                 "cache": cache, "resume": resume})
        out["sections"].append(sec)
    return out


# mtime-keyed parse cache: /status re-loads the library every second in
# the box's most common state (stopped-but-remembered), and the full
# json.load + normalize per poll is pure CPU on a battery box (review
# P5). Callers mutate the returned dict, so hand out a deepcopy — still
# ~10x cheaper than re-parsing, and mtime_ns catches every save.
_LIB_CACHE = {"key": None, "lib": None}


def load_library():
    try:
        st = os.stat(LIB_FILE)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return {"version": 1, "sections": []}
    if _LIB_CACHE["key"] != key:
        try:
            with open(LIB_FILE) as f:
                _LIB_CACHE["lib"] = normalize_library(json.load(f))
            _LIB_CACHE["key"] = key
        except (OSError, ValueError):
            return {"version": 1, "sections": []}
    return copy.deepcopy(_LIB_CACHE["lib"])


def library_with_covers():
    """load_library() + best-effort show artwork per entry (menu covers
    for the screen UI; the PWA ignores the field). Cheap by construction:
    collection_image only consults local files and in-process caches —
    never the network. Entries without a synced/remembered cover get
    None until their feed is first expanded or cached."""
    lib = load_library()
    fresh = new_targets()  # entries with an unacknowledged new episode
    for s in lib.get("sections", []):
        for e in s.get("entries", []):
            try:
                e["image"] = content.collection_image(e["target"])
            except Exception:
                e["image"] = None
            if e["target"] in fresh:
                e["new"] = True
    return lib


# Serializes every load->mutate->save of library.json: three writers
# exist (PUT /library, /library/section-logo, the sweeper's profile
# sync right after every save) and an unserialized pair silently
# reverts the loser's edit (review 2026-07-18 R4). Never hold this
# across network I/O — fetch first, then lock, re-load, apply, save.
LIB_LOCK = threading.Lock()


def save_library(lib):
    _EXPAND_CACHE.clear()  # order/cache settings may have changed
    os.makedirs(os.path.dirname(LIB_FILE), exist_ok=True)
    tmp = LIB_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(lib, f, indent=2, ensure_ascii=False)
    os.replace(tmp, LIB_FILE)


def find_entry(lib, entry_id):
    for s in lib["sections"]:
        for e in s["entries"]:
            if e["id"] == entry_id:
                return e
    return None


# --- profile-follow sections (spotify_user) --------------------------------------
# A section with a spotify_user is a subscription: its entries mirror that
# profile's PUBLIC playlists. The parent curates from their phone by making
# playlists public/private; the sweeper picks the change up here.

def sync_profile_sections():
    """Refresh every profile-follow section from the Web API. Returns True
    when the library changed (and was saved). A profile that can't be
    fetched (offline, no credentials, deleted user) keeps the entries from
    the last successful sweep — the box never loses content over a blip."""
    users = [s.get("spotify_user") for s in load_library()["sections"]
             if s.get("spotify_user")]
    if not users:
        return False
    # Network first, WITHOUT the lock — a slow Web API call must never
    # block a parent's PUT /library
    fetched = {}
    for user in users:
        try:
            fetched[user] = spotify_web.user_playlists(user)
        except Exception as exc:
            log(f"profile sync: {user}: {exc!r}")
    # Then re-load fresh under the lock and apply: edits that landed
    # while we were fetching are preserved instead of reverted
    with LIB_LOCK:
        lib = load_library()
        # Manually curated targets win: a playlist already in a normal
        # section is skipped here, or normalize_library would reject the
        # duplicate id.
        seen = {e["target"] for s in lib["sections"]
                if not s.get("spotify_user") for e in s["entries"]}
        changed = False
        for sec in lib["sections"]:
            user = sec.get("spotify_user")
            if not user:
                continue
            if user not in fetched:  # fetch failed — keep last sweep's list
                seen.update(e["target"] for e in sec["entries"])
                continue
            # Rebuild the list but PRESERVE per-entry settings the parent
            # set (cache/order/resume) for playlists that persist — the
            # old wholesale rebuild reset cache to 0 on every sync, which
            # silently disarmed spotify pre-caching on followed playlists.
            prev = {e["target"]: e for e in sec["entries"]}
            entries = [{"name": p["name"], "target": p["target"],
                        "order": prev.get(p["target"], {}).get("order",
                                                               "auto"),
                        "cache": prev.get(p["target"], {}).get("cache", 0),
                        "resume": prev.get(p["target"], {}).get("resume",
                                                                True)}
                       for p in fetched[user] if p["target"] not in seen]
            seen.update(e["target"] for e in entries)
            if [(e["name"], e["target"]) for e in entries] != \
               [(e["name"], e["target"]) for e in sec["entries"]]:
                sec["entries"] = entries
                changed = True
                log(f"profile sync: {user}: {len(entries)} public playlist(s)")
        if changed:
            save_library(normalize_library(lib))
        return changed


# --- background episode caching (the "offline: keep newest N" setting) ----------
# Entries with cache > 0 are synced by the daemon itself: right after the
# library is saved (add a podcast -> download starts immediately) and then
# every SYNC_INTERVAL so new episodes land without anyone pressing play.
# Syncs are incremental (existing files skipped, catalog cache TTL'd), run
# sequentially at nice 19, and failures are just retried next sweep.

SYNC_INTERVAL_S = int(os.environ.get("VIBB_SYNC_INTERVAL", 6 * 3600))
# 90s (not 30): the boot resume fires ~30s in, and the sweep's podcast
# downloads saturate the Zero's single 2.4GHz link — a track skip right
# after boot then waited on a starved go-librespot control call (field
# log 2026-07-17: /next timed out ~20s after resume, mid-sweep). Push the
# first sweep past the initial interaction window.
SYNC_DELAY_S = int(os.environ.get("VIBB_SYNC_DELAY", 90))
_sync_wake = threading.Event()


def _sync_args_for(target, n):
    m = re.match(r"https?://radio\.nrk\.no/podkast/([a-z0-9_-]+)", target, re.I)
    if m:
        return ["sync", m.group(1), str(n), "podcast"]
    m = re.match(r"https?://radio\.nrk\.no/serie/([a-z0-9_-]+)/?$", target, re.I)
    if m:
        return ["sync", m.group(1), str(n), "series"]
    if target.startswith(("http://", "https://")) and not is_spotify(target):
        return ["sync-feed", target, str(n)]
    return None  # spotify (global cache) / local folders (already offline)


SWEEP_STAMP = os.path.join(STATE_DIR, "last-sweep.json")
SYNC_STAGGER_S = int(os.environ.get("VIBB_SYNC_STAGGER", 4))
# The sweep must NEVER disturb active listening: its downloads share the
# single 2.4GHz radio with the Spotify stream AND the A2DP link, and a
# saturated radio starves control calls and fools the offline prober
# (field 2026-07-18: the sweep made pausing a song a two-minute fight).
# The daemon sets BUSY_CHECK to "is anything audible right now"; while it
# returns True the sweep holds off, and an in-flight download is
# ABANDONED (terminated, retried next sweep — syncs are incremental so
# little is lost) the moment playback starts.
BUSY_CHECK = None  # set by the daemon; None = never busy (CLI use)
SYNC_SETTLE_S = float(os.environ.get("VIBB_SYNC_SETTLE", "15"))
SYNC_BUSY_RECHECK_S = int(os.environ.get("VIBB_SYNC_BUSY_RECHECK", 30))


def _busy():
    try:
        return bool(BUSY_CHECK and BUSY_CHECK())
    except Exception:
        return False  # a broken check must never stall the sweep forever


class SweepYield(Exception):
    """Raised when a running sync is abandoned because playback started."""


def _last_sweep():
    """Wall-clock time of the last completed sweep, or 0. Wall-clock (not
    monotonic) so it survives reboots — the whole point is to NOT sweep
    again on every restart when the last one was within the interval."""
    try:
        with open(SWEEP_STAMP) as f:
            return float(json.load(f)["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0


def _stamp_sweep():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(SWEEP_STAMP + ".tmp", "w") as f:
            json.dump({"at": time.time()}, f)
        os.replace(SWEEP_STAMP + ".tmp", SWEEP_STAMP)
    except OSError:
        pass


def _sync_one(args, mod=None):
    """Run one content.py sync, kept off the audio's back: nice-19, and
    pinned to a single core (taskset) so the ffmpeg transcode can't take
    both cores from playback. Downloads are sequential (one at a time).
    Watched: if playback starts MID-download the child is terminated and
    SweepYield raised — the radio belongs to the music, and the next
    sweep re-runs the (incremental) sync for pennies."""
    cmd = [sys.executable, mod or content.__file__, *args]
    if _TASKSET:
        cmd = [_TASKSET, "-c", "1"] + cmd
    # stderr to a temp file, not DEVNULL: a subprocess that dies (a bad
    # import, an auth failure) used to vanish without a trace — a whole
    # storytel series "synced" in six seconds and downloaded nothing,
    # invisibly (field 2026-08-15). A real file can't deadlock a pipe,
    # and we surface its tail on a non-zero exit.
    errf = tempfile.TemporaryFile()

    def tail():
        """The last of the subprocess's stderr, for the journal. A large
        storytel download narrates its progress there, so this is how a
        yielded-mid-book download shows how far it got — otherwise a
        big book looks stuck when it is actually filling in."""
        try:
            errf.seek(0)
            return errf.read()[-600:].decode("utf-8", "replace").strip()
        except OSError:
            return ""

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=errf,
                            preexec_fn=lambda: os.nice(19))
    deadline = time.monotonic() + 3600
    while True:
        try:
            proc.wait(timeout=2)
            if proc.returncode:
                t = tail()
                log(f"sync exited {proc.returncode} ({' '.join(args[:2])})"
                    + (f": {t}" if t else ""))
            errf.close()
            return
        except subprocess.TimeoutExpired:
            pass
        if _busy() or time.monotonic() > deadline:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            t = tail()               # surface progress-so-far on a yield too,
            errf.close()             # or a big book looks stuck when it isn't
            if t:
                log(f"sync yielded ({' '.join(args[:2])}): {t}")
            if time.monotonic() > deadline:
                raise subprocess.TimeoutExpired(cmd, 3600)
            raise SweepYield()


_TASKSET = shutil.which("taskset")


# --- spotify pre-cache politeness (snapshot gating) --------------------------
# Re-POSTing /cache/download every 6h sweep is cheap for go-librespot
# (cached tracks are skipped) but still re-enumerates every context
# against Spotify's API four times a day. Gate it: playlists re-queue
# only when their snapshot_id changed (Spotify stamps a new one on ANY
# edit — one light GET /cache/snapshot against go-librespot's own
# session decides, fork v0.0.4, no Web API credentials needed),
# albums/tracks/episodes are immutable and queue exactly ONCE.
# Everything fails open — go-librespot down, offline, malformed state:
# behave like before the gate.

PRECACHE_STATE = os.path.join(STATE_DIR, "spotify-precache.json")


def _soloist():
    """The Spotify engine is soloistd (the install-time toggle)."""
    return _paths.GO_UNIT == "vibb-soloistd"


def _default_cache(target):
    """AM-45: under soloist a Spotify entry warms the moment it is added
    (owner: "warm the moment I add something"); the PWA toggle still turns
    it off per entry. Everything else keeps today's opt-in 0."""
    return 1 if (_soloist() and is_spotify(target)) else 0


# --- 4d: the soloist pass, driven from here (AM-37, AM-67, AM-70) ---------
WARM_POLL_S = float(os.environ.get("VIBB_WARM_POLL_S", "5"))
WARM_MAX_S = float(os.environ.get("VIBB_WARM_MAX_S", str(25 * 60)))
WARM_START = None   # set by the daemon: called when a pass starts (wifi ps off)


def _warm_health():
    try:
        with urllib.request.urlopen(spotify.API + "/soloist/health", timeout=3) as r:
            return json.loads(r.read() or b"{}")
    except (OSError, ValueError):
        return {}


def _warm_entry(uri, name):
    """POST /cache/download to soloistd and STAY with the pass: touch BUSY
    and the warming marker every WARM_POLL_S while it runs, abort it the
    moment the box turns audible (AM-67), and stamp the library's
    precache state ONLY when the sidecar says done (AM-70) — an aborted,
    stalled or partial pass stays due for the next sweep."""
    try:
        _radio.wait_paging_clear()
    except Exception:
        pass
    try:
        out = json.loads(spotify.go("/cache/download", timeout=10, body={"uri": uri}) or b"{}")
    except (OSError, ValueError) as exc:
        log(f"warm {name}: {exc!r}")
        return False
    if out.get("done"):
        log(f"warm {name}: already done ({out.get('warmed')} warmed)")
        _precache_done(uri)
        return True
    log(f"warm {name}: queued")
    if WARM_START:
        try:
            WARM_START()
        except Exception:
            pass
    t0 = time.monotonic()
    aborted = False
    while True:
        _radio.touch_busy()
        _radio.touch_warming()
        h = _warm_health()
        w = h.get("warming")
        if not w:
            break
        if not aborted and (_busy() or time.monotonic() - t0 > WARM_MAX_S):
            log(f"warm {name}: box busy — aborting the pass")
            try:
                spotify.go("/cache/abort", timeout=25)
            except OSError:
                pass
            aborted = True
        _sync_wake.wait(WARM_POLL_S)
        _sync_wake.clear()
    last = h.get("warm_last") or {}
    result = last.get("result") if last.get("uri") == uri else None
    if result == "done":
        log(f"warm {name}: done")
        _precache_done(uri)
        return True
    log(f"warm {name}: {result or 'no result'} — stays due")
    return False
_PENDING_SNAP = {}  # uri -> snapshot observed by the due-check


def _precache_state():
    try:
        with open(PRECACHE_STATE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _precache_save(state):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(PRECACHE_STATE + ".tmp", "w") as f:
            json.dump(state, f, indent=2)
        os.replace(PRECACHE_STATE + ".tmp", PRECACHE_STATE)
    except OSError:
        pass  # advisory politeness state — never break the sweep


def _precache_due(uri):
    state = _precache_state()
    prev = state.get(uri)
    kind = uri.split(":")[1] if uri.count(":") >= 2 else ""
    if kind in ("album", "track", "episode", "show"):
        return prev is None  # immutable content: queue once EVER
    if kind == "playlist":
        try:
            snap = spotify.snapshot(uri)
        except Exception:
            return True  # fail open — behave like before the gate
        if snap and prev == snap:
            return False
        _PENDING_SNAP[uri] = snap
        return True
    return True  # artist/unknown: always (they change invisibly)


def _precache_done(uri):
    state = _precache_state()
    state[uri] = _PENDING_SNAP.pop(uri, None) or "queued"
    _precache_save(state)


def _precache_prune(active_uris):
    """Forget targets whose cache setting was turned OFF — so turning it
    back on re-queues once (the parent's 'download again' lever)."""
    state = _precache_state()
    kept = {u: s for u, s in state.items() if u in active_uris}
    if kept != state:
        _precache_save(kept)


# --- 'new episode' badge state ----------------------------------------------
# A podcast/series entry is 'new' when a sweep has downloaded an episode
# newer than the one the listener last engaged with — surfaced as a dot
# on its carousel tile, cleared when they play it. We never auto-jump to
# the new episode (resume stays predictable, no surprise audio); the dot
# just says 'there's fresh content here'. State: {target: {newest, ack}}
# — newest != ack means unacknowledged new content.
NEW_STATE = os.path.join(STATE_DIR, "podcast-new.json")


def _new_state():
    try:
        with open(NEW_STATE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _new_save(state):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(NEW_STATE + ".tmp", "w") as f:
            json.dump(state, f, indent=2)
        os.replace(NEW_STATE + ".tmp", NEW_STATE)
    except OSError:
        pass  # advisory badge state — never break the sweep


def _mark_new_seen(target, newest_id):
    """Record the newest cached episode after a sync. The FIRST time we
    see a show we acknowledge it (existing content must not all light up
    as 'new' the moment this feature ships); after that, a changed newest
    id leaves 'ack' behind so the entry reads as new until it's played."""
    if not newest_id:
        return
    state = _new_state()
    cur = state.get(target)
    if cur is None:
        state[target] = {"newest": newest_id, "ack": newest_id}
    elif cur.get("newest") != newest_id:
        state[target] = {"newest": newest_id, "ack": cur.get("ack")}
    else:
        return
    _new_save(state)


def is_new(target):
    """Does this entry have unacknowledged new content?"""
    s = _new_state().get(target) or {}
    return bool(s.get("newest")) and s.get("newest") != s.get("ack")


def acknowledge_new(target):
    """The listener engaged with the show (played it) — clear its badge."""
    state = _new_state()
    s = state.get(target)
    if s and s.get("ack") != s.get("newest"):
        s["ack"] = s.get("newest")
        _new_save(state)


def new_targets():
    """Every target with an unacknowledged new episode (for /library)."""
    return {t for t, s in _new_state().items()
            if s.get("newest") and s.get("newest") != s.get("ack")}


def _new_prune(active_targets):
    state = _new_state()
    kept = {t: s for t, s in state.items() if t in active_targets}
    if kept != state:
        _new_save(kept)


def _cache_sweeper():
    # Sweeps run on battery too: a box that mostly lives off the charger
    # otherwise never syncs at all — no fresh episodes, no covers. The
    # downloads run nice-19 + one-core so playback always wins.
    # TTL across reboots: if the last sweep was within SYNC_INTERVAL_S,
    # wait out the remainder instead of re-sweeping seconds after every
    # boot (redundant network+CPU that stole a track skip — field log
    # 2026-07-17). Fresh boxes / long-off boxes still sweep at SYNC_DELAY.
    due_in = max(SYNC_DELAY_S,
                 _last_sweep() + SYNC_INTERVAL_S - time.time())
    _sync_wake.wait(due_in)
    _sync_wake.clear()
    while True:
        while _busy():  # active listening owns the radio — hold the sweep
            _sync_wake.wait(SYNC_BUSY_RECHECK_S)
            _sync_wake.clear()
        try:
            # First, so new playlists get covers in this very sweep.
            sync_profile_sections()
        except Exception as exc:
            log(f"profile sync failed: {exc!r}")
        lib = load_library()
        try:
            # Spotify covers (oEmbed): fetch what's missing, drop orphans.
            # Cheap once cached — one network round-trip per NEW entry.
            content.ensure_spotify_art(
                [e["target"] for s in lib["sections"] for e in s["entries"]])
        except Exception as exc:
            log(f"spotify art fetch failed: {exc!r}")
        try:
            content.shrink_covers()  # one-time downscale of old full-size art
        except Exception as exc:
            log(f"cover shrink failed: {exc!r}")
        for s in lib["sections"]:
            for e in s["entries"]:
                n = e.get("cache") or 0
                # n>0 keep newest N, n==-1 keep all, n==0 no offline copies
                if n != 0 and is_spotify(e["target"]):
                    uri = spotify.to_uri(e["target"])
                    if not uri or not _precache_due(uri):
                        continue  # unchanged playlist / done album: free
                    # Spotify pre-cache (fork v0.0.3): POST /cache/download
                    # pulls the whole context into go-librespot's disk
                    # cache without playing — every later skip is a cache
                    # hit instead of a cold CDN load. Async + internally
                    # rate-limited (concurrency/delay/jitter + circuit
                    # breaker), skips already-cached tracks, so a repeat
                    # request per sweep is cheap. Same discipline as the
                    # podcast syncs: only fires when nothing is audible.
                    while _busy():
                        _sync_wake.wait(SYNC_BUSY_RECHECK_S)
                        _sync_wake.clear()
                    if _soloist():
                        # 4d: the POST is the pass; stay with it (AM-37/67/70)
                        _warm_entry(uri, e["name"])
                        continue
                    try:
                        spotify.go("/cache/download", timeout=10,
                                   body={"uri": uri})
                        log(f"cache sweep: {e['name']} (spotify "
                            "pre-cache queued)")
                        _precache_done(uri)
                    except OSError as exc:
                        log(f"spotify pre-cache {e['name']}: {exc!r}")
                    continue
                if os.path.isdir(e["target"]):
                    # Local collection: heal per-track art for files that
                    # never went through the uploader (hand-copied) —
                    # runs regardless of the cache setting (the files ARE
                    # the cache). folder_art_pending is pure stats, so a
                    # healed folder costs no subprocess; the .none
                    # markers keep art-less files from re-probing forever
                    # (QA 2026-08-13).
                    if not content.folder_art_pending(e["target"]):
                        continue
                    while _busy():
                        _sync_wake.wait(SYNC_BUSY_RECHECK_S)
                        _sync_wake.clear()
                    log(f"cache sweep: {e['name']} (art-heal)")
                    try:
                        _sync_one(["art-heal", e["target"]])
                    except SweepYield:
                        log(f"cache sweep yields to playback ({e['name']} "
                            "abandoned — retried next sweep)")
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        log(f"cache sweep failed for {e['name']}: {exc!r}")
                    _sync_wake.wait(SYNC_STAGGER_S)
                    _sync_wake.clear()
                    continue
                if n != 0 and storytel.is_storytel(e["target"]):
                    # storytel downloads run storytel.py, not content.py:
                    # bearer auth, a 302 to a signed url, one big file per
                    # book. Otherwise IDENTICAL to the path below — the
                    # busy-yield, SweepYield and new-badge all apply.
                    while _busy():
                        _sync_wake.wait(SYNC_BUSY_RECHECK_S)
                        _sync_wake.clear()
                    log(f"cache sweep: {e['name']} (storytel {n})")
                    try:
                        _sync_one(["sync", e["target"], str(n)],
                                  mod=storytel.__file__)
                    except SweepYield:
                        log(f"cache sweep yields to playback ({e['name']} "
                            "abandoned — retried next sweep)")
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        log(f"cache sweep failed for {e['name']}: {exc!r}")
                    try:
                        _mark_new_seen(e["target"],
                                       content.newest_episode_id(e["target"]))
                    except Exception:
                        pass
                    _sync_wake.wait(SYNC_STAGGER_S)
                    _sync_wake.clear()
                    continue
                args = _sync_args_for(e["target"], n) if n != 0 else None
                if not args:
                    continue
                while _busy():  # re-checked before EVERY entry
                    _sync_wake.wait(SYNC_BUSY_RECHECK_S)
                    _sync_wake.clear()
                log(f"cache sweep: {e['name']} ({' '.join(args)})")
                try:
                    _sync_one(args)
                except SweepYield:
                    log(f"cache sweep yields to playback ({e['name']} "
                        "abandoned — retried next sweep)")
                except (OSError, subprocess.TimeoutExpired) as exc:
                    log(f"cache sweep failed for {e['name']}: {exc!r}")
                # the sync wrote the fresh listing (feed.json / catalog);
                # note its newest episode so a genuinely new one badges
                try:
                    _mark_new_seen(e["target"],
                                   content.newest_episode_id(e["target"]))
                except Exception:
                    pass
                _sync_wake.wait(SYNC_STAGGER_S)  # breathe between entries
                _sync_wake.clear()
        _stamp_sweep()
        try:
            _precache_prune({spotify.to_uri(e["target"])
                             for s in lib["sections"] for e in s["entries"]
                             if e.get("cache") and is_spotify(e["target"])})
            _new_prune({e["target"] for s in lib["sections"]
                        for e in s["entries"]})
        except Exception:
            pass
        _sync_wake.wait(SYNC_INTERVAL_S)  # a library save wakes us early
        _sync_wake.clear()
        # Coalesce edit bursts: EVERY save wakes us, and a parent
        # toggling eight settings in the PWA fired eight full sweep
        # passes back to back (field 2026-07-19 22:44). Wait until the
        # edits have been quiet for SYNC_SETTLE_S before starting.
        while _sync_wake.wait(SYNC_SETTLE_S):
            _sync_wake.clear()


# --- expansion (entry -> playable, titled episode list) -------------------------

def _natural_order(target):
    """The order content.expand_entries returns for this kind of target.
    Heuristic — used to decide whether an explicit order needs a reverse."""
    if re.match(r"https?://radio\.nrk\.no/podkast/", target, re.I):
        return "newest_first"
    if re.match(r"https?://radio\.nrk\.no/serie/", target, re.I):
        return "oldest_first"   # serial stories play from the beginning
    if target.startswith("storytel:"):
        return "oldest_first"   # a book series is read book 1 first
    if os.path.isdir(target):
        return "oldest_first"   # sorted filenames, part 1 first
    return "newest_first"       # RSS convention


def _cached_stems():
    """Basenames (sans extension) of every downloaded episode in the cache."""
    stems = set()
    for _root, _dirs, files in os.walk(CACHE_DIR):
        for f in files:
            stems.add(os.path.splitext(f)[0])
    return stems


_EXPAND_CACHE = {}  # (target, order, name) -> (monotonic, result)
EXPAND_TTL_S = 300  # menus re-open constantly; feeds change hourly at most


def expand_target(target, order="auto", name=None, tracks=False):
    if is_spotify(target):
        # The cover (oEmbed, fetched by the sweeper) still shows.
        try:
            image = content.collection_image(target)
        except Exception:
            image = None
        episodes = []
        pending = False
        if tracks:
            # Fork v0.1.2: playlists AND albums are expandable —
            # go-librespot lists the tracks (no Web API). Opt-in via
            # tracks=True: only the now-view song picker asks for it;
            # the browse flows keep treating spotify entries as leaf
            # "play all" cards (a menu tap must PLAY, not open a list).
            # Row shape matches the episode picker: id doubles as the
            # /play episode param, which player.py routes to
            # skip_to_uri.
            try:
                uri = spotify.to_uri(target)
                listing = spotify.context_tracks(uri) if uri else {}
                # The settle-poll is bounded (4s): a cold 800-track
                # context can time out still enumerating (ready=false)
                # or still sweeping metadata (cached < length). Say so —
                # without the flag the screen read an empty listing as
                # "no list exists" and the PWA pinned an empty queue
                # card until the target changed (architect review
                # 2026-08-03). A retry completes it; a FAILURE below
                # stays pending=False: retrying a 400 forever is wrong.
                pending = (not listing.get("ready", True)
                           or (listing.get("cached") or 0)
                           < (listing.get("length") or 0))
                for t in listing.get("tracks") or []:
                    meta = t.get("track")
                    if not meta:
                        continue  # sweep still filling — next open has it
                    artists = ", ".join(meta.get("artist_names") or [])
                    title = meta.get("name") or t.get("uri")
                    episodes.append(
                        {"id": t.get("uri"),
                         "title": f"{title} — {artists}" if artists
                                  else title,
                         "url": t.get("uri"),
                         "image": meta.get("album_cover_url"),
                         "cached": False})
            except (OSError, ValueError):
                # OSError: not listable (400), pre-v0.1.2 fork, api down.
                # ValueError: a session drop mid-listing answers 204/empty
                # body and json.loads(b"") raises it — same degradation
                # as down, not a crash bubbling to the /expand route
                # (QA audit 2026-08-03).
                episodes, pending = [], False
        return {"kind": "spotify", "name": name, "target": target,
                "order": "auto", "image": image, "episodes": episodes,
                "pending": pending}
    key = (target, order, name)
    hit = _EXPAND_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < EXPAND_TTL_S:
        return hit[1]
    result = _expand_target_uncached(target, order, name)
    _EXPAND_CACHE[key] = (time.monotonic(), result)
    return result


def _expand_target_uncached(target, order, name):
    entries = content.expand_entries(target)
    if order != "auto" and order != _natural_order(target):
        entries = list(reversed(entries))
    stems = _cached_stems()
    episodes = []
    for e in entries:
        url = e["url"]
        eid = e.get("id")
        cached = (not url.startswith("http") and os.path.exists(url)) or \
                 (eid is not None and os.path.splitext(str(eid))[0] in stems)
        episodes.append({"id": eid, "title": e.get("title"), "url": url,
                         "image": e.get("image"), "cached": bool(cached)})
    try:  # show-level artwork (local cover file when synced -> works offline)
        image = content.collection_image(target)
    except Exception:
        image = None
    return {"kind": "list", "name": name, "target": target, "order": order,
            "image": image, "episodes": episodes}



# --- static files + artwork proxy (the PWA) --------------------------------------

def artwork_roots():
    """Directories the artwork proxy may serve from: the episode cache,
    uploaded section logos, and any local folder that is a library target
    (their cover.jpg files)."""
    roots = [os.path.realpath(CACHE_DIR), os.path.realpath(ART_DIR)]
    for s in load_library()["sections"]:
        for e in s["entries"]:
            if os.path.isdir(e["target"]):
                roots.append(os.path.realpath(e["target"]))
    return roots


def artwork_allowed(path):
    real = os.path.realpath(path)
    return any(real == r or real.startswith(r + os.sep)
               for r in artwork_roots())


