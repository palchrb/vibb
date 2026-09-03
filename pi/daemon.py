#!/usr/bin/env python3
"""vibbd — Vibb orchestration daemon: one authority for playback.

Owns the answer to "what is playing / what played last" and routes all
commands, so cards, buttons, the CLI and (later) the parent PWA behave
coherently instead of guessing at each other. HTTP API on 127.0.0.1:3679:

  POST /play       {"target": <any link/path>, "fresh": bool,
                    "episode": <id>}  episode = start the queue there
  POST /playpause  |  /pause  |  /next  |  /prev  |  /stop
  POST /shuffle    {"enabled": bool} — mpv reshuffles the playlist,
                   Spotify toggles shuffle_context
  POST /volume     {"volume": 0-100} or {"delta": +/-n} — routes to the
                   active source (mpv softvol / go-librespot volume)
  GET  /volume     current volume of the active source (0-100)
  POST /seek       {"position": <seconds>} or {"delta": +/-n} — absolute
                   wins; both clamp short of the end. Live streams and a
                   sonos renderer with no snapshot refuse with routed:null
  GET  /status     unified now-playing (source, title, position, ...)
  GET  /library    the parent-curated library (sections -> named links)
  PUT  /library    replace the library (validated, atomic write)
  POST /library/section-logo  {"id": <section>, "data": <base64|null>}
                   upload/remove a home-screen logo for a category
  GET  /expand?id=<entry>|target=<url>   entry -> playable episode list
                   with titles + cached flags (offline-aware menus)
  GET  /output     current audio output ("bt" or "local")
  POST /output     {"device": "bt"|"local", "fallback": bool} — mpv
                   switches live over IPC; fallback=true (btwatchd's
                   follow-the-speaker policy) is skipped without an I2S card;
                   go-librespot needs a config rewrite + service restart
  GET  /settings   box settings (screen timeout, idle shutdown, volume cap)
  PUT  /settings   update settings (validated; consumers re-read live)
  GET  /system     battery (PiSugar), disk/cache usage, wifi state, temps
  POST /system/wifi      {"enabled": bool} — rfkill wifi
  POST /system/shutdown  {"restart": bool} — graceful poweroff/reboot
  POST /wifi/reconnect {"secs"?} — on-demand: unblock the radio and wait
                      for a known network to join (offline-Spotify X); on
                      success clears spotify_offline + unparks go-librespot
  POST /wifi/scan     list nearby networks (ssid/signal/secured/known)
  POST /wifi/connect  {"ssid", "password"?} — join a network (nmcli);
                      leaves the setup hotspot first, restores it on failure
  POST /wifi/forget   {"ssid"} — delete the saved profile
  POST /wifi/add      {"ssid", "password"?} — save a profile WITHOUT the
                      network in range (pre-provision the cabin wifi);
                      auto-joins when first seen
  POST /spotify/logout   forget the Spotify login (drop credentials +
                         restart go-librespot) — the new account then picks
                         the box under Devices in the Spotify app
  POST /storytel/credentials {"email","password"} save the audiobook
                         account (or {"email":null} to clear); privileged,
                         never echoes the password
  POST /storytel/shelf   the account's audiobooks grouped into series, for
                         the PWA picker (privileged: reveals the library)
  POST /storytel/logout  forget the Storytel account
  GET  /storytel/status  {configured, queued, sync} — booleans/counts only
  POST /wifi/hotspot  {"enabled": bool} — the setup hotspot (Vibb-<host>).
                      Also auto-starts on fresh boxes: no saved wifi network
                      and nothing connected. A :80 redirect server + wildcard
                      DNS (dnsmasq-shared.d) pops the phone's captive portal
                      straight into the PWA.
  GET  /bt         known/paired/connected speakers + the configured one
  POST /bt/scan    scan ~20s, list nearby devices (pick one -> /bt/connect)
  POST /bt/pair    {"name"?} — one-button flow: auto-pair the single audio
                   device in pairing mode (play.sh's validated flow)
  POST /bt/lost    internal (btwatchd): the speaker's transport died —
                   stop mpv before it error-skips the queue, arm the
                   screen's "disconnected" choice popup
  POST /bt/visible {"secs"?} — incoming pairing mode: the box becomes
                   discoverable for ~2 min and accepts a pairing started
                   FROM a car/head unit; the new bond shows up in GET /bt.
                   Once BONDED, a device that connects itself while the
                   configured speaker is absent is auto-adopted as the
                   active speaker (btwatchd follow-the-connector, owner
                   request 2026-07-27) — an unbonded device never is
  POST /bt/connect {"mac"}  — connect a speaker; pairs first when the mac
                   is new (picked from a scan), routes audio to it
  POST /bt/forget  {"mac"}  — drop the bond (permanent)
  POST /bt/disconnect {"mac"} — hang up without forgetting
  POST /bt/rename  {"mac", "name"} — custom display name (blank resets);
                   shows in the PWA + on the screen (BlueZ Device1.Alias)

The library lives in /etc/vibb/library.json ON THE BOX — menus must
render (and cached content must play) with no internet at all. A future
parent cloud service is a sync mirror of this file, never the source.

Command routing:
  1. mpv session running (player.py child)  -> mpv IPC
  2. Spotify actively playing (also when started from the phone) -> go-librespot
  3. last source was Spotify                -> go-librespot
  4. otherwise, remembered target           -> re-play it (bookmark resumes)

Rule 4 is the fix for "short press after a stopped podcast wakes some
old Spotify track": a dead session's controls bring back what YOU last
played, at the position you left it.

Playback itself is delegated: /play spawns player.py, which routes
Spotify links to go-librespot and everything else to mpv-with-resume.
The daemon stays a thin, state-owning router.
"""

import base64
import functools
import json
import mimetypes
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The vibb package sits next to this script in the repo, or under
# /usr/local/lib/vibb-py when installed. Repo wins; exactly one is used.
_here = os.path.dirname(os.path.abspath(__file__))
for _p in (_here, "/usr/local/lib/vibb-py"):
    if os.path.isdir(os.path.join(_p, "vibb")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
        break
from vibb import backup as _backup  # noqa: E402 — state snapshots to restic
from vibb import audio as _audio  # noqa: E402
from vibb import content, mpv as _mpv, spotify as _spotify  # noqa: E402
from vibb import renderer as _renderer  # noqa: E402 — sonos axis+client
from vibb import spotify_web as _spotify_web  # noqa: E402
from vibb import storytel as _storytel  # noqa: E402 — audiobook source
from vibb.bookmarks import (episode_pos as _bm_episode_pos,  # noqa: E402
                              load_state as _bm_load,
                              save_state as _bm_save)
def _uptime():
    """Seconds since the KERNEL started — the only clock here that never
    jumps (the wall clock moves when the PiSugar RTC lands mid-boot)."""
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError):
        return 0.0


from vibb.paths import (  # noqa: E402
    ART_DIR, MEDIA_DIR, RUN_DIR, STATE_DIR, clock_trusted,
    go_restarted_within,
    note_go_restart)

# Module-level aliases: internal code (and the tests, which monkeypatch
# these names) keeps calling daemon.<helper>.
is_spotify = _spotify.is_spotify
go = _spotify.go
go_status = _spotify.status
spotify_playing = _spotify.playing
spotify_command = _spotify.command
spotify_skip = _spotify.skip
mpv_ipc = _mpv.ipc
mpv_get = _mpv.get

LAST_FILE = os.path.join(STATE_DIR, "last-play.json")
VOL_FILE = os.path.join(STATE_DIR, "volume.json")


def _local_volume(stored, pcm):
    """Volume to USE for this pcm — capped on the built-in speaker (and
    on every output while the audio policy self-test says fail-safety)."""
    return _cap_local_volume(
        stored, pcm, load_settings().get("local_fallback_cap", 35),
        everywhere=_audio.cap_everywhere())


def _go_volume_cap(pcm):
    """A live go-librespot reopen onto the HAT keeps the session's
    volume — the level a parent set for HEADPHONES. Re-apply the cap for
    the device it just landed on, at use (volume.json untouched), exactly
    like the mpv live retarget does (PLAN-pipewire-soloist.md §F, NEW-1:
    until 2026-09-02 Spotify reached the amplifier uncapped on every
    path). Outside ORCH.lock: the API can be slow and the screen's 1/s
    /status readers must not queue behind it."""
    if pcm != OUTPUT_PCMS["local"] and not _audio.cap_everywhere():
        return
    try:
        v = _local_volume(ORCH._volume_setting(), pcm)
        steps = go_status(timeout=2).get("volume_steps") or 65535
        go("/player/volume", timeout=2, body={"volume": round(v * steps / 100)})
        log(f"go-librespot on the speaker: volume capped to {v}")
    except OSError:
        pass
NOW_FILE = os.path.join(STATE_DIR, "now-playing.json")
QUEUE_FILE = os.path.join(STATE_DIR, "now-queue.json")

_QUEUE_CACHE = {"mtime": None, "data": None}

# poked on spotify plays: the bookmarker idles at a 30s heartbeat between
# sessions, which let a short play (<30s) end entirely between ticks —
# no bookmark ever written ("no spotify bookmark on disk" later)
_bm_wake = threading.Event()
_sonos_wake = threading.Event()  # poke the sonos poller (controls, switch)

# the supervisor's (and play-path's) verdict on actual internet — surfaced
# in /status as spotify_offline so the clients can SAY "no internet"
# instead of silently failing (wifi can be up while the WAN is dead)
_SPOT_OFFLINE = [False]


def _queue_map():
    """player.py's url -> {id,title,image} map for the running queue,
    parsed once per spawn (mtime-cached — /status polls every second)."""
    try:
        m = os.path.getmtime(QUEUE_FILE)
    except OSError:
        return None
    if _QUEUE_CACHE["mtime"] != m:
        try:
            with open(QUEUE_FILE) as f:
                _QUEUE_CACHE["data"] = json.load(f)
            _QUEUE_CACHE["mtime"] = m
        except (OSError, ValueError):
            return None
    return _QUEUE_CACHE["data"]
PORT = int(os.environ.get("VIBB_PORT", "3679"))
PORTAL_PORT = int(os.environ.get("VIBB_PORTAL_PORT", "80"))
# The parent PWA is served to the LAN (http://vibb.local:3679). Keep this
# port firewalled from the internet — the API is deliberately auth-less on
# the home network (a PIN gate is a product-phase addition).
BIND = os.environ.get("VIBB_BIND", "0.0.0.0")
# restart playback when it claims to play but makes no progress this long
STALL_S = float(os.environ.get("VIBB_STALL_S", "30"))
# how often the stall watchdog samples position + radio TX counters
STALL_POLL_S = float(os.environ.get("VIBB_STALL_POLL", "5"))
# frozen-position stalls on the BT output escalate to a link rebuild on
# the Nth CONSECUTIVE one (field 2026-08-03: a headset died without the
# chip ever reporting the disconnect — bluez kept listing the transport,
# _audio_ready kept saying yes, and the plain restart looped 12 times).
# 'Consecutive' anchors on the freeze POINT, not the exact position: each
# respawn resumes ~3s earlier from the bookmark, so equality never holds;
# progress PAST the anchor by FREEZE_PROGRESS_S is what proves life.
FREEZE_ESCALATE = int(os.environ.get("VIBB_FREEZE_ESCALATE", "2"))
FREEZE_PROGRESS_S = float(os.environ.get("VIBB_FREEZE_PROGRESS", "10"))
# resume-position display hold (see Orchestrator._settle_position): a
# bookmark below RESUME_MIN_S is never resumed, so nothing to hold; the
# hold releases once live is within TOL of the target, and never lasts
# longer than MAX_S after spawn
RESUME_MIN_S = float(os.environ.get("VIBB_RESUME_MIN", "20"))
# The resume overlap, in seconds. Norwegian read-aloud runs ~2.5 words/s,
# so 3s is about one clause — under the 5s the spotify layer already
# treats as "the same spot" (spotify.py PREV_RESTART_MS). Music repeats
# less: 3s of a song is a stutter, not a re-read.
RESUME_OVERLAP_SPEECH_S = float(os.environ.get("VIBB_RESUME_OVERLAP", "3"))
RESUME_OVERLAP_MUSIC_S = float(os.environ.get("VIBB_RESUME_OVERLAP_MUSIC",
                                              "1"))
RESUME_OVERLAP_LONG_S = float(os.environ.get("VIBB_RESUME_OVERLAP_LONG", "8"))
RESUME_LONG_GAP_S = float(os.environ.get("VIBB_RESUME_LONG_GAP", "120"))
# Above this duration, prev stops meaning "restart this track" and means
# only "previous track". Owner's number (2026-08-14). The gap it sits in
# is an order of magnitude wide — kids' podcast episodes run 10-40 min,
# audiobooks 8-12 h — so the exact value is not load-bearing; what is
# load-bearing is that a restart can never be issued against something
# long enough that losing the position costs hours.
PREV_RESTART_MAX_S = float(os.environ.get("VIBB_PREV_RESTART_MAX", "1800"))
# A bookmark may not collapse to the TOP of a track on its own. The
# signature of every way we have lost a position is the same: the
# speaker reports a few seconds in, because it restarted the track
# (a forgotten session, a blind resume, a refused seek). A human moving
# backwards does not look like that — seeking ten minutes back from hour
# three lands at 2h50m, not at 0 — so keying on "landed at the top"
# rather than on the SIZE of the drop is what makes this guard sharp
# instead of a threshold that fights real seeks.
# How close the speaker must land for a seek to count as confirmed, and
# how long we hold our own value while it gets there. A SOAP seek plus
# the poll cadence is a couple of seconds; 12 is slack without being a
# window in which a real divergence hides.
# How long a refused-seek hold may silence bookmark writes. Past this
# much real playback the listening outranks the position we failed to
# resume to — an unbounded hold loses the place just as surely.
BM_HOLD_MAX_S = float(os.environ.get("VIBB_BM_HOLD_MAX", "120"))
SONOS_SEEK_TOL_S = float(os.environ.get("VIBB_SONOS_SEEK_TOL", "6"))
SONOS_SEEK_HOLD_S = float(os.environ.get("VIBB_SONOS_SEEK_HOLD", "12"))
# Minimum spacing between seek SOAPs at the speaker — defense in depth
# behind the UI's own single-flight poster (PWA/API clients have no
# poster). Latest-target-wins already guarantees the final seek lands.
SONOS_SEEK_SPACING_S = float(os.environ.get("VIBB_SONOS_SEEK_SPACING",
                                            "0.7"))
BM_RESTART_FLOOR_S = float(os.environ.get("VIBB_BM_RESTART_FLOOR", "90"))
# ...and only when there was something substantial to lose.
BM_REGRESS_MIN_S = float(os.environ.get("VIBB_BM_REGRESS_MIN", "300"))
# How long an explicit user action (a /seek) keeps the guard open. The
# speaker can take a while to report the new position, and 30s was too
# tight — a deliberate seek back whose confirmation landed late would
# have been refused, and then every write after it too.
BM_USER_GRACE_S = float(os.environ.get("VIBB_BM_USER_GRACE", "180"))
POSITION_SETTLE_MAX_S = float(os.environ.get("VIBB_SETTLE_MAX", "20"))
POSITION_SETTLE_TOL_S = float(os.environ.get("VIBB_SETTLE_TOL", "3"))
# how often the boot-time session verdict re-checks the clock
BOOT_TICK_S = float(os.environ.get("VIBB_BOOT_TICK", "2"))
# a spotify session that reads 'empty' right after a timed-out control is
# very likely a SLOW TRACK LOAD, not a finished album — hold skips off the
# replay fallback for this long after any control timeout
SPOT_TIMEOUT_HOLD_S = float(os.environ.get("VIBB_SPOT_TIMEOUT_HOLD", "30"))
# and even without a recent timeout, re-read an 'empty' session after this
# beat before concluding the album truly ended (mid-load blips resolve)
EMPTY_RECHECK_S = float(os.environ.get("VIBB_EMPTY_RECHECK", "2"))
# how long after a spawn to trust player.py's published paused-state while
# mpv's IPC socket is still coming up (the ~1-3s tap->audio window), so the
# screen shows 'playing' at once instead of a dead card
MPV_START_GRACE_S = float(os.environ.get("VIBB_MPV_START_GRACE", "12"))
# how long after the daemon starts to prewarm mpv's decode path (idle, off
# the boot rush) so the first human play hits a warm page cache
PREWARM_DELAY_S = float(os.environ.get("VIBB_PREWARM_DELAY", "15"))
# What GET /artwork may serve. That endpoint's allowlist is directory
# based, and CACHE_DIR holds audio next to the covers.
ARTWORK_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
WEB_DIR = os.environ.get("VIBB_WEB") or (
    os.path.join(_here, "web") if os.path.isdir(os.path.join(_here, "web"))
    else "/usr/share/vibb/web")


def _tick(seconds):
    """All background loops wait through this seam. Tests monkeypatch
    daemon._tick to drive loops deterministically — patching the global
    time.sleep also hit OTHER live daemon threads (they stole scripted
    ticks and the fake could raise inside the arbiter), a real flake
    (QA review Q2)."""
    time.sleep(seconds)


def log(msg):
    print(f"vibbd: {msg}", flush=True)


def player_path():
    p = os.path.join(_here, "player.py")
    return p if os.path.exists(p) else "/usr/local/bin/vibb-player"


# --- moved to the vibb package; aliases keep internal call sites and the
# --- tests' daemon.<name> monkeypatching working unchanged ----------------------

from vibb import bt as _bt, btbus, netmgmt as _netmgmt  # noqa: E402
from vibb import token as _token  # noqa: E402 — privileged-endpoint gate
from vibb import library as _library  # noqa: E402 — BUSY_CHECK wiring
from vibb import radio as _radio  # noqa: E402 — shared-radio yield markers
from vibb.library import (  # noqa: E402
    artwork_allowed, expand_target, find_entry, library_with_covers,
    load_library, normalize_library, save_library, state_key, _cache_sweeper,
    _natural_order, _sync_wake)
from vibb.netmgmt import (  # noqa: E402
    HOTSPOT_PSK, HOTSPOT_SSID, set_wifi, start_hotspot,
    stop_hotspot, wifi_add, wifi_connect, wifi_forget, wifi_reconnect,
    wifi_scan, wifi_state, _wifi_watchdog)
from vibb.output import (  # noqa: E402
    OUTPUT_PCMS, OUT_FILE, audio_ready, current_output, _i2s_card_present,
    local_volume as _cap_local_volume, reopen_go_output,
    _retarget_go_librespot)
from vibb import sysinfo as _sysinfo  # noqa: E402 — cache-size invalidation
from vibb.sysinfo import (  # noqa: E402
    load_settings, plugged_cached, shutdown, system_status,
    update_settings, _battery_runtime_tracker)

MAC_RE = _bt.MAC_RE
bt_status = _bt.bt_status
bt_action = _bt.bt_action
bt_scan = _bt.bt_scan


# --- the orchestrator ----------------------------------------------------------

