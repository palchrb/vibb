"""go-librespot client — the ONE place that talks to the Spotify daemon."""

import glob
import hashlib
import json
import os
import subprocess
import re
import time
import urllib.parse
import urllib.request

from vibb.paths import STATE_DIR

API = os.environ.get("VIBB_GO_API", "http://127.0.0.1:3678")
CONFIG = os.environ.get("VIBB_GO_CONFIG", "")

# spotify:user:<id>:collection is Liked Songs (fork v0.1.9 lists and
# plays it). It has no share link — the URI is what a card carries.
URI_RE = re.compile(
    r"^spotify:(?:(?:track|album|playlist|artist|episode|show)"
    r":[A-Za-z0-9]+|user:[^:\s]+:collection)$")
LINK_RE = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z-]+/)?"
    r"(track|album|playlist|artist|episode|show)/([A-Za-z0-9]+)")


def is_spotify(target):
    return (target.startswith("spotify:") or "open.spotify.com" in target
            or "spotify.link/" in target)


_SHORTLINKS = {}  # resolved spotify.link redirects — to_uri runs in status
                  # polls and bookmark ticks, which must never re-fetch


def to_uri(target):
    """A share link/URI -> spotify:<type>:<id>, or None."""
    if URI_RE.match(target):
        return target
    if "spotify.link/" in target:  # short links redirect to open.spotify.com
        if target in _SHORTLINKS:
            target = _SHORTLINKS[target]
        else:
            with urllib.request.urlopen(target, timeout=10) as r:
                _SHORTLINKS[target] = target = r.url
    m = LINK_RE.search(target)
    return f"spotify:{m.group(1)}:{m.group(2)}" if m else None


def snapshot(uri):
    """A playlist's revision, asked of go-librespot's OWN session (fork
    v0.0.4: GET /cache/snapshot). Hex snapshot_id, or None for
    non-playlists. Raises when unreachable/never-logged-in — callers
    treat that as unknown and fail open. No Spotify Web API credentials
    involved, so this works on a box with no client-id configured."""
    q = urllib.parse.urlencode({"uri": uri})
    with urllib.request.urlopen(API + "/cache/snapshot?" + q,
                                timeout=5) as r:
        return (json.loads(r.read()) or {}).get("snapshot_id")