class Orchestrator:
    # Class-level defaults for optional state read on hot paths. Tests
    # build orchestrators with object.__new__ and set fields by hand, so
    # anything read outside __init__'s reach needs a default here or a
    # fixture somewhere gets an AttributeError — which on the poller
    # thread would be fatal, not just noisy.
    sonos_opt_pos = None
    sonos_bm_hold = None
    _seek_at = -1e9

    def __init__(self):
        self.lock = threading.Lock()
        self.child = None
        self.target = None
        self.source = None
        self.reverse = False
        self.resume = True  # library 'from start' entries set this False
        self.mpv_shuffle = False  # mpv has no queryable shuffle state
        self.spot_pending = None  # a freshly tapped spotify target is
        # loading: go-librespot still describes the PREVIOUS context —
        # /status shows the tapped entry's own identity meanwhile
        try:
            with open(LAST_FILE) as f:
                d = json.load(f)
            self.target, self.source = d.get("target"), d.get("source")
            self.reverse = bool(d.get("reverse"))
            self.resume = bool(d.get("resume", True))
            # The session stamp, captured HERE: _persist() rewrites
            # LAST_FILE (dropping it) the moment anything plays, and
            # ORCH is built at import time — before any thread runs.
            self.boot_stopped_at = float(d.get("stopped_at") or 0)
            if self.target:
                log(f"remembered last play: [{self.source}] {self.target}")
            log(f"orchestrator up at boot+{_uptime():.1f}s")
        except (OSError, ValueError):
            pass
        self.boot_stopped_at = getattr(self, "boot_stopped_at", 0.0)
        self.child_started = 0.0
        # Sonos renderer session: the poller's last /state snapshot (with
        # a monotonic stamp — a stale snapshot must read as NOT playing,
        # or the box never sleeps), and OUR queue for the url kind (the
        # box stays the sequencer for podcasts; sonos owns the queue only
        # for spotify sharelinks — owner decision 2026-08-09).
        self.sonos_snap = {}
        self.sonos_snap_at = 0.0
        self.sonos_queue = []   # [{'url','title','id','image'}]
        self.sonos_idx = None
        self.sonos_kind = None
        self.sonos_ctx = None        # canonical spotify context uri —
        # stored, never recomputed (to_uri can hit the network; the
        # poller's hot path must not)
        self.sonos_map_trusted = True  # positional jumps legal?
        self.sonos_opt_tr = None     # (transport, at): our own verb
        # holds against stale polls until confirmed or ~8s pass (QA A)
        self._sonos_bm_last = 0.0    # throttle: the poller wrote the
        # bookmark EVERY 5s tick — 720 SD bursts/hour (QA review)
        self.sonos_pending = None    # (uri, at): we JUST jumped —
        # the poller must not adopt the still-playing OLD track and yank
        # the index backwards (field 2026-08-09: mash flip-flop 11->10)
        self._sonos_step_want = None  # coalesced mash target
        self._sonos_stepping = False
        self._sonos_step_lock = threading.Lock()
        self.sonos_bm_hold = None    # (track_uri, min_pos): suppress
        # bookmark writes until playback passes min_pos — a refused seek
        # otherwise lets the 5s poll destroy a good bookmark (arch Q4)
        # last spotify control timeout; far past, NOT 0.0 — on a young
        # monotonic clock 0.0 would read as 'timed out seconds ago'
        self._spot_cmd_timeout_at = -1e9
        self._seek_at = -1e9         # last DELIBERATE seek; releases the
        # resume hold in _settle_position (same far-past reasoning above)
        self.sonos_opt_pos = None    # (position, at): our own seek, held
        # until the speaker lands near it — the twin of sonos_opt_tr
        self._sonos_seek_want = None  # coalesced mash target, like the
        self._sonos_seeking = False   # positional step above it
        self._crash_respawns = 0  # crashed-child heals this boot (max 2)
        threading.Thread(target=self._arbiter, daemon=True).start()
        threading.Thread(target=self._stall_watchdog, daemon=True).start()

    def _arbiter(self):
        """The box stays Spotify Connect-discoverable while mpv plays; if the
        user picks it from the phone mid-podcast, both would fight over the
        BT output. Watch for that takeover and yield mpv gracefully (its
        bookmark is saved, so the card resumes later).

        Two guards keep this from firing on the box's OWN Spotify (self.child
        is player.py for spotify targets too, so 'child alive + spotify
        playing' is NOT proof of a phone): only when the current source is
        mpv (a podcast is what's playing, so a Spotify session appearing IS
        an intrusion), AND the session carries a non-box play_origin. Without
        them the box's boot-resume into a Spotify playlist logged a phantom
        'spotify took over (phone)' and killed its own player (field
        2026-07-20 08:18:39)."""
        while True:
            _tick(4)
            try:
                with self.lock:
                    alive = self._mpv_alive()
                    source = self.source
                    age = time.monotonic() - self.child_started
                # only a podcast/local session can BE taken over; the box's
                # own Spotify child is not a takeover of anything
                if not alive or source != "mpv" or age < 10:
                    continue
                # grace period covered by age>=10s: player.py pauses spotify
                # right after starting; don't mistake that brief overlap
                st = go_status()
                origin = st.get("play_origin")
                phone = spotify_playing(st) and origin not in (
                    "go-librespot", "", None)
                if phone:
                    with self.lock:
                        if self._mpv_alive() and self.source == "mpv":
                            log("spotify took over (phone) — yielding mpv")
                            self._stop_child()
                            self.source = "spotify"
                            self._persist()
            except Exception as e:  # a dead arbiter = silent feature loss
                log(f"arbiter error: {e!r}")

    def _stall_watchdog(self):
        """A dropped BT speaker can wedge mpv: the process stays alive but
        audio writes block, the position freezes, and every button press
        routes into a wall — the box looks hung until someone reboots it.
        Watch for 'claims to be playing but no progress for STALL_S', then
        restart playback (the 3s bookmark resumes it in place) once the
        output is able to make sound again.

        A second failure mode leaves the position TICKING: bluez still
        says connected, bluealsa still lists the PCM, mpv keeps decoding —
        but nothing leaves the radio (a zombie transport). The controller's
        TX byte counter is ground truth there: A2DP moves ~35kB/s, so a
        counter that stays flat across STALL_S of claimed playback means
        the link is dead and must be torn down and rebuilt — waiting on
        _audio_ready() would never fire, since bluez keeps lying."""
        last_pos, last_change = None, time.monotonic()
        last_tx, last_tx_change = None, time.monotonic()
        crashed_since = 0.0  # first poll that saw the crashed child dead
        frz_streak, frz_anchor = 0, None  # consecutive bt frozen stalls
        while True:
            _tick(STALL_POLL_S)
            try:
                with self.lock:
                    alive = self._mpv_alive()
                    age = time.monotonic() - self.child_started
                if not alive or age < 30:  # startup grace: file/stream open
                    crashed_since = (self._heal_crashed_child(crashed_since)
                                     if not alive else 0.0)
                    last_pos, last_change = None, time.monotonic()
                    last_tx, last_tx_change = None, time.monotonic()
                    continue
                crashed_since = 0.0
                if self._crash_respawns:
                    # a respawned child that SURVIVED the startup grace is
                    # a success — hand the healer its budget back. Without
                    # this the 2/boot cap burned out permanently (field
                    # 2026-08-03: cap hit mid-evening, the next real crash
                    # stayed dead until reboot).
                    log("respawned player is stable — crash budget reset")
                    self._crash_respawns = 0
                paused = mpv_get("pause")
                pos = mpv_get("playback-time")
                now = time.monotonic()
                # deliberate pause is not a stall, and sends no audio —
                # the TX clock must not run while paused; an unresponsive
                # IPC (both None) is treated the same as a frozen position
                if paused is True:
                    last_pos, last_change = pos, now
                    last_tx, last_tx_change = None, now
                    continue
                zombie = False
                if pos is not None and pos != last_pos:
                    if frz_anchor is not None \
                            and pos > frz_anchor + FREEZE_PROGRESS_S:
                        # played PAST the old freeze point — that is real
                        # progress, not the bookmark re-approaching it
                        frz_streak, frz_anchor = 0, None
                    last_pos, last_change = pos, now
                    # the clock moves — but does anything leave the radio?
                    # (only the bt output routes through the controller)
                    if current_output()["output"] != "bt":
                        last_tx, last_tx_change = None, now
                        continue
                    tx = _bt.hci_tx_bytes()
                    # None = can't judge (no adapter/hciconfig); a lower
                    # value = counter reset or wrap — both restart the clock
                    if tx is None or last_tx is None or tx != last_tx:
                        last_tx, last_tx_change = tx, now
                        continue
                    if now - last_tx_change < STALL_S:
                        continue
                    zombie = True
                    log(f"playback stalled {int(now - last_tx_change)}s "
                        f"(position moves, radio TX flat) — rebuilding the "
                        f"bluetooth link and restarting player")
                else:
                    stalled = now - last_change
                    if stalled < STALL_S:
                        continue
                    # Frozen position on the BT output: a dead-but-
                    # CONNECTED transport looks exactly like this — the
                    # chip never reported the disconnect, bluez keeps
                    # listing the PCM, _audio_ready below answers yes,
                    # and a plain restart just freezes at the same spot
                    # (field 2026-08-03: 12 identical cycles against a
                    # powered-off headset). The Nth consecutive freeze
                    # with no progress past the anchor escalates to the
                    # zombie cure: tear the link down and rebuild it, so
                    # btwatchd/UI finally see a real speaker-away.
                    # (A wifi rebuffer can false-positive here — frozen
                    # position, healthy link — but only after
                    # FREEZE_ESCALATE whole stall windows, and the
                    # reconnect costs a beat, not the bookmark.)
                    if current_output()["output"] == "bt":
                        same = (frz_anchor is not None
                                and (pos is None
                                     or pos <= frz_anchor
                                     + FREEZE_PROGRESS_S))
                        frz_streak = frz_streak + 1 if same else 1
                        if not same:
                            frz_anchor = pos
                        if frz_streak >= FREEZE_ESCALATE:
                            zombie = True
                            frz_streak, frz_anchor = 0, None
                    else:
                        frz_streak, frz_anchor = 0, None
                    if zombie:
                        log(f"playback stalled {int(stalled)}s (position "
                            f"frozen on bt again — dead-but-connected "
                            f"transport) — rebuilding the bluetooth link "
                            f"and restarting player")
                    else:
                        log(f"playback stalled {int(stalled)}s (position "
                            f"frozen) — restarting player")
                with self.lock:
                    self._stop_child()  # bookmark survives (terminated flag)
                ready = False
                healed = False
                if zombie:
                    # bluez is lying (the PCM is still listed), so
                    # _audio_ready() would answer yes against a dead link
                    # and we'd respawn straight back into the zombie.
                    # Tear down + reconnect first, THEN trust the probe.
                    healed = True
                    _bt_recover("reconnect")
                for i in range(12):  # give a rebooting speaker ≤60s
                    ready = _audio_ready()
                    if ready:
                        break
                    # same self-heal as the player's racing guard: crash
                    # signature in the kernel log -> recover immediately,
                    # otherwise give a plain speaker dropout 20s first
                    if not healed and (i >= 4 or _bt._hci_crashed()):
                        healed = True
                        log("audio missing — running bluetooth recovery")
                        _bt_recover("ensure")
                    time.sleep(5)
                if not ready:
                    # speaker still gone: don't restart into a void — the
                    # bookmark is saved, any button press resumes later
                    log("output still not ready — leaving playback stopped")
                    last_pos, last_change = None, time.monotonic()
                    last_tx, last_tx_change = None, time.monotonic()
                    continue
                with self.lock:
                    if (self.target and self.source == "mpv"
                            and not self._mpv_alive()):
                        self._spawn(self.target, reverse=self.reverse,
                                    resume=self.resume,
                                    rewind=self._resume_overlap())
                last_pos, last_change = None, time.monotonic()
                last_tx, last_tx_change = None, time.monotonic()
            except Exception as e:
                log(f"stall watchdog error: {e!r}")

    def _heal_crashed_child(self, dead_since):
        """A player child that DIES — OOM kill, segfault — left 'playing'
        on the screen and silence in the room: the stall watchdog stood
        down on a dead child, and unlike a BT blip nothing auto-resumed
        (review 2026-07-18 R5). Respawn a CRASHED child: nonzero rc only
        (a deliberate stop clears self.child before this can see it, and
        a finished queue exits 0), and only when the persisted intent
        says audio was audibly playing (player.py's published pause
        state for this very target), the output can make sound, and the
        crash is fresh — the same no-surprise-audio window as a BT blip
        (BT_RESUME_S; an output that's away retries inside that window,
        then never again). Max 2 respawns per boot: a player that keeps
        dying has a real problem, and the bookmarked ghost state (press
        play to resume exactly there) is the honest fallback.

        Called from the watchdog's dead-child branch with the previous
        first-seen-dead stamp; returns the next stamp (0.0 = nothing to
        watch / respawned)."""
        with self.lock:
            child, target, source = self.child, self.target, self.source
            reverse, resume = self.reverse, self.resume
        if child is None or child.poll() in (None, 0):
            return 0.0  # no child, still alive, or a clean exit
        now = time.monotonic()
        dead_since = dead_since or now
        if now - dead_since > BT_RESUME_S or self._crash_respawns >= 2 \
                or source != "mpv" or not target:
            return dead_since
        try:  # persisted intent — never guess toward surprise audio
            with open(NOW_FILE) as f:
                published = json.load(f)
        except (OSError, ValueError):
            return dead_since
        if published.get("target") != target or published.get("paused"):
            return dead_since
        if not _audio_ready():
            return dead_since  # speaker away — retry within the window
        with self.lock:
            if self.child is not child or self._mpv_alive():
                return 0.0  # another path already spawned/stopped it
            rc = child.poll()
            if rc != _audio.SINK_WAIT_EXIT:
                self._crash_respawns += 1
            else:
                # 'the sink node was not there yet' (pipewire, AM-9) is
                # not a crash: the player declined to spawn into a void
                # and _audio_ready() above says the node exists NOW
                pass
            log(f"player died (rc {rc}) while playing — "
                f"respawning ({self._crash_respawns}/2 this boot)")
            self.child = None
            self._spawn(target, reverse=reverse, resume=resume,
                        rewind=self._resume_overlap())
        return 0.0

    def _persist(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = LAST_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"target": self.target, "source": self.source,
                       "reverse": self.reverse, "resume": self.resume,
                       "updated": time.time()}, f)
        os.replace(tmp, LAST_FILE)

    def _mpv_alive(self):
        return self.child is not None and self.child.poll() is None

    def _stop_child(self):
        _storytel_wake.set()   # a book just stopped/switched — mirror it now
        if self._mpv_alive():
            child = self.child
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # A wedged player is almost always an mpv grandchild
                # blocked in a write to a dead BT transport; its own 8s
                # escalation (player._stop) should have fired, so getting
                # here means the python parent itself is stuck. Kill the
                # WHOLE process group (start_new_session in _spawn) —
                # SIGKILLing just the parent orphaned the mpv, which kept
                # the bluealsa PCM held and turned every later spawn into
                # a 'Device or resource busy' cascade (field 2026-08-03).
                # The killpg runs BEFORE the wait() reaps the leader, so
                # the pgid cannot have been reused.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                # the PCM must actually be FREE before the next spawn:
                # wait out the group (grandchildren reap via init fast) —
                # signal 0 probes membership, sends nothing
                for _ in range(20):
                    try:
                        os.killpg(child.pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.1)
        self.child = None

    def _ensure_spotify_backend(self):
        """go-librespot may be parked by the offline supervisor (its tick
        is 60s — far too slow for a play tap). True when the unit is (or
        was just) started, False when there is genuinely no internet so
        the caller can fail FAST instead of a 30s silent session-wait."""
        try:
            if subprocess.run(["systemctl", "is-active", "--quiet",
                               "go-librespot"], timeout=10).returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            return True  # can't tell — let the normal path try
        if not _internet_up():
            _SPOT_OFFLINE[0] = True
            return False
        _SPOT_OFFLINE[0] = False
        try:
            subprocess.run(["systemctl", "start", "go-librespot"],
                           timeout=30)
            log("go-librespot was parked — started for the play request")
        except (OSError, subprocess.TimeoutExpired):
            pass
        return True

    def _resume_overlap(self, source=None):
        """How far to back up when resuming after an OUTAGE (never after
        a tap). The child should hear a word or two twice; missing a
        sentence is what reads as "it broke".

        This lives in the daemon because a player process cannot know
        it: every fault spawns a fresh one, so it sees neither how long
        the output was gone nor whether this is the third attempt in a
        minute. Speech gets a clause, music gets a beat, a long outage
        gets a run-up, and a flapping speaker gets nothing — ten faults
        would otherwise walk the story ten clauses backwards."""
        src = source or self.source
        lost_at = _BT_WAIT.get("lost") or 0.0
        gone_s = (time.monotonic() - lost_at) if lost_at else 0.0
        if self._crash_respawns > 1 and gone_s < 15:
            return 0.0                       # flap guard
        if gone_s > RESUME_LONG_GAP_S:
            return RESUME_OVERLAP_LONG_S     # they stopped listening
        return (RESUME_OVERLAP_MUSIC_S if src == "spotify"
                else RESUME_OVERLAP_SPEECH_S)

    def _spawn(self, target, fresh=False, episode=None, reverse=False,
               cache=None, resume=True, exact=False, rewind=0.0):
        args = [sys.executable, player_path()]
        if fresh:
            args.append("--fresh")
        if not resume:
            args.append("--no-resume")
        if exact:
            args.append("--exact")
        if rewind:
            args += ["--rewind", f"{rewind:g}"]
        if reverse:
            args.append("--reverse")
        if episode:
            args += ["--episode", episode]
        if cache is not None:
            args += ["--cache", str(cache)]
        args.append(target)
        if is_spotify(target) or target.startswith(("http://", "https://")):
            # NOT storytel: it is download-only, so a play is a local file,
            # not a CDN pull — the network-heavy moment is the sweeper's
            # download, which already yields to playback via _busy().
            _radio.touch_busy()  # network-heavy start: blind BT pages yield
            _PS_KICK.set()       # and wifi power save flips off NOW
        # Own process group: _stop_child can then SIGKILL the WHOLE tree
        # when the player wedges — killing just the python parent left a
        # write-blocked mpv grandchild holding the bluealsa PCM (field
        # 2026-08-03: every later spawn hit 'Device or resource busy').
        self.child = subprocess.Popen(args, start_new_session=True)
        self.child_started = time.monotonic()

    def play(self, target, fresh=False, episode=None, reverse=False,
             cache=None, resume=True, boot=False):
        # Renderer-first, ahead of every local shortcut: with a sonos
        # renderer there is no mpv child and no local spotify session to
        # resume — falling through would spawn a LOCAL player next to the
        # remote one (QA silent-pass #1, the double-playback trap). Boot
        # resume is the exception: reconciliation adopts a live remote
        # session instead of replaying it (never start audio in a room
        # nobody asked for at boot).
        # Same tile, already OUR live session on the speaker: pressing A
        # in the carousel must not re-transfer the queue — re-expand,
        # re-mint the signed url, re-push the DIDL — which is the audible
        # hiccup (owner 2026-08-18). This mirrors the mpv/spotify
        # "already loaded -> unpause" shortcuts below; the Sonos path
        # simply never had one, though handle_carousel's own comment
        # states the intent ("A never restarts anything"). Deliberately
        # NARROW — anything not provably a steady live session of ours
        # falls through to sonos_start_target, whose full transfer IS the
        # heal for a drifted map, a foreign takeover or a stale session:
        #  - not fresh / not episode: "from the start" and an explicit
        #    episode pick must still respawn (the local shortcuts' rule)
        #  - _sonos_fresh + ours: the same 15s notion the playpause
        #    branch trusts; stale reads as not-ours, and a foreign
        #    session must keep healing via the full transfer
        #  - no press in flight: within the poller's own 8s settle
        #    window (sonos_pending / sonos_opt_tr) the speaker may still
        #    report the OLD track, so "already playing this" could be
        #    yesterday's truth — fall through, exactly today's behaviour
        #  - map/queue intact: a sharelink press with sonos_map_trusted
        #    False is the RE-SYNC and must not be swallowed; the url
        #    kind needs its queue mapping (sonos_idx) for the same reason
        if (_renderer.is_sonos() and not boot and not fresh
                and episode is None and target == self.target
                and self.source == "sonos"):
            snap = self._sonos_fresh() or {}
            now = time.monotonic()
            settling = ((self.sonos_pending is not None
                         and now - self.sonos_pending[1] < 8)
                        or (self.sonos_opt_tr is not None
                            and now - self.sonos_opt_tr[1] < 8))
            intact = (self.sonos_map_trusted
                      if self.sonos_kind == "spotify_sharelink"
                      else self.sonos_idx is not None)
            if snap.get("ours") and intact and not settling:
                if snap.get("transport") == "PLAYING":
                    log(f"play (already on sonos) -> no-op: {target}")
                    return {"source": "sonos", "target": target,
                            "resumed": True}
                if snap.get("transport") == "PAUSED_PLAYBACK":
                    # resume in place — one verb, not a re-transfer (the
                    # playpause branch's resume side, minus its pause
                    # half). A refusal (uid moved, speaker gone) falls
                    # through: the full start IS the heal.
                    try:
                        code, _r = _renderer.post(
                            "/resume", {"if_uid": _renderer.read().get("uid")})
                    except _renderer.SidecarDown:
                        code = None
                    if code == 200:
                        if self.sonos_snap:
                            self.sonos_snap = dict(self.sonos_snap,
                                                   transport="PLAYING")
                            self.sonos_snap_at = time.monotonic()
                        self.sonos_opt_tr = ("PLAYING", time.monotonic())
                        _sonos_wake.set()
                        log(f"play (already on sonos) -> resume: {target}")
                        return {"source": "sonos", "target": target,
                                "resumed": True}
        if _renderer.is_sonos() and not boot:
            _radio.touch_busy()
            return self.sonos_start_target(target, episode=episode)
        # The backend probe/start (systemctl is-active, and up to 30s of
        # systemctl start against a parked unit) must never run under
        # ORCH.lock: every /status reader — the screen's 1/s poll —
        # queued behind it (review 2026-07-18 R2). Probing BEFORE the
        # lock is equivalent: a parked unit can't satisfy the
        # resume-in-place shortcut anyway (its API is down), and when
        # the shortcut does hit, the extra is-active probe is a no-op.
        backend_ok = not is_spotify(target) or self._ensure_spotify_backend()
        if not boot:
            _SESSION["live"] = False   # a tap ends the power-on session
        with self.lock:
            if boot:
                # NO CALLER TODAY. Boot no longer starts audio at all
                # (owner 2026-08-18: a reboot lands PAUSED on the
                # now-playing screen, one tap continues), so _boot_resume
                # and the sonos reconcile's box-resume are both gone. The
                # guard is kept because it costs nothing and is the
                # documented contract for any future boot starter: it is
                # the LAST of the possible starters, behind the A-press
                # replay (command rule 4) and the transport-up blip
                # resume, which both spawn under this same lock and stamp
                # child_started. If anyone beat us here, our job is done —
                # proceeding would hit play()'s stop-and-respawn shortcuts
                # against a child whose IPC/session isn't up yet and
                # audibly restart it (triple-start race, architect review
                # 2026-07-18).
                if self.child_started > 0:
                    log("boot resume: playback already started — standing "
                        "down")
                    return {"status": "already-started"}
                try:
                    if spotify_playing(go_status(timeout=2)):
                        log("boot resume: spotify already playing — "
                            "standing down")
                        return {"status": "already-started"}
                except OSError:
                    pass  # api busy/down — the guards above suffice
            _kick_bt_connect()  # pressing play = wanting sound NOW
            # Same card back in the slot (or same link replayed): if its
            # session is still loaded, unpause instead of restarting.
            # An explicit episode pick must respawn — the user asked for a
            # specific place in the queue, not "continue".
            if (not fresh and not episode and target == self.target
                    and self.source == "mpv" and self._mpv_alive()):
                try:
                    r = mpv_ipc(["set_property", "pause", False])
                    if r.get("error") == "success":
                        log(f"play (already loaded) -> unpause: {target}")
                        return {"source": "mpv", "target": target,
                                "resumed": True}
                except OSError:
                    pass  # IPC gone but child alive? fall through to respawn
            # Same shortcut for Spotify: a live session for this target
            # continues in place (unpause) — a respawn would reload the
            # context and seek, an audible 2-3s hiccup for nothing.
            if (not fresh and not episode and target == self.target
                    and self.source == "spotify" and is_spotify(target)):
                try:
                    st = go_status()
                    if (st.get("track") or {}) and not st.get("stopped"):
                        if st.get("paused"):
                            go("/player/resume")
                        log(f"play (already loaded) -> resume: {target}")
                        return {"source": "spotify", "target": target,
                                "resumed": True}
                except OSError:
                    pass  # session gone — fall through to respawn (bookmark)
            if is_spotify(target) and not backend_ok:
                # parked and genuinely offline: say so NOW — spawning a
                # player that waits 30s for a session that cannot come
                # just looks like a dead box (field report)
                log("play: no internet — spotify can't start")
                return {"source": "spotify", "target": target,
                        "error": "no-internet"}
            if target != self.target:
                # switching to a DIFFERENT context: flush the outgoing
                # spotify position first. The bookmarker thread isn't torn
                # down on a switch (unlike player.py on the mpv side) — it
                # just moves to the new target and drops the old bm_pending,
                # so the last <=30s of the previous url (incl. a seek just
                # made) would die with the throttle. Same gap the reboot
                # flush closes, triggered by a switch instead of a TERM.
                _flush_spotify_bookmark()
            self._stop_child()
            self._spawn(target, fresh, episode, reverse, cache, resume)
            self.mpv_shuffle = False  # fresh queue plays in order
            self.target = target
            self.reverse = reverse
            self.resume = resume
            self.source = "spotify" if is_spotify(target) else "mpv"
            self.spot_pending = None
            if self.source == "spotify":
                # remember what go-librespot is switching FROM: until the
                # loaded track changes, its /status still describes the
                # previous context and must not reach the now-playing card
                try:
                    pre = (go_status().get("track") or {}).get("uri")
                except Exception:
                    pre = None
                self.spot_pending = {"pre_uri": pre, "at": time.monotonic()}
                _bm_wake.set()  # bookmark even a short session
            self._persist()
            log(f"play [{self.source}] {target}"
                + (f" (episode {episode})" if episode else ""))
            return {"source": self.source, "target": target}

    def _volume_setting(self):
        """The volume the user chose (volume.json), not what is in use."""
        try:
            with open(VOL_FILE) as f:
                return max(0, min(100, round(json.load(f)["volume"])))
        except (OSError, ValueError, KeyError, TypeError):
            return 100

    def _save_volume(self, v):
        """Remember the box volume so player.py can start mpv at it."""
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = VOL_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"volume": v}, f)
            os.replace(tmp, VOL_FILE)
        except OSError:
            pass

    def volume(self, absolute=None, delta=None):
        """One volume knob for the box: set/adjust whatever is active.
        mpv gets its softvol (0-100); Spotify gets go-librespot's volume
        scaled from our 0-100 to its volume_steps."""
        cap = load_settings()["volume_cap"]  # child-safety ceiling
        if _audio.cap_everywhere():
            # AM-7: a safety drift in the audio policy — the live knob
            # cannot exceed the landing cap anywhere until the next green
            # self-test (the value shown stays what the user chose)
            cap = min(cap, load_settings().get("local_fallback_cap", 35))
        if self.source == "sonos":
            # No cap on the remote renderer (owner decision 2026-08-09):
            # the amplifier is a family speaker in a shared room, not the
            # box against a child's ear. Deltas work off the poller's
            # last-seen volume; someone turning the knob in the Sonos app
            # is reported, never fought.
            try:
                if absolute is None:
                    # base on our own last SET first: the poll refreshes
                    # the speaker volume only every 5s, so a press burst
                    # computed the same target from the same stale base —
                    # "every press does nothing, then one works" (field
                    # 2026-08-09)
                    opt = getattr(self, "_sonos_vol_opt", None)
                    cur = opt[0] if opt else None  # our own last SET —
                    # never expires (cleared on renderer/speaker change)
                    if cur is None:
                        # even a STALE snapshot beats guessing 50: the
                        # seeded snapshots wiped the volume field, and
                        # the 50-fallback made the first press JUMP the
                        # speaker (field 2026-08-09 evening)
                        cur = (self.sonos_snap or {}).get("volume")
                    absolute = (50 if cur is None else cur) + delta
                v = max(0, min(100, round(absolute)))
                code, _r = _renderer.post(
                    "/volume", {"v": v,
                                "if_uid": _renderer.read().get("uid")})
                _sonos_wake.set()
                if code == 200:
                    self._sonos_vol_opt = (v, time.monotonic())
                    return {"routed": "sonos", "volume": v}
                return {"routed": "sonos", "volume": None,
                        "error": _r.get("error")}
            except _renderer.SidecarDown:
                return {"routed": None, "volume": None,
                        "error": "sonos-sidecar-down"}
        with self.lock:
            if self._mpv_alive() and self.source == "mpv":
                try:
                    if absolute is None:
                        cur = mpv_get("volume")
                        absolute = (100 if cur is None else cur) + delta
                    v = max(0, min(cap, round(absolute)))
                    r = mpv_ipc(["set_property", "volume", v])
                    if r.get("error") == "success":
                        self._save_volume(v)
                        log(f"volume -> mpv {v}")
                        return {"routed": "mpv", "volume": v}
                except OSError:
                    pass  # child starting up; fall through to spotify
            st = go_status()
            steps = st.get("volume_steps") or 65535
            if absolute is None:
                absolute = (st.get("volume") or 0) * 100 / steps + delta
            v = max(0, min(cap, round(absolute)))
            try:
                go("/player/volume", body={"volume": round(v * steps / 100)})
                self._save_volume(v)
                log(f"volume -> spotify {v}")
                return {"routed": "spotify", "volume": v}
            except OSError:
                log("volume: no active player")
                return {"routed": None, "volume": None}

    def get_volume(self):
        if self.source == "sonos":
            opt = getattr(self, "_sonos_vol_opt", None)
            v = (opt[0] if opt
                 else (self.sonos_snap or {}).get("volume"))
            _sonos_wake.set()  # refresh the real number for the card
            return {"routed": "sonos", "volume": v}
        with self.lock:
            if self._mpv_alive() and self.source == "mpv":
                v = mpv_get("volume")
                if v is not None:
                    return {"routed": "mpv", "volume": round(v)}
        st = go_status()
        if st:
            steps = st.get("volume_steps") or 65535
            return {"routed": "spotify",
                    "volume": round((st.get("volume") or 0) * 100 / steps)}
        return {"routed": None, "volume": None}

    # --- Sonos renderer ---------------------------------------------------
    # The renderer axis is ORTHOGONAL to output: output stays local|bt
    # ("where the box plays when it comes back"), renderer says who makes
    # sound right now. Never add "sonos" to OUTPUT_PCMS — player.py:144
    # would read pcm null and output.py:88 silently falls back to bt,
    # which is the double-playback trap (architect review 2026-08-08).

    SONOS_STALE_S = 15.0  # a snapshot older than this reads as NOT playing

    def _sonos_fresh(self):
        snap = self.sonos_snap
        return (snap if snap and (time.monotonic() - self.sonos_snap_at)
                < self.SONOS_STALE_S else None)

    def _sonos_stream_moved(self, snap):
        """Act on the sidecar's migration hint: the owner's son removed
        the room the session started on, Sonos promoted another member,
        and the sidecar's probe verified OUR stream lives there now
        (stage B2, owner use case 2026-08-23). The DAEMON owns identity:
        write renderer.json, then re-attach the sidecar session with the
        EXISTING /adopt (zero transport commands — the music never
        stopped). Returns True when it acted (the caller re-ticks).

        Guards, each load-bearing:
        - renderer must still be sonos AND its uid must equal the
          SNAPSHOT's session uid: a stale hinted snapshot read after the
          user re-picked a different room in the picker must be a no-op
          (the sidecar's /play already cleared the hint and moved on).
        - snapshot freshness: never act on a ghost.
        - the hint's uri (SESSION.uri echoed) rides into /adopt — the
          snapshot's own uri is empty during STOPPED and would kill
          ours-detection for the rest of the session."""
        mv = snap.get("stream_moved")
        if not mv or not mv.get("uid"):
            return False
        rd = _renderer.read()
        if (rd.get("renderer") != "sonos"
                or rd.get("uid") != snap.get("uid")
                or mv["uid"] == rd.get("uid")):
            return False
        if (snap.get("stale_s") is None
                or snap["stale_s"] >= 12):
            return False
        # adopt FIRST, identity second: a failed adopt then changes
        # nothing at all, and the hint (still published) retries next
        # tick. The sub-ms window where the sidecar holds the new uid
        # while renderer.json still names the old one just 409s a verb
        # harmlessly — the reverse order would strand a rewritten
        # renderer.json behind this method's own uid-match guard.
        try:
            _renderer.post("/adopt", {"uid": mv["uid"],
                                      "kind": self.sonos_kind,
                                      "uri": mv.get("uri")})
        except _renderer.SidecarDown:
            return False
        _renderer.write("sonos", uid=mv["uid"], name=mv.get("name"))
        self._sonos_vol_opt = None   # new speaker, new volume world
        log(f"sonos: stream moved to {mv.get('name')} ({mv['uid']}) — "
            "following")
        return True

    def _sonos_position(self):
        """Last MEASURED position (+ extrapolation while PLAYING). Never
        extrapolates past ~60s of staleness, never invents from STOPPED."""
        snap = self.sonos_snap
        # A seek we issued but the speaker has not reached yet: show the
        # target, PINNED — no extrapolation on top, or the bar creeps
        # forward between ticks and snaps back on each one. Dropped the
        # moment the track changes, so a stale target can never be
        # painted onto (or bookmarked as) the next book.
        opt = self.sonos_opt_pos
        if opt and time.monotonic() - opt[1] <= SONOS_SEEK_HOLD_S \
                and opt[2] == snap.get("uri"):
            return opt[0]
        rel = snap.get("rel_s")
        if rel is None or snap.get("transport") not in ("PLAYING",
                                                        "PAUSED_PLAYBACK"):
            return None
        age = time.monotonic() - self.sonos_snap_at
        if snap.get("transport") == "PLAYING" and age < 60:
            # add the sidecar-side measurement age too: rel_s was read up
            # to POLL_S before we fetched it, and dropping that lag left
            # the bar 2-5s behind the Sonos app, constantly (field
            # 2026-08-09; architect G1-b)
            rel = rel + age + (snap.get("stale_s") or 0)
        dur = snap.get("dur_s")
        return min(rel, dur) if dur else rel

    def _sonos_refresh_live(self):
        """One live probe (~1.5s budget in the sidecar) so the bookmark
        gets the EXACT second instead of a poll up to 5s stale. Any
        failure keeps the snapshot we have — last measured beats
        nothing, and the switch must never hang on a speaker."""
        try:
            snap = _renderer.get("/state?live=1", timeout=2.5)
            if snap.get("armed") and snap.get("rel_s") is not None:
                self.sonos_snap = snap
                self.sonos_snap_at = time.monotonic()
        except (_renderer.SidecarDown, ValueError):
            pass

    def _sonos_bookmark_now(self, force=True):
        """Persist the last measured position — called BEFORE any Stop or
        renderer change (after a Stop the transport reads 0:00, and
        bookmarking that is the most damaging ordering bug available)."""
        snap = self.sonos_snap
        if (self.sonos_idx is None or not self.target
                or not snap.get("ours")):
            return
        rel = snap.get("rel_s")  # measured only — never the extrapolation
        if rel is None or snap.get("transport") == "STOPPED":
            return
        if snap.get("transport") == "PLAYING":
            rel = float(rel) + (snap.get("stale_s") or 0)
        if not force and time.monotonic() - self._sonos_bm_last < 25:
            return  # SD hygiene: same 30s-class budget as bm_throttle
        self._sonos_bm_last = time.monotonic()
        if self.sonos_idx >= len(self.sonos_queue):
            return  # bounds: never let a poller tick raise (QA F1 note)
        ep = self.sonos_queue[self.sonos_idx]
        if self.sonos_kind == "spotify_sharelink":
            if not self.sonos_ctx:
                return
            hold = self.sonos_bm_hold
            if hold and hold[0] == ep["id"] and float(rel) < hold[1]:
                return  # refused seek: never overwrite the good position
            self.sonos_bm_hold = None
            _spotify.save_bookmark({
                "context_uri": self.sonos_ctx, "uri": ep["id"],
                "position": int(float(rel) * 1000),  # ms, like the box
                "duration": int((snap.get("dur_s") or 0) * 1000) or None,
                "name": ep.get("title"), "artists": [],
                "artwork": ep.get("image")})
            return
        if self.sonos_kind != "url":
            return
        # The SAME refused-seek guard the sharelink branch above has. It
        # was never wired to this branch, so a resume whose seek the
        # speaker refused played from the top and the next poll wrote
        # that near-zero position straight over the good one: 52 minutes
        # into a Harry Potter book became 39 seconds (field 2026-08-15).
        # Podcasts had this hole too; a long audiobook is just where it
        # finally hurt enough to see.
        hold = self.sonos_bm_hold
        if hold and hold[0] == ep.get("id") and float(rel) < hold[1]:
            # BOUNDED. Unbounded, a resume refused at 50 minutes meant no
            # bookmark for the next 50 minutes of real listening — and
            # switching back to the box then jumped FORWARD over what the
            # child had just heard. Losing the place wearing the other
            # hat (QA 2026-08-15). Past BM_HOLD_MAX_S of actual playback
            # the listening is real and outranks the stale position.
            if len(hold) < 3 or time.monotonic() - hold[2] < BM_HOLD_MAX_S:
                return  # refused seek: never overwrite the good position
            log("sonos: the refused-seek hold expired — this is real "
                "listening now, bookmarking it")
        self.sonos_bm_hold = None
        # A BOOKMARK MAY NOT COLLAPSE TO THE TOP OF A TRACK ON ITS OWN.
        # Belt and braces over the hold above, which only covers the one
        # cause we know by name (a refused seek); the speaker found three
        # other ways to report a few seconds in — a session it forgot, a
        # blind resume onto an empty queue, a track it re-opened — and
        # each silently destroyed the child's place (field 2026-08-15).
        #
        # Keyed on WHERE it landed, not on how far it fell. A human going
        # backwards does not land at the top: seeking ten minutes back
        # from hour three lands at 2h50m. So a threshold on the size of
        # the drop would fight real seeks (the seek card steps up to five
        # minutes) while this does not. And the deliberate ways to reach
        # the top announce themselves — ORCH.seek stamps _seek_at, and
        # starting an episode arms the hold above.
        try:
            prev = _bm_episode_pos(_bm_load(state_key(self.target)) or {},
                                   ep.get("id"), ep["url"])
        except Exception:
            prev = 0.0
        if (float(rel) < BM_RESTART_FLOOR_S and prev > BM_REGRESS_MIN_S
                and time.monotonic() - self._seek_at > BM_USER_GRACE_S):
            log(f"sonos: the speaker restarted the track ({int(prev)}s -> "
                f"{int(rel)}s) and nothing asked for it — keeping the "
                "bookmark")
            return
        # Pass the duration ONLY when the SPEAKER measured it. save_state
        # deletes an episode slot when pos > duration - RESUME_MIN_S, and
        # a shelf-supplied length short by half a minute would destroy
        # the bookmark of a ten-hour book near its end. Blanket-None was
        # too blunt the other way: a Sonos reports a real duration for an
        # ordinary podcast (field 2026-08-15: 1352s), and dropping it
        # meant an episode played to the end kept its bookmark instead of
        # resetting for the next tap.
        _bm_save(state_key(self.target), ep["url"], float(rel),
                 episode_id=ep.get("id"),
                 duration=None if snap.get("dur_from_shelf")
                 else snap.get("dur_s"))

    def _sonos_body(self, ep, uid, start_s):
        """/play body for one queue entry (contract: tests/sonos_contract).
        Series ride the NRK service (x-sonos-http); plain urls stream from
        origin. Artwork must be an http url — Sonos cannot fetch a cached
        local path and does not resolve .local."""
        m = re.match(r"https?://radio\.nrk\.no/serie/([a-z0-9_-]+)",
                     self.target or "", re.I)
        art = ep.get("image")
        if art and not str(art).startswith("http"):
            art = None
        album = None
        try:  # the library entry's display name, when the target has one
            lib = load_library()
            album = next((e.get("name") for s in lib["sections"]
                          for e in s["entries"]
                          if e.get("url") == self.target), None)
        except Exception:
            pass
        body = {"uid": uid, "title": ep.get("title"),
                "album": album, "art": art, "start_s": start_s}
        if m and ep.get("id"):
            body.update(kind="nrk_program", uri=self.target,
                        series=m.group(1), program_id=ep["id"])
        elif _storytel.is_storytel(self.target or ""):
            # A storytel book is a LOCAL file; a Sonos cannot play a path.
            # Mint the signed CDN url HERE, milliseconds before the SOAP
            # /play, because it is short-lived — that timing is the whole
            # trick, and it means the box need not have downloaded the
            # book at all to play it in another room.
            #
            # NEVER raises: this is called from _sonos_step_worker and
            # _sonos_poller, neither of which guards it, and an escape
            # there kills the thread — leaving next/prev dead until a
            # daemon restart, or losing snapshots and bookmarks for the
            # session. login() raises RuntimeError (unconfigured,
            # refused, inside the refused-login cooldown) as well as
            # OSError, so both are caught.
            try:
                body.update(kind="url",
                            uri=_storytel.asset_url(ep["id"], timeout=6),
                            art=ep.get("art_url") or art,
                            # We KNOW the length from the shelf; a Sonos
                            # asked to derive it from a signed url with no
                            # file extension reports 0:00 until it has
                            # buffered, and the screen draws no progress
                            # bar without a duration (field 2026-08-15:
                            # right seconds, no orange).
                            duration_s=ep.get("dur_s"))
            except (OSError, RuntimeError) as e:
                return {"error": f"storytel url unavailable: {e}"}
        else:
            body.update(kind="url", uri=ep["url"])
        return body

    def _sonos_play_entry(self, idx, start_s=0.0):
        """Push queue entry idx to the speaker. Lock NOT held (SOAP to a
        sleeping speaker takes seconds — same discipline as set_output's
        slow half)."""
        rd = _renderer.read()
        if rd["renderer"] != "sonos":
            return {"error": "renderer is not sonos"}
        ep = self.sonos_queue[idx]
        if self.sonos_kind == "spotify_sharelink":
            if not self.sonos_map_trusted:
                # drift: re-establish the invariant with a fresh transfer
                # on this explicit press (never on a timer — arch R3a)
                return self._sonos_start_spotify(self.target, rd,
                                                 episode=ep["id"])
            code, resp = _renderer.post(
                "/queue_play", {"index": idx, "start_s": start_s,
                                "if_uid": rd.get("uid")})
            if code != 200:
                # do NOT advance sonos_idx onto a track that is not
                # playing; distrust the map and let the next press heal
                self.sonos_map_trusted = False
                log(f"sonos: queue_play refused ({code}) — map distrusted")
                return {"error": resp.get("error") or f"http-{code}"}
            if self._sonos_step_want is None:
                self.sonos_idx = idx  # no newer press — confirm
            # else: a newer optimistic index is already on the card;
            # writing the completed (older) jump over it blinked the
            # title backwards mid-mash
            self.sonos_bm_hold = None
            self.sonos_pending = (ep["id"], time.monotonic())
            self.sonos_opt_tr = ("PLAYING", time.monotonic())
            self.sonos_snap = {
                "armed": True, "uid": rd.get("uid"),
                "kind": self.sonos_kind, "transport": "PLAYING",
                "rel_s": float(start_s),
                "dur_s": ep.get("dur_s"),
                "ours": True, "reachable": True, "seq": -1,
                "stale_s": 0.0,
                "volume": (self.sonos_snap or {}).get("volume")}
            self.sonos_snap_at = time.monotonic()
            log(f"sonos: playing [{idx + 1}/{len(self.sonos_queue)}] "
                f"{ep.get('title') or ep['id']}")
            return {"source": "sonos", "target": self.target, "index": idx}
        # ONE body, built once: the second call existed only to read back
        # `kind`, and it re-ran load_library() and the regex for nothing.
        # With storytel it would also mint a SECOND signed url — and
        # could fail after the speaker is already playing.
        body = self._sonos_body(ep, rd["uid"], start_s)
        if body.get("error"):        # could not resolve a playable uri
            log(f"sonos: {body['error']}")
            return {"error": body["error"]}
        code, resp = _renderer.post("/play", body)
        if code != 200:
            log(f"sonos: play refused ({code}: {resp.get('error')})")
            return {"error": resp.get("error") or f"http-{code}"}
        self.sonos_idx = idx
        self.sonos_kind = body["kind"]
        self.sonos_pending = (ep.get("id") or ep["url"], time.monotonic())
        self.sonos_opt_tr = ("PLAYING", time.monotonic())
        # seed the snapshot so the card extrapolates from the position we
        # TRANSFERRED at, instead of flashing old->0->sonos for up to 5s
        # (field 2026-08-09); the first real poll takes over
        self.sonos_snap = {
            "armed": True, "uid": rd["uid"], "kind": self.sonos_kind,
            "transport": "PLAYING", "rel_s": float(start_s),
            # seed the length when the entry carries one (storytel knows
            # it from the shelf): without it the screen has a position
            # but no duration, so it draws the times and NO progress bar
            # until the speaker reports one
            "dur_s": ep.get("dur_s"),
            "dur_from_shelf": bool(ep.get("dur_s")),
            # and the uri WITH it — the poller may only carry a duration
            # forward for the SAME track, and a seed with no uri could
            # never be matched (which would silently lose the length again)
            "uri": body.get("uri"),
            "ours": True, "reachable": True, "seq": -1,
            "stale_s": 0.0,
            "volume": (self.sonos_snap or {}).get("volume")}
        self.sonos_snap_at = time.monotonic()
        if start_s >= 5 and not resp.get("sought"):
            # The bookmark was NOT kept — this log said so for months
            # while nothing arranged it. The seek was refused, playback
            # runs from 0, and the poller then wrote that near-zero
            # position straight over the good one. Hold it until playback
            # passes where we meant to resume: 52 minutes into a Harry
            # Potter book became 39 seconds without this (field
            # 2026-08-15). Same guard the sharelink path has had.
            self.sonos_bm_hold = (ep.get("id"), start_s,
                                  time.monotonic())
            log(f"sonos: seek refused — playing from the top, bookmark "
                f"held at {int(start_s)}s")
        log(f"sonos: playing [{idx + 1}/{len(self.sonos_queue)}] "
            f"{ep.get('title') or ep['url']}")
        return {"source": "sonos", "target": self.target, "index": idx}

    def _sonos_start_spotify(self, target, rd, episode=None):
        """v2: vibb owns the LOGIC, the Sonos merely holds the queue.
        ONE ShareLink add of the whole context, then positional jumps.
        Queue/metadata come from go-librespot's cached context listing —
        the same source as the box's song picker, so the screen behaves
        identically to local playback (owner requirement 2026-08-09)."""
        uri = _spotify.to_uri(target)
        # classify BEFORE any transfer: Liked Songs has no share link.
        # shows/episodes ARE supported — the architect's regex guess said
        # otherwise, but the owner field-verified episode links through
        # SoCo ShareLink 2026-08-09 (and sonos-remotes played shows).
        if not uri or uri.split(":")[1] not in (
                "track", "album", "playlist", "artist", "show", "episode"):
            log(f"sonos: unsupported spotify kind for sharelink: {uri}")
            return {"error": "unsupported-on-sonos"}
        if uri.split(":")[1] in ("track", "episode"):
            # a single item has no listing to enumerate: one-row queue,
            # metadata filled by the speaker's DIDL fallback
            listing = {"tracks": [{"uri": uri, "track": {}}]}
        else:
            try:
                listing = _spotify.context_tracks(uri, settle_s=10) or {}
            except (OSError, ValueError):
                return {"error": "spotify-listing-unavailable"}
        rows = []
        for t in listing.get("tracks") or []:
            tr = t.get("track") or {}
            name = tr.get("name")
            arts = tr.get("artist_names") or []
            dur = tr.get("duration")  # ms; None until the metadata
            rows.append({                # sweep resolves the row
                "url": t.get("uri"), "id": t.get("uri"),
                "title": (f"{name} — {', '.join(arts)}" if name and arts
                          else name),
                "image": tr.get("album_cover_url"),
                "dur_s": (dur / 1000.0) if dur else None})
        if not rows:
            return {"error": "nothing-to-play"}
        try:
            go("/player/pause")  # quiesce the box's own session
        except OSError:
            pass
        idx, start_s = 0, 0.0
        bm = _spotify.read_bookmark(uri)
        want = episode or (bm or {}).get("uri")
        if want:
            hit = next((i for i, r in enumerate(rows)
                        if r["id"] == want), None)
            if hit is not None:
                idx = hit
                if not episode and bm:
                    start_s = float(bm.get("position") or 0) / 1000.0
                    # ms -> s happens HERE and nowhere else (unit seam)
        code, resp = _renderer.post("/play", {
            "uid": rd["uid"], "kind": "spotify_sharelink", "uri": target,
            "track_index": idx, "start_s": start_s})
        if code != 200:
            return {"error": resp.get("error") or f"http-{code}"}
        log(f"play [sonos] {target} (track {idx + 1}/{len(rows)})")
        with self.lock:
            self.target, self.source = target, "sonos"
            self.sonos_kind = "spotify_sharelink"
            self.sonos_queue, self.sonos_idx = rows, idx
            self.sonos_ctx = uri
            qlen = resp.get("queue_len")
            self.sonos_map_trusted = (qlen is None
                                      or qlen == len(rows))
            if not self.sonos_map_trusted:
                log(f"sonos: queue drift ({qlen} on speaker vs "
                    f"{len(rows)} listed) — positional jumps disabled")
            self.sonos_bm_hold = None
            self.sonos_pending = (rows[idx]["id"], time.monotonic())
            self.sonos_opt_tr = ("PLAYING", time.monotonic())
            self.sonos_snap = {
                "armed": True, "uid": rd["uid"],
                "kind": self.sonos_kind, "transport": "PLAYING",
                "rel_s": float(start_s),
                "dur_s": rows[idx].get("dur_s"),
                "ours": True, "reachable": True, "seq": -1,
                "stale_s": 0.0,
                "volume": (self.sonos_snap or {}).get("volume")}
            self.sonos_snap_at = time.monotonic()
            if start_s >= 5 and not resp.get("sought"):
                # seek refused: playback runs from 0 — the poller must
                # NOT overwrite the good bookmark until we pass it
                self.sonos_bm_hold = (rows[idx]["id"], start_s,
                                      time.monotonic())
                log("sonos: seek refused — bookmark held")
            self._persist()
        _sonos_wake.set()
        return {"source": "sonos", "target": target, "index": idx}

    def sonos_start_target(self, target, episode=None):
        """Resolve a target to the sonos session: expand (remote urls
        preferred), rotate to the bookmark, push the episode. Spotify
        targets go as one sharelink — SONOS OWNS THAT QUEUE."""
        rd = _renderer.read()
        if rd["renderer"] != "sonos":
            return {"error": "renderer is not sonos"}
        if is_spotify(target):
            return self._sonos_start_spotify(target, rd, episode)
        # We KNOW the renderer here — do not depend on a module global
        # that a DIFFERENT THREAD sets at startup. The poller flips
        # PREFER_REMOTE, so a play issued before that thread has run
        # expanded to the downloaded books only; a book being streamed
        # was then missing from the queue, the bookmark's episode id
        # matched nothing, idx fell back to 0 — and the series restarted
        # at book one from zero, bookmarking zero on the way (field
        # 2026-08-15: "restart vibbd under sonos, play again, bookmark
        # nulled").
        content.PREFER_REMOTE = True
        entries = content.expand_entries(target)
        # A storytel row's url is a LOCAL path (the bookmark key); its
        # playable uri is minted per-play in _sonos_body, so it gets the
        # same escape hatch the NRK series service already has. Without
        # this every book — downloaded or not — is filtered out here.
        playable = [e for e in entries
                    if str(e["url"]).startswith(("http://", "https://"))
                    or re.match(r"https?://radio\.nrk\.no/serie/", target)
                    or _storytel.is_storytel(target)]
        skipped = len(entries) - len(playable)
        if skipped:
            log(f"sonos: {skipped} local-only entr"
                f"{'y' if skipped == 1 else 'ies'} skipped (no stream url)")
        if not playable:
            return {"error": "nothing-remote-playable"}
        st = _bm_load(state_key(target)) or {}
        idx, pos = 0, 0.0
        if episode is not None:
            idx = next((i for i, e in enumerate(playable)
                        if e.get("id") == episode), 0)
            pos = _bm_episode_pos(st, episode, playable[idx]["url"])
        elif st:
            bid = st.get("id")
            hit = next((i for i, e in enumerate(playable)
                        if (bid and e.get("id") == bid)
                        or e["url"] == st.get("url")), None)
            if hit is not None:
                idx = hit
                pos = _bm_episode_pos(st, playable[idx].get("id"),
                                      playable[idx]["url"])
        log(f"play [sonos] {target} ({len(playable)} episodes)")
        with self.lock:
            self.target, self.source = target, "sonos"
            self.sonos_queue = playable
            self.sonos_kind = "url"
            self._persist()
        return self._sonos_play_entry(idx, start_s=pos)

    def sonos_step(self, delta):
        """next/prev for the url kind (our queue, with the wrap rules the
        box already teaches); sharelink delegates to the speaker's own
        queue. prev >5s in restarts the episode — same as mpv rule."""
        if self.sonos_idx is None or not self.sonos_queue:
            return {"error": "no queue"}
        if delta < 0 and self.sonos_pending is None \
                and (self._sonos_position() or 0) > 5:
            # >5s in -> restart the track. Skipped while a jump is still
            # settling: the snapshot then carries the PREVIOUS track's
            # position, and prev "restarted" instead of stepping back
            # (field 2026-08-09, mash session two)
            idx = self.sonos_idx
        else:
            n = len(self.sonos_queue)
            base = (self._sonos_step_want
                    if self._sonos_step_want is not None else self.sonos_idx)
            idx = (base + delta) % n
        # Coalesce: each SOAP jump costs ~1s, and a mash of presses
        # queued them serially — the UI timed out 15 times in a row
        # (field 2026-08-09). One worker, always jumping to the LATEST
        # wanted index; presses return instantly with the optimistic
        # index so the card flips at press speed.
        self.sonos_pending = (self.sonos_queue[idx]["id"],
                              time.monotonic())
        with self._sonos_step_lock:
            self._sonos_step_want = idx
            if not self._sonos_stepping:
                self._sonos_stepping = True
                threading.Thread(target=self._sonos_step_worker,
                                 daemon=True).start()
        self.sonos_idx = idx  # optimistic — worker/poller confirm
        return {"ok": True, "index": idx, "routed": "sonos"}

    def _sonos_step_worker(self):
        while True:
            with self._sonos_step_lock:
                want, self._sonos_step_want = self._sonos_step_want, None
                if want is None:
                    self._sonos_stepping = False
                    return
            # MUST NOT die: an uncaught raise here exits the thread with
            # _sonos_stepping still True, and every later next/prev sees
            # "a worker is running" and never spawns one — next/prev on
            # sonos is then DEAD until a daemon restart (field 2026-09-01:
            # a mash storm queued verbs past the sidecar timeout,
            # _sonos_play_entry raised SidecarDown, and the buttons
            # bricked mid-session). The seek twin below has carried this
            # exact guard all along; broad on purpose — a lost step is a
            # shrug, a dead coalescer is a brick.
            try:
                self._sonos_play_entry(want, 0.0)
            except Exception as e:
                log(f"sonos step: play_entry failed "
                    f"({e.__class__.__name__}) — step dropped, "
                    "coalescer lives on")

    def _renderer_to_sonos(self, uid, name):
        """Hand the session to a speaker. Order is load-bearing: capture
        the box-side position FIRST (stopping the child flushes the
        player's bookmark), THEN persist the renderer, THEN start remote.
        None of the local/bt machinery runs — no OUT_FILE write, no
        BT_QUIET, no _kick_bt_connect, no go-librespot retarget."""
        if not uid:
            return None  # handler answers 400
        prev = _renderer.read()
        if prev["renderer"] == "sonos" and prev.get("uid") \
                and prev["uid"] != uid:
            # room -> room: bookmark at the exact second, then silence
            # the OLD speaker — without this Bad 2 etg kept playing while
            # Peisestue started (field 2026-08-09)
            self._sonos_refresh_live()
            self._sonos_bookmark_now()
            try:
                _renderer.post("/stop", {"if_uid": prev["uid"]})
            except _renderer.SidecarDown:
                pass
        was_spotify = self.source == "spotify" and self.target \
            and is_spotify(self.target)
        with self.lock:
            target = self.target
            self._stop_child()  # flushes the mpv bookmark on its way out
        if was_spotify:
            _flush_spotify_bookmark()
        _renderer.write("sonos", uid=uid, name=name)
        self._sonos_vol_opt = None  # new speaker, new volume world
        content.PREFER_REMOTE = True
        _library._EXPAND_CACHE.clear()  # entries differ by renderer: sonos
        #   lists every book, the box only the downloaded ones. A stale
        #   300s entry otherwise shows the wrong list — and tapping an
        #   undownloaded book on the box plays a DIFFERENT one from 0.
        _sonos_wake.set()
        log(f"renderer -> sonos: {name or uid}")
        if target:
            r = self.sonos_start_target(target)
            if r.get("error"):
                return {"output": current_output()["output"],
                        "renderer": "sonos", "warning": r["error"]}
        return {"output": current_output()["output"], "renderer": "sonos",
                "name": name}

    def _renderer_to_box(self):
        """The way home: read the position BEFORE stopping the transport
        (after a Stop it reads 0:00), bookmark it, silence the speaker,
        then the ordinary local play resumes from that bookmark."""
        self._sonos_refresh_live()
        self._sonos_bookmark_now()
        resume_target = self.target if self.sonos_kind in (
            "url", "spotify_sharelink") else None
        try:
            rd = _renderer.read()
            _renderer.post("/stop", {"if_uid": rd.get("uid")})
        except _renderer.SidecarDown:
            log("sonos: sidecar down during switch-back — speaker may "
                "still be playing (stop it from the Sonos app)")
        _renderer.write("box")
        content.PREFER_REMOTE = False
        _library._EXPAND_CACHE.clear()  # see the note on the sonos side
        self._sonos_vol_opt = None
        with self.lock:
            self.sonos_snap, self.sonos_snap_at = {}, 0.0
            self.sonos_queue, self.sonos_idx = [], None
            kind, self.sonos_kind = self.sonos_kind, None
            if self.source == "sonos":
                self.source = "spotify" if kind == "spotify_sharelink" \
                    else "mpv"
        log("renderer -> box")
        if resume_target:
            # Fire-and-forget resume on the box, from the bookmark just
            # written. self.target is CLEARED first: play()'s
            # already-loaded shortcut requires target == self.target, and
            # after a sonos session go-librespot still holds whatever it
            # played BEFORE the transfer — "resume" then unpaused the
            # wrong context entirely (field 2026-08-09: came home to the
            # 80s playlist while the card said Coco). A forced respawn
            # reads the context bookmark instead: right track, right
            # second.
            with self.lock:
                self.target = None
            threading.Thread(target=self.play, args=(resume_target,),
                             daemon=True).start()

    def set_output(self, device, fallback=False, uid=None, name=None):
        if device == "sonos":
            return self._renderer_to_sonos(uid, name)
        if _renderer.is_sonos():
            if fallback:
                # btwatchd's A2DP announce (or its local fallback) must
                # never yank sound away from a playing sonos session —
                # the JBL wandering into range mid-episode was QA's
                # silent-pass #4. A normal reply keeps btwatchd's own
                # announced-state machine consistent.
                return {"skipped": "renderer is sonos",
                        "output": current_output()["output"]}
            self._renderer_to_box()
        pcm = OUTPUT_PCMS.get(device)
        if not pcm:
            return None  # handler answers 400
        if fallback and device == "bt" and _audio.stack() == "pipewire":
            # btwatchd's announce: the speaker's node may be new (first
            # connect after boot) or renamed (package upgrade) since
            # asound.conf was written, and this path is about to point
            # mpv/go-librespot at vibb_bt. Refresh the pin FIRST — file
            # only, the single reopen below stays the one reopen (AM-10).
            _audio.ensure_bt_route(_speaker_mac())
        if fallback and device == "local" and not _i2s_card_present():
            # btwatchd's speaker-away fallback: without a built-in/HAT
            # card there is nothing to fall back TO — keep bt configured
            # so the reconnect logic brings audio back by itself
            return {"skipped": "no built-in sound card", "output":
                    current_output()["output"]}
        if fallback and current_output()["output"] == device:
            # converge anyway: a deferred mpv switch (transport wasn't up
            # when the user flipped the output) applies on this announce
            if device == "bt" and _bt_output_ready():
                with self.lock:
                    if self._mpv_alive():
                        try:
                            mpv_ipc(["set_property", "audio-device",
                                     f"alsa/{pcm}"])
                            log("output bt: deferred mpv switch applied")
                        except OSError:
                            pass
                # v0.0.7: reopen the output live (session kept, no
                # restart, no radio burst). Falls back to the config
                # rewrite + restart on a pre-v0.0.7 binary. Runs OUTSIDE
                # the lock either way — a restart queues every /status
                # reader behind it (review 2026-07-18 R2).
                if reopen_go_output(pcm):
                    log("output bt: deferred go-librespot output "
                        "reopened live")
                elif _retarget_go_librespot(pcm):
                    _note_go_restart()
                    log("output bt: deferred go-librespot retarget "
                        "applied")
            return {"unchanged": True, "output": device}
        with self.lock:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(OUT_FILE + ".tmp", "w") as f:
                json.dump({"output": device, "pcm": pcm}, f)
            os.replace(OUT_FILE + ".tmp", OUT_FILE)
            # BT quiet marker: the USER explicitly choosing the built-in
            # speaker (not btwatchd's drop fallback) tells btwatchd to stop
            # blind reconnect pages; ANY transition to bt clears it so a later
            # drop still recovers. (A drop -> fallback=True local -> marker
            # untouched -> btwatchd keeps its full reconnect ladder.)
            try:
                if device == "bt":
                    os.remove(_bt.BT_QUIET_FILE)
                elif device == "local" and not fallback:
                    open(_bt.BT_QUIET_FILE, "a").close()
            except OSError:
                pass
            if not fallback:
                # The user asked for the speaker NOW (OUT_FILE already
                # says bt, so the helper checks the right output)
                _kick_bt_connect()
            mpv_switched = False
            if self._mpv_alive():
                if device == "bt" and not _bt_output_ready():
                    # NEVER point a live mpv at a bluealsa device with no
                    # A2DP transport: it errors the track and skips to the
                    # next, over and over (field: 'jumps between episodes
                    # like crazy'). Record the intent; btwatchd's announce
                    # applies the mpv switch once the transport exists.
                    log("output -> bt: no A2DP transport yet — mpv stays "
                        "on the current device until the speaker is ready")
                else:
                    try:  # mpv can retarget its audio device live
                        mpv_switched = mpv_ipc(
                            ["set_property", "audio-device", f"alsa/{pcm}"]
                        ).get("error") == "success"
                        # The live retarget is the loudest path in the
                        # box: it moves audio onto the HAT amplifier
                        # while mpv keeps the softvol a parent set for
                        # HEADPHONES. Re-apply the cap for the device we
                        # just landed on — at use only, so volume.json
                        # still holds what the user actually chose.
                        if mpv_switched:
                            v = _local_volume(
                                self._volume_setting(), pcm)
                            mpv_ipc(["set_property", "volume", v])
                    except OSError:
                        pass
            # A resume IN FLIGHT loads its track PAUSED (play_spotify
            # loads, seeks, then unpauses) — so 'was playing' misses
            # it, the restart killed the loading session, and nobody
            # picked the baton back up: the player child waited 20s on
            # a dead session, resumed into an EMPTY new one (silent
            # no-op) and exited (field 2026-07-18 18:01:36 — box came
            # up mute). A live spotify player child IS playback intent.
            # Snapshot it (and the replay coordinates) under the lock;
            # the slow go-librespot surgery below runs WITHOUT it.
            spot_resuming = (self.child is not None
                             and self.child.poll() is None
                             and self.source == "spotify")
            resume_target, resume_flag = self.target, self.resume
        # From here on: status probe + systemctl restart, seconds of I/O
        # that used to hold ORCH.lock and froze every /status reader —
        # the screen's 1/s poll — for the whole switch (review R2).
        restarted = False
        go_action = "unchanged"
        if device == "bt" and not _bt_output_ready():
            # same rule as mpv above: don't bounce go-librespot into a
            # device with no transport — the restart's wifi burst lands
            # exactly during AVDTP setup on the SHARED radio (the
            # coexistence load that crashes the Zero's BT firmware).
            # (A live reopen onto bluealsa can block on a mid-reconnect
            # speaker too, so it waits for the transport just the same.)
            pass
        elif reopen_go_output(pcm):
            # v0.0.7 live reopen: the audio output moves to the new
            # device WITHOUT tearing down the session — track, position,
            # volume and paused-state all survive, so there is nothing to
            # resume and no restart to dedup, and the shared radio stays
            # quiet. This is the path on a current binary.
            go_action = "reopened live"
            _go_volume_cap(pcm)  # the surviving volume is the headphone one
        else:
            # pre-v0.0.7 fallback: audio_device is startup config there,
            # so the switch is a config rewrite + restart that kills the
            # session mid-song — we bring the music back from the
            # bookmark below.
            try:
                st = go_status(timeout=2)
            except OSError:
                st = {}  # api busy/flapping — the checks below cope
            # box-initiated playback only: a phone streaming its own
            # music through the box must not get hijacked into the
            # box's old target after the restart
            spot_was_playing = (spotify_playing(st)
                                and st.get("play_origin")
                                in ("go-librespot", "", None))
            restarted = _retarget_go_librespot(pcm)
            if restarted:
                _note_go_restart()
                go_action = "restarted"
            if restarted and (spot_was_playing or spot_resuming) \
                    and resume_target and is_spotify(resume_target):
                # unlike mpv (live IPC retarget), the restart killed
                # the session mid-song — bring the music back where
                # it was (player.py waits for the session, then
                # resumes from the bookmark). --exact: this is an
                # interruption, not a re-tap — even 0:08 into a song
                # must come back at 0:08, or it reads as a restart.
                # Stop a still-waiting old player first: left alive it
                # would fire ITS resume into the fresh session later.
                with self.lock:
                    if self.target == resume_target:
                        # (unless a fresh tap changed the target while
                        # the lock was down — that play owns the child)
                        self._stop_child()
                        self._spawn(resume_target, resume=resume_flag,
                                    exact=True)
                        log("output switch: resuming spotify from the "
                            "bookmark")
        log(f"output -> {device} (pcm {pcm}, "
            f"mpv {'switched' if mpv_switched else 'n/a'}, "
            f"go-librespot {go_action})")
        out = {"output": device, "pcm": pcm,
               "mpv_switched": mpv_switched,
               "spotify_restarted": restarted}
        if device == "local" and not _i2s_card_present():
            out["warning"] = ("no I2S sound card found — is the HAT "
                              "mounted and hat-audio-on + reboot done? "
                              "Playback will be silent until then.")
        return out

    def shuffle(self, enabled):
        """mpv: reshuffle/restore the playlist order (current track keeps
        playing). Spotify: shuffle_context — enabling BEFORE /play makes
        playback start on a random track, so the PWA can pre-arm it."""
        if _renderer.is_sonos():
            # Without this the fall-through below sends shuffle to
            # go-librespot while the audio is coming out of a Sonos
            # speaker — invisible, and wrong in a second room. The
            # sidecar sets play_mode NORMAL deliberately (sonosd.py:
            # "kills family shuffle/repeat leftovers"), and every
            # positional queue jump depends on that order, so shuffling
            # there is a real feature, not a one-liner.
            log("shuffle: not supported on a sonos renderer")
            return {"routed": None, "shuffle": None}
        with self.lock:
            if self._mpv_alive() and self.source == "mpv":
                cmd = ["playlist-shuffle"] if enabled else ["playlist-unshuffle"]
                try:
                    if mpv_ipc(cmd).get("error") == "success":
                        self.mpv_shuffle = enabled
                        log(f"shuffle {enabled} -> mpv")
                        return {"routed": "mpv", "shuffle": enabled}
                except OSError:
                    pass
            try:
                go("/player/shuffle_context", body={"shuffle_context": enabled})
                log(f"shuffle {enabled} -> spotify")
                return {"routed": "spotify", "shuffle": enabled}
            except OSError:
                return {"routed": None, "shuffle": None}

    def _sonos_known_duration(self, uri):
        """The length of the book in `uri`, from our own queue — or None.

        A Sonos handed a signed url reports TrackDuration 0:00:00 and
        never works the real length out, so the card had no progress bar
        whenever we had not seeded one: after a daemon restart, and
        whenever playback was started FROM THE SPEAKER rather than from
        vibb (field 2026-08-15, "right sometimes"). Carrying the last
        known value forward only helps when there IS one, which is why
        it was intermittent. This looks the answer up instead, keyed on
        the consumableId the signed url carries — exact, so it can never
        put one book's length on another."""
        if not _storytel.is_storytel(self.target or "") or not uri:
            return None
        want = (urllib.parse.parse_qs(
            urllib.parse.urlsplit(str(uri)).query).get("consumableId")
            or [None])[0]
        if not want:
            return None
        for e in self.sonos_queue or []:
            if e.get("id") == want:
                return e.get("dur_s")
        return None

    SEEK_TAIL_S = 5.0   # never land ON the end of a track

    def seek(self, position=None, delta=None):
        """Jump to a point in what is playing: mpv, spotify or sonos.

        Every branch resolves to an ABSOLUTE target and returns it, and
        the screen only ever sends absolute. With an accelerating press
        the UI fires several jumps inside a second, and every source's
        reported position is cached or polled (go_status caches 1s, a
        sonos snapshot can be 15s old), so N RELATIVE jumps would all
        resolve against the same stale base and land as one. Absolute
        targets compound by construction. `delta` stays for the PWA and
        anything else with no position state of its own.

        Clamped short of the end on every branch. mpv's seek past EOF
        ends the file and steps the playlist, and the player's
        dead-output watchdog reads that as a skip — the same path that
        rolled a kid back to an earlier episode three times (field
        2026-08-12), which is also why the branch stamps
        touch_user_skip() before the command rather than after."""
        def target(base, dur):
            """Absolute seconds, or None when there is nothing to seek in."""
            if dur is None:
                return None      # live stream: no duration, no destination
            want = position if position is not None else (base or 0.0) + delta
            return max(0.0, min(float(want), float(dur) - self.SEEK_TAIL_S))

        if _renderer.is_sonos():
            # First, and never falling through: with a sonos renderer the
            # audio is in another room, and the fall-through below would
            # seek go-librespot instead — invisible and wrong, exactly
            # the hole shuffle() closes above.
            snap = self._sonos_fresh() or {}
            tgt = target(self._sonos_position(), snap.get("dur_s"))
            if tgt is None:
                return {"routed": None, "position": None, "reason": "live"}
            # Coalesce like sonos_step: one SOAP jump costs ~1s and a
            # mash queued them serially until the UI timed out 15 times
            # in a row (field 2026-08-09). One worker, latest target wins.
            with self._sonos_step_lock:
                self._sonos_seek_want = tgt
                if not self._sonos_seeking:
                    self._sonos_seeking = True
                    threading.Thread(target=self._sonos_seek_worker,
                                     daemon=True).start()
            # The optimistic position is a DISPLAY fact, and it is kept
            # out of sonos_snap on purpose: that snapshot is also the
            # bookmark's source of truth, so anything we write into it
            # for the screen becomes a candidate for the disk. A held
            # guess persisted as a real position is how a seek the
            # speaker silently refused could bookmark a place it never
            # went (QA 2026-08-15). _sonos_position() applies this; the
            # snapshot stays measured-only.
            self.sonos_opt_pos = (tgt, time.monotonic(),
                                  (self.sonos_snap or {}).get("uri"))
            self.sonos_bm_hold = None  # that hold protects a bookmark from
            # a REFUSED start-seek; a deliberate one outranks it
            self._seek_at = time.monotonic()
            log(f"seek -> sonos {tgt:.0f}s")
            return {"routed": "sonos", "position": tgt}

        with self.lock:
            if self._mpv_alive() and self.source == "mpv":
                tgt = target(mpv_get("playback-time"), mpv_get("duration"))
                if tgt is None:
                    return {"routed": None, "position": None,
                            "reason": "live"}
                _radio.touch_user_skip()
                try:
                    res = mpv_ipc(["seek", tgt, "absolute"])
                except OSError:
                    res = {}
                if res.get("error") != "success":
                    # A live mpv session OWNS the transport (see command()):
                    # a refusal must not fall through and seek a paused
                    # spotify session in the background instead.
                    return {"routed": None, "position": None,
                            "reason": res.get("error") or "mpv-refused"}
                self._seek_at = time.monotonic()
                _bm_wake.set()
                log(f"seek -> mpv {tgt:.0f}s")
                return {"routed": "mpv", "position": tgt}

        # Spotify, OFF the lock: go_status can take seconds and every
        # /status reader queues behind it (review 2026-07-18 R2).
        try:
            track = (go_status() or {}).get("track") or {}
        except OSError:
            track = {}
        dur_ms = track.get("duration")
        tgt = target((track.get("position") or 0) / 1000.0,
                     dur_ms / 1000.0 if dur_ms else None)
        if tgt is None:
            return {"routed": None, "position": None, "reason": "no-session"}
        try:
            go("/player/seek", body={"position": int(tgt * 1000)})
        except OSError:
            return {"routed": None, "position": None, "reason": "spotify-down"}
        self._seek_at = time.monotonic()
        _bm_wake.set()
        log(f"seek -> spotify {tgt:.0f}s")
        return {"routed": "spotify", "position": tgt}

    def _sonos_seek_worker(self):
        while True:
            with self._sonos_step_lock:
                want, self._sonos_seek_want = self._sonos_seek_want, None
                if want is None:
                    self._sonos_seeking = False
                    return
            try:
                _renderer.post("/seek", {"s": want,
                                         "if_uid": _renderer.read().get("uid")})
            except _renderer.SidecarDown:
                pass      # the poller re-reads the truth within a beat
            _sonos_wake.set()
            _bm_wake.set()
            # pace OUTSIDE the lock (it is the shared queue mutex for
            # steps too — sleeping under it would block next/prev and
            # every /seek handler); a trailing sleep before exit is
            # harmless, and latest-wins keeps the final target intact
            time.sleep(SONOS_SEEK_SPACING_S)

    def _sonos_command(self, action):
        """Transport controls for the remote renderer. Runs OFF the lock:
        a SOAP round to a sleeping speaker takes seconds, and every
        /status reader queues behind this lock (review 2026-07-18 R2)."""
        rd = _renderer.read()
        guard = {"if_uid": rd.get("uid")}
        snap = self._sonos_fresh() or {}
        try:
            if action in ("next", "prev"):
                return self.sonos_step(1 if action == "next" else -1)
            if action == "playpause":
                # THE ROOT CAUSE, measured 2026-08-15. /resume only means
                # anything from PAUSED_PLAYBACK. A STOPPED speaker has no
                # position to resume from, so a bare resume restarts the
                # track at zero — and _sonos_on_term STOPS the speaker on
                # every daemon restart, which is why "restart vibbd, press
                # play" replayed an episode from the beginning while going
                # out and back in (a real /play) resumed correctly. The
                # bookmark was never damaged; the resume simply never read
                # it. Anything that is not a live paused session of ours
                # goes through the start path, which does read it.
                if (not snap.get("ours")
                        or snap.get("transport") not in ("PLAYING",
                                                         "PAUSED_PLAYBACK")):
                    if not self.target:
                        return {"error": "nothing to play"}
                    log("sonos: not a live paused session — starting from "
                        "the bookmark instead of a blind resume")
                    return self.sonos_start_target(self.target)
                if snap.get("transport") == "PLAYING":
                    self._sonos_bookmark_now()
                    code, _r = _renderer.post("/pause", guard)
                    new_tr = "PAUSED_PLAYBACK"
                else:
                    code, _r = _renderer.post("/resume", guard)
                    new_tr = "PLAYING"
                if code == 200 and self.sonos_snap:
                    # optimistic flip, HELD until the sidecar confirms:
                    # the post-verb wake used to fetch the still-stale
                    # snapshot and flip the card back (QA §1A)
                    patch = {"transport": new_tr}
                    if new_tr == "PAUSED_PLAYBACK":
                        # FREEZE the extrapolated position, don't fall back
                        # to the raw measurement. While PLAYING the bar
                        # adds the snapshot's age (and the sidecar's own
                        # measurement lag) so it keeps up with the Sonos
                        # app; pausing stops that, and without this the bar
                        # visibly jumps BACKWARDS by exactly that much and
                        # forwards again on resume (field 2026-08-15).
                        frozen = self._sonos_position()
                        if frozen is not None:
                            patch["rel_s"] = frozen
                            patch["stale_s"] = 0
                    self.sonos_snap = dict(self.sonos_snap, **patch)
                    self.sonos_snap_at = time.monotonic()
                    self.sonos_opt_tr = (new_tr, time.monotonic())
                _sonos_wake.set()  # re-poll now: the card should flip fast
                return {"routed": "sonos", "ok": code == 200}
            return {"error": f"unsupported on sonos: {action}"}
        except _renderer.SidecarDown:
            return {"error": "sonos-sidecar-down"}

    def pause(self):
        """Pause (never toggle) whatever is audible. Used by the card-slot
        switch on card removal: player stays loaded, so re-inserting the
        same card unpauses instantly."""
        if self.source == "sonos":
            self._sonos_bookmark_now()
            # ours-gate (stage B1): while grouped-away/taken-over the
            # speaker carries SOMEONE ELSE'S stream — a card removal
            # must not pause the parent's whole group on a firmware
            # that forwards member verbs. Raw snap, not _sonos_fresh:
            # a stale ours-False must still suppress; a stale ours-True
            # degrades to today's refused post.
            if not (self.sonos_snap or {}).get("ours"):
                return {"paused": []}
            try:
                _renderer.post("/pause",
                               {"if_uid": _renderer.read().get("uid")})
                return {"paused": ["sonos"]}
            except _renderer.SidecarDown:
                return {"paused": [], "error": "sonos-sidecar-down"}
        with self.lock:
            acted = []
            if self._mpv_alive():
                try:
                    if mpv_ipc(["set_property", "pause", True]).get("error") \
                            == "success":
                        acted.append("mpv")
                except OSError:
                    pass
            if spotify_playing():
                try:
                    go("/player/pause")
                    acted.append("spotify")
                except OSError:
                    pass
            log(f"pause -> {', '.join(acted) if acted else 'nothing playing'}")
            return {"paused": acted}

    def unpause(self):
        """Resume (never toggle) whatever is loaded — the mirror of
        pause(). NOT named resume(): self.resume is already the library
        entry's resume-position flag, and a method by that name is
        silently shadowed by the instance attribute.

        AVRCP sends DISTINCT play and pause commands: a car head unit
        says "play" when it wants sound, not "flip whatever you are
        doing". Mapping its PLAY onto a toggle made the box PAUSE while
        the car thought it was starting playback, the car send PLAY
        again, and so on — audible stutter the whole trip (field
        2026-07-27, Skoda head unit). Idempotent by construction: if it
        is already playing, this is a no-op.
        """
        if self.source == "sonos":
            # ours-gate (stage B1), the mirror of pause(): a card
            # re-insert must not resume the PARENT's paused group. A
            # no-op is honest here — A on the tile is the reclaim.
            if not (self.sonos_snap or {}).get("ours"):
                return {"resumed": []}
            try:
                _renderer.post("/resume",
                               {"if_uid": _renderer.read().get("uid")})
                _sonos_wake.set()
                return {"resumed": ["sonos"]}
            except _renderer.SidecarDown:
                return {"resumed": [], "error": "sonos-sidecar-down"}
        with self.lock:
            acted = []
            if self._mpv_alive():
                try:
                    if mpv_ipc(["set_property", "pause", False]).get("error") \
                            == "success":
                        acted.append("mpv")
                except OSError:
                    pass
            elif self.source == "spotify":
                try:
                    st = go_status()
                    if (st.get("track") or {}) and not st.get("stopped"):
                        if st.get("paused"):
                            go("/player/resume")
                        acted.append("spotify")
                except OSError:
                    pass
            log(f"resume -> {', '.join(acted) if acted else 'nothing loaded'}")
            return {"resumed": acted}

    def stop(self, keep_bookmark=False):
        """Stop = done: also clear the resume bookmark, so the next play
        starts from the top. (Pause / power-off keep the position.)

        keep_bookmark=True is the extras-handoff variant (2026-07-29):
        the player must DIE — a merely paused mpv keeps the ALSA device
        open, which the extra needs — but the kid's position must
        survive the gaming session. Field: the wrapper's plain /stop
        logged 'bookmark cleared' and wiped the audiobook position on
        every RetroPie launch."""
        if self.source == "sonos":
            # stop = done, same rule remotely: silence the speaker; the
            # bookmark-clearing below still applies via the shared path
            try:
                _renderer.post("/stop",
                               {"if_uid": _renderer.read().get("uid")})
            except _renderer.SidecarDown:
                pass  # speaker may play on — the app is the backstop
        with self.lock:
            self._stop_child()
            try:
                go("/player/pause")
            except OSError:
                pass
            if keep_bookmark:
                log("stop (bookmark kept — handoff)")
                return {"stopped": True, "bookmark": "kept"}
            # Clear ONLY the current target's bookmark: stopping a podcast
            # must not wipe the Spotify playlist's position (or vice versa)
            if self.target and is_spotify(self.target):
                try:
                    _spotify.clear_bookmark(
                        _spotify.to_uri(self.target) or self.target)
                except OSError:
                    pass
            elif self.target:
                try:
                    os.remove(os.path.join(STATE_DIR,
                                           state_key(self.target) + ".json"))
                except OSError:
                    pass
            # nothing to flush at shutdown, and don't resurrect the just-
            # cleared bookmark if a reboot lands before the next tick
            _SPOT_PENDING_BM[0] = None
            _SPOT_LAST_PLAYING[0] = False
            log("stop (bookmark cleared)")
            return {"stopped": True}

    def _spot_control(self, action):
        """Run one spotify control; False = it timed out / failed. The
        timeout moment is remembered: for the next SPOT_TIMEOUT_HOLD_S a
        session that reads 'empty' is treated as still-loading, because
        the timed-out command is very likely still executing inside
        go-librespot (field 2026-07-18 16:14: the timed-out /next
        finished 14s later). Also turns what used to be a 500 on the
        HTTP handler into a clean busy-drop."""
        try:
            if action in ("next", "prev"):
                _radio.touch_busy()  # a skip = an imminent CDN track load
                _PS_KICK.set()       # wifi power save off before the fetch
            spotify_command(action)
            return True
        except OSError as e:
            self._spot_cmd_timeout_at = time.monotonic()
            # NOT 'press again': the timed-out command usually still
            # executes inside go-librespot (field 2026-07-18 20:26: the
            # 'dropped' /next landed 15s later) — a repeat press would
            # double-skip
            log(f"{action}: spotify control slow ({e.__class__.__name__})"
                " — it likely still lands; give it a moment")
            return False

    def command(self, action):
        # Renderer-first, ahead of the fast-skip AND the busy-gate: with
        # no mpv child, control flow would otherwise fall through to the
        # respawn rules and start a LOCAL player next to the remote one
        # (architect review 2026-08-08 — must be the first rule).
        if self.source == "sonos":
            return self._sonos_command(action)
        # v0.0.8 fast-path: spotify next/prev must reach go-librespot at
        # the REAL press cadence — its skip debounce coalesces a burst
        # into two track loads, but only if it SEES the burst. The busy-
        # drop below serialized presses ~1s apart (each a fresh leading
        # edge = a full load + key request), which both defeated the
        # debounce and re-created the 429 storm (field 2026-07-23 21:52:
        # 10 loads in 9s from a prev mash). The gate protected against
        # SLOW queued controls; deferred skips are millisecond calls now,
        # presses queued during the leading load dequeue with
        # time-since-skip ~ 0 and defer, and ordering is the fork's
        # pointer arithmetic. mpv and playpause keep the locked path —
        # this only fires for a live spotify session.
        if action in ("next", "prev") and self.source == "spotify" \
                and not self._mpv_alive():
            threading.Thread(target=self._spot_fast_skip, args=(action,),
                             daemon=True).start()
            return {"routed": "spotify", "fast": True}
        # Drop, don't queue, presses that arrive while a control is still
        # running. go-librespot's API can take seconds per next/prev while
        # it loads the new track; each queued press then held this lock
        # for ANOTHER slow HTTP round, the UI timed out, the kid mashed
        # harder, and stale prev/next commands fired half a minute late
        # (field 2026-07-18 15:43: a prev storm landing out of order). A
        # dropped press is honest: nothing happened, press again.
        if not self.lock.acquire(timeout=1.0):
            log(f"{action}: control busy — dropped (previous command "
                "still running)")
            return {"routed": None, "busy": True}
        try:
            return self._command_locked(action)
        finally:
            self.lock.release()
            # a control may have moved the position (prev rewinds to 0,
            # next/seek jump) — wake the bookmarker so the in-memory
            # bookmark is fresh within a beat, not up to a 5s tick later
            _bm_wake.set()

    def _spot_fast_skip(self, action):
        """Forward one next/prev to go-librespot immediately (no ORCH.lock
        held across the HTTP round, no busy-drop) so the fork's skip
        debounce sees the true press cadence. On failure — e.g. the
        session died and /player/next 500s — fall back to the full locked
        path, which owns the replay-last logic (next on a dead session
        must still bring the music back)."""
        _kick_bt_connect()  # any transport control = sound intent
        try:
            _radio.touch_busy()  # a settle load = an imminent CDN fetch
            _PS_KICK.set()
            # ONE raw call — command()'s prev dance (status + sleep +
            # second prev) serialized a mash into ~1s clumps and starved
            # the fork's debounce; its semantics belong to the fork now
            spotify_skip(action)
            _bm_wake.set()
            return
        except TimeoutError as e:
            # slow ≠ dead: the command usually still lands inside
            # go-librespot (it was busy settling a debounced burst / 429
            # backoff). Falling back here re-sent the skip AND let the
            # locked path read the mid-settle session as 'empty' and
            # replay the whole target (field 2026-07-23 22:16:46: a prev
            # TimeoutError -> fallback -> 'session is empty' -> replay
            # while the fork was alive and loading). Stamp the hold
            # window so emptiness is distrusted, and stop.
            self._spot_cmd_timeout_at = time.monotonic()
            log(f"{action}: fast-path slow ({e.__class__.__name__}) — it "
                "likely still lands; give it a moment")
            return
        except OSError as e:
            log(f"{action}: fast-path failed ({e.__class__.__name__}) — "
                "falling back to the locked path")
        if not self.lock.acquire(timeout=1.0):
            log(f"{action}: control busy — dropped (fallback)")
            return
        try:
            self._command_locked(action)
        finally:
            self.lock.release()
            _bm_wake.set()

    def _command_locked(self, action):
        _kick_bt_connect()  # any transport control = sound intent
        # 1) a running mpv session owns the controls
        if self._mpv_alive() and self.source == "mpv":
            try:
                if action == "prev":
                    # >5s into the episode: restart it (standard player
                    # semantics). A second prev (within 5s of the start)
                    # goes to the PREVIOUS episode.
                    #
                    # NOT on long-form audio. The restart writes position
                    # 0, the player's poll persists it within 33s, and
                    # nothing protects the old value — so on an audiobook
                    # a single prev costs hours, and BROWSING backwards
                    # costs one bookmark per book passed (two presses
                    # each, every odd one a restart). The rule exists
                    # because there was no seek; since 2026-08-14 there
                    # is one, and holding B on the seek card reaches the
                    # start of a short episode in about two seconds.
                    # Gated on DURATION, not on where the file came from:
                    # a three-hour podcast has exactly the same problem.
                    pos = mpv_get("playback-time")
                    dur = mpv_get("duration")
                    long_form = isinstance(dur, (int, float)) \
                        and dur > PREV_RESTART_MAX_S
                    if not long_form and isinstance(pos, (int, float)) \
                            and pos > 5:
                        cmd = ["seek", 0, "absolute"]
                    else:
                        # Resume ROTATES the queue so the bookmarked
                        # episode sits in slot 0 — the previous episode
                        # wraps to the END of the playlist. mpv's
                        # playlist-prev is a no-op at slot 0, which
                        # made the second prev fall through to
                        # 'nothing to control' (field 2026-07-18:
                        # 'prev just restarts the same track').
                        ppos = mpv_get("playlist-pos")
                        count = mpv_get("playlist-count")
                        if ppos == 0 and isinstance(count, int) \
                                and count > 1:
                            cmd = ["set_property", "playlist-pos",
                                   count - 1]
                        else:
                            cmd = ["playlist-prev"]
                elif action == "next":
                    # Symmetric with prev's wrap above: at the LAST slot
                    # playlist-next is a no-op, so 'next' got stuck and
                    # fell through to 'nothing to control' — with the
                    # queue rotated so slot 0 holds the resumed episode,
                    # the kid could never reach it by pressing next (only
                    # prev or a natural playout wrapped around). Field
                    # 2026-07-20, the 3-episode NRK series 'ninas-
                    # hemmelige-reise': next stuck on ep 2, only prev
                    # reached ep 3. Wrap to the first slot instead.
                    ppos = mpv_get("playlist-pos")
                    count = mpv_get("playlist-count")
                    if isinstance(ppos, int) and isinstance(count, int) \
                            and count > 1 and ppos >= count - 1:
                        cmd = ["set_property", "playlist-pos", 0]
                    else:
                        cmd = ["playlist-next"]
                else:
                    cmd = ["cycle", "pause"]  # playpause
                # A live mpv session OWNS the transport: a non-success
                # (end of queue, a transient refusal) must NOT fall
                # through to the spotify-replay path and log the
                # misleading 'nothing to control' (which also risked
                # respawning the wrong source).
                if action in ("next", "prev"):
                    # stamp BEFORE the command: the player's watchdog
                    # must never see the resulting track change without
                    # the human context (field 2026-08-12: four mashed
                    # nexts read as a dead output and rolled the queue
                    # back to the last audible episode, three times)
                    _radio.touch_user_skip()
                res = mpv_ipc(cmd)
                if res.get("error") == "success":
                    log(f"{action} -> mpv")
                else:
                    log(f"{action} -> mpv (no-op: {res.get('error')})")
                return {"routed": "mpv"}
            except OSError:
                pass  # child starting up; fall through but don't respawn
        # ONE short status probe feeds rules 2+3. The old shape called
        # spotify_playing() (a 5s-timeout status) and then go_status()
        # (another 5s) back to back while holding the control lock — a
        # busy go-librespot turned every press into ~10s of lock time.
        # And CRUCIALLY: an unreachable-because-BUSY API must never be
        # mistaken for a dead session (field 2026-07-18 15:44: a /next
        # during a slow track load fell through to rule 4 and RESTARTED
        # the whole album from 0:00).
        st = None
        try:
            st = go_status(timeout=2)
        except OSError:
            if self.source == "spotify" and _go_unit_active():
                log(f"{action}: go-librespot is busy (api not answering) "
                    "— dropped, press again")
                return {"routed": None, "busy": True}
        # 2) Spotify actively playing (covers phone-initiated sessions)
        if st and spotify_playing(st):
            if not self._spot_control(action):
                return {"routed": None, "busy": True}
            self.source = "spotify"
            self._persist()
            log(f"{action} -> spotify (active)")
            return {"routed": "spotify"}
        # 3) last thing used was Spotify -> resume/skip there — but only
        # when a track is actually loaded. After a reboot go-librespot
        # is logged in with an EMPTY session; a playpause into that void
        # "succeeds" silently and the button feels dead. Fall through to
        # rule 4 instead: replay the target, which resumes exactly.
        if self.source == "spotify" and st is not None:
            # v0.0.8: pending_track_uri = a debounced skip is mid-settle;
            # the session may read trackless for that beat but it is very
            # much alive — treat it as live, never as empty (field
            # 2026-07-23 22:16: a mid-burst 'empty' read replayed the
            # whole playlist)
            if ((st.get("track") or {}) or st.get("pending_track_uri")) \
                    and not st.get("stopped"):
                if not self._spot_control(action):
                    return {"routed": None, "busy": True}
                log(f"{action} -> spotify (last)")
                return {"routed": "spotify"}
            # The session READS empty — but a SLOW track load looks
            # exactly like this for a beat (field 2026-07-18 16:14: /next
            # timed out at :35, prev at :47 saw an 'empty' session, Del 4
            # finished loading at :49). Two guards before a skip may
            # treat emptiness as the album's end:
            if action != "playpause":
                if (time.monotonic() - self._spot_cmd_timeout_at
                        < SPOT_TIMEOUT_HOLD_S):
                    # a control timed out moments ago — it is very likely
                    # STILL EXECUTING; emptiness proves nothing
                    log(f"{action}: session reads empty right after a "
                        "slow control — dropped (likely still loading)")
                    return {"routed": None, "busy": True}
                # transient-empty guard: re-read after a beat; a mid-load
                # blip resolves, a finished album stays empty
                time.sleep(EMPTY_RECHECK_S)
                try:
                    st2 = go_status(timeout=2)
                except OSError:
                    log(f"{action}: session state unclear — dropped")
                    return {"routed": None, "busy": True}
                if ((st2.get("track") or {}) or st2.get(
                        "pending_track_uri")) and not st2.get("stopped"):
                    if not self._spot_control(action):
                        return {"routed": None, "busy": True}
                    log(f"{action} -> spotify (loaded during recheck)")
                    return {"routed": "spotify"}
            log("spotify session is empty — replaying last target")
        # 4) dead session + remembered target -> bring it back. Playpause
        # always may (unambiguous 'give me music'). next/prev may TOO —
        # but only when the emptiness is TRUSTWORTHY: the API answered
        # and said so twice (album ran off its end — next on the last
        # Coco track must wrap to the start, not go dead), or the source
        # isn't spotify at all (a finished podcast queue). What must
        # never replay on a skip is an UNREACHABLE spotify API (st is
        # None): busy-not-dead — replaying there restarted a playing
        # album from 0:00 (the 15:44 disaster).
        trusted = st is not None or self.source != "spotify"
        if (action == "playpause" or trusted) \
                and self.target and not self._mpv_alive():
            if is_spotify(self.target) \
                    and not self._ensure_spotify_backend():
                log(f"{action}: no internet — spotify can't start")
                return {"routed": None, "error": "no-internet"}
            self._spawn(self.target, reverse=self.reverse,
                        resume=self.resume or session_resume(),
                        exact=True, rewind=self._resume_overlap())
            log(f"{action} -> resuming last: {self.target}")
            return {"routed": "resume", "target": self.target}
        log(f"{action}: nothing to control")
        return {"routed": None}

    def _settle_position(self, live, now):
        """Hold the reported position steady at the resume bookmark while
        mpv is still seeking there. A freshly spawned mpv reports
        playback-time as it loads (0, 1, 2 ...) and only THEN seeks to
        the bookmark, so the raw value flaps 0:00 -> 0:53 on every start
        and every reconnect respawn. player.py publishes resume_pos;
        report it verbatim until the live position reaches it (the seek
        landed), then track live — bounded to the first
        POSITION_SETTLE_MAX_S after spawn so a target that can never be
        reached can't freeze the bar forever."""
        if self._seek_at > self.child_started:
            # The user moved the position DELIBERATELY since this child
            # started. Holding at the resume bookmark now would fight
            # them: seek back inside the first 20s of a resumed episode
            # and the bar would jump forward and lie until the settle
            # window expired, while the audio sat where they put it.
            return live
        try:
            rp = float(now.get("resume_pos")) if now else 0.0
        except (TypeError, ValueError, AttributeError):
            return live
        if rp <= RESUME_MIN_S:
            return live  # fresh start (ramps from 0 anyway) — nothing to hold
        if time.monotonic() - self.child_started > POSITION_SETTLE_MAX_S:
            return live
        if live is None or live < rp - POSITION_SETTLE_TOL_S:
            return rp  # the seek has not landed yet — hold at the bookmark
        return live  # within tolerance: seek landed, track live from here

    def status(self):
        # A control can hold the lock for ~20s against a wedged
        # go-librespot api (prev = status + command + re-status, all
        # slow) — the screen's 1/s poll must NEVER queue behind that
        # (field 2026-07-18 23:xx: whole UI frozen). 0.5s, then fall
        # back to racy-but-atomic attribute reads: a momentarily stale
        # source/target beats a dead screen.
        if self.lock.acquire(timeout=0.5):
            try:
                mpv_alive = self._mpv_alive()
                target, source = self.target, self.source
            finally:
                self.lock.release()
        else:
            mpv_alive = self._mpv_alive()
            target, source = self.target, self.source
        out = {"source": source, "target": target, "playing": False,
               "title": None, "position": None, "duration": None,
               "artwork": None, "episode_id": None, "shuffle": False,
               "spotify_offline": bool(_SPOT_OFFLINE[0]),
               "session": _SESSION["verdict"] or "pending",
               "output": current_output()["output"]}
        if source == "sonos":
            # Same keys, same units as the mpv card — that identity IS
            # the "only difference is where the sound comes out" promise.
            # Reads ONLY the poller's snapshot: never the network, never
            # the sidecar (the go-librespot lesson at the top of this
            # method, twice over via PS-throttled wifi). A stale snapshot
            # reads as NOT playing — a session that died hours ago must
            # not hold the box awake all night.
            rd = _renderer.read()
            out["renderer"] = "sonos"
            out["renderer_name"] = rd.get("name")
            snap = self._sonos_fresh()
            if snap:
                out["playing"] = (snap.get("transport") == "PLAYING"
                                  and bool(snap.get("reachable"))
                                  and bool(snap.get("ours")))
                # position only while the stream is OURS: a grouped-away
                # member reports the PARENT's RelTime (or x-rincon junk)
                # and painting it under the kid's book title was pure
                # nonsense — never persisted (the bookmark is ours-gated)
                # but wrong on screen every second (QA 2026-08-23)
                out["position"] = (self._sonos_position()
                                   if snap.get("ours") else None)
                out["duration"] = snap.get("dur_s")
                # grouped-away FIRST: a member's foreign_uri is always
                # the x-rincon string, so taken-over shadowed this state
                # completely — it was unreachable in exactly the scenario
                # it names (QA 2026-08-23). Order is the fix.
                if snap.get("grouped_away"):
                    out["renderer_state"] = "grouped-away"
                elif not snap.get("ours") and snap.get("foreign_uri"):
                    out["renderer_state"] = "taken-over"
                elif snap.get("lost_session"):
                    out["renderer_state"] = "lost-session"
            elif self.sonos_snap:
                out["renderer_state"] = "unreachable"
                out["position"] = self.sonos_snap.get("rel_s")
                out["duration"] = self.sonos_snap.get("dur_s")
            if (self.sonos_kind == "spotify_sharelink"
                    and (self.sonos_idx is None
                         or not self.sonos_map_trusted)):
                # untrusted map / unknown position: the speaker's DIDL is
                # the only truth available — demoted fallback, never the
                # primary (v2)
                out["title"] = (snap or {}).get("track_title")
                out["artwork"] = (snap or {}).get("track_art")
                if (snap or {}).get("track_artist"):
                    out["artists"] = [snap["track_artist"]]
            elif self.sonos_idx is not None and self.sonos_queue:
                ep = self.sonos_queue[self.sonos_idx]
                out["title"] = (ep.get("title")
                                or (snap or {}).get("track_title"))
                out["episode_id"] = ep.get("id")
                out["artwork"] = ep.get("image")  # same shape as mpv card
            return out
        if mpv_alive and source == "mpv":
            # gated on source too: a lingering/starting mpv child while
            # the box plays spotify leaked mpv's media-title (a raw URL)
            # over the spotify card (field 2026-07-18 23:xx)
            out["shuffle"] = self.mpv_shuffle
            pause = mpv_get("pause")
            if pause is None and (time.monotonic() - self.child_started
                                  < MPV_START_GRACE_S):
                # mpv is spawned but its IPC socket isn't up yet (the ~1-3s
                # window right after a tap): trust the intent player.py
                # published BEFORE launching mpv, so the screen shows
                # 'playing' at once instead of a dead card for a few seconds
                try:
                    with open(NOW_FILE) as f:
                        pause = bool(json.load(f).get("paused"))
                except (OSError, ValueError):
                    pause = False  # a fresh tap means to play
            out["playing"] = pause is False
            out["title"] = mpv_get("media-title")
            out["position"] = mpv_get("playback-time")
            out["duration"] = mpv_get("duration")  # None = live stream
            now = None
            try:  # which episode (player.py publishes it; match on path)
                with open(NOW_FILE) as f:
                    now = json.load(f)
                mpath = mpv_get("path")
                q = _queue_map()
                item = (q.get("items") or {}).get(mpath) \
                    if q and q.get("target") == target else None
                if now.get("url") == mpath:
                    out["episode_id"] = now.get("id")
                    out["title"] = now.get("title") or out["title"]
                    out["artwork"] = now.get("image")
                elif item:
                    # mpv advanced (or was skipped) and player.py's publish
                    # is a poll behind — the queue map resolves the LIVE
                    # path instantly, so the new name/art show the same
                    # second the audio changes
                    out["episode_id"] = item.get("id")
                    out["title"] = item.get("title") or out["title"]
                    out["artwork"] = item.get("image")
                elif now.get("target") == target:
                    # Transition: mpv is still loading (no path yet), or
                    # plays something outside the map. Serve the last
                    # published name and art rather than flashing a raw
                    # .mp3 filename and the show cover — media-title is
                    # only kept when it is a real title, not a basename.
                    if (mpath is None or not out["title"]
                            or out["title"] == os.path.basename(mpath)
                            or out["title"] == mpath):
                        out["title"] = now.get("title") or out["title"]
                    out["artwork"] = now.get("image")
            except (OSError, ValueError):
                pass
            out["position"] = self._settle_position(out["position"], now)
            # Re-resolve the episode art to the LOCAL cached file if present:
            # an episode that started while STREAMING baked the remote URL
            # into now-playing.json for the whole session, so a later wake
            # would re-fetch it over wifi (seconds). The sweep caches it under
            # the target's dir; prefer that. Read-time only — never persisted
            # (a dead local path can't fall back to a remote URL).
            if out.get("episode_id") and out.get("artwork"):
                _dir = content.cache_key_for(target)
                if _dir:
                    out["artwork"] = content._image_local_or_remote(
                        _dir, out["episode_id"], out["artwork"])
        # short timeout: /status is polled ~1/s by the single-threaded
        # screen, and go-librespot is briefly unresponsive while it
        # restarts (output switch / transport rebuild) — the default 5s
        # here froze the whole UI for ~5s on a BT drop (field 2026-07-17).
        # mpv card: the now-view is served 100% from local state (mpv IPC +
        # now-playing.json); go-librespot only fills out["spotify"] (PWA), so
        # use a SHORT probe there — a screen wake's first /status shouldn't
        # block ~1.5s on go-librespot. Fresh when go answers fast; the
        # hold-cache below serves the last state when it doesn't.
        gs_timeout = (GO_ST_MPV_TIMEOUT if (mpv_alive and source == "mpv"
                      and out.get("title")) else GO_STATUS_TIMEOUT)
        st = go_status(timeout=gs_timeout)
        if st.get("track"):
            self._go_st_cache = (time.monotonic(), st)
        else:
            # 5s covers a transient timeout — but a COLD track load
            # (first-listen playlist, api blocked 8-15s) outlived it and
            # the card fell to 'Nothing playing' mid-skip (field
            # 2026-07-19). A fresh BUSY marker, a recent timed-out
            # control, or a v0.0.8 pending skip proves a load is in
            # flight: keep the card for the full load-hold window. The
            # trackless answer need not be EMPTY: mid-settle during a
            # debounced burst the api answers with pending_track_uri and
            # no track, and that flashed 'Nothing playing' on the screen
            # (field 2026-07-23 22:16). A trackless answer WITHOUT any
            # in-flight signal keeps the old behavior: only a fully
            # empty response gets the short hold; a deliberate stop
            # shows immediately.
            in_flight = bool(st.get("pending_track_uri")) or _radio.busy() \
                or (time.monotonic() - self._spot_cmd_timeout_at
                    < SPOT_TIMEOUT_HOLD_S)
            if not st or in_flight:
                at, cached = getattr(self, "_go_st_cache", (0.0, None))
                hold = SPOT_TIMEOUT_HOLD_S if in_flight else GO_ST_HOLD_S
                if cached and time.monotonic() - at < hold:
                    fresh = st
                    st = dict(cached)  # a load is in flight — hold the card
                    # v0.1.0: the fresh answer's pending/next metadata is
                    # newer than the cached card — carry it over
                    for k in ("pending_track_uri", "pending_track",
                              "next_track"):
                        if fresh.get(k):
                            st[k] = fresh[k]
        track = st.get("track") or {}
        sp_playing = spotify_playing(st)
        # v0.1.0: the fork's metadata cache (whole-playlist sweep at context
        # load) describes the debounced skip target and the upcoming track
        # before any stream is loaded
        pend_t = st.get("pending_track") or {}
        next_t = st.get("next_track") or {}
        out["spotify"] = {"playing": sp_playing,
                          "track": track.get("name") or None,
                          # uri of the loaded track — the song picker
                          # marks the playing row with it (v0.1.1)
                          "track_uri": track.get("uri") or None,
                          "artists": track.get("artist_names") or [],
                          "album": track.get("album_name") or None,
                          "artwork": track.get("album_cover_url") or None,
                          # v0.0.8: the not-yet-settled skip target while a
                          # debounced burst is in flight (None otherwise)
                          "pending_track_uri":
                              st.get("pending_track_uri") or None,
                          # v0.1.0: upcoming track's cover — the UI prewarms
                          # its art cache so the NEXT skip shows art instantly
                          "next_artwork":
                              next_t.get("album_cover_url") or None}
        # A paused Spotify track is still "what's on" — keep showing it
        # (title/artwork/position) with playing=False, like the mpv side does.
        # Gate on "mpv supplied nothing" rather than "child dead": while a
        # spawn is starting up the socket answers nothing, and blanking the
        # card to 'Nothing playing' for those seconds looks broken.
        # Only when Spotify is actually in charge though (current source, or
        # audibly playing right now): a track parked paused in go-librespot
        # from an EARLIER session must not hijack the card — the play button
        # routes to the current source, and card and button must agree.
        if (out["title"] is None and track and not st.get("stopped")
                and (sp_playing or source == "spotify")):
            out["playing"] = sp_playing
            out["shuffle"] = bool(st.get("shuffle_context"))
            out["source"] = "spotify"
            out["title"] = track.get("name")
            out["duration"] = (track.get("duration") or 0) / 1000 or None
            # position lives on the track object (ms, live-extrapolated)
            out["position"] = (track.get("position") or 0) / 1000
            out["artwork"] = out["spotify"]["artwork"]
        # v0.1.0: a debounced skip is in flight and the fork's metadata
        # cache KNOWS the target — show where the kid is GOING (name +
        # cover), not the track being left behind. The screen then reads
        # correctly during the whole burst instead of trailing one track
        # behind ("tekst men ikke art", field 2026-07-23 mash test), and
        # the cover fetch overlaps the settle load instead of starting
        # after it.
        if pend_t and source == "spotify" and out.get("source") != "mpv":
            out["source"] = "spotify"
            out["playing"] = True
            out["title"] = pend_t.get("name") or out["title"]
            out["duration"] = (pend_t.get("duration") or 0) / 1000 or None
            out["position"] = 0
            art = pend_t.get("album_cover_url")
            if art:
                out["artwork"] = art
            out["spotify"]["track"] = pend_t.get("name") or None
            out["spotify"]["artists"] = pend_t.get("artist_names") or []
            out["spotify"]["album"] = pend_t.get("album_name") or None
            if art:
                out["spotify"]["artwork"] = art
        # A freshly tapped spotify target is still loading: go-librespot's
        # /status keeps describing the PREVIOUS context for a few seconds,
        # which put another playlist's cover and title on the card (kids:
        # "wrong picture!"). Until the loaded track actually changes (or
        # 20s passes), present the tapped entry's own identity instead:
        # its bookmark's track + position and its pre-cached mosaic.
        p = self.spot_pending
        if p and source == "spotify" and target and is_spotify(target):
            if ((track.get("uri") and track.get("uri") != p.get("pre_uri"))
                    or time.monotonic() - p["at"] > 20):
                self.spot_pending = None  # the new context took over
            else:
                try:
                    uri = _spotify.to_uri(target)
                    bm = _spotify.read_bookmark(uri) if uri else None
                except OSError:
                    bm = None
                name = (bm or {}).get("name")
                if not name:
                    e = next((e for s in load_library().get("sections", [])
                              for e in s.get("entries", [])
                              if e.get("target") == target), None)
                    name = (e or {}).get("name") or "Spotify"
                out["source"], out["playing"] = "spotify", True
                out["title"] = name
                out["position"] = (bm.get("position") or 0) / 1000 \
                    if bm else None
                out["duration"] = ((bm.get("duration") or 0) / 1000 or None) \
                    if bm else None
                try:  # the entry's own mosaic is pre-cached on disk
                    out["artwork"] = content.collection_image(target) \
                        or (bm or {}).get("artwork")
                except Exception:
                    out["artwork"] = (bm or {}).get("artwork")
                # The card's SUBTITLE reads out['spotify']['artists'],
                # which this guard used to leave describing the OUTGOING
                # track — so the new album's name sat above the previous
                # album's artist for the second or two before the load
                # landed (field 2026-08-02, carousel album -> album).
                # Present this context's own bookmark instead, and show
                # NOTHING rather than something wrong when there is none
                # (a never-played album). track_uri goes too: the song
                # picker marks the playing row with it.
                out["spotify"] = dict(out["spotify"],
                                      track=(bm or {}).get("name"),
                                      track_uri=(bm or {}).get("uri"),
                                      artists=(bm or {}).get("artists") or [],
                                      album=None,
                                      artwork=out["artwork"])
        # Ghost sessions: nothing is live, but a bookmarked target is
        # remembered -> present it as paused-at-position instead of
        # "nothing playing". Pressing play resumes exactly there.
        if out["title"] is None and target and is_spotify(target):
            try:
                bm = _spotify.read_bookmark(
                    _spotify.to_uri(target) or target)
            except OSError:
                bm = None
            if bm and bm.get("uri"):
                # gate on IDENTITY, not position: a bookmark parked at
                # 0:xx (an episode boundary) is still "this track next" —
                # the >20s gate hid the track card after reboot and the
                # screen fell back to the tile (field 2026-08-10)
                out["playing"] = mpv_alive  # a spawn in flight IS starting
                out["source"] = "spotify"
                out["title"] = bm.get("name")
                out["artwork"] = bm.get("artwork")
                out["position"] = (bm.get("position") or 0) / 1000
                out["duration"] = (bm.get("duration") or 0) / 1000 or None
        if out["title"] is None and target and not is_spotify(target) \
                and (self.resume or session_resume()):
            try:
                with open(os.path.join(STATE_DIR,
                                       state_key(target) + ".json")) as f:
                    bk = json.load(f)
            except (OSError, ValueError):
                bk = None
            if bk and (bk.get("id") or bk.get("url") or bk.get("pos")):
                # identity-gated, like the spotify arm above: pos 0 at an
                # episode boundary must still show WHICH episode is next
                out["playing"] = mpv_alive  # a spawn in flight IS starting
                out["source"] = "mpv"
                out["position"] = bk.get("pos") or 0
                out["episode_id"] = bk.get("id")
                try:
                    with open(NOW_FILE) as f:
                        now = json.load(f)
                except (OSError, ValueError):
                    now = {}
                if now.get("target") == target:
                    out["title"] = now.get("title")
                    out["artwork"] = now.get("image")
                    out["episode_id"] = now.get("id") or out["episode_id"]
                    out["duration"] = now.get("duration")
                if not out["title"]:
                    # the persisted queue map knows every episode's
                    # title/art — resolve the BOOKMARKED one instead of
                    # falling back to a raw basename
                    try:
                        with open(os.path.join(
                                STATE_DIR, "now-queue.json")) as f:
                            q = json.load(f)
                        if q.get("target") == target:
                            for u, it in (q.get("items") or {}).items():
                                if (it.get("id") == bk.get("id")
                                        or u == bk.get("url")):
                                    out["title"] = it.get("title")
                                    out["artwork"] = it.get("image")
                                    break
                    except (OSError, ValueError):
                        pass
                if not out["title"]:
                    out["title"] = os.path.basename(target.rstrip("/"))
        # Stopped-but-remembered: no bookmark (stop cleared it), yet play
        # WILL start this target from the top — say so ("ready at 0:00")
        # instead of pretending nothing exists. Card and button must agree.
        if out["title"] is None and target:
            name = None
            for sec in load_library().get("sections", []):
                for e in sec.get("entries", []):
                    if e.get("target") == target:
                        name = e.get("name")
                        break
                if name:
                    break
            try:
                with open(NOW_FILE) as f:
                    now = json.load(f)
            except (OSError, ValueError):
                now = {}
            if now.get("target") == target:
                name = name or now.get("title")
                out["artwork"] = now.get("image")
            if name:
                out["source"] = "spotify" if is_spotify(target) else "mpv"
                out["title"] = name
                out["position"] = 0
                out["playing"] = mpv_alive  # a spawn in flight IS starting
        # Offline-proof cover for the screen: the live artwork above is a
        # remote URL (gfx.nrk.no episode art, or a Spotify track's
        # i.scdn.co album cover) that can't load with no net — after a
        # reboot the box resumes before wifi is up, and the card stayed
        # blank. The cached collection cover (synced shows' cover.jpg,
        # a playlist's pre-built mosaic) is on disk for both kinds; serve
        # it so the screen always shows SOMETHING and upgrades to the
        # live cover once the network fetch lands.
        if target:
            try:
                out["artwork_local"] = content.collection_image(target)
            except Exception:
                out["artwork_local"] = None
        if source == "sonos":
            # a happily-playing sonos session must not put the "speaker
            # not connected" popup on the screen: _bt_wait_state answers
            # for the LOCAL bt output, which is not in the audio path now
            # (architect risk #2, 2026-08-08)
            out["bt_waiting"] = out["bt_ready"] = out["bt_lost"] = False
        else:
            out["bt_waiting"], out["bt_ready"], out["bt_lost"] = \
                _bt_wait_state(out["playing"])
        # Steady 'is the configured speaker connected' for the screen's
        # status icon. /status polls at 1-2s, so the icon tracks a
        # connect/drop as fast as the popup does — the /system field
        # (same value) refreshes only every 30s and lagged visibly
        # (field 2026-07-20). Present only when a speaker is configured,
        # so a built-in-only box shows no BT icon.
        try:
            with open(_bt.MAC_FILE) as f:
                _spk = f.read().strip()
        except OSError:
            _spk = ""
        if _spk and out["output"] == "bt":
            # Only probe the BT transport (a bluealsa-aplay fork / dbus
            # enumerate, ~1/s) when the speaker is the ACTIVE output. On the
            # built-in speaker it is pointless AND it hammers a wedged
            # controller's bluealsa when the speaker is off/crashed. Omitting
            # the field (not sending False) lets the BT icon keep its last
            # value via /system's 30s poll — the UI fold is key-guarded on
            # the field's presence, so this is a clean no-op there. The
            # bt_lost/bt_waiting resume path below is untouched (QA).
            out["bt_connected"] = _bt_transport_ready()
        if _audio.stack() == "pipewire":
            # the policy self-test's verdict (ok | fail-safety | fail-rf |
            # down | pending) — key-guarded like bt_connected, so bluealsa
            # boxes emit nothing and the UI fold is a no-op there
            out["audio_policy"] = _audio.selftest_state().get("verdict") or "pending"
        if out["bt_lost"] or out["bt_waiting"]:
            # both speaker popups offer the same escape — A plays on the
            # built-in speaker instead — but only where one exists
            # (BT-only boxes get X to connect and nothing else)
            out["bt_local_ok"] = _i2s_card_present()
        return out


ORCH = Orchestrator()


def _bt_playback_active():
    """Is there an mpv session on the bluetooth output right now?
    netmgmt's wifi probe holds while this is true — an NM scan on the
    shared 2.4GHz radio mid-A2DP stutters the audio and is the documented
    firmware crasher (bt.py recover()). Paused counts too: a kid
    mid-listen resumes any second, and resuming into a live ~30s probe
    window is the same collision — the hold only ends when the session
    is gone (stop, end of queue, idle teardown). Spotify is deliberately
    not checked: the probe only runs with wifi down, where it can't
    stream."""
    try:
        if current_output()["output"] != "bt":
            return False
        with ORCH.lock:
            return ORCH._mpv_alive()
    except Exception:
        return False


_netmgmt.probe_hold[0] = _bt_playback_active


def _net_changed():
    """A wifi SWITCH while online strands go-librespot's long-lived TCP
    connections (AP/dealer/spclient) — they die silently and it spends
    minutes in 30-60s timeout storms that wedge its local API, which
    /status and /playpause block on: the field-reported frozen UI
    (2026-07-17). Restarting is ~5s and deterministic. try-restart:
    a parked unit stays parked — the supervisor owns starting it.
    One debounce gate for BOTH triggers (the /wifi/connect hook and the
    IP watchdog): skip when go-librespot was already restarted moments
    ago (retarget, unpark, the other trigger racing) — its sockets are
    already bound to the new address."""
    with _GO_REBUILD_LOCK:
        fresh = time.monotonic() - _GO_REBUILD["at"] < NET_HEAL_COOLDOWN_S
    if fresh:
        log("network changed — go-librespot restarted recently, skipping")
        return
    log("network changed — restarting go-librespot (stale connections)")
    try:
        subprocess.run(["systemctl", "try-restart", "go-librespot"],
                       timeout=30)
        _note_go_restart()
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"go-librespot restart after net change failed: {e!r}")