def go(path, timeout=5, body=None):
    """POST to the go-librespot API. Raises OSError when unreachable."""
    data = json.dumps(body).encode() if body is not None else b"{}"
    req = urllib.request.Request(API + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = r.read()
    # every command invalidates the /status cache below, so the reads
    # that FOLLOW a press (step confirmation, the screen's next poll)
    # are always fresh — only externally-caused changes can be seen up
    # to STATUS_TTL_S late
    _status_cache["val"] = None
    return out


def _conf_dir():
    return os.path.dirname(CONFIG) if CONFIG else ""


def _ctl(verb):
    try:
        subprocess.run(["systemctl", verb, "go-librespot"], timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pass


def logged_in_user():
    """The account go-librespot has persisted, read from state.json —
    available even before it has (re)connected, unlike the live /status."""
    try:
        with open(os.path.join(_conf_dir(), "state.json")) as f:
            return (json.load(f).get("credentials") or {}).get("username")
    except (OSError, ValueError):
        return None


def zeroconf_open():
    """True while the box advertises as an OPEN Spotify Connect device that
    any account on the LAN can claim — and, with persist_credentials, whose
    login overwrites ours (last-connector-wins). We close this once logged
    in so a passing phone can't hijack the box."""
    try:
        with open(CONFIG) as f:
            for line in f:
                m = re.match(r"\s*zeroconf_enabled:\s*(true|false)\b", line)
                if m:
                    return m.group(1) == "true"
    except OSError:
        pass
    return False


def _set_zeroconf(enabled):
    """Flip the zeroconf_enabled line in config.yml. tmp+rename so a
    battery brown-out mid-write can never truncate the config (a broken
    config.yml = go-librespot won't start at all); ownership is copied to
    the tmp file first — go-librespot runs as the login user, not root,
    and a root-owned config would break the next manual edit.
    Returns True when the file actually changed."""
    want = "true" if enabled else "false"
    try:
        with open(CONFIG) as f:
            src = f.read()
    except OSError:
        return False
    new, n = re.subn(r"(?m)^(\s*zeroconf_enabled:\s*)(?:true|false)\b",
                     lambda m: m.group(1) + want, src)
    if n == 0:  # key absent — prepend it
        new = f"zeroconf_enabled: {want}\n" + src
    if new == src:
        return False
    st = os.stat(CONFIG)
    with open(CONFIG + ".tmp", "w") as f:
        f.write(new)
    try:
        os.chown(CONFIG + ".tmp", st.st_uid, st.st_gid)
    except OSError:
        pass  # unprivileged test runs — ownership already ours
    os.replace(CONFIG + ".tmp", CONFIG)
    return True


def lock():
    """Close the open Connect door once an account is logged in, so nobody
    else can claim the box. No-op when already locked or not yet logged in,
    so it is safe to poll on a timer. Returns True only on the transition."""
    if not _conf_dir() or not zeroconf_open() or not logged_in_user():
        return False
    _set_zeroconf(False)
    _ctl("restart")
    return True


def logout():
    """PWA 'Switch account': forget the login AND re-open the Connect door
    so a DIFFERENT account can claim the box from the Spotify app (same
    wifi). The auto-lock (see lock()) closes the door again as soon as the
    new account is on, so the box is never open longer than necessary."""
    conf_dir = _conf_dir()
    if not conf_dir or not os.path.isdir(conf_dir):
        return {"ok": False, "error": "go-librespot config dir not found"}
    _ctl("stop")
    removed = []
    for name in ("credentials.json", "state.json"):
        try:
            os.remove(os.path.join(conf_dir, name))
            removed.append(name)
        except OSError:
            pass
    _set_zeroconf(True)
    _ctl("start")
    return {"ok": True, "removed": removed, "open": True}


# Three independent clocks poll /status — the screen's 1s tick via
# vibbd, mpris' 3s tick (also via vibbd) and the 5s bookmarker —
# and each paid its own round-trip + a request thread in go-librespot
# (QA power audit 2026-08-10 #6). One short shared cache collapses
# them; go() invalidates on every command so nothing a press causes is
# ever served stale. Failures are NOT cached: an unreachable player
# must be re-probed, not remembered.
STATUS_TTL_S = 1.0
_status_cache = {"at": 0.0, "val": None}


def status(timeout=5):
    """The /status dict, {} when unreachable or not logged in."""
    now = time.monotonic()
    if (_status_cache["val"] is not None
            and now - _status_cache["at"] < STATUS_TTL_S):
        return dict(_status_cache["val"])  # copy: a mutating caller
        #                                    must not poison the cache
    try:
        with urllib.request.urlopen(API + "/status", timeout=timeout) as r:
            val = json.loads(r.read()) or {}
    except (OSError, ValueError):
        return {}
    _status_cache["at"] = time.monotonic()
    _status_cache["val"] = val
    return dict(val)


def status_strict(timeout=2):
    """/status that RAISES when unreachable. status() folds every OSError
    into {} — right for a screen poll, wrong for a decision that must
    tell 'not running' (refused) from 'wedged' (timing out under a
    playing session). Refreshes the cache on success."""
    with urllib.request.urlopen(API + "/status", timeout=timeout) as r:
        val = json.loads(r.read()) or {}
    _status_cache["at"] = time.monotonic()
    _status_cache["val"] = val
    return dict(val)


def playing(st=None):
    st = status() if st is None else st
    return bool(st.get("track")) and not st.get("paused") and not st.get("stopped")


# --- resume bookmarks ------------------------------------------------------------
# Spotify's cloud only remembers positions for its own clients, so vibbd
# bookkeeps track+position while the box plays. One file PER CONTEXT: with
# the old single file, alternating between two playlist cards wiped each
# other's position ("it started from the top again").

LEGACY_BM_FILE = os.path.join(STATE_DIR, "spotify-bookmark.json")


def bm_path(context_uri):
    """The bookmark file for one context (playlist/album/show URI)."""
    h = hashlib.sha1(context_uri.encode()).hexdigest()[:12]
    return os.path.join(STATE_DIR, f"spotify-bm-{h}.json")


def bookmark_step(st, context_uri):
    """One bookmarker tick's decision (pure): the bookmark dict to persist
    for context_uri, or None to leave the file alone. Only a box-initiated
    session may write — go-librespot stamps play_origin 'go-librespot' on
    plays from OUR api; a phone streaming its own music through the box
    (Spotify Connect) carries the phone's origin, and used to get the box
    context stamped over the phone's track/position, corrupting resume."""
    track = st.get("track") or {}
    if not track or st.get("paused") or st.get("stopped"):
        return None
    if not context_uri:
        return None  # nothing to resume against later
    origin = st.get("play_origin")
    if origin and origin != "go-librespot":
        return None  # phone-driven — don't clobber the box bookmark
    return {"context_uri": context_uri,
            "uri": track.get("uri"),
            "position": track.get("position") or 0,
            "duration": track.get("duration") or 0,
            "name": track.get("name"),
            "artists": track.get("artist_names") or [],
            "artwork": track.get("album_cover_url"),
            "updated": time.time()}


def save_bookmark(bm):
    path = bm_path(bm["context_uri"])
    with open(path + ".tmp", "w") as f:
        json.dump(bm, f)
    os.replace(path + ".tmp", path)


def read_bookmark(context_uri):
    """The bookmark dict for a context (per-context file first, then the
    pre-per-context single file), or None. The legacy file may hold ANY
    old context, so it only counts when its context matches — serving it
    unchecked put another playlist's title/art/position on the now-
    playing card (field: 'shows something completely different')."""
    for path in (bm_path(context_uri), LEGACY_BM_FILE):
        try:
            with open(path) as f:
                bm = json.load(f)
        except (OSError, ValueError):
            continue
        if bm.get("context_uri") == context_uri:
            return bm
    return None


def clear_bookmark(context_uri):
    """Forget one context's position (stop / play --fresh)."""
    for path in (bm_path(context_uri), LEGACY_BM_FILE):
        try:
            os.remove(path)
        except OSError:
            pass


def clear_all_bookmarks():
    """Forget every position — the bookmarks belong to the old account."""
    for path in glob.glob(os.path.join(STATE_DIR, "spotify-bm-*.json")) \
            + [LEGACY_BM_FILE]:
        try:
            os.remove(path)
        except OSError:
            pass


PREV_RESTART_MS = 5000  # >5s into the track: prev restarts it — the same
                        # semantics the mpv side uses (daemon command())


CONTEXT_SETTLE_S = float(os.environ.get("VIBB_CONTEXT_SETTLE", "4"))


def context_tracks(uri, timeout=5, settle_s=None):
    """A context's track listing straight from go-librespot (GET
    /context/tracks) — names, artists and cover urls with NO Web API
    involved. Playlists, albums, artists (fork v0.1.7), shows (their
    episodes) and the Liked Songs collection (both v0.1.9) all
    enumerate the same way.

    v0.1.7 answers WITHOUT waiting on the network: the first call for
    an unknown context kicks off enumeration and returns ready=false
    with an EMPTY listing, and metadata fills in behind that. Rendered
    straight, that opens the song picker on nothing and makes the kid
    back out and try again. So poll the documented way — until the
    listing is ready AND something is actually renderable — bounded by
    settle_s, with the picker's 'Fetching episodes ...' frame already
    on screen.

    A listing that is ready but EMPTY (a show) returns at once: there
    is nothing to wait for. And 'ready' defaults to True when absent,
    so a pre-v0.1.7 binary (version skew, a rollback) behaves exactly
    as it did instead of polling out the whole budget.

    Raises OSError when unreachable; HTTPError 400 = not a listable
    uri, 404 = a pre-v0.1.2 fork."""
    url = API + "/context/tracks?uri=" + urllib.parse.quote(uri, safe="")
    deadline = time.monotonic() + (CONTEXT_SETTLE_S if settle_s is None
                                   else settle_s)
    while True:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            d = json.loads(r.read().decode())
        ready = d.get("ready", True)  # pre-v0.1.7: always ready
        renderable = d.get("cached") or not d.get("length")
        if (ready and renderable) or time.monotonic() >= deadline:
            return d
        time.sleep(0.25)


def skip(action, timeout=10):
    """One raw /player/next|prev call, nothing else — the v0.0.8 fast
    path. The fork's skip debounce owns burst semantics now, and its own
    rewind-vs-skip rule covers prev — command()'s dance below (status
    probe + 0.4s sleep + compensating second prev) cost 3-4 serialized
    API rounds PER PRESS, which clumped a mash into ~1s spaced leading
    edges and starved the debounce (field 2026-07-23 22:16: 0ms/401ms
    load pairs — coalescing worked, but only 1-2 presses/s arrived).
    Generous timeout: the call rides a background thread and a leading-
    edge load takes 1-1.5s through bluealsa; the 5s default produced
    spurious TimeoutErrors mid-settle."""
    go({"next": "/player/next", "prev": "/player/prev"}[action],
       timeout=timeout)


def command(action):
    """playpause/next/prev. prev follows standard player semantics, same
    as the mpv side: deep into the track it restarts it (seek 0); near
    the start it goes to the actual previous track. go-librespot's own
    prev applies its own rewind-vs-skip rule, so both halves are forced
    explicitly: seek for the restart, double-press when its first prev
    only rewound instead of skipping."""
    if action != "prev":
        go({"playpause": "/player/playpause", "next": "/player/next"}[action])
        return
    track = status().get("track") or {}
    if (track.get("position") or 0) > PREV_RESTART_MS:
        go("/player/seek", body={"position": 0})
        return
    before = track.get("uri")
    go("/player/prev")
    time.sleep(0.4)
    after = status().get("track") or {}
    # position is on the track object (ms) — same uri near 0 = only rewound
    if after.get("uri") == before and (after.get("position") or 0) < 2000:
        go("/player/prev")