_netmgmt.net_changed[0] = _net_changed
NET_HEAL_COOLDOWN_S = float(os.environ.get("VIBB_NET_HEAL_COOLDOWN", "60"))
NET_IP_POLL_S = float(os.environ.get("VIBB_NET_IP_POLL", "15"))


def _wlan_ip():
    """The current IPv4 source address for internet traffic — a pure
    kernel route lookup (UDP connect sends NO packet), so polling this
    is radio-free. None = no default route (offline)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: never routable
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


def _ip_watchdog():
    """Catch the network changes our /wifi/connect hook can't see:
    NM-initiated failover, a DHCP lease on a new net, iface bounce.
    Field 2026-07-18 23:21: the iPhone hotspot died, NM auto-fell back
    to the home AP, and go-librespot kept zombie TCPs bound to the OLD
    address for minutes ('did not receive last pong', put-state
    timeouts) — every API call wedged, the whole box degraded. Rules:
    heal only on a REAL address change (A->B, or A->gone->B); A->gone->A
    is a blip (same lease came back, sockets still valid); offline is
    the supervisor's business, not ours."""
    last = _wlan_ip()  # seed: boot is not a change
    while True:
        _tick(NET_IP_POLL_S)
        try:
            cur = _wlan_ip()
            if cur is None:
                continue  # offline — keep the baseline (blip tolerance)
            if last is None:
                last = cur  # booted offline: first address = baseline
                continue
            if cur != last:
                _net_changed()
                last = cur
        except Exception as e:
            log(f"ip watchdog error: {e!r}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the journal clean
        pass

    def _send(self, code, obj):
        """Client may hang up while waiting on a long operation (bt pair
        can take a minute) — a dead socket is not an error worth a
        journal traceback."""
        try:
            self._send_unsafe(code, obj)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_unsafe(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, cache=False):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self._send(404, {"error": "not found"})
            return
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control",
                         "max-age=3600" if cache else "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, name):
        """Serve a file from the PWA web dir; True when handled."""
        path = os.path.realpath(os.path.join(WEB_DIR, name))
        if not path.startswith(os.path.realpath(WEB_DIR) + os.sep):
            return False
        if not os.path.isfile(path):
            return False
        self._send_file(path)
        return True

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/status":
            self._send(200, ORCH.status())
        elif url.path == "/volume":
            self._send(200, ORCH.get_volume())
        elif url.path == "/library":
            self._send(200, library_with_covers())
        elif url.path == "/output":
            out = current_output()
            out.update(_renderer.read())  # renderer/uid/name ride along
            self._send(200, out)
        elif url.path == "/sonos":
            # speaker list via the sidecar (uid+name only — GET is token-
            # free by the SAFE rule, and speaker IPs are LAN topology).
            # ?rescan=1 runs SSDP (3s+); ?fresh=1 is ONE topology call
            # against a cached ip (~200ms) that also carries the group
            # map; plain GET serves the cache instantly.
            q = urllib.parse.parse_qs(url.query)
            try:
                rescan = (q.get("rescan") or ["0"])[0] == "1"
                fresh = (q.get("fresh") or ["0"])[0] == "1"
                path = "/players" + ("?rescan=1" if rescan
                                     else "?fresh=1" if fresh else "")
                self._send(200, _renderer.get(
                    path, timeout=20 if rescan else 6 if fresh else 3))
            except _renderer.SidecarDown:
                self._send(503, {"error": "sonos-sidecar-down",
                                 "players": []})
        elif url.path == "/settings":
            self._send(200, load_settings())
        elif url.path == "/system":
            st = system_status()
            try:
                # short timeout: while go-librespot flaps at boot (no DNS
                # yet) a 5s wait here starves /system — the screen sits on
                # its splash even though playback is already running
                st["spotify_user"] = go_status(timeout=1).get("username")
            except OSError:
                st["spotify_user"] = None
            if st.get("spotify_user") is None:  # /status is None while it
                st["spotify_user"] = _spotify.logged_in_user()  # reconnects
            st["spotify_open"] = _spotify.zeroconf_open()
            st["spotify_api"] = _spotify_web.configured()
            # bt_ready feeds the screen's connection icon — present ONLY
            # when a speaker is configured (no key -> no icon), True when
            # its A2DP transport is live. The 40s post-boot confusion
            # (log 2026-07-20: wifi up, speaker off, nothing playing)
            # reads at a glance instead of only via the popup.
            try:
                with open(_bt.MAC_FILE) as f:
                    _mac = f.read().strip()
            except OSError:
                _mac = ""
            if _mac:
                st["bt_ready"] = _bt_transport_ready()
            self._send(200, st)
        elif url.path == "/bt":
            self._send(200, bt_status())
        elif url.path == "/spotify/profile":
            # Live preview of a profile's public playlists — the PWA calls
            # this to validate a username before saving a follow-section.
            q = urllib.parse.parse_qs(url.query)
            user = _spotify_web.parse_user((q.get("user") or [None])[0])
            if not user:
                self._send(400, {"error": "user required"})
            elif not _spotify_web.configured():
                self._send(503, {"error": (
                    "Spotify API credentials are not set up on this box — "
                    "run install.sh and answer the client id/secret prompt "
                    "(free app at developer.spotify.com/dashboard)")})
            else:
                try:
                    self._send(200, {"user": user, "playlists":
                                     _spotify_web.user_playlists(user)})
                except urllib.error.HTTPError as e:
                    msg = ("no Spotify profile named "
                           f"{user!r}" if e.code == 404
                           else f"Spotify API error {e.code}")
                    self._send(502, {"error": msg})
                except Exception as e:
                    log(f"profile preview failed for {user}: {e!r}")
                    self._send(502, {"error": str(e)})
        elif url.path == "/storytel/status":
            # SAFE["GET"] is True, so the whole LAN reads this: booleans
            # and counts ONLY, never the email, the jwt or the password.
            self._send(200, {
                "configured": _storytel.configured(),
                "queued": _storytel.outbox_pending(),
                "sync": bool(load_settings().get("storytel_sync", 1)),
            })
        elif url.path in ("/backup/status", "/backup/snapshots"):
            # SAFE["GET"] is True, so these must gate themselves. Unlike
            # /storytel/status these are NOT booleans-only: the repo string
            # names the owner's bucket/host, and the snapshot list is a
            # record of the box's history. Privileged, both of them.
            if not self._require_token():
                return
            if url.path == "/backup/status":
                self._send(200, _backup.status())
                return
            try:
                self._send(200, {"snapshots": _backup.snapshots()})
            except RuntimeError as e:
                self._send(503, {"error": str(e)})
        elif url.path == "/expand":
            q = urllib.parse.parse_qs(url.query)
            entry_id = (q.get("id") or [None])[0]
            target = (q.get("target") or [None])[0]
            # A RAW target is privileged, exactly as it is on POST /play.
            # ?id= names something the parent already curated; ?target=
            # is arbitrary, and unauthenticated it lists the audio files
            # in any directory this root daemon can read AND makes the
            # box fetch a url of the caller's choosing. It also hands out
            # the local PATHS of licensed books — the map for the file
            # server /artwork used to be.
            if target and not entry_id and not self._require_token():
                return
            order, name = "auto", None
            if entry_id:
                entry = find_entry(load_library(), entry_id)
                if not entry:
                    self._send(404, {"error": f"no library entry {entry_id}"})
                    return
                target = entry["target"]
                order, name = entry["order"], entry["name"]
            if not target:
                self._send(400, {"error": "id or target required"})
                return
            # tracks=1 (fork v0.1.1): include a spotify playlist's track
            # list as episode rows — opt-in, used by the now-view song
            # picker only, so browse taps keep playing directly
            want_tracks = (q.get("tracks") or ["0"])[0] not in ("0", "")
            try:
                self._send(200, expand_target(target, order, name,
                                              tracks=want_tracks))
            except Exception as e:  # expansion hits the network; stay alive
                log(f"expand failed for {target}: {e!r}")
                self._send(502, {"error": str(e)})
        elif url.path == "/media":
            self._send(200, {"collections": media_collections(),
                             "free": _free_bytes(MEDIA_DIR),
                             "floor": MEDIA_FREE_FLOOR})
        elif url.path == "/artwork":
            path = (urllib.parse.parse_qs(url.query).get("path") or [None])[0]
            if not path:
                self._send(400, {"error": "path required"})
            elif not artwork_allowed(path):
                self._send(403, {"error": "path not allowed"})
            elif os.path.splitext(
                    os.path.realpath(path))[1].lower() not in ARTWORK_EXTS:
                # IMAGES ONLY. The allowlist is about DIRECTORIES, and
                # CACHE_DIR holds the audio too — so this endpoint would
                # hand any cached episode or audiobook to anything on the
                # LAN, with no token, because every GET is open by
                # structural necessity. Free podcast audio made that
                # merely untidy; a downloaded audiobook makes it a
                # distribution channel we did not mean to build.
                self._send(403, {"error": "not an image"})
            else:
                self._send_file(path, cache=True)
        elif url.path == "/":
            if not self._static("index.html"):
                self._send(404, {"error": "PWA files not installed"})
        elif "/" not in url.path[1:] and self._static(url.path[1:]):
            pass  # /app.js, /style.css, /manifest.json ...
        else:
            self._send(404, {"error": "not found"})

    def _token_ok(self):
        return _token.verify(self.headers.get("X-Vibb-Token"))

    def _path_is_safe(self):
        allow = SAFE.get(self.command)
        if allow is True:
            return True
        if not allow:
            return False  # unknown method -> privileged
        path = urllib.parse.urlparse(self.path).path
        return (path.rstrip("/") or "/") in allow

    def _deny(self, code):
        # Drain a bounded body first, or the close becomes an RST and the
        # client sees a connection reset instead of our 401 (this hits
        # the big privileged bodies: PUT /library, section-logo uploads).
        # Only when it hasn't been read yet: a body-dependent denial
        # (/play with a raw target) happens AFTER the handler consumed
        # it, and reading again would block until the client times out.
        if not getattr(self, "_body_consumed", False):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if 0 < n <= 65536:
                try:
                    self.rfile.read(n)
                except OSError:
                    pass
        _log_denial(self.command, self.path)
        self._send(401, {"error": "this phone is not linked to the box",
                         "code": code})

    def _require_token(self):
        """Demand the box token regardless of the path — for a request
        that is privileged because of its BODY (see /play with a raw
        target). False means a 401 has already been sent."""
        if not REQUIRE_TOKEN or self._token_ok():
            return True
        self._deny("token_invalid" if self.headers.get("X-Vibb-Token")
                   else "token_required")
        return False

    def _authorized(self):
        """False (and a 401 already sent) when this request may not
        proceed. SAFE-listed paths pass without a token."""
        if self._path_is_safe():
            return True
        return self._require_token()

    def _media_upload(self):
        """Stream one uploaded audio file straight to disk.

        NEVER buffers: a 300MB audiobook read into memory would take the
        box down (512MB, and mpv/go-librespot are already resident). The
        body is copied in chunks to a .part file and renamed on success,
        so an interrupted upload leaves no half-file that expand_entries
        would try to play.
        """
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        coll = _media_safe_name((q.get("collection") or [""])[0] + ".mp3")
        coll = os.path.splitext(coll)[0]
        name = _media_safe_name((q.get("name") or [""])[0])
        if not coll or not name:
            self._send(400, {"error": "collection and name required "
                                      "(audio or cover image)"})
            return
        try:
            total = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            total = 0
        if total <= 0:
            self._send(400, {"error": "Content-Length required"})
            return
        if total > MEDIA_MAX_BYTES:
            self._send(413, {"error": f"file larger than "
                                      f"{MEDIA_MAX_BYTES // 10**6} MB"})
            return
        os.makedirs(MEDIA_DIR, exist_ok=True)
        free = _free_bytes(MEDIA_DIR)
        if free - total < MEDIA_FREE_FLOOR:
            # Refusing is the kind outcome: a full card breaks playback,
            # the cache sweep and the bookmarks, not just this upload.
            self._send(507, {"error": "not enough free space on the box",
                             "free": free, "needed": total})
            return
        d = os.path.join(MEDIA_DIR, coll)
        os.makedirs(d, exist_ok=True)
        dest, tmp = os.path.join(d, name), os.path.join(d, name + ".part")
        got = 0
        try:
            with open(tmp, "wb") as f:
                while got < total:
                    chunk = self.rfile.read(min(262144, total - got))
                    if not chunk:
                        raise OSError("upload ended early")
                    f.write(chunk)
                    got += len(chunk)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, dest)
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            log(f"media upload failed ({name}): {e!r}")
            self._send(500, {"error": str(e)})
            return
        tags, art = {}, False
        if os.path.splitext(name)[1].lower() in MEDIA_EXTS[:7]:  # audio
            tags = _media_note_meta(d, name, dest)
            art = _media_extract_cover(dest, d)
            # ... and the file's OWN art to .art/<name>.jpg: a folder of
            # loose singles shows each song's cover, not the first
            # upload's (QA-reviewed 2026-08-13). No picture stream ->
            # a .none marker, so the nightly heal skips the file.
            content.extract_track_art(d, name)
        log(f"media: uploaded {coll}/{name} ({got // 1000} kB)"
            + (f" — {tags['title']}" if tags.get("title") else "")
            + (" +cover" if art else ""))
        self._send(200, {"ok": True, "collection": coll, "name": name,
                         "path": d, "bytes": got, "tags": tags,
                         "cover": art})

    def _json_ct(self):
        """Cross-origin CSRF guard for every state-changing request.

        A browser can only send an unauthorized CROSS-SITE request as a
        'simple request', and those may not carry
        `Content-Type: application/json` (only form-urlencoded, plain
        text or multipart). Anything else forces a CORS preflight, which
        this server answers 501 (no do_OPTIONS, and no Access-Control-*
        header anywhere) — so the real request is never sent. Requiring
        JSON therefore makes every POST/PUT unreachable from a random
        web page the parent happens to visit.

        This was live, not theoretical (QA review 2026-07-25): do_POST
        parses the body with json.loads and, on ValueError, fell through
        with `body = {}` instead of failing. Every endpoint that needs
        no body — /system/shutdown, /wifi/reconnect, /bt/scan,
        /bt/visible, /spotify/logout, /stop — could therefore be fired
        by a plain auto-submitting <form> on any page someone on the LAN
        opened. No enctype trick required.

        Safe for every internal caller: boxapi.py, the PWA's api()
        wrapper and play.sh's curl calls all set the header already.
        """
        ct = (self.headers.get("Content-Type") or "").split(";")[0]
        ct = ct.strip().lower()
        if ct == "application/json":
            return True
        # A file upload can't be JSON. octet-stream is still NOT a
        # "simple request" — an HTML form can only send urlencoded,
        # text/plain or multipart — so a cross-origin page still can't
        # reach this without a preflight we never grant. (multipart
        # WOULD be reachable, which is exactly why uploads are raw
        # octet-stream with the name in the query string instead.)
        if ct == "application/octet-stream" and \
                urllib.parse.urlparse(self.path).path == "/media/upload":
            return True
        # Drain a bounded body before answering: replying while request
        # data is still unread turns the socket close into an RST, and
        # the client sees a connection reset instead of our error.
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if 0 < n <= 65536:
            try:
                self.rfile.read(n)
            except OSError:
                pass
        self._send(415, {"error": "Content-Type: application/json required"})
        return False

    def do_PUT(self):
        if not self._json_ct():
            return
        n = int(self.headers.get("Content-Length") or 0)
        self._body_consumed = True  # _deny must not try to drain it again
        try:
            body = json.loads(self.rfile.read(n)) if n else {}
        except ValueError:
            self._send(400, {"error": "invalid json"})
            return
        if self.path == "/library":
            try:
                lib = normalize_library(body)
            except ValueError as e:
                self._send(400, {"error": str(e)})
                return
            with _library.LIB_LOCK:  # vs the sweeper's profile sync
                save_library(lib)
            log(f"library updated ({sum(len(s['entries']) for s in lib['sections'])} entries)")
            # Free the disk held by entries just removed (or flipped to 'no
            # offline'): only entries that still want offline copies keep them.
            try:
                keep = [e["target"] for s in lib["sections"] for e in s["entries"]
                        if e.get("cache")]
                gone = content.prune_cache(keep)
                _sysinfo.invalidate_dir_sizes()  # /system sizes are stale
                if gone:
                    log(f"cache: pruned {len(gone)} orphaned offline "
                        f"cache(s): {', '.join(gone)}")
            except Exception as e:  # cleanup must never fail the save
                log(f"cache prune failed: {e!r}")
            _sync_wake.set()  # start caching new/changed entries right away
            self._send(200, lib)
        elif self.path == "/settings":
            try:
                self._send(200, update_settings(body))
            except ValueError as e:
                self._send(400, {"error": str(e)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._json_ct():
            return
        if urllib.parse.urlparse(self.path).path == "/media/upload":
            self._media_upload()   # streams the body; must not be read here
            return
        n = int(self.headers.get("Content-Length") or 0)
        self._body_consumed = True  # _deny must not try to drain it again
        try:
            body = json.loads(self.rfile.read(n)) if n else {}
        except ValueError:
            body = {}
        try:
            if self.path == "/play":
                target = body.get("target")
                # A RAW target plays whatever URL the caller names — the
                # one open endpoint that can put NEW, uncurated content
                # into a kid's room (and makes the box fetch an
                # attacker-chosen URL). The {"id": ...} form can only
                # ever play something a parent already put in the
                # library, so it stays open for RFID cards, buttons and
                # the phone shortcut.
                if target and not self._require_token():
                    return
                reverse = False
                cache = None  # None = legacy behaviour for raw targets
                resume = True  # 'from start' entries turn this off
                if not target and body.get("id"):
                    entry = find_entry(load_library(), body["id"])
                    if not entry:
                        self._send(404, {"error": f"no library entry {body['id']}"})
                        return
                    target = entry["target"]
                    # Play in the same order the menu showed the episodes
                    reverse = (entry["order"] != "auto"
                               and entry["order"] != _natural_order(target))
                    cache = entry.get("cache", 0)
                    resume = entry.get("resume", True)
                if not target:
                    self._send(400, {"error": "target or id required"})
                    return
                _library.acknowledge_new(target)  # played it -> clear its dot
                self._send(200, ORCH.play(target, bool(body.get("fresh")),
                                          body.get("episode") or None, reverse,
                                          cache, resume))
            elif self.path in ("/playpause", "/next", "/prev"):
                self._send(200, ORCH.command(self.path[1:]))
            elif self.path == "/pause":
                self._send(200, ORCH.pause())
            elif self.path == "/resume":
                self._send(200, ORCH.unpause())
            elif self.path == "/shuffle":
                if not isinstance(body.get("enabled"), bool):
                    self._send(400, {"error": "enabled (bool) required"})
                    return
                self._send(200, ORCH.shuffle(body["enabled"]))
            elif self.path == "/volume":
                if body.get("volume") is None and body.get("delta") is None:
                    self._send(400, {"error": "volume or delta required"})
                    return
                self._send(200, ORCH.volume(absolute=body.get("volume"),
                                            delta=body.get("delta")))
            elif self.path == "/seek":
                if body.get("position") is None and body.get("delta") is None:
                    self._send(400, {"error": "position or delta required"})
                    return
                try:
                    pos, dl = body.get("position"), body.get("delta")
                    pos = None if pos is None else float(pos)
                    dl = None if dl is None else float(dl)
                except (TypeError, ValueError):
                    self._send(400, {"error": "position/delta must be numeric"})
                    return
                self._send(200, ORCH.seek(position=pos, delta=dl))
            elif self.path == "/output":
                r = ORCH.set_output(body.get("device"),
                                    fallback=bool(body.get("fallback")),
                                    uid=body.get("uid"),
                                    name=body.get("name"))
                if r is None:
                    self._send(400, {"error": "device must be one of "
                                     f"{sorted(OUTPUT_PCMS)} or sonos "
                                     "(with uid)"})
                    return
                self._send(200, r)
            elif self.path == "/system/wifi":
                if not isinstance(body.get("enabled"), bool):
                    self._send(400, {"error": "enabled (bool) required"})
                    return
                self._send(200, set_wifi(body["enabled"]))
            elif self.path == "/system/shutdown":
                self._send(200, shutdown(bool(body.get("restart"))))
            elif self.path == "/spotify/logout":
                # the bookmarks belong to the old account
                _spotify.clear_all_bookmarks()
                r = _spotify.logout()
                self._send(200 if r.get("ok") else 500, r)
            elif self.path == "/storytel/credentials":
                # Privileged by default-deny (not in SAFE). Store the
                # account, or clear it when email is null. NEVER echo the
                # password. A probe login validates it synchronously.
                email = body.get("email")
                if email and not isinstance(body.get("password"), str):
                    self._send(400, {"error": "password (string) required"})
                    return
                _storytel.save_credentials(email, body.get("password") or "")
                if not email:
                    self._send(200, {"configured": False})
                    return
                try:
                    n = len(_storytel.normalize_shelf(_storytel.bookshelf()))
                    _sync_wake.set()   # start downloading what's curated
                    self._send(200, {"configured": True, "series": n})
                except RuntimeError:
                    _storytel.save_credentials(None, None)
                    self._send(401, {"error": "Storytel refused that "
                                     "email/password"})
                except OSError as e:
                    self._send(502, {"error": f"could not reach Storytel: {e}"})
            elif self.path == "/storytel/logout":
                # The books go with the account. Storytel's own app drops
                # its downloads on logout, and a box that can no longer
                # authenticate has no business keeping a playable shelf.
                gone = _storytel.forget_downloads()
                _storytel.save_credentials(None, None)
                log(f"storytel: logged out, {gone} downloaded book(s) removed")
                self._send(200, {"configured": False, "removed": gone})
            elif self.path == "/storytel/shelf":
                # The picker: the account's audiobooks grouped into series.
                if not _storytel.configured():
                    self._send(503, {"error": (
                        "No Storytel account on this box yet — add one in "
                        "the Storytel panel first.")})
                    return
                try:
                    series = _storytel.normalize_shelf(_storytel.bookshelf())
                    in_lib = {e["target"]
                              for s in load_library().get("sections", [])
                              for e in s.get("entries", [])
                              if _storytel.is_storytel(e["target"])}
                    for g in series:
                        # 'on the box' must mean DOWNLOADED, not merely
                        # added to the library — else a series whose
                        # download failed still reads as present (field
                        # 2026-08-15)
                        g["in_library"] = g["target"] in in_lib
                        g["downloaded"] = _storytel.downloaded_count(
                            g["target"])
                    self._send(200, {"series": series})
                except RuntimeError:
                    self._send(401, {"error": "Storytel login failed — "
                                     "the saved password may be stale"})
                except OSError as e:
                    self._send(502, {"error": f"could not reach Storytel: {e}"})
            elif self.path == "/library/section-logo":
                # Upload (base64/data-URI) or remove (data: null) a home-
                # screen logo for one section. The PWA downsizes client-side.
                sid = str(body.get("id") or "")
                with _library.LIB_LOCK:  # load->mutate->save, one writer
                    lib = load_library()
                    sec = next((s for s in lib["sections"]
                                if s["id"] == sid), None)
                    if not sec:
                        self._send(404, {"error": f"no section {sid!r}"})
                        return
                    path = os.path.join(ART_DIR, f"section-{sid}.jpg")
                    data = body.get("data")
                    if not data:  # remove the logo
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                        sec.pop("image", None)
                    else:
                        try:
                            b64 = data.split(",", 1)[1] \
                                if data.startswith("data:") else data
                            raw = base64.b64decode(b64, validate=True)
                        except (ValueError, AttributeError):
                            self._send(400, {"error": "invalid image data"})
                            return
                        if not 100 <= len(raw) <= 3_000_000:
                            self._send(400,
                                       {"error": "image must be 100B-3MB"})
                            return
                        os.makedirs(ART_DIR, exist_ok=True)
                        with open(path + ".tmp", "wb") as f:
                            f.write(raw)
                        os.replace(path + ".tmp", path)
                        sec["image"] = path
                    save_library(normalize_library(lib))
                log(f"section logo {'set' if data else 'removed'}: {sid}")
                self._send(200, lib)
            elif self.path == "/backup/configure":
                # Point the box at ANY rclone-backed restic repo. The owner
                # runs `rclone config` on their own machine (which is what
                # handles OAuth backends) and pastes the resulting block —
                # nothing here is tied to one provider. Privileged by
                # default-deny: POST is not in SAFE.
                try:
                    self._send(200, _backup.configure(
                        body.get("rclone_conf") or "",
                        repo=body.get("repo") or None,
                        repo_password=body.get("repo_password") or None,
                        path=body.get("path") or None))
                except RuntimeError as e:
                    self._send(400, {"error": str(e)})
            elif self.path == "/audio/selftest":
                # re-run the policy probes now (pipewire only): ~5s of
                # silent streams, off the request thread
                if _audio.stack() != "pipewire":
                    self._send(409, {"error": "audio-stack-not-pipewire"})
                else:
                    threading.Thread(target=_audio_policy_run, args=("request",),
                                     daemon=True).start()
                    self._send(202, {"started": True})
            elif self.path == "/backup/now":
                try:
                    r = _backup.backup_now()
                    log(f"backup: snapshot {r.get('snapshot_id')} "
                        f"({r.get('files')} files)")
                    self._send(200, r)
                except RuntimeError as e:
                    self._send(503, {"error": str(e)})
            elif self.path == "/backup/restore":
                # Applies config, secrets and every child's place, then
                # reboots: the daemon holds library/settings in memory and
                # would write them back over the restore, and go-librespot,
                # BlueZ and NM never re-read on their own.
                try:
                    # Before the apply, not after: the bookmarker threads run
                    # throughout, and anything they write from here on would
                    # be pre-restore state landing on top of the restore.
                    _RESTORING[0] = True
                    manifest = _backup.restore_snapshot(
                        body.get("snapshot") or "latest")
                except RuntimeError as e:
                    # Reset the flag: a FAILED restore wrote nothing, and
                    # leaving it set wedges the daemon in "restoring" for the
                    # rest of its life — _on_term then exits without flushing
                    # and every bookmark from that point is silently lost
                    # (QA 2026-08-17). Only a restore that actually applied
                    # may stand the writers down.
                    _RESTORING[0] = False
                    self._send(503, {"error": str(e)})
                except ValueError as e:
                    _RESTORING[0] = False
                    self._send(400, {"error": str(e)})
                else:
                    log(f"backup: restored {len(manifest['files'])} files — "
                        "rebooting so every consumer re-reads from disk")
                    self._send(200, {"restored": len(manifest["files"]),
                                     "created": manifest.get("created"),
                                     "rebooting": True})
                    threading.Thread(
                        target=lambda: (time.sleep(1), subprocess.run(
                            ["systemctl", "reboot"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)),
                        daemon=True).start()
            elif self.path == "/wifi/reconnect":
                # on-demand 'get the net back now' (offline-Spotify popup's
                # X). Quiesce A2DP — the NM scan shares the 2.4GHz radio —
                # then actively wait for a known network; on success clear
                # the offline flag and unpark go-librespot so the next
                # play works at once, without waiting on the supervisor.
                try:
                    secs = min(max(int(body.get("secs") or 30), 5), 60)
                except (TypeError, ValueError):
                    secs = 30
                resume = _bt_quiesce()
                r = wifi_reconnect(secs)
                _bt_resume(resume)
                if r and r.get("ok"):
                    _SPOT_OFFLINE[0] = False
                    threading.Thread(
                        target=lambda: subprocess.run(
                            ["systemctl", "start", "go-librespot"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=30),
                        daemon=True).start()
                self._send(409 if r is None else 200,
                           r or {"error": "wifi operation already in progress"})
            elif self.path == "/wifi/hotspot":
                if not isinstance(body.get("enabled"), bool):
                    self._send(400, {"error": "enabled (bool) required"})
                    return
                if body["enabled"]:
                    ok = start_hotspot()
                    self._send(200, {"ok": ok, "ssid": HOTSPOT_SSID,
                                     "password": HOTSPOT_PSK})
                else:
                    stop_hotspot()
                    self._send(200, {"ok": True})
            elif self.path == "/wifi/scan":
                # a wifi scan sweeps all 13 channels off-frequency — as
                # A2DP-hostile as BT discovery, so it gets the same
                # quiesce. Only on the bt output: a scan can't hurt the
                # built-in speaker, and stopping local playback for it
                # would be an audible interruption for nothing.
                resume = (_bt_quiesce()
                          if current_output()["output"] == "bt" else False)
                r = wifi_scan()
                _bt_resume(resume)
                self._send(409 if r is None else 200,
                           r or {"error": "wifi operation already in progress"})
            elif self.path in ("/wifi/connect", "/wifi/forget",
                               "/wifi/add"):
                ssid = str(body.get("ssid") or "").strip()
                if not ssid or len(ssid) > 32:
                    self._send(400, {"error": "ssid required (max 32 chars)"})
                    return
                pw = str(body["password"]) if body.get("password") else None
                if self.path == "/wifi/connect":
                    r = wifi_connect(ssid, pw)
                elif self.path == "/wifi/add":
                    r = wifi_add(ssid, pw)
                else:
                    r = wifi_forget(ssid)
                self._send(409 if r is None else 200,
                           r or {"error": "wifi operation already in progress"})
            elif self.path == "/bt/scan":
                resume = _bt_quiesce()  # discovery makes A2DP stutter badly
                r = bt_scan()
                _bt_resume(resume)
                self._send(409 if r is None else 200,
                           r or {"error": "bt operation already in progress"})
            elif self.path == "/bt/pair":
                args = ["connect"]
                if body.get("name"):
                    args.append(str(body["name"]))
                resume = _bt_quiesce()
                r = bt_action(args, timeout=120)
                _bt_resume(resume)
                self._send(409 if r is None else 200,
                           r or {"error": "bt operation already in progress"})
            elif self.path == "/bt/lost":
                # internal: btwatchd's transport-died hint (see
                # _bt_transport_lost — guarded, safe on duplicates)
                self._send(200, _bt_transport_lost())
            elif self.path == "/bt/visible":
                try:
                    secs = min(max(int(body.get("secs") or 120), 10), 300)
                except (TypeError, ValueError):
                    secs = 120
                # an incoming SSP dance during A2DP streaming is the same
                # firmware crasher as an outgoing pair — quiesce around it
                resume = _bt_quiesce()
                r = bt_action(["visible", str(secs)], timeout=secs + 150)
                _bt_resume(resume)
                self._send(409 if r is None else 200,
                           r or {"error": "bt operation already in progress"})
            elif self.path in ("/bt/connect", "/bt/forget",
                               "/bt/disconnect"):
                mac = str(body.get("mac") or "")
                if not MAC_RE.match(mac):
                    self._send(400, {"error": "valid mac required"})
                    return
                cmd = {"/bt/connect": "use", "/bt/forget": "forget",
                       "/bt/disconnect": "disconnect"}[self.path]
                resume = _bt_quiesce() if cmd == "use" else False
                # 240s, not 90: connect can legitimately run a full
                # firmware recover() (two re-attach rounds + rfkill
                # power-cycle) — a 90s SIGKILL could land BETWEEN
                # rfkill block and unblock and leave the radio down
                # for good (review 2026-07-18 R6)
                r = bt_action([cmd, mac], timeout=240 if cmd == "use" else 30)
                if cmd == "use":
                    _bt_resume(resume)
                self._send(409 if r is None else 200,
                           r or {"error": "bt operation already in progress"})
            elif self.path == "/bt/rename":
                mac = str(body.get("mac") or "")
                if not MAC_RE.match(mac):
                    self._send(400, {"error": "valid mac required"})
                    return
                # a custom name for the speaker (blank clears it), sanitized
                # before it reaches BlueZ / the screen; a plain property
                # write, no radio quiesce.
                name = _clean_bt_name(body.get("name"))
                r = bt_action(["rename", mac, name], timeout=20)
                self._send(409 if r is None else 200,
                           r or {"error": "bt operation already in progress"})
            elif self.path == "/media/delete":
                coll = _media_safe_name(str(body.get("collection") or "")
                                        + ".mp3")
                coll = os.path.splitext(coll)[0]
                if not coll:
                    self._send(400, {"error": "collection required"})
                    return
                d = os.path.join(MEDIA_DIR, coll)
                name = _media_safe_name(body.get("name"))
                try:
                    if name:  # one file
                        os.remove(os.path.join(d, name))
                        content.drop_track_art(d, name)  # + its art/marker
                    else:     # the whole collection — .art/ first: a bare
                        # os.remove on a DIRECTORY raises and left the
                        # collection undeletable from the PWA (QA blocker
                        # 2026-08-13)
                        art_d = os.path.join(d, content.ART_DIR)
                        if os.path.isdir(art_d):
                            for f in os.listdir(art_d):
                                os.remove(os.path.join(art_d, f))
                            os.rmdir(art_d)
                        for f in os.listdir(d):
                            os.remove(os.path.join(d, f))
                        os.rmdir(d)
                except OSError as e:
                    self._send(404, {"error": str(e)})
                    return
                log(f"media: removed {coll}" + (f"/{name}" if name else ""))
                self._send(200, {"ok": True})
            elif self.path == "/stop":
                self._send(200, ORCH.stop(
                    keep_bookmark=bool(body.get("keep"))))
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:  # never let one request kill the daemon
            log(f"error on {self.path}: {e!r}")
            self._send(500, {"error": str(e)})


# --- privileged-endpoint gate (SECURITY.md Model A+B) ----------------
# DEFAULT DENY. SAFE lists what may be reached WITHOUT the box token;
# every other path, and every method not named here, needs it. The
# direction is the whole point: an endpoint someone forgets to classify
# fails CLOSED, so anything added later is privileged until a human
# deliberately puts it in this table.
#
# GET/HEAD being blanket-safe is STRUCTURAL, and the real reason is
# BOOTSTRAP, not <img>: the browser fetches index.html, app.js, the
# manifest and the icons before any of our code runs, and the token
# lives in localStorage which app.js itself creates — so gating those
# would make it impossible to ever deliver the token they would need.
# (The <img> argument used to be cited here and is soft: the PWA builds
# its two artwork <img> tags in JS and could fetch+blob them, and the
# screen never uses /artwork at all — it opens local files directly and
# already sends the token on every API GET.)
#
# TWO obligations, and the second is the one whose absence cost us:
#
#     NEVER serve a state-changing or secret-revealing endpoint via GET.
#
#     NEVER let a GET serve arbitrary file bytes. A GET that serves a
#     file must be constrained by CONTENT TYPE, not merely by which
#     directory the file sits in. /artwork was allowlisted by directory,
#     those directories later grew audio, and it quietly became a way to
#     pull a licensed audiobook off the box with a plain url (found
#     2026-08-15). A path allowlist answers "where", never "what".
#
# (True today: every GET is a read, and no endpoint ever returns the
# token — rotation happens on the box screen, not over HTTP.)
SAFE = {
    "GET": True,
    "HEAD": True,
    # Playback only. The worst a LAN prankster can do with these is annoy
    # somebody, and keeping them open is what lets the phone shortcut
    # ("Hey Siri, pause Vibb") work with no setup at all.
    # /play is here for its {"id": ...} form; a RAW target is checked
    # separately inside the handler (it can put arbitrary content in a
    # kid's room, so it needs the token).
    "POST": frozenset({"/play", "/playpause", "/next", "/prev", "/pause",
                       "/resume", "/shuffle", "/volume", "/stop", "/seek"}),
    # PUT /library replaces the entire library and PUT /settings rewrites
    # config — both privileged.
    "PUT": frozenset(),
}
# Recovery valve for a box whose screen is broken. Documented in
# SECURITY.md, never shipped enabled.
REQUIRE_TOKEN = os.environ.get("VIBB_REQUIRE_TOKEN", "1") != "0"
# Set for the rest of this process's life once a restore has applied files:
# every writer of restored state must stand down, or the reboot's own
# shutdown flush puts pre-restore positions back (see _on_term).
_RESTORING = [False]

_DENY_LOG = {"at": 0.0, "n": 0}


def _log_denial(method, path):
    """One line a minute with a count — a LAN scanner must not be able to
    flood the journal (and SD card) with rejections."""
    now = time.monotonic()
    _DENY_LOG["n"] += 1
    if now - _DENY_LOG["at"] < 60:
        return
    n = _DENY_LOG["n"]
    _DENY_LOG["at"], _DENY_LOG["n"] = now, 0
    extra = f" (+{n - 1} more)" if n > 1 else ""
    log(f"unauthorized {method} {path} — no valid box token{extra}")


def _gate(fn):
    @functools.wraps(fn)
    def wrapper(self):
        if not self._authorized():
            return
        return fn(self)
    wrapper._vibb_gated = True
    return wrapper


# Wrap EVERY request method the class defines. Doing it here, rather than
# by editing each do_* by hand, is what makes the default-deny real: a
# do_DELETE added in a year is gated the day it is written, without
# anyone remembering this file. (Snapshot the names — we mutate the class
# while looking at it.)
for _name in [n for n in list(vars(Handler)) if n.startswith("do_")]:
    setattr(Handler, _name, _gate(vars(Handler)[_name]))
# PortalHandler (:80) is deliberately NOT gated: it only ever answers a
# 302 to its own captive-portal page and serves no API.



# --- uploaded media (own audiobooks, ripped CDs, the kids' recordings) ---
# Books run 150-400MB, ~10x a podcast episode, so a full card is a REAL
# failure mode here — and a full card takes the whole box down, not just
# the upload. Refuse below the floor rather than filling it.
MEDIA_FREE_FLOOR = int(os.environ.get("VIBB_MEDIA_FREE_FLOOR",
                                      str(1_500_000_000)))
MEDIA_MAX_BYTES = int(os.environ.get("VIBB_MEDIA_MAX_BYTES",
                                     str(2_000_000_000)))
MEDIA_EXTS = (".mp3", ".m4a", ".m4b", ".ogg", ".opus", ".flac", ".wav",
              ".jpg", ".jpeg", ".png")  # audio + cover art


MEDIA_META = ".vibb-meta.json"


def _media_probe(path):
    """Embedded tags for one uploaded file, via ffprobe.

    ffmpeg is already a dependency (install.sh), so this costs no new
    package — and it reads headers only, so it is fast even on a 300MB
    audiobook. Best-effort: a file with no tags just keeps its filename
    as the title, which is what happened before this existed.
    """
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", path],
            capture_output=True, text=True, timeout=30)
        tags = (json.loads(r.stdout or "{}").get("format") or {}).get("tags")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    tags = {k.lower(): v for k, v in (tags or {}).items()}
    out = {}
    for key in ("title", "album", "artist", "album_artist"):
        if tags.get(key):
            out[key] = str(tags[key])[:200]
    # "3" or "3/12" -> 3, so chapters order by tag rather than filename
    m = re.match(r"\s*(\d+)", str(tags.get("track") or ""))
    if m:
        out["track"] = int(m.group(1))
    return out


def _media_extract_cover(src, folder):
    """Pull embedded art out to cover.jpg — the name collection_image()
    already looks for, so the art then works with no other change. Only
    when the folder has no cover yet: a parent-uploaded cover.jpg must
    win over whatever is baked into the file."""
    for name in ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg"):
        if os.path.exists(os.path.join(folder, name)):
            return False
    dest = os.path.join(folder, "cover.jpg")
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", src, "-an",
             "-frames:v", "1", "-update", "1", dest],
            capture_output=True, timeout=60)
        if r.returncode == 0 and os.path.getsize(dest) > 0:
            return True
        os.remove(dest)
    except (OSError, subprocess.SubprocessError):
        try:
            os.remove(dest)
        except OSError:
            pass
    return False


def _media_note_meta(folder, name, path):
    """Record this file's tags in the collection's sidecar, so the menus
    show real titles in track order instead of raw filenames."""
    meta_path = os.path.join(folder, MEDIA_META)
    try:
        with open(meta_path) as f:
            meta = json.load(f)
    except (OSError, ValueError):
        meta = {}
    tags = _media_probe(path)
    if tags:
        meta[name] = tags
    else:
        meta.pop(name, None)
    try:
        tmp = meta_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(meta, f)
        os.replace(tmp, meta_path)
    except OSError:
        pass
    return tags


def _free_bytes(path):
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize
    except OSError:
        return 0


def _media_safe_name(raw):
    """A single path component, no traversal, keeping a known extension.
    Uploads name their own files, so this is the security boundary."""
    name = os.path.basename(str(raw or "").replace("\\", "/")).strip()
    name = re.sub(r"[^A-Za-z0-9._ '()\-]", "_", name)[:120].lstrip(".")
    ext = os.path.splitext(name)[1].lower()
    return name if name and ext in MEDIA_EXTS else ""


def media_collections():
    """[{name, files, bytes}] — what the PWA lists and plays."""
    out = []
    try:
        names = sorted(os.listdir(MEDIA_DIR))
    except OSError:
        return out
    for coll in names:
        d = os.path.join(MEDIA_DIR, coll)
        if not os.path.isdir(d):
            continue
        files, total = [], 0
        for f in sorted(os.listdir(d)):
            try:
                total += os.path.getsize(os.path.join(d, f))
            except OSError:
                pass
            if os.path.splitext(f)[1].lower() in MEDIA_EXTS[:7]:
                files.append(f)
        out.append({"name": coll, "path": d, "files": files,
                    "bytes": total})
    return out


def _clean_bt_name(raw):
    """Sanitize a user-supplied speaker name before it reaches BlueZ and
    the screen: drop control/non-printable chars, collapse to a single
    line, cap the length. Blank (after cleaning) clears the alias."""
    return "".join(c for c in str(raw or "") if c.isprintable())[:64].strip()


def _bt_quiesce():
    """Connecting/pairing WHILE A2DP streams crashes the Zero 2 W's BT
    firmware outright (kernel: 'hardware error 0x00' — seen in the field
    when adding headset #2 mid-play). Silence the radio first; the caller
    resumes afterwards and the bookmark makes it seamless."""
    resume = False
    with ORCH.lock:
        if ORCH._mpv_alive():
            resume = True
            log("bt connect: stopping playback first (firmware safety)")
            ORCH._stop_child()  # bookmark survives; we resume after
    try:
        if spotify_playing():
            resume = True
            go("/player/pause")
    except OSError:
        pass
    return resume


def _bt_resume(resume):
    if not resume:
        return
    with ORCH.lock:
        target, reverse, resume = ORCH.target, ORCH.reverse, ORCH.resume
        if target and not ORCH._mpv_alive():
            log("bt connect done — resuming playback on the new output")
            ORCH._spawn(target, reverse=reverse, resume=resume,
                        exact=True, rewind=ORCH._resume_overlap())


def _wifi_boot_reenable():
    """'Wifi off' in the PWA rfkill-blocks the radio, and systemd-rfkill
    restores that block across reboots — a headless box would stay dark
    and unreachable forever. Make the switch session-only: a power cycle
    always brings wifi (and with it the PWA) back."""
    try:
        enabled, _ssid, _ip = wifi_state()
        if not enabled:
            log("wifi was left off — re-enabling on startup")
            set_wifi(True)
    except Exception as e:
        log(f"wifi boot re-enable failed: {e!r}")


# Box-initiated Spotify: the bookmarker keeps this true/false from a status
# fetched while go-librespot is still alive, so shutdown's was_playing snapshot
# can trust it WITHOUT a live query. At poweroff systemd TERMs go-librespot in
# the same cgroup, so a fresh status() there races its death and reads 'not
# playing' — mpv sidesteps the same race via its now-playing.json fallback, and
# Spotify had none. Box-initiated ONLY (source==spotify AND a spotify target),
# so a phone-driven Connect session never arms boot-resume.
_SPOT_LAST_PLAYING = [False]

# The freshest box-initiated bookmark, kept in memory so a reboot/poweroff
# can flush it even mid-song. The bookmarker throttles DISK writes (SD
# hygiene: 30s / on track change), so a position — e.g. a seek made seconds
# ago — otherwise lives only in bm_pending and dies with the thread at TERM,
# leaving boot-resume to continue from a stale spot. _on_term flushes this.
_SPOT_PENDING_BM = [None]

SONOS_POLL_S = float(os.environ.get("VIBB_SONOS_POLL", "5"))


def _sonos_poller():
    """The renderer's heartbeat: reads the sidecar's /state snapshot (a
    localhost memory read — the sidecar owns the SOAP polling), publishes
    it for status(), writes the bookmark from MEASURED positions, and
    advances OUR queue when a url-kind episode ends. Idles at 30s ticks
    while the renderer is box; _sonos_wake pokes it on switches and
    controls so the card flips fast.

    Startup doubles as RECONCILIATION: daemon restarted mid-session
    (install.sh restarts it on every update) with the speaker still
    playing must re-attach — never re-play, which would jump the episode
    back over music that never stopped (architect crash matrix b)."""
    if _renderer.is_sonos():
        content.PREFER_REMOTE = True
        _library._EXPAND_CACHE.clear()  # entries differ by renderer: sonos
        #   lists every book, the box only the downloaded ones. A stale
        #   300s entry otherwise shows the wrong list — and tapping an
        #   undownloaded book on the box plays a DIFFERENT one from 0.
        try:
            snap = _renderer.get("/state")
            if snap.get("armed") and snap.get("transport") in (
                    "PLAYING", "PAUSED_PLAYBACK"):
                with ORCH.lock:
                    ORCH.source = "sonos"
                    ORCH.sonos_snap = snap
                    ORCH.sonos_snap_at = time.monotonic()
                # rebuild the queue view for the screen (ids/titles);
                # adopt tells the sidecar we are its vibbd again
                if ORCH.target and not is_spotify(ORCH.target):
                    try:
                        _story = _storytel.is_storytel(ORCH.target)
                        ORCH.sonos_queue = [
                            e for e in content.expand_entries(ORCH.target)
                            if _story or str(e["url"]).startswith(
                                ("http://", "https://"))]
                        ORCH.sonos_kind = "url"
                        uri = snap.get("uri")
                        if _story:
                            # The speaker holds a SIGNED url minted by the
                            # previous daemon process — string equality can
                            # never match it, and a None index silently
                            # disables bookmarks and queue advance for the
                            # rest of the session. Match on the book id,
                            # which the signed url carries as a QUERY
                            # PARAM. Not as a substring: the 75-char token
                            # can contain a six-digit run by luck, and
                            # next() would then adopt the wrong book and
                            # bookmark this position onto it.
                            want = (urllib.parse.parse_qs(
                                urllib.parse.urlsplit(str(uri)).query)
                                .get("consumableId") or [None])[0]
                            ORCH.sonos_idx = next(
                                (i for i, e in enumerate(ORCH.sonos_queue)
                                 if want and e["id"] == want), None)
                        else:
                            ORCH.sonos_idx = next(
                                (i for i, e in enumerate(ORCH.sonos_queue)
                                 if e["url"] == uri), None)
                        # Restore the LENGTH from the queue row. The
                        # adopted snapshot comes straight from the
                        # speaker, which reports 0 for a signed url — and
                        # on a fresh process there is no earlier value to
                        # carry forward, so a restart mid-book left the
                        # card with duration 0 and no position at all
                        # (field 2026-08-15: 33666s before the restart,
                        # 0 after). We know it from the shelf.
                        if ORCH.sonos_idx is not None \
                                and not (ORCH.sonos_snap or {}).get("dur_s"):
                            known = ORCH.sonos_queue[ORCH.sonos_idx].get(
                                "dur_s")
                            if known:
                                ORCH.sonos_snap = dict(ORCH.sonos_snap,
                                                       dur_s=known,
                                                       dur_from_shelf=True)
                    except Exception:
                        pass
                elif ORCH.target:
                    ORCH.sonos_kind = "spotify_sharelink"
                    try:
                        uri = _spotify.to_uri(ORCH.target)
                        listing = _spotify.context_tracks(uri) or {}
                        ORCH.sonos_ctx = uri
                        ORCH.sonos_queue = [
                            {"url": t.get("uri"), "id": t.get("uri"),
                             "title": (t.get("track") or {}).get("name"),
                             "image": (t.get("track") or {}).get(
                                 "album_cover_url")}
                            for t in listing.get("tracks") or []]
                    except Exception:
                        ORCH.sonos_map_trusted = False
                rd = _renderer.read()
                _renderer.post("/adopt", {"uid": rd.get("uid"),
                                          "kind": ORCH.sonos_kind,
                                          "uri": snap.get("uri")})
                log("sonos: adopted a live session on daemon start")
            else:
                # No live remote session at boot -> the renderer reverts
                # to the BOX (owner 2026-08-09): a reboot must never
                # start audio on a speaker in a room nobody asked in.
                # It does NOT then play locally either (owner 2026-08-18):
                # the box lands on the now-playing screen for whatever was
                # in progress, PAUSED, and one tap continues it. Moving a
                # prior sonos session onto the built-in speaker at boot was
                # the specific complaint.
                _renderer.write("box")
                content.PREFER_REMOTE = False
                _library._EXPAND_CACHE.clear()
                with ORCH.lock:
                    if ORCH.source == "sonos":
                        ORCH.source = ("spotify" if ORCH.target
                                       and is_spotify(ORCH.target)
                                       else "mpv")
        except _renderer.SidecarDown:
            log("sonos: sidecar not up yet — reconcile on next tick")
    ends_near = 0
    while True:
        _sonos_wake.wait(timeout=SONOS_POLL_S
                         if ORCH.source == "sonos" else 30)
        _sonos_wake.clear()
        if ORCH.source != "sonos":
            continue
        try:
            snap = _renderer.get("/state")
        except _renderer.SidecarDown:
            continue  # keep the LAST snapshot — it goes stale honestly,
            #           and stale reads as not-playing; never zeroed
        try:

            pend = ORCH.sonos_pending
            turi_now = snap.get("track_spotify_uri")
            if (pend and turi_now and turi_now != pend[0]
                    and time.monotonic() - pend[1] < 8):
                # settle: the speaker still reports the PREVIOUS track.
                # The index guard existed, but rel/dur leaked through and
                # the bar showed 2:31 on the new title before snapping to
                # 0:03 — on EVERY sharelink press (QA §1B). Keep the seeded
                # position fields and extrapolation base.
                old_s = ORCH.sonos_snap or {}
                snap = dict(snap, rel_s=old_s.get("rel_s"),
                            dur_s=old_s.get("dur_s"),
                            track_title=old_s.get("track_title"),
                            track_art=old_s.get("track_art"))
                ORCH.sonos_snap = snap
            else:
                if not snap.get("dur_s"):
                    # Keep a length we already know. A Sonos handed a signed
                    # url with no file extension does not work the length out
                    # for itself — it reports TrackDuration "0:00:00", which
                    # the sidecar turns into 0, NOT None. Testing `is None`
                    # let that zero through and it erased the seeded duration,
                    # so the screen showed 0:00 and drew no bar at all (field
                    # 2026-08-15). Falsy is the correct test; the speaker's
                    # own value wins the moment it reports a real one.
                    #
                    # ONLY FOR THE SAME TRACK. Carrying a duration across a
                    # track change is destructive, not cosmetic: save_state
                    # DELETES an episode's bookmark when pos > duration - 20
                    # ("finished"), so a 12-minute Kokosbananas length dragged
                    # into an 8-hour Harry Potter wiped the child's place the
                    # moment it passed 11:40 (field 2026-08-15).
                    # Authoritative first: the shelf knows this book's length,
                    # keyed on the consumableId in the url. That covers the
                    # cases carrying-forward cannot — a restart, and playback
                    # started from the SPEAKER, where no earlier snapshot of
                    # ours exists at all.
                    known = ORCH._sonos_known_duration(snap.get("uri"))
                    if known:
                        # flagged: good enough to draw a bar with, not to
                        # decide "finished" with (see _sonos_bookmark_now)
                        snap = dict(snap, dur_s=known, dur_from_shelf=True)
                    else:
                        prev = ORCH.sonos_snap or {}
                        kept = prev.get("dur_s")
                        if kept and prev.get("uri") \
                                and prev["uri"] == snap.get("uri"):
                            snap = dict(snap, dur_s=kept)
                ORCH.sonos_snap = snap
                ORCH.sonos_snap_at = time.monotonic()
            opt = ORCH.sonos_opt_tr
            if opt:
                if (snap.get("transport") == opt[0]
                        or time.monotonic() - opt[1] > 8):
                    ORCH.sonos_opt_tr = None  # confirmed or expired
                else:
                    ORCH.sonos_snap = dict(ORCH.sonos_snap,
                                           transport=opt[0])
            # The same hold for POSITION, and for the same reason. A seek
            # patches the snapshot optimistically, then this poller replaces
            # it wholesale with the speaker's report — and the speaker needs
            # a second or two to actually get there. So the bar jumped to the
            # target, snapped back to the old spot, then jumped forward
            # again: the box and the speaker visibly disagreeing mid-seek
            # (field 2026-08-15). Hold our value until the speaker lands
            # near it, or the window passes.
            optp = ORCH.sonos_opt_pos
            if optp:
                # Clear only — the hold lives in _sonos_position(), never in
                # the snapshot, so the bookmark can never persist our guess.
                rel_now = snap.get("rel_s")
                landed = (rel_now is not None
                          and abs(float(rel_now) - optp[0]) <= SONOS_SEEK_TOL_S)
                if (landed or time.monotonic() - optp[1] > SONOS_SEEK_HOLD_S
                        or optp[2] != snap.get("uri")):
                    ORCH.sonos_opt_pos = None
            # Migration-follow (stage B2): the sidecar found OUR stream
            # on a promoted coordinator and hinted. MUST run before the
            # ours-gate below — the migration window IS a not-ours
            # window. The daemon owns identity: renderer.json + /adopt.
            if ORCH._sonos_stream_moved(snap):
                _sonos_wake.set()
                continue
            stale = snap.get("stale_s")
            fresh = stale is not None and stale < 12
            if not fresh or not snap.get("ours"):
                continue  # foreign/hijacked or old data: observe, never act
            if ORCH.sonos_kind == "spotify_sharelink":
                # the playing track's own decoded uri is the inbound
                # authority — exact under every queue divergence; Track is
                # only a cross-check (architect Q1). A missing uri leaves
                # sonos_idx UNCHANGED: coercing to 0 would rewrite the
                # bookmark to track 1.
                turi = snap.get("track_spotify_uri")
                pend = ORCH.sonos_pending
                if pend and turi == pend[0]:
                    ORCH.sonos_pending = None  # our jump landed
                elif pend and time.monotonic() - pend[1] < 8:
                    turi = None  # still settling: the speaker reports the
                    #              OLD track — adopting it yanked the index
                    #              backwards under a mash (field 2026-08-09)
                elif pend:
                    ORCH.sonos_pending = None  # settle expired — trust reality
                if turi:
                    hit = next((i for i, r in enumerate(ORCH.sonos_queue)
                                if r["id"] == turi), None)
                    if hit is not None:
                        ORCH.sonos_idx = hit
                    elif snap.get("ours"):
                        ORCH.sonos_map_trusted = False
            rel, dur = snap.get("rel_s"), snap.get("dur_s")
            if (ORCH.sonos_kind in ("url", "spotify_sharelink")
                    and rel is not None
                    and snap.get("transport") in ("PLAYING",
                                                  "PAUSED_PLAYBACK")):
                ORCH._sonos_bookmark_now(force=False)
                ends_near = (ends_near + 1
                             if dur and rel > dur - 20 else 0)
            if (ORCH.sonos_kind == "url" and ORCH.sonos_idx is not None
                    and snap.get("transport") == "STOPPED" and ends_near):
                # the episode ran off its end (STOPPED right after we saw the
                # tail) — OUR queue advances; a stop far from the end is a
                # human and stays stopped
                ends_near = 0
                nxt = ORCH.sonos_idx + 1
                if nxt < len(ORCH.sonos_queue):
                    log("sonos: episode ended — next")
                    ORCH._sonos_play_entry(nxt, 0.0)
                else:
                    # queue ran out — tell the sidecar the session is over,
                    # or it keeps polling an idle speaker (/stop is the only
                    # verb that disarms it; RF power audit 2026-08-10 #1)
                    log("sonos: queue finished — stop")
                    try:
                        _renderer.post("/stop",
                                       {"if_uid": _renderer.read().get("uid")})
                    except _renderer.SidecarDown:
                        pass


        except Exception as exc:
            # ONE guard for the whole tick. Everything above was bare:
            # a ValueError from a malformed url, an OSError from a full
            # SD card inside the bookmark write — and this thread dies,
            # taking snapshots, bookmarks and queue advance with it for
            # the rest of the session. The code already defended point
            # by point ('never let a poller tick raise'); one guard is
            # cheaper and cannot be forgotten (QA 2026-08-15).
            log(f"sonos poll tick failed: {exc!r}")
_storytel_wake = threading.Event()
_STORYTEL_MIRRORED = {}   # consumableId -> pos_ms last noted, in-process
#                           dedup so a steady position pushes nothing


def _storytel_bookmarker():
    """Mirror local audiobook positions OUT to Storytel as a backup.

    One-way only: it READS the local bookmark file (the source of truth,
    written by player.py) and queues changed positions to the outbox,
    which drains when online and holds when not. It never reads a
    position back — the box has no RTC, so a last-writer-wins merge has
    no trustworthy clock, and v1 sidesteps that entirely. Off the play
    path start to finish; a dead account is invisible to the listener.

    A 60s heartbeat, plus a poke on stop/switch. The in-process dedup
    map means steady-state notes nothing and the flush is a cheap no-op
    with no network — only a CHANGED position is ever pushed."""
    while True:
        _storytel_wake.wait(60)
        _storytel_wake.clear()
        try:
            _storytel_mirror_tick()
        except Exception:
            pass


def _storytel_mirror_tick():
    """One pass of the mirror: note changed local positions, then flush."""
    if not load_settings().get("storytel_sync", 1):
        _STORYTEL_MIRRORED.clear()
        return
    for s in load_library().get("sections", []):
        for e in s.get("entries", []):
            t = e["target"]
            if not _storytel.is_storytel(t):
                continue
            st = _bm_load(state_key(t)) or {}
            eps = st.get("episodes") or {}
            # Storytel's OWN duration per book, read once per target and
            # only when something here is finished.
            durs = (_storytel.shelf_durations(t)
                    if any(r.get("done") for r in eps.values()
                           if isinstance(r, dict)) else {})
            for cid, rec in eps.items():
                pos = rec.get("pos")
                if not isinstance(pos, (int, float)) or pos <= 0:
                    continue
                if rec.get("done"):
                    # A finished book must land on Storytel's own duration,
                    # not mpv's measurement of the file we downloaded: the
                    # percentage the app shows is computed against THEIR
                    # number, so ours left books sitting at 96-98% forever
                    # (field 2026-08-18, the owner's progress screen).
                    pos = durs.get(str(cid)) or pos
                ms = int(pos * 1000)
                if _STORYTEL_MIRRORED.get(cid) != ms:
                    _storytel.outbox_note(cid, pos)
                    _STORYTEL_MIRRORED[cid] = ms
    _storytel.outbox_flush()


def _spotify_bookmarker():
    """Spotify's cloud remembers positions for ITS clients only — so we
    bookkeep like we do for mpv: while Spotify plays, snapshot the track,
    position and (when the box started it) the context every few seconds.
    play {uri, skip_to_uri} + seek replays it exactly, queue intact.
    The per-tick accept rules (box-initiated only, per-context files) live
    in spotify.bookmark_step/save_bookmark."""
    interval = 5
    # SD hygiene twin of player.py's throttle (energy audit 2026-07-20
    # #2): 5s ticks used to json+rename every tick — 720 SD bursts/hour
    # of listening. Write on track change or every 30s; when the tick
    # stops yielding a bookmark (pause/stop/phone takeover) the last
    # throttled position flushes immediately — pausing still bookmarks
    # the pause point, and only a hard power cut can lose <=30s.
    bm_flush_s = float(os.environ.get("VIBB_BOOKMARK_FLUSH", "30"))
    bm_written = [0.0, None]  # wall clock of last write, track uri
    bm_pending = None
    while True:
        woke = _bm_wake.wait(interval)
        _bm_wake.clear()
        try:
            st = go_status()
            # remember whether OUR spotify is audibly playing, for the
            # shutdown snapshot (see the _SPOT_LAST_PLAYING note) — reuses
            # this status, no extra I/O
            _SPOT_LAST_PLAYING[0] = (ORCH.source == "spotify"
                                     and bool(ORCH.target)
                                     and is_spotify(ORCH.target)
                                     and spotify_playing(st))
            track = st.get("track") or {}
            # power hygiene: with no session at all there is nothing to
            # bookkeep — drop to a 30s heartbeat instead of waking the CPU
            # 12x/min around the clock. A live (even paused) session keeps
            # the 5s cadence so resume stays accurate.
            interval = 30 if (not track or st.get("stopped")) else 5
            if woke:
                interval = 5  # a play was just issued — watch closely
            if ORCH.source == "mpv" and ORCH._mpv_alive():
                # mpv owns playback but spotify still reports playing: this
                # is the switch race — /play set target+source to the mpv
                # target instantly, while player.py takes a moment to pause
                # spotify. Writing now would stamp the wrong context over a
                # perfectly resumable bookmark. Skip the tick.
                continue
            context = None
            if ORCH.source == "spotify" and ORCH.target \
                    and is_spotify(ORCH.target):
                context = _spotify.to_uri(ORCH.target)
            bm = _spotify.bookmark_step(st, context)
            if bm is not None:
                _SPOT_PENDING_BM[0] = bm  # freshest position for shutdown flush
                if (bm.get("uri") != bm_written[1]
                        or time.monotonic() - bm_written[0] >= bm_flush_s):
                    _spotify.save_bookmark(bm)
                    bm_written = [time.monotonic(), bm.get("uri")]
                    bm_pending = None
                else:
                    bm_pending = bm
            elif bm_pending is not None:
                _spotify.save_bookmark(bm_pending)
                bm_written = [time.monotonic(), bm_pending.get("uri")]
                bm_pending = None
        except Exception:
            pass


def _audio_ready():
    return audio_ready()  # shared logic lives in vibb.output


def _bt_recover(verb):
    """Run a bt.py recovery verb ('ensure' or 'reconnect') as a
    subprocess — it takes the cross-process radio lock there, so a
    btwatchd retry can't race the recovery mid-flight."""
    try:
        # capture, don't devnull: recover()'s diagnostics (throttled/power
        # state, 'Re-probed BT serdev', 'Controller is back') must reach
        # the journal — they are the crash evidence (field 2026-07-23)
        r = subprocess.run([sys.executable, _bt.__file__, verb],
                           capture_output=True, text=True, timeout=240)
        for line in (r.stdout or "").splitlines():
            if line.strip():
                log(f"bt-recovery: {line.strip()}")
        if r.returncode == 0 and _audio.stack() == "pipewire":
            # the recovery re-attached the radio under WirePlumber: prove
            # the policy still holds before the next landing (AM-8)
            threading.Thread(target=_audio_policy_run, args=("bt-recovery",),
                             daemon=True).start()
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"bluetooth recovery ({verb}) failed: {e!r}")
        return False


_AUDIO_POLICY_LOCK = threading.Lock()


def _audio_policy_run(why):
    """One self-test run at a time; a second trigger while one runs is
    dropped (the running one answers the same question)."""
    if not _AUDIO_POLICY_LOCK.acquire(blocking=False):
        return
    try:
        log(f"audio policy self-test ({why})")
        _audio.policy_selftest()
    except Exception as e:
        log(f"audio policy self-test failed to run: {e!r}")
    finally:
        _AUDIO_POLICY_LOCK.release()


def _audio_policy_watch():
    """pipewire only: wait for the server (<=60s), run the self-test if
    it is due (never run, or PipeWire restarted = core cookie changed),
    then watch the cookie once a minute — one pw-dump — and re-run on a
    restart. A crash-looping daemon never probe-loops: the verdict file
    outlives the process and 'due' is false while it is fresh."""
    for _ in range(60):
        if _audio.server_up():
            break
        _tick(1)
    while True:
        try:
            d = _audio.pw_dump()
            if not d:
                if _audio.selftest_state().get("verdict") != "down":
                    _audio.policy_selftest()  # records 'down' (both outputs read not-ready)
            elif _audio.selftest_due(d):
                _audio_policy_run("boot" if not _audio.selftest_state() else "pipewire restarted")
        except Exception as e:
            log(f"audio policy watch: {e!r}")
        _tick(_audio.SELFTEST_POLL_S)


def _speaker_mac():
    try:
        with open(_bt.MAC_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def _bt_transport_ready():
    """Does the configured speaker have a live A2DP PCM right now?"""
    mac = _speaker_mac()
    return bool(mac) and btbus.a2dp_pcm_present(mac)


def _bt_output_ready():
    """The transport AND, under pipewire, the sink node (AM-11): a pcm
    pinned to a node that is not there yet fails at hw_params, and the
    transport precedes its node by milliseconds. Only for the switch
    paths (one pw-dump each) — the 1/s readers keep the D-Bus gate."""
    if not _bt_transport_ready():
        return False
    return (_audio.stack() != "pipewire"
            or _audio.sink_ready("bt", _speaker_mac()))


_BT_HEAL = {"lock": threading.Lock(), "last": 0.0}
# After a CLEAN recovery the full 5-min cooldown is too long (the second
# crash of a flappy evening would sit dead until it expired), but ZERO is
# too short: recovery itself restarts bluetooth + kicks a 150s page
# window, and if the controller re-crashes immediately that would loop
# back-to-back driver reloads with no breathing room (energy/RF audit
# 2026-07-24 #2). A short success cooldown heals the second crash fast
# while capping the loop rate.
BT_HEAL_SUCCESS_COOLDOWN_S = float(
    os.environ.get("VIBB_BT_HEAL_OK_COOLDOWN", "45"))
BT_HEAL_COOLDOWN_S = float(os.environ.get("VIBB_BT_HEAL_COOLDOWN", "300"))
# A clean probe re-checks once after this long — the command-timeout
# wedge signature needs ~50s to reach _hci_crashed's threshold (see the
# clean-exit comment below), so the delay must comfortably clear that.
BT_HEAL_REPROBE_S = float(os.environ.get("VIBB_BT_HEAL_REPROBE", "90"))


def _heal_crashed_controller(rearm=True):
    """btwatchd is deliberately passive on adapter loss (PLAN-bt-dbus.md
    §1), so a kick can't fix a CRASHED firmware — its Connect just keeps
    failing NotReady. Field log 2026-07-17: 'hardware error 0x00' left
    the speaker dead indefinitely, because playback fell back to the
    local output and the stall watchdog (the only other healer) never
    saw a stall. So play intent itself checks the crash signature and
    runs recovery in the background — cheap when healthy (one hciconfig
    ioctl; the kernel journal is only read when the controller is down),
    deduped by the non-blocking lock and cooldown-guarded so button
    mashing can't stack recoveries. After a successful recovery the
    bluetooth restart re-enters btwatchd's fast window on its own; the
    extra kick just shaves the last seconds off."""
    if not _BT_HEAL["lock"].acquire(blocking=False):
        return  # a recovery is already running
    try:
        wait = BT_HEAL_COOLDOWN_S - (time.monotonic() - _BT_HEAL["last"])
        if wait > 0:
            # Inside the cooldown. A real crash landing here used to be
            # dropped on the floor: field 2026-07-27 20:09, the third
            # crash of the evening hit 27s after a clean heal (still in
            # the 45s success cooldown), nobody pressed play again, and
            # the controller looped hardware errors until a manual
            # reboot. Arm exactly ONE delayed re-probe for cooldown
            # expiry; it re-runs this function, which re-checks all of
            # it (signature gone by then = quiet no-op).
            if not _BT_HEAL.get("recheck") and _bt._hci_crashed():
                _BT_HEAL["recheck"] = True
                log(f"bt heal: crash inside the cooldown — re-probing in "
                    f"{int(wait) + 5}s")
                _schedule_heal_recheck(wait + 5)
            return
        if not _bt._hci_crashed():
            # The wedge signature MATURES after the trigger: btwatchd's
            # transport-died notify lands seconds after the kernel kills
            # a stalled link, when the journal holds ONE 'command tx
            # timeout' — the third only arrives with the next connect
            # attempts, ~50s in (field 2026-07-30 12:14: probe ran
            # clean, the signature completed at +50s, and with output
            # fallen back to local no play intent ever probed again —
            # the controller sat wedged until reboot). Arm ONE silent
            # re-probe; the re-probe itself runs with rearm=False so a
            # plain speaker-away can never chain probes forever.
            if rearm and not _BT_HEAL.get("recheck"):
                _BT_HEAL["recheck"] = True
                _schedule_heal_recheck(BT_HEAL_REPROBE_S)
            return  # plain speaker-away: btwatchd's job, not ours
        _BT_HEAL["last"] = time.monotonic()
        log("bt heal: crashed controller found — recovering")
        if _bt_recover("recover"):
            # A recovery that RAN CLEAN must not park the healer for the
            # full 5 minutes: flappy evenings re-crash sooner (field
            # 2026-07-22: two crashes in one evening) and the second
            # crash would otherwise sit dead until expiry. But not ZERO
            # either — back off just enough that a re-crash can't loop
            # driver reloads. Rewind 'last' to leave only the short
            # success cooldown; a FAILING recovery keeps the full one
            # (the bluetooth-restart-loop guard).
            _BT_HEAL["last"] = (time.monotonic() - BT_HEAL_COOLDOWN_S
                                + BT_HEAL_SUCCESS_COOLDOWN_S)
        # Re-base the auto-resume window at recovery completion — a slow
        # heal must not eat BT_RESUME_S. Compare-and-set, NOT a bare
        # write (QA 2026-07-24): re-stamp only while 'lost' is STILL
        # armed. If the transport blipped back mid-heal the consumer
        # already cleared it (and may be playing) — a bare re-stamp
        # would then fire a spurious second resume/output-rebuild under
        # live audio. Also correct on the play-intent spawn path, where
        # 'lost' never was armed. Leaf lock, taken bare.
        with _BT_WAIT_LOCK:
            if _BT_WAIT["lost"]:
                _BT_WAIT["lost"] = time.monotonic()
        try:
            with open(_bt.KICK_FILE + ".tmp", "w") as f:
                f.write(str(time.time()))
            os.replace(_bt.KICK_FILE + ".tmp", _bt.KICK_FILE)
        except OSError:
            pass
    except Exception as e:  # a dead healer = the field bug comes back
        log(f"bt heal error: {e!r}")
    finally:
        _BT_HEAL["lock"].release()


def _schedule_heal_recheck(delay):
    """One delayed re-entry into _heal_crashed_controller (separated so
    the test gate can stub the timer)."""
    def _fire():
        _BT_HEAL["recheck"] = False
        _heal_crashed_controller(rearm=False)
    t = threading.Timer(delay, _fire)
    t.daemon = True
    t.start()


# the box screen's speaker popups (field log 2026-07-17: the speaker came
# up 25s before anyone pressed play again — nobody KNEW it was ready).
# since>0 = a play attempt hit a disconnected speaker ("not connected,
# waiting..." popup); lost>0 = the speaker DIED mid-play and we stopped
# the player ("disconnected — X: reconnect, A: play on the box speaker");
# when the transport then shows up, either flips to a short "connected —
# press play" window. All consumed via /status.
_BT_WAIT = {"since": 0.0, "ready_until": 0.0, "lost": 0.0}
_BT_WAIT_LOCK = threading.Lock()  # status threads + the watcher tick
# A speaker reconnect can trigger several go-librespot restarts at once
# (btwatchd's output retarget + the blip-resume's output rebuild). Each
# restart bursts the shared radio mid-A2DP-setup, which makes the NEXT
# reconnect flap — a self-feeding storm (field log 2026-07-17 23:07). One
# restart per reconnect is enough: the rest just wait for the API.
_GO_REBUILD = {"at": 0.0}
_GO_REBUILD_LOCK = threading.Lock()
GO_REBUILD_COOLDOWN_S = float(os.environ.get("VIBB_GO_REBUILD_COOLDOWN", "8"))
BT_WAIT_TICK_S = float(os.environ.get("VIBB_BT_WAIT_TICK", "3"))
BT_WAIT_S = float(os.environ.get("VIBB_BT_WAIT_S", "180"))
# /status must stay snappy for the 1/s screen poll; go-librespot can hang
# a few seconds mid-restart, so cap how long its status query may block
GO_ST_HOLD_S = float(os.environ.get("VIBB_GO_ST_HOLD", "5"))
GO_STATUS_TIMEOUT = float(os.environ.get("VIBB_GO_STATUS_TIMEOUT", "1.5"))
# mpv now-view: go-librespot only fills the (PWA-only) spotify sub-dict, so
# probe it with a short timeout instead of blocking the screen wake ~1.5s.
GO_ST_MPV_TIMEOUT = float(os.environ.get("VIBB_GO_ST_MPV_TIMEOUT", "0.3"))
BT_READY_FLASH_S = float(os.environ.get("VIBB_BT_READY_FLASH", "20"))
# auto-resume window after an auto-stop. 150s (not 30): a speaker OFF/ON
# cycle takes 20-60s to re-establish A2DP (own reconnect flaps during its
# boot, btwatchd's ladder runs 20-40s) — field log 2026-07-17 19:02 landed
# at 51s and got the press-A popup instead of just continuing. Within the
# popup's own lifetime the loss is recent and someone is present; beyond
# BT_WAIT_S the lost state has expired and NOTHING resumes by itself.
BT_RESUME_S = float(os.environ.get("VIBB_BT_RESUME_S", "150"))


def _bt_blip_resume():
    """The speaker came back within seconds of dying mid-play — resume
    by itself, like headphones against a phone: a blip is the CODE's
    problem, not the kid's (no 'press A' homework for a 5s dropout).
    Outside the blip window the popup's 'press A' stays — blasting
    audio when a speaker reappears an hour later is wrong the other
    way. Same respawn guard as the stall watchdog: if the kid meanwhile
    resumed, stopped or switched output, this is a no-op. Spotify needs
    its output REBUILT first (see _go_output_rebuild) — a plain resume
    plays silently into the dead ALSA handle — then the same spawn path
    replays from the spotify bookmark."""
    with ORCH.lock:
        source, target = ORCH.source, ORCH.target
        if (target and source == "mpv"
                and not ORCH._mpv_alive()):
            log("speaker back within the blip window — resuming")
            ORCH._spawn(target, reverse=ORCH.reverse, resume=ORCH.resume,
                        rewind=ORCH._resume_overlap("mpv"))
            return
    if source == "spotify" and target:
        _go_output_rebuild()
        with ORCH.lock:
            if ORCH.target == target and not ORCH._mpv_alive():
                log("speaker back within the blip window — resuming spotify")
                ORCH._spawn(target, reverse=ORCH.reverse, resume=ORCH.resume,
                            exact=True,
                            rewind=ORCH._resume_overlap("spotify"))


def _bt_transport_lost():
    """btwatchd's transport-died notification. If mpv is playing into
    the dead speaker, every episode now ERRORS and auto-advances (field
    log 2026-07-17: ~15 episodes skipped in 3s — the stall watchdog
    can't see it, the position is moving). Stop the player — the 3s
    bookmark preserves the exact episode/position, the same trick the
    stall watchdog uses — and arm the screen's choice popup. Spotify
    plays via go-librespot, not an mpv child: there its ALSA output just
    died under it ('output device failed' in its log, the track burning
    on silently) — pause it instead, same popup, and the spotify
    bookmarker keeps the position.

    NO local fail-over (e81a53b's keep-playing branch reverted, owner
    decision 2026-07-23): when the headset is the chosen output, the
    right behavior is pause + the fastest possible automatic reconnect
    + auto-resume, zero child interaction — the box speaker suddenly
    blasting next to a kid wearing dead headphones is worse than a
    short gap in them.

    A heal probe spawns UNCONDITIONALLY on every bt-output transport
    loss — even at idle — and self-discriminates: the heal checks the
    crash signature itself (one cheap ioctl when healthy; journal only
    read when the controller is down) and exits quietly for a plain
    headset power-off, which is btwatchd's job. Discriminating HERE
    would block btwatchd's 3s notify timeout on journalctl I/O. With
    the child stopped the stall watchdog never runs again, so this
    spawn is what turns a controller crash into an automatic recovery
    (~5s trigger) instead of a dead box until the next button press.

    Guarded: a drop for a speaker we're not playing into is a no-op, so
    a stale or duplicate notification can never kill local playback —
    and the hold-X park is covered by the same guard, because
    set_output couples the quiet marker to output=local."""
    if current_output()["output"] != "bt":
        return {"stopped": False}
    threading.Thread(target=_heal_crashed_controller, daemon=True).start()
    # _BT_WAIT writes go under _BT_WAIT_LOCK (a bare write can be
    # consumed by a stale transport-up event in _bt_wait_advance, review
    # R7) — but NEVER while holding ORCH.lock: the established order is
    # _BT_WAIT_LOCK -> ORCH.lock (_bt_wait_advance holds the wait lock
    # and calls source_is_spotify, which takes ORCH.lock). Taking them
    # in the opposite order here would be an AB/BA deadlock, so the
    # mpv branch stops the child under ORCH.lock and arms the wait
    # AFTER releasing it.
    stopped_mpv = False
    with ORCH.lock:
        if ORCH._mpv_alive():
            log("bt transport lost mid-play — stopping (bookmark "
                "survives)")
            ORCH._stop_child()
            stopped_mpv = True
    if stopped_mpv:
        with _BT_WAIT_LOCK:
            _BT_WAIT["lost"] = time.monotonic()
        return {"stopped": True}
    try:
        if spotify_playing():
            log("bt transport lost mid-play — pausing spotify")
            go("/player/pause")
            with _BT_WAIT_LOCK:
                _BT_WAIT["lost"] = time.monotonic()
                _BT_WAIT["lost_spotify"] = True
            return {"stopped": True}
    except OSError:
        pass  # go-librespot unreachable = nothing playing through it
    return {"stopped": False}


def _note_go_restart():
    """Record that go-librespot was just (re)started elsewhere (an output
    retarget), so a blip-resume rebuild on the same reconnect skips its
    own redundant restart."""
    with _GO_REBUILD_LOCK:
        _GO_REBUILD["at"] = time.monotonic()
    note_go_restart()  # cross-process marker: bt.py's route rewrite sees it


def _go_output_rebuild():
    """go-librespot's ALSA output dies WITH the bt transport
    ('snd_pcm_recover: No such device') and STAYS dead: a later
    /player/resume resumes the SESSION but never reopens the device —
    'playing' with no sound (field log 2026-07-17 19:21; two output
    toggles 'fixed' it only because the toggle restarts the service).
    Restart rebuilds the output; the session comes back empty, which
    routes any resume through the proven replay-last path. Wait for the
    login so a replay right after doesn't race the API.

    Deduped: if go-librespot was already (re)started in the last few
    seconds — the output retarget on the same reconnect, or a racing
    rebuild — its ALSA handle is already fresh, so we skip the restart
    and only wait for the API. Restarting again just re-bursts the
    shared radio and re-flaps the speaker (field storm 2026-07-17).

    Comes back on the CURRENT output device. Switching the output to bt
    while audio played on the built-in speaker leaves go-librespot's
    config on vibb_local (the switch is deferred until the transport
    exists) — a plain restart would resume on the built-in one, so the
    kid pressed reconnect and it kept playing there, needing a manual
    bt/local toggle to move to the headset (field 2026-07-17). Retarget
    rewrites the config to the current output AND restarts, which is
    exactly what that toggle did."""
    with _GO_REBUILD_LOCK:
        now = time.monotonic()
        # 'fresh' also honours bt.py's ALSA-route restart (cross-process
        # marker): a first-pair connect writes the route + restarts, so
        # rebuilding the device again on the same transport-up is the
        # redundant second bounce we're deduping (only ever skips when
        # the retarget below finds nothing to change).
        fresh = (now - _GO_REBUILD["at"] < GO_REBUILD_COOLDOWN_S
                 or go_restarted_within(GO_REBUILD_COOLDOWN_S))
        _GO_REBUILD["at"] = now
    pcm = current_output().get("pcm")
    if pcm and reopen_go_output(pcm):
        # v0.0.7: reopen the dead ALSA handle LIVE on the current output.
        # This rebuilds the device WITHOUT restarting — the session stays
        # up, so audio flows again on its own with no replay-last and no
        # radio burst, and it also rewrites the config to the current
        # output (fixing a deferred switch left on the wrong device, the
        # exact job the retarget-restart used to do). Session intact means
        # login never dropped, so skip the wait-for-login below.
        log("go-librespot output reopened live on the current device "
            "(no restart, session kept)")
        _go_volume_cap(pcm)  # a rebuild onto the HAT lands at headphone level
        return
    # pre-v0.0.7 fallback: audio_device is startup config, so rebuilding
    # the device means a restart (which drops the session -> replay-last).
    if pcm and _retarget_go_librespot(pcm):
        # config pointed at the wrong device — moved it + restarted
        log("go-librespot retargeted to the current output (restart)")
    elif fresh:
        log("go-librespot already rebuilt this reconnect — waiting for "
            "login, not restarting again")
    else:
        log("rebuilding go-librespot's audio output (restart)")
        try:
            subprocess.run(["systemctl", "restart", "go-librespot"],
                           timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"go-librespot output rebuild failed: {e!r}")
            return
    for _ in range(20):
        try:
            if go_status(timeout=2).get("username"):
                break
        except OSError:
            pass
        _tick(1)


def _speaker_back(now, elapsed, spot):
    """The speaker's transport just came up while a play intent (waiting)
    or a mid-play drop (lost) was pending. Within the blip window: just
    resume — no 'press A' homework, the kid already expressed the intent
    (field preference 2026-07-17). Beyond it: fall back to the press-A
    flash so a speaker that reappears much later can't blast audio by
    surprise (rebuilding go-librespot first for a spotify session, so
    that A lands on a live output instead of a dead handle)."""
    if elapsed <= BT_RESUME_S:
        threading.Thread(target=_bt_blip_resume, daemon=True).start()
        return 0.0  # no flash — playback comes back on its own
    if spot:
        threading.Thread(target=_go_output_rebuild, daemon=True).start()
    return now + BT_READY_FLASH_S


def _bt_wait_advance():
    """The transport-ready-driven transitions (auto-resume / press-A
    flash) and expiry. Runs from /status AND, crucially, from a
    background tick (_bt_wait_watcher): the screen sleeps and STOPS
    polling /status to save battery, so if this only ran on a poll the
    blip auto-resume never fired until a button woke the screen (field
    2026-07-17: 'have to press once for it to start'). No-op unless a
    wait is pending, so it's cheap on the timer."""
    # LOCK DISCIPLINE (review R7): _BT_WAIT_LOCK is a LEAF lock — held
    # only for dict reads/writes, never across I/O and never while
    # taking ORCH.lock. Writers that already hold ORCH.lock (play ->
    # _kick_bt_connect) may then take it safely. The transport probe
    # (dbus) and source_is_spotify (ORCH.lock) run between two short
    # critical sections, with a re-check in the second.
    with _BT_WAIT_LOCK:
        now = time.monotonic()
        # expire stale intents first (the kid walked away with the speaker
        # still off): each has its own clock, so age them independently
        if _BT_WAIT["lost"] and now - _BT_WAIT["lost"] > BT_WAIT_S:
            _BT_WAIT["lost"] = 0.0
            _BT_WAIT.pop("lost_spotify", None)
        if _BT_WAIT["since"] and now - _BT_WAIT["since"] > BT_WAIT_S:
            _BT_WAIT["since"] = 0.0  # stale intent
        pending = bool(_BT_WAIT["lost"] or _BT_WAIT["since"])
    if not pending or not _bt_transport_ready():
        return
    # The speaker coming back is ONE physical event. Both a mid-play
    # drop (lost) and a play-intent (since) can be pending together —
    # you switch the output to bt, then the link blips before A2DP
    # settles. Resuming for each separately calls _speaker_back TWICE
    # = two go-librespot restarts + two respawns racing (field storm
    # 2026-07-17 23:07, 'rebuilding output' logged twice a second
    # apart). Coalesce: resume ONCE, clearing both intents atomically.
    with _BT_WAIT_LOCK:
        if not (_BT_WAIT["lost"] or _BT_WAIT["since"]):
            return  # another thread consumed the event during the probe
        spot_flag = _BT_WAIT.pop("lost_spotify", False)
        # the most RECENT intent decides the blip window (be lenient:
        # a fresh play-intent right before the drop is still a blip)
        elapsed = now - max(_BT_WAIT["lost"], _BT_WAIT["since"])
        _BT_WAIT["lost"] = 0.0
        _BT_WAIT["since"] = 0.0
    ready_until = _speaker_back(now, elapsed,
                                spot_flag or source_is_spotify())
    with _BT_WAIT_LOCK:
        _BT_WAIT["ready_until"] = ready_until


def _bt_wait_watcher():
    """Drive the speaker-popup state off a timer, not just off /status —
    so a sleeping screen still gets the auto-resume the moment the
    speaker comes back."""
    while True:
        _tick(BT_WAIT_TICK_S)
        try:
            if _BT_WAIT["lost"] or _BT_WAIT["since"]:
                _bt_wait_advance()
        except Exception as e:
            log(f"bt wait watcher error: {e!r}")


def _bt_wait_state(playing):
    """(bt_waiting, bt_ready, bt_lost) for /status."""
    if playing:
        # Playback is on — normally every popup is done. Exception: the
        # user switched the output to the BT speaker but it isn't
        # connected, so audio is still coming from the built-in speaker.
        # Keep the 'not connected' popup up (X connects it, A drops back
        # to the built-in) until it connects or the output goes local.
        with _BT_WAIT_LOCK:
            since = _BT_WAIT["since"]
        if (since and current_output()["output"] == "bt"
                and not _bt_transport_ready()):
            return True, False, False
        with _BT_WAIT_LOCK:
            _BT_WAIT.update(lost=0.0, since=0.0, ready_until=0.0)
            _BT_WAIT.pop("lost_spotify", None)
        return False, False, False
    _bt_wait_advance()
    now = time.monotonic()
    with _BT_WAIT_LOCK:  # one consistent snapshot for the three flags
        return (bool(_BT_WAIT["since"]),  # pending, transport not ready
                now < _BT_WAIT["ready_until"],
                bool(_BT_WAIT["lost"]))


def source_is_spotify():
    with ORCH.lock:
        return ORCH.source == "spotify"


def _kick_bt_connect():
    """Play intent while the BT speaker has no transport: poke btwatchd
    to attempt a connect right away instead of waiting out its blind-retry
    backoff — up to 300s of silence after a boot where the speaker came
    on late. No-op on the built-in output or with the speaker connected."""
    if current_output()["output"] != "bt" or _bt_transport_ready():
        return
    with _BT_WAIT_LOCK:  # leaf lock — safe under ORCH.lock (see advance)
        _BT_WAIT["since"] = time.monotonic()  # screen shows "waiting..."
    try:
        with open(_bt.KICK_FILE + ".tmp", "w") as f:
            f.write(str(time.time()))
        os.replace(_bt.KICK_FILE + ".tmp", _bt.KICK_FILE)
        log("speaker not connected — kicked btwatchd to connect it now")
    except OSError:
        pass
    # a kick alone can't help a crashed controller — check off-thread
    # (zero added latency on the button) and self-heal if needed
    threading.Thread(target=_heal_crashed_controller, daemon=True).start()


def _internet_up():
    """Actual-internet probe — shared with player/content via radio.py
    (env VIBB_PROBE_ADDR). Kept as a daemon-level name: tests patch
    daemon._internet_up."""
    return _radio.internet_up(2)


def _go_unit_active():
    """Is go-librespot's systemd unit running? Distinguishes 'API busy'
    (unit active, HTTP not answering — loading tracks, slow dealer) from
    'actually down'. Busy must never be treated as dead."""
    try:
        return subprocess.run(
            ["systemctl", "is-active", "--quiet", "go-librespot"],
            timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired, AttributeError):
        return False


def _spotify_supervisor():
    """go-librespot is useless without internet, but restarts forever —
    each round costs ~1s of Zero CPU and journal noise. Park the unit
    while the box is offline; it is back within a minute of
    connectivity returning. Manual restarts while offline (e.g. an
    output switch rewrote its config) get re-parked on the next tick."""
    parked = False
    misses = 0
    park_grace_s = float(os.environ.get("VIBB_SPOT_PARK_GRACE", "180"))
    # Two cadences (review P1): while offline/parked the 20s tick keeps
    # recovery snappy (60s once made "no internet" lag a button-press
    # generation behind reality). While ONLINE and idle, each probe is a
    # radio wake out of the PS nap for nothing — noticing a loss can
    # wait 2 minutes (the play paths surface errors on their own).
    idle_tick_s = float(os.environ.get("VIBB_SPOT_PROBE_IDLE", "120"))
    while True:
        _tick(20 if (parked or _SPOT_OFFLINE[0]) else idle_tick_s)
        try:
            if _radio.paging():
                # a BT page owns the radio right now — a probe result is
                # noise either way (field 2026-07-18 20:17: page-deauthed
                # wifi + a probe mid-DHCP parked go-librespot for nothing)
                continue
            if _internet_up():
                misses = 0
                _SPOT_OFFLINE[0] = False
                if parked:
                    subprocess.run(["systemctl", "start", "go-librespot"],
                                   timeout=30)
                    log("spotify: internet is back — go-librespot started")
                    _note_go_restart()  # the ip watchdog must not re-restart it
                    parked = False
                # Once an account is on, close the open Connect door so a
                # passing phone can't overwrite our login. No-op when
                # already locked or not logged in.
                if _spotify.lock():
                    log("spotify: locked to the logged-in account "
                        "(zeroconf closed — box can't be hijacked)")
            else:
                misses += 1
                if misses < 2:
                    # ONE missed probe is not "offline": btwatchd paging
                    # an absent speaker congests the shared 2.4GHz radio
                    # enough to time out the 2s probe — field log
                    # 2026-07-17 19:08: a false 'No internet' banner and
                    # go-librespot park/start churn mid-Spotify, from
                    # nothing but a switched-off headset
                    continue
                try:
                    if go_status(timeout=2).get("track"):
                        # A LOADED session — playing OR paused — is never
                        # parked. Playing audio is proof the net works
                        # (the probe lies under self-inflicted load: the
                        # cache sweep's downloads + the stream + A2DP all
                        # share the 2.4GHz radio). And parking a PAUSED
                        # session destroys the kid's pause: the session
                        # dies, the next button hits 'session is empty ->
                        # replaying last' and the music RESTARTS (field
                        # 2026-07-18 15:13-15:15: pause fought the parker
                        # for two minutes). Idle-shutdown covers the
                        # battery angle for a box left paused offline.
                        misses = 0
                        continue
                except OSError:
                    # Unreachable is NOT proof of dead: a BUSY api (rapid
                    # next/prev, slow track loads) times out too, and
                    # parking then kills live music (field 2026-07-18
                    # 15:44:38). Only park when the unit isn't even
                    # running; an active-but-slow go-librespot is left
                    # alone to finish what it's doing.
                    if _go_unit_active():
                        misses = 0
                        continue
                if _radio.uptime() < park_grace_s:
                    # boot is a storm of self-inflicted radio events (BT
                    # boot pages, wifi association, DHCP) — a failed
                    # probe here says nothing about the internet. Field
                    # 2026-07-18 20:17:11: parked 70s after boot because
                    # a page deauthed wifi mid-DHCP. Misses keep
                    # counting, so a REAL offline box parks the moment
                    # the grace expires.
                    continue
                _SPOT_OFFLINE[0] = True
                if _go_unit_active():  # don't fork systemctl every tick
                    subprocess.run(["systemctl", "stop", "go-librespot"],
                                   timeout=30)
                if not parked:
                    log("spotify: no internet — go-librespot parked "
                        "(auto-starts when connectivity returns)")
                    parked = True
        except Exception as e:
            log(f"spotify supervisor error: {e!r}")


def _flag_was_playing():
    """At shutdown (SIGTERM from systemd), stamp LAST_FILE.

    `stopped_at` is the load-bearing field: ORCH reads it at import
    (boot_stopped_at) and session_verdict judges the resume window off
    it. `was_playing` no longer gates anything — boot stopped starting
    audio in 2026-08 (it lands PAUSED on now-playing instead), so nothing
    reads it. It is still written: it costs one bool, it is the honest
    record of how the box went down, and tests/spot_boot_flag.py pins the
    TERM-race contract that produced it."""
    try:
        playing = False
        if ORCH.child is not None and ORCH.child.poll() is None:
            p = mpv_get("pause")
            if p is not None:
                playing = p is False
            else:
                # systemd TERMs the whole cgroup at once — mpv may already
                # be gone. player.py published the pause state to a file
                # for exactly this moment.
                try:
                    with open(NOW_FILE) as f:
                        # missing key (file from an older player) = playing
                        playing = not json.load(f).get("paused", False)
                except (OSError, ValueError):
                    playing = True  # child alive, no info: assume playing
        if not playing and ORCH.source == "sonos":
            # remote renderer: the poller's snapshot, never a live SOAP
            # round at TERM (same race the spotify mirror exists for)
            snap = ORCH._sonos_fresh()
            playing = bool(snap and snap.get("transport") == "PLAYING")
        if not playing:
            # box-initiated spotify: trust the state the bookmarker last saw
            # while go-librespot was alive (a fresh query here races its
            # cgroup TERM at poweroff); live probe stays as the fallback.
            playing = _SPOT_LAST_PLAYING[0] or spotify_playing()
        with open(LAST_FILE) as f:
            last = json.load(f)
        last["was_playing"] = bool(playing)
        # When it stopped, for the resume window. Only with a clock we
        # trust: a stamp taken on the pre-RTC clock would read up to an
        # hour stale next boot (field 2026-08-12: RTC moved the clock 46
        # min AFTER the daemon was up). No stamp = the window can't
        # judge = resume, which is the pre-window behaviour.
        last["stopped_at"] = time.time() if clock_trusted() else 0
        with open(LAST_FILE + ".tmp", "w") as f:
            json.dump(last, f)
        os.replace(LAST_FILE + ".tmp", LAST_FILE)
    except Exception:
        pass


def _flush_spotify_bookmark():
    """Reboot/poweroff while OUR spotify plays: the bookmarker throttles
    disk writes, so the freshest position lives only in memory and would
    die with the thread at TERM — boot-resume then continues from a stale
    spot (field: seek to the start, reboot mid-song, resume lands back at
    the old position). Flush the last in-memory bookmark here. Playing-
    gated (stop/pause already flushed or cleared, and must not be
    resurrected), and it's an in-memory value — no live go_status(), which
    would race go-librespot's concurrent TERM (the daemon is deliberately
    NOT ordered after it)."""
    try:
        if _SPOT_LAST_PLAYING[0] and _SPOT_PENDING_BM[0] is not None:
            _spotify.save_bookmark(_SPOT_PENDING_BM[0])
    except Exception:
        pass


def _on_term(*_args):
    # A restore just wrote last-play.json and the spotify bookmarks from a
    # snapshot, and the reboot it triggers arrives here as SIGTERM. Flushing
    # our IN-MEMORY position over them would undo part of the restore with
    # stale pre-restore state — so a restore in progress skips persistence
    # entirely and just leaves (QA 2026-08-17).
    if _RESTORING[0]:
        os._exit(0)
    _flag_was_playing()
    _flush_spotify_bookmark()
    _sonos_on_term()
    os._exit(0)


def _sonos_on_term():
    """Box off = music off (owner default 2026-08-09): a TERM while the
    renderer is sonos bookmarks the last MEASURED position and stops the
    speaker — otherwise the episode plays on to an empty room and boot
    resume later jumps it backwards. settings sonos_keep_playing=True
    opts out (the PWA toggle): then the speaker plays on and the box
    just leaves quietly. Install-restarts land here too — that is
    correct for the stop-default (the reconcile adopts on the way up
    only if something still plays, i.e. the keep-playing case)."""
    try:
        if not _renderer.is_sonos() or ORCH.source != "sonos":
            return
        ORCH._sonos_refresh_live()
        ORCH._sonos_bookmark_now()
        # ours-gate (stage B1): an install-restart while grouped-away
        # must not STOP the parent's whole group. The bookmark calls
        # above self-guard; only the transport verb needs the gate.
        if not (ORCH.sonos_snap or {}).get("ours"):
            return
        if not load_settings().get("sonos_keep_playing"):
            _renderer.post("/stop",
                           {"if_uid": _renderer.read().get("uid")},
                           timeout=5)
    except Exception:
        pass  # never block a shutdown on a speaker


# --- the power-on session ------------------------------------------------------
# One global slot (LAST_FILE) already records what was playing and gets
# replaced whenever anything else starts. Adding a stop-stamp turns it
# into a SESSION: power on soon after switching off and the box carries
# on exactly where it was; power on days later and it just wakes up in
# the carousel. The per-entry `resume` flag is untouched — it still
# decides what a TAP does (podcast: continue forever; music: track 1).

SESSION_ALWAYS = -1
CLOCK_WAIT_S = float(os.environ.get("VIBB_CLOCK_WAIT", "25"))
_SESSION = {"verdict": None, "live": True}


def session_age_verdict(stopped_at, window_h, now, trusted):
    """'fresh' | 'expired' | 'unknown'. Pure — the tests drive this one.

    Every uncertain case resolves to 'fresh', i.e. today's behaviour:
    boxes with no RTC that boot offline never get a trustworthy clock,
    and silently dropping THEIR resume would be a regression nobody
    could see. Only a confidently old session expires."""
    if window_h == 0:
        return "expired"                    # never resume: no clock needed
    if window_h == SESSION_ALWAYS:
        return "fresh"                      # always: no clock needed
    if not stopped_at:
        return "fresh"                      # unstamped (upgrade, hard cut)
    if not trusted:
        return "unknown"                    # ask again once the clock lands
    age = now - stopped_at
    if age < 0:
        return "fresh"                      # clock jumped back — no signal
    return "expired" if age >= window_h * 3600 else "fresh"


def session_verdict(block_s=0.0):
    """This boot's verdict, decided ONCE and remembered. Waits up to
    block_s for the clock to become trustworthy (the RTC load lands
    after us on purpose — the daemon is deliberately unordered at
    basic.target). A clock that never settles reads as fresh."""
    if _SESSION["verdict"]:
        return _SESSION["verdict"]
    window = load_settings().get("resume_window_h", SESSION_ALWAYS)
    stopped = ORCH.boot_stopped_at
    deadline = time.monotonic() + block_s
    while True:
        v = session_age_verdict(stopped, window, time.time(), clock_trusted())
        if v != "unknown":
            break
        if time.monotonic() >= deadline:
            log("session: clock never became trustworthy — continuing "
                "the last session")
            v = "fresh"
            break
        time.sleep(BOOT_TICK_S)
    _SESSION["verdict"] = v
    _SESSION["live"] = v == "fresh"
    if v == "expired":
        log(f"session older than the {window}h resume window — "
            f"starting fresh")
    return v


def session_resume():
    """This power-on still owns playback: the first play press continues
    the interrupted session, whatever the entry's per-TAP resume flag
    says. Boot itself no longer starts anything, so this is what makes
    the one tap after a reboot land at the right second instead of at
    track 1. Ends at that tap (Orchestrator.play with boot=False)."""
    return _SESSION["live"] and session_verdict() == "fresh"


class PortalHandler(BaseHTTPRequestHandler):
    """Port-80 helper: redirects everything to the PWA. On the setup
    hotspot, wildcard DNS (dnsmasq-shared.d) sends the phone's captive
    probes here — a redirect instead of the expected 204/Success makes
    the phone pop its 'sign in to network' sheet with the PWA in it.
    On the home LAN it doubles as http://vibb.local -> the PWA."""

    def log_message(self, *args):
        pass

    def _redirect(self):
        host = self.request.getsockname()[0]  # our address on that network
        self.send_response(302)
        self.send_header("Location", f"http://{host}:{PORT}/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_HEAD = _redirect


def _portal_server():
    try:
        srv = ThreadingHTTPServer((BIND, PORTAL_PORT), PortalHandler)
    except OSError as e:
        log(f"portal on :{PORTAL_PORT} not started ({e}) — captive portal off")
        return
    log(f"portal redirect on :{PORTAL_PORT}")
    srv.serve_forever()


def _a_cached_audio_file():
    """Newest downloaded episode, to warm the exact demux/decode/resample
    path the next play will fault in (newest = most likely to be tapped).
    None when the cache is empty."""
    cache = os.environ.get("VIBB_CACHE", "/var/lib/vibb/cache")
    newest, newest_mtime = None, -1.0
    for root, _dirs, files in os.walk(cache):
        for fn in files:
            if fn.endswith((".mp3", ".m4a", ".aac", ".opus", ".ogg")):
                p = os.path.join(root, fn)
                try:
                    mt = os.path.getmtime(p)
                except OSError:
                    continue
                if mt > newest_mtime:
                    newest, newest_mtime = p, mt
    return newest


def _prewarm_mpv():
    """The first mpv launch of a boot cold-loads mpv + the ffmpeg stack
    (tens of MB) from the SD card — field log 2026-07-17: 11s of silence
    before the first audio. A plain 'mpv --version' pages the binary in
    but NOT the demux/decode/resample path (dlopened on demand) nor the
    specific episode file — so the first real play still faulted them in.
    Instead decode ~0.3s of a cached episode to a NULL sink (no DAC
    touched, no sound, works with the speaker off): that warms the real
    codec + 44.1kHz resample path and the mp3's own pages. The delay keeps
    it off the boot rush — the point is a warm cache BEFORE the first play."""
    time.sleep(PREWARM_DELAY_S)
    warm = _a_cached_audio_file() or "av://lavfi:sine=f=440"
    try:
        subprocess.run(
            ["mpv", "--no-config", "--no-video", "--really-quiet",
             "--load-scripts=no", "--no-ytdl",
             "--ao=null", "--ao-null-untimed",  # decode full-speed, no DAC
             "--audio-samplerate=44100", "--audio-channels=stereo",
             "--length=0.3", warm],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        log("mpv prewarmed (decode path paged in)")
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"mpv prewarm failed: {e!r}")


WIFI_PS_TICK_S = float(os.environ.get("VIBB_WIFI_PS_TICK", "15"))
# Play intent kicks the governor NOW: waiting for the next tick left PS
# ON through an entire 27s resume (field 2026-07-18 20:04) — the flip
# must land before the CDN burst, not after it.
_PS_KICK = threading.Event()


def _streaming_now():
    """True while audio streams OVER THE NETWORK (Spotify, or mpv on a
    remote URL); False when idle/cached; None when it CANNOT be known
    right now. go-librespot's api blocks while it loads a track — which
    is precisely when the radio works hardest — so an unreachable api
    with a running unit means 'probably mid-load', NOT idle. The
    governor once read that blind spot as 'not streaming' and switched
    power save ON in the middle of a CDN download, stretching a track
    load to ~19s (field 2026-07-18 16:14:44)."""
    # A fresh BUSY marker = a network-heavy start/skip is in flight RIGHT
    # NOW, whatever the api says — during a /next the api can answer with
    # an idle-looking state mid-load, and the governor flipped PS ON in
    # the middle of the CDN fetch (field 2026-07-18 20:26:32, 23s skip)
    if _radio.busy():
        return True
    with ORCH.lock:
        alive = ORCH._mpv_alive()
    if alive and mpv_get("pause") is False:
        p = mpv_get("path")
        if isinstance(p, str) and p.startswith(("http://", "https://")):
            return True
    try:
        if spotify_playing():
            return True
    except OSError:
        if _go_unit_active():
            return None  # api busy (likely loading) — hold the PS state
    # a control that timed out moments ago means a load is very likely
    # still in flight even though the api now answers idle-ish — unknown,
    # never 'idle' (same field skip as above: the /next was 'dropped' at
    # 8s but executed at 23s)
    if time.monotonic() - ORCH._spot_cmd_timeout_at < SPOT_TIMEOUT_HOLD_S:
        return None
    return False


def _ps_want_off():
    """The governor's per-tick verdict: True = PS off, False = PS on,
    None = unknown (hold current state). Network streaming always wants
    PS off (the original rule). The wifi_ps_bt_off SETTING (PWA, default
    off — it costs ~15-20% listening runtime) extends that to any BT
    audio session, even fully cached: with PS on, every beacon wake is a
    coex re-arbitration against the never-pausing A2DP stream, the
    suspected BCM43430 crash trigger (field 2026-07-22, 3 crashes under
    steady cached A2DP + PS-on). With wifi off entirely this is moot —
    no beacons at all is even better for BT, and the iw calls no-op."""
    base = _streaming_now()
    if base is not None and not base:
        try:
            if load_settings().get("wifi_ps_bt_off") \
                    and _bt_playback_active():
                return True
        except Exception:
            pass  # settings unreadable — fall back to the base verdict
    return base


def _wifi_ps_governor():
    """Wi-Fi power save trades latency for battery — and the two sides
    win at DIFFERENT times. Idle/cached: PS on is pure battery win.
    Network streaming: the AP buffers packets until the radio's next
    nap-wakeup, and under BT coexistence those latency spikes starved
    go-librespot's control plane (put-state 'context deadline exceeded',
    /next timeouts — field 2026-07-18 15:30). Toggle PS off only while
    something streams over the net, back on when idle. Respects the
    boot-time choice: if PS was already OFF (perf mode / operator
    preference) the governor leaves it alone entirely."""
    if os.environ.get("VIBB_WIFI_PS_GOVERNOR", "1") != "1":
        return
    # Crash recovery FIRST: the marker means a previous daemon turned PS
    # off for a stream and died before turning it back on. Without it,
    # the baseline loop below reads 'off' for 5 minutes, stands down,
    # and PS stays off until the next reboot — +30-50mA around the clock
    # (energy audit 2026-07-20 #1). /run clears at boot, so an operator's
    # deliberate perf-mode PS-off (no marker) is still honored.
    if os.path.exists(_PS_OFF_MARKER):
        try:
            subprocess.run(["iw", "dev", "wlan0", "set", "power_save", "on"],
                           timeout=10, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            os.remove(_PS_OFF_MARKER)
            log("wifi ps governor: restored power save after restart "
                "(previous daemon died with PS off)")
            _ps_govern()  # marker proves PS is ours to manage — no baseline
            return
        except OSError:
            pass  # can't read/fix — fall through to the normal baseline
    # The baseline read must WAIT OUT the boot: at daemon start wlan0
    # exists but PS is still off — NetworkManager enables it ~2min later
    # and vibb-power(save) re-asserts it. Reading 'off' once at t=0 and
    # standing down forever left PS ON through every stream (field
    # 2026-07-18 15:43: no governor log line the whole session, controls
    # starved). Poll until PS is seen ON once (then manage); a box whose
    # operator keeps PS off never shows 'on' and the governor stands down.
    managed = False
    tries = int(os.environ.get("VIBB_WIFI_PS_BASELINE_TRIES", "30"))
    for i in range(tries):
        try:
            r = subprocess.run(["iw", "dev", "wlan0", "get", "power_save"],
                               capture_output=True, text=True, timeout=10)
            if "on" in (r.stdout or ""):
                managed = True
                break
        except (OSError, subprocess.TimeoutExpired):
            pass  # no iw / wlan0 not up yet — keep waiting
        if i + 1 < tries:
            _tick(10)
    if not managed:
        log("wifi ps governor: power save never seen on — not managing")
        return
    _ps_govern()


_PS_OFF_MARKER = os.path.join(RUN_DIR, "vibb-wifi-ps-off")


def _ps_mark(off):
    """Advisory crash-note: 'the governor set PS off'. Best-effort — a
    failed write only means a crash recovers PS on the next reboot
    instead of the next daemon start, i.e. exactly today's behavior."""
    try:
        if off:
            with open(_PS_OFF_MARKER, "w"):
                pass
        else:
            os.remove(_PS_OFF_MARKER)
    except OSError:
        pass


def _ps_govern():
    log("wifi ps governor: managing (ps off while streaming or on the "
        "charger, on when idle on battery)")
    ps_off = False  # current state we set (baseline = on)
    idle_since = None  # when continuous not-streaming began (PS still off)
    none_since = None  # when the 'unknown' verdict began (PS still off)
    hyst_s = float(os.environ.get("VIBB_WIFI_PS_HYST", "180"))
    # A wedged go-librespot (api up but never answering — a documented
    # failure mode) makes _streaming_now() return None forever, and the
    # loop below used to hold PS OFF indefinitely on that (+30-50mA all
    # night, energy audit 2026-07-24 #1). Bound it: 'unknown' for longer
    # than a real track load could take means the api is stuck, not
    # loading — fall through to the idle path and let PS come back on.
    none_bound_s = float(os.environ.get("VIBB_WIFI_PS_NONE_BOUND", "300"))
    while True:
        _PS_KICK.wait(WIFI_PS_TICK_S)  # play intent ends the wait early
        _PS_KICK.clear()
        try:
            want_off = _ps_want_off()
            if want_off is None:  # api mid-load: never flip PS blindly...
                if not ps_off:
                    none_since = None
                    continue
                # ...unless PS has been stuck off under 'unknown' too long
                # to be a genuine load — then treat it as idle (below).
                if none_since is None:
                    none_since = time.monotonic()
                if time.monotonic() - none_since < none_bound_s:
                    continue
                log("wifi ps governor: 'streaming?' unknown for "
                    f"{int(none_bound_s)}s (go-librespot wedged?) — "
                    "treating as idle")
                want_off = False
            else:
                none_since = None
            charger_off = False
            if not want_off and plugged_cached() \
                    and not _bt_playback_active():
                # Charger rule (owner 2026-07-29): on wall power the
                # idle doze buys no battery and costs PWA/SSH snappiness
                # — keep PS off while plugged. NEVER over BT audio
                # though: cached-playback-keeps-PS-on is a deliberate
                # coex optimization (less wifi airtime helps A2DP on the
                # shared antenna), and charging doesn't change the RF
                # physics.
                want_off, charger_off = True, True
            if want_off:
                idle_since = None
            elif ps_off:
                # Hysteresis: PS goes back ON only after a LONG idle.
                # Flipping 10s after a pause killed the Spotify AP TCP
                # (silent 'pong ack' death -> re-auth on the next play)
                # and a flip mid-activity has caused a field problem
                # every single time. ~3 min of PS-off idle costs ~0.2%
                # battery per pause (RF review 2026-07-18).
                if idle_since is None:
                    idle_since = time.monotonic()
                if time.monotonic() - idle_since < hyst_s:
                    continue
            if want_off == ps_off:
                continue
            subprocess.run(["iw", "dev", "wlan0", "set", "power_save",
                            "off" if want_off else "on"],
                           timeout=10, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            ps_off = want_off
            _ps_mark(want_off)
            log("wifi power save off (charging)" if charger_off
                else "wifi power save off (streaming)" if want_off
                else "wifi power save on (idle)")
        except Exception as e:
            log(f"wifi ps governor error: {e!r}")


def _audible_now():
    """Is anything actually making sound (or about to)? The cache
    sweeper's busy-gate: its downloads must never share the radio with
    live audio. A just-spawned mpv (IPC not up yet) counts as audible —
    that's exactly the tap->audio window the sweep must stay out of."""
    if ORCH.source == "sonos":
        # A playing sonos session counts as AUDIBLE (the speaker streams
        # over the same wifi the sweep would download on) even though
        # _streaming_now stays False (the BOX isn't streaming — that is
        # the power-save win). Opposite answers, both correct.
        snap = ORCH._sonos_fresh()
        if snap and snap.get("transport") == "PLAYING":
            return True
    with ORCH.lock:
        alive = ORCH._mpv_alive()
        started = ORCH.child_started
    if alive:
        p = mpv_get("pause")
        if p is False:
            return True
        if p is None and time.monotonic() - started < MPV_START_GRACE_S:
            return True
    try:
        return bool(spotify_playing())
    except OSError:
        # api blocked + unit running = very likely mid-track-load, the
        # worst moment for sweep downloads to grab the radio — busy.
        # A parked/dead unit is genuinely not audible.
        return _go_unit_active()


def main():
    try:
        signal.signal(signal.SIGTERM, _on_term)
    except ValueError:
        pass  # not the main thread (tests run main() in a thread)
    _library.BUSY_CHECK = _audible_now  # the sweep yields to live audio
    threading.Thread(target=_wifi_ps_governor, daemon=True).start()
    # Settle the session verdict in its own thread. It used to hang off
    # the boot-resume thread, which returned early on the common boots
    # (nothing was playing, sonos, resume off) — so the verdict stayed
    # unset, /status answered "pending" forever, and the screen sat on
    # the splash for its full patience every single time (field
    # 2026-08-13). That thread is gone entirely now; this one is the only
    # thing that stamps the verdict, and the screen's boot landing reads
    # it to choose now-playing vs the carousel. With the default window
    # it resolves instantly; only an hours window ever waits.
    threading.Thread(target=session_verdict, args=(CLOCK_WAIT_S,),
                     daemon=True).start()
    threading.Thread(target=_prewarm_mpv, daemon=True).start()
    threading.Thread(target=_bt_wait_watcher, daemon=True).start()
    threading.Thread(target=_cache_sweeper, daemon=True).start()
    threading.Thread(target=_spotify_bookmarker, daemon=True).start()
    threading.Thread(target=_storytel_bookmarker, daemon=True).start()
    threading.Thread(target=_sonos_poller, daemon=True).start()
    # off the bind path (review 2026-07-18 B1): the re-enable forks
    # rfkill/iw/nmcli probes with up-to-5s timeouts, and running it
    # synchronously here delayed "listening" — the screen sits on its
    # splash waiting for /system until the server is up
    threading.Thread(target=_wifi_boot_reenable, daemon=True).start()
    threading.Thread(target=_wifi_watchdog, daemon=True).start()
    threading.Thread(target=_battery_runtime_tracker, daemon=True).start()
    threading.Thread(target=_spotify_supervisor, daemon=True).start()
    threading.Thread(target=_ip_watchdog, daemon=True).start()
    threading.Thread(target=_portal_server, daemon=True).start()
    if _audio.stack() == "pipewire":
        threading.Thread(target=_audio_policy_watch, daemon=True).start()
    # Self-heal: install.sh normally creates this, but a deleted or
    # corrupt file must produce a NEW token rather than a box where every
    # privileged endpoint is permanently unreachable. ensure() never
    # rewrites a valid token, so linked phones survive a restart.
    try:
        if _token.ensure() and not REQUIRE_TOKEN:
            log("WARNING: VIBB_REQUIRE_TOKEN=0 — privileged endpoints "
                "are UNPROTECTED (recovery mode)")
    except OSError as e:
        log(f"WARNING: no API token ({e!r}) — privileged endpoints will "
            "refuse every request until this is fixed")
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    log(f"listening on {BIND}:{PORT} (PWA: http://vibb.local:{PORT})")
    server.serve_forever()


if __name__ == "__main__":
    main()
