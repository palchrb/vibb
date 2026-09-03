#!/usr/bin/env python3
"""Vibb player — THE entrypoint for playing any link.

Usage: player.py [--fresh] [--reverse] [--episode <id>] [--cache <n>]
                 <target> [url...]

Routing:
  - Spotify links/URIs (track/album/playlist/artist/episode/show, incl.
    spotify.link short links) -> go-librespot's HTTP API
  - everything else (NRK podcast/serie, RSS feeds, streams, local files)
    -> expanded via nrk.py and played with mpv, with resume: position is
    polled over mpv's IPC socket and the next run of the same target
    continues where it stopped. A background episode sync is kicked off
    for NRK podcasts.

So this is the pure-python way to play anything:

    sudo python3 player.py "https://open.spotify.com/track/..."
    sudo python3 player.py "https://radio.nrk.no/podkast/<slug>"
    sudo python3 player.py --fresh "<link>"     # ignore remembered position

Runs mpv over the given queue and remembers where playback stopped
(episode + position, polled every 3s over mpv's IPC socket). The next
run with the same <target> rotates the queue to the remembered episode
and seeks to the remembered position — so a BT dropout, Ctrl+C, power
cut or a re-tapped card continues instead of starting over.

State lives in /var/lib/vibb/state/<key>.json, keyed on the podcast
slug when <target> is an NRK podcast link, else a hash of the target.
State is cleared when the whole queue finishes naturally.
"""

import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, "/usr/local/lib/vibb-py"):
    if os.path.isdir(os.path.join(_p, "vibb")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
        break
from vibb import content, mpv as _mpv, radio, spotify  # noqa: E402
from vibb.output import audio_ready, local_volume  # noqa: E402
from vibb.paths import STATE_DIR, read_settings  # noqa: E402

is_spotify = spotify.is_spotify
POLL_S = 3


def log(msg):
    print(f"player: {msg}", file=sys.stderr, flush=True)


from vibb.library import state_key  # noqa: E402  (shared with vibbd)
# Bookmarks live in vibb/bookmarks.py since the Sonos renderer — vibbd
# writes positions with no player process alive. Re-exported here so every
# existing caller (and test) keeps its player.* name.
from vibb.bookmarks import (RESUME_MIN_S, clear_state,  # noqa: E402,F401
                              episode_pos, load_state, rotate_to_bookmark,
                              save_state, state_path)


def ipc(sock_path, *command):
    return _mpv.ipc(list(command), sock=sock_path)


def ipc_get(sock_path, prop):
    try:
        return _mpv.get(prop, sock=sock_path)
    except (OSError, ValueError):
        return None


def output_pcm():
    """The ALSA pcm playback goes to — set via vibbd POST /output
    (bt = the paired speaker, local = the built-in/HAT speaker)."""
    try:
        with open(os.path.join(STATE_DIR, "output.json")) as f:
            return json.load(f)["pcm"]
    except (OSError, ValueError, KeyError):
        return "vibb_bt"


_sync_child = None  # the per-play background sync (content.py) subprocess


def _stop_sync_child():
    """Terminate the per-play sync when THIS player stops. player.py's
    death (stop / target switch / reboot) must take the sync down with
    it: left alone it becomes an orphan that keeps downloading while the
    next source plays — the same shared-radio contention the sweep's
    busy-gate exists to prevent. A natural queue end doesn't call this
    (the box is idle then; finishing the sync is free and useful)."""
    global _sync_child
    p, _sync_child = _sync_child, None
    if p is not None and p.poll() is None:
        try:
            p.terminate()
        except OSError:
            pass


def mpv_command(urls, volume, sock, pcm, paused=False):
    """The mpv argv for a play. Extracted so a test can pin the
    audio-critical flags — dropping --audio-samplerate/--audio-channels
    plays low-bitrate audiobooks SILENTLY over A2DP, so they must never be
    lost to a 'startup trim'.

    paused=True for a bookmark resume: load silent, seek over IPC, then
    unpause — the audible dance played EPISODE START for a few seconds
    before the seek landed (field 2026-07-30). A per-file --start group
    would be instant but poisons playlist wraps: next/prev re-enter slot
    0 and would re-apply the stale bookmark mid-playthrough."""
    return [
        "mpv", "--no-video", "--really-quiet",
    ] + (["--pause"] if paused else []) + [
        # Startup trims (shave cold-spawn; none change what plays):
        #  --ao=alsa   : straight to ALSA, skip the AO autoprobe
        #  --no-config : no mpv.conf exists — skip the config scans
        #  --load-scripts=no / --no-ytdl : URLs are pre-expanded by
        #                content.py, so the ytdl Lua hook is never needed
        "--ao=alsa", "--no-config", "--load-scripts=no", "--no-ytdl",
        # A2DP/SBC to the speaker runs at 44100 Hz; low-bitrate audiobooks
        # arrive at 16000 Hz which the BT link can't deliver (silence).
        # Resample everything to 44100 stereo so any source rate plays.
        "--audio-samplerate=44100", "--audio-channels=stereo",
        # A 0.5s audio ring instead of mpv's ~0.2s default: over A2DP the
        # device buffer was ~100ms (field: 'Selected HW buffer ... 17640
        # bytes'), so any coex hiccup on the shared radio clicked
        # immediately. Half a second rides out short RF gaps; track
        # changes flush the ring, so skips stay instant, and the local
        # I2S path doesn't care. (The often-cited bluealsa 'delay' knob
        # is NOT this — it only adjusts the delay REPORTED to apps.)
        "--audio-buffer=0.5",
        f"--volume={volume}",
        f"--audio-device=alsa/{pcm}",
        f"--input-ipc-server={sock}",
    ] + list(urls)


def online(timeout=2):
    """Quick connectivity probe. VIBB_OFFLINE=1 forces offline mode
    (manual travel switch / tests). Plain IP:port — no DNS to hang on. A
    healthy link connects in well under 1s, so the caller can pass a short
    timeout when this probe is on the tap->audio path."""
    if os.environ.get("VIBB_OFFLINE"):
        return False
    return radio.internet_up(timeout)


SPOT_RESUME_MIN_MS = 20000


def _apply_box_volume():
    """One volume knob across sources: mpv reads volume.json at every
    start — give go-librespot the same treatment, else it keeps whatever
    its previous session used and Spotify plays louder/softer than NRK.

    Capped for the HAT exactly like a fresh mpv is (local_volume): the
    headphone level a parent set is a room-filling level on the amplifier,
    and until 2026-09-02 Spotify was the one source that reached the HAT
    UNCAPPED (NEW-1) — every such landing had a person pressing something,
    which is the only reason it never bit. Applied at use, never written
    back, so volume.json keeps meaning "what the user chose"."""
    try:
        with open(os.path.join(STATE_DIR, "volume.json")) as f:
            v = max(0, min(100, round(json.load(f)["volume"])))
    except (OSError, ValueError, KeyError, TypeError):
        return  # never set through the box yet — leave as is
    v = local_volume(v, output_pcm(),
                     read_settings().get("local_fallback_cap", 35))
    try:
        steps = spotify.status().get("volume_steps") or 65535
        spotify.go("/player/volume", body={"volume": round(v * steps / 100)})
        log(f"volume set to {v} (box level)")
    except OSError:
        pass


def accept_spot_bookmark(bm, uri, exact=False):
    """The bookmark to resume from, or None for a clean start. Rejections
    are logged with WHY — invaluable when 'it started over' reports come
    in from the field. An early position keeps the TRACK and only drops
    the seek: rejecting the whole bookmark restarted the entire playlist
    when the box replayed within the first 20s of a song.

    exact=True (an output switch resuming an interrupted session, not a
    user re-tap) skips the below-threshold zeroing entirely: the music
    was audibly at 0:08 a second ago — coming back at 0:00 reads as
    'it restarted' (field report), not as a convenience."""
    if bm is None:
        log("no spotify bookmark on disk — starting from the top")
        return None
    if bm.get("context_uri") != uri:
        log(f"bookmark is for another context "
            f"({bm.get('context_uri')!r} != {uri!r}) — clean start")
        return None
    if not bm.get("uri"):
        log("bookmark has no track uri — clean start")
        return None
    if not exact and (bm.get("position") or 0) <= SPOT_RESUME_MIN_MS:
        log(f"bookmark position {int((bm.get('position') or 0) / 1000)}s "
            f"is early — keeping the track, playing it from 0:00")
        bm = {**bm, "position": 0}
    return bm


# first /player/play timeout: see the retry comment in play_spotify
PLAY_TIMEOUT_S = float(os.environ.get("VIBB_SPOT_PLAY_TIMEOUT", "8"))


def play_spotify(target, fresh=False, exact=False, start_uri=None,
                 rewind=0.0):
    uri = spotify.to_uri(target)
    if not uri:
        log(f"could not parse spotify link: {target}")
        sys.exit(1)

    # Shared 2.4GHz radio: claim it for the CDN-heavy session start, but
    # let an in-flight BT page finish first (bounded). BUSY before the
    # PAGING wait — the order stops btwatchd slipping a fresh page into
    # the gap. See vibb/radio.py.
    radio.touch_busy()
    radio.wait_paging_clear()

    # Exact resume: vibbd bookkeeps track+position while Spotify plays
    # (its cloud only resumes for Spotify's own clients). Same context ->
    # play {uri, skip_to_uri, position} keeps the queue intact and lands
    # exactly where we left off in ONE atomic call (fork v0.0.5).
    bm = None
    if start_uri:
        # An explicit track pick from the song picker (v0.1.1): start
        # THERE, from the top — the bookmark's "where we left off" is
        # exactly what the kid is navigating away from
        pass
    elif fresh:
        spotify.clear_bookmark(uri)
    else:
        bm = accept_spot_bookmark(spotify.read_bookmark(uri), uri, exact)

    # go-librespot may have JUST been restarted (an output switch rewrites
    # asound.conf and bounces the service) — wait for the session before
    # playing, or the play request races the Spotify login and times out.
    for _ in range(30):
        if spotify.status().get("username"):
            break
        time.sleep(1)
    else:
        log("go-librespot session never came up — check: journalctl -u go-librespot")
        sys.exit(1)

    # The cap has to land BEFORE audio starts: applied only after
    # /player/play (below, kept as belt) it left the first 0.5-2s of every
    # Spotify landing on the HAT at headphone level (QA 2026-09-02, AM-13).
    _apply_box_volume()

    body = {"uri": uri}
    if start_uri:
        body["skip_to_uri"] = start_uri
    elif bm:
        body["skip_to_uri"] = bm["uri"]
        # fork v0.0.5: go-librespot loads the track PAUSED, seeks to
        # `position`, then resumes — atomically, server-side. That
        # replaces the old load-paused -> poll-until-loaded -> seek ->
        # resume dance here, which both played nothing audible from 0:00
        # AND raced go-librespot's blocking api: a status read mid-load
        # looked 'empty', and a slow (>20s) load silently skipped the
        # seek and resumed at 0:00 (field 2026-07-18). One call, no race.
        # (An early bookmark has position 0 -> omit it, play from the top.)
        # The resume overlap lands here for spotify: the mpv branch never
        # runs for these targets, so subtracting it there was a no-op
        # (QA 2026-08-13). Milliseconds, clamped at 0.
        if bm["position"]:
            pos_ms = max(0, int(bm["position"]) - int(rewind * 1000))
            if pos_ms != int(bm["position"]):
                log(f"resume overlap: backing up {rewind:g}s to "
                    f"{pos_ms // 1000}s")
            if pos_ms:
                body["position"] = pos_ms
    # Even with the session up, the FIRST request after a restart can be
    # slow server-side (dealer/audio-key fetch still warming: 'context
    # deadline exceeded' in go-librespot's log) — retry instead of dying.
    # 8s, not 15: when the server has genuinely failed the retry lands on
    # a WARM context in ~1s (field 2026-07-18 20:04: 15s wait, then a 1s
    # retry), so failing fast wins. But an aborted request usually KEEPS
    # executing inside go-librespot — so before re-POSTing (which would
    # reload the whole context behind it), check whether it landed.
    pre_uri = None
    try:
        pre_uri = (spotify.status(timeout=2).get("track") or {}).get("uri")
    except OSError:
        pass
    last_err = None
    for attempt in range(3):
        try:
            spotify.go("/player/play", timeout=PLAY_TIMEOUT_S, body=body)
            last_err = None
            break
        except OSError as e:
            last_err = e
            for _ in range(3):  # did the timed-out request land anyway?
                try:
                    tr = (spotify.status(timeout=2).get("track")
                          or {}).get("uri")
                except OSError:
                    time.sleep(2)  # api busy = still chewing on it
                    continue
                if tr and (tr == bm["uri"] if bm else tr != pre_uri):
                    last_err = None
                    log(f"play attempt {attempt + 1} timed out but "
                        "landed — continuing")
                break
            if last_err is None:
                break
            log(f"play attempt {attempt + 1} failed ({e}) — retrying in 1s")
            time.sleep(1)
    if last_err is not None:
        log(f"go-librespot API unreachable ({last_err}) — check: journalctl -u go-librespot")
        sys.exit(1)
    log(f"spotify: playing {uri}"
        + (f" (resuming {bm['uri']} at {bm['position'] // 1000}s)" if bm else ""))
    _apply_box_volume()

    time.sleep(2)
    track = spotify.status().get("track") or {}
    if track.get("name"):
        artists = ", ".join(track.get("artist_names") or [])
        log(f"now playing: {track['name']} — {artists}")


def _count_fast_skip(fast_skips, dwell_s):
    """Dead-output evidence bookkeeping for the watchdog below. A
    sub-10s track dwell counts toward "mpv is silently chewing the
    queue" — EXCEPT right after a human skip: the daemon stamps a
    marker on every user next/prev, and rapid changes inside its
    TTL are a finger, not a dying sink (field 2026-08-12: four
    mashed nexts hit fast_skips>=3 and rolled the kid back to the
    last audible episode, three times in one minute). The
    not-audio_ready() clause below is untouched, so a sink that is
    GENUINELY gone still triggers on the very next track change,
    mash or no mash."""
    if radio.user_skip_fresh():
        return 0
    return fast_skips + 1 if dwell_s < 10 else 0


def main():
    args = sys.argv[1:]
    fresh = False
    reverse = False  # flip the expanded queue (library 'order' override)
    episode = None   # explicit episode pick from the menu (vibbd /play)
    cache_n = None   # library entry cache setting; None = legacy behaviour
    no_resume = False  # library 'from start' setting: never remember position
    exact = False    # output-switch resume: honor even a sub-threshold pos
    rewind = 0.0     # seconds to back up when resuming after an outage: the
    #                  child should hear a word or two twice, never miss a
    #                  sentence. The DAEMON picks the value — it knows how
    #                  long the output was gone and whether this is a repeat
    #                  fault; a player process only ever sees one fault.
    while args and args[0] in ("--fresh", "--reverse", "--episode", "--cache",
                               "--no-resume", "--exact", "--rewind"):
        if args[0] == "--fresh":
            fresh = True
            args = args[1:]
        elif args[0] == "--no-resume":
            no_resume = True
            args = args[1:]
        elif args[0] == "--exact":
            exact = True
            args = args[1:]
        elif args[0] == "--rewind":
            try:
                rewind = max(0.0, float(args[1]))
            except (IndexError, ValueError):
                rewind = 0.0
            args = args[2:]
        elif args[0] == "--reverse":
            reverse = True
            args = args[1:]
        elif args[0] == "--cache":
            if len(args) < 2 or not re.fullmatch(r"-?\d+", args[1]):
                print("--cache needs a number", file=sys.stderr)
                sys.exit(1)
            cache_n = int(args[1])  # -1 = keep all offline
            args = args[2:]
        else:
            if len(args) < 2:
                print("--episode needs an id", file=sys.stderr)
                sys.exit(1)
            episode = args[1]
            args = args[2:]
    if not args:
        print("usage: player.py [--fresh] [--no-resume] [--exact] "
              "[--reverse] [--episode <id>] [--cache N] <target> [url...]",
              file=sys.stderr)
        sys.exit(1)
    target, urls = args[0], args[1:]

    if is_spotify(target):
        # --episode on a spotify target = a track pick from the song
        # picker: the id IS the track uri, played via skip_to_uri
        play_spotify(target, fresh=fresh, exact=exact,
                     start_uri=episode, rewind=rewind)
        return

    titles, ids, images = {}, {}, {}
    if not urls:  # expand the link ourselves — pure-python entrypoint
        try:
            # play must never wait on catalog/feed refreshes (psapi calls,
            # or 8s+ timeouts when offline) — the cached listing is always
            # good enough to START; the background sync freshens it
            content.STALE_OK = True
            entries = content.expand_entries(target)
            urls = [e["url"] for e in entries]
            titles = {e["url"]: e["title"] for e in entries if e.get("title")}
            ids = {e["url"]: e["id"] for e in entries if e.get("id")}
            images = {e["url"]: e["image"] for e in entries if e.get("image")}
        except Exception as e:
            log(f"expansion failed ({e!r}) — playing the raw link")
            urls = [target]
    if reverse and len(urls) > 1:
        urls.reverse()  # titles/ids are url-keyed dicts — unaffected
        log("queue order reversed (library setting)")

    # Offline? Don't let mpv grind through dead stream URLs — play what is
    # on disk. (All-remote queues are left alone: failing is the only option.)
    streams = [u for u in urls if u.startswith(("http://", "https://"))]
    if streams and len(streams) < len(urls) and not online(timeout=1):
        urls = [u for u in urls if not u.startswith(("http://", "https://"))]
        log(f"offline — playing {len(urls)} cached episode(s), "
            f"skipping {len(streams)} streams")
    key = state_key(target)
    if fresh or no_resume:
        clear_state(key)
        if fresh:
            log("starting fresh — cleared remembered position")

    try:
        # short timeout: this blocks the mpv launch, and go-librespot can be
        # slow to answer while it warms up right after boot — a paused
        # session is nice-to-have, not worth stalling audio for
        spotify.go("/player/pause", timeout=1)  # don't talk over Spotify
    except OSError:
        pass

    # Queue planning. An explicit --episode (picked in a menu) wins over the
    # bookmark; otherwise resume: rotate the queue to the remembered episode.
    # Matching is on the stable episode id first — the same episode can be a
    # stream URL one run and a cached local file the next, so URLs alone are
    # not reliable.
    start_pos = 0.0
    st = None if no_resume else load_state(key)
    url_by_id = {eid: u for u, eid in ids.items()}
    if episode:
        picked = url_by_id.get(episode)
        if picked is not None and picked not in urls:
            picked = None  # filtered away (offline) — fall back gracefully
        if picked is None:
            log(f"episode '{episode}' not in this queue — playing from start")
        else:
            idx = urls.index(picked)
            urls = urls[idx:] + urls[:idx]
            # Every episode remembers its own position — picking any episode
            # continues where it was last left (not just the last-played one).
            ep_pos = episode_pos(st, episode, picked)
            if ep_pos > RESUME_MIN_S:
                start_pos = ep_pos
            name = titles.get(picked) or episode
            log(f"starting at '{name}'"
                + (f", {int(start_pos)}s" if start_pos else ""))
    elif st:
        urls, start_pos = rotate_to_bookmark(urls, st, url_by_id)
        if start_pos:
            log(f"resuming '{titles.get(urls[0]) or urls[0]}' "
                f"at {int(start_pos)}s")
        elif urls and (st.get("id") or st.get("url")) \
                and (url_by_id.get(st.get("id")) == urls[0]
                     or st.get("url") == urls[0]):
            log(f"continuing at '{titles.get(urls[0]) or urls[0]}' "
                "(from its start)")

    # The resume overlap: back up a beat so the child hears a word twice
    # instead of missing a sentence. Applied HERE, where start_pos is
    # final and before the publish below — subtracting at the seek would
    # leave vibbd holding the display at the un-rewound position for the
    # whole overlap (_settle_position), so the bar would freeze and
    # now-playing.json would lie (QA 2026-08-13).
    if rewind and start_pos:
        start_pos = max(0.0, start_pos - rewind)
        log(f"resume overlap: backing up {rewind:g}s to {int(start_pos)}s")

    # Publish the FIRST item before mpv even starts: vibbd's /status
    # then serves the right episode name + artwork from the first frame,
    # instead of flashing a raw .mp3 filename and the show cover while
    # mpv loads and the poll loop gets around to its first write.
    # The WHOLE queue map (url -> id/title/image) goes out too: with it,
    # vibbd resolves mpv's live path itself, so a track change shows
    # the new name the same second the audio changes — no 3s poll gap.
    os.makedirs(STATE_DIR, exist_ok=True)
    now_file = os.path.join(STATE_DIR, "now-playing.json")
    if urls:
        try:
            with open(now_file + ".tmp", "w") as f:
                json.dump({"id": ids.get(urls[0]), "url": urls[0],
                           "title": titles.get(urls[0]),
                           "image": images.get(urls[0]),
                           "paused": False, "duration": None,
                           # where mpv is HEADED: it loads at 0 and only
                           # then seeks here, so vibbd holds the display
                           # steady at the bookmark until the seek lands
                           # instead of flashing 0:00 -> bookmark on every
                           # start and every reconnect respawn
                           "resume_pos": start_pos,
                           "target": target}, f)
            os.replace(now_file + ".tmp", now_file)
            qf = os.path.join(STATE_DIR, "now-queue.json")
            with open(qf + ".tmp", "w") as f:
                json.dump({"target": target,
                           "items": {u: {"id": ids.get(u),
                                         "title": titles.get(u),
                                         "image": images.get(u)}
                                     for u in urls}}, f)
            os.replace(qf + ".tmp", qf)
        except OSError:
            pass

    # Fixed socket path so the button daemon (vibb-buttons) can find us
    sock_dir = "/run" if os.access("/run", os.W_OK) else "/tmp"
    sock = os.environ.get("VIBB_MPV_SOCK",
                          os.path.join(sock_dir, "vibb-mpv.sock"))
    try:
        os.remove(sock)
    except OSError:
        pass
    # Start at the box volume last set through vibbd (POST /volume)
    try:
        with open(os.path.join(STATE_DIR, "volume.json")) as f:
            volume = max(0, min(100, round(json.load(f)["volume"])))
    except (OSError, ValueError, KeyError, TypeError):
        volume = 100
    if any(u.startswith(("http://", "https://")) for u in urls):
        # remote stream: claim the radio, let an in-flight BT page finish
        radio.touch_busy()
        radio.wait_paging_clear()
    # The box keeps one volume number but the built-in speaker and a pair
    # of headphones do not share a scale — cap what reaches the amplifier
    # (never written back, so the headphone level survives).
    pcm = output_pcm()
    volume = local_volume(
        volume, pcm, read_settings().get("local_fallback_cap", 35))
    proc = subprocess.Popen(mpv_command(urls, volume, sock, pcm,
                                        paused=bool(start_pos)))
    terminated = []  # set when WE are told to stop (reboot/daemon restart)

    def _stop(*_args):
        terminated.append(True)
        proc.terminate()
        # A TERM'd mpv blocked in a write to a dead BT transport never
        # exits — and the poll loop below waits for it, eating vibbd's
        # 10s patience until only this python parent got SIGKILLed and
        # the mpv survived as an orphan HOLDING the bluealsa PCM (field
        # 2026-08-03: 'Device or resource busy' for every later spawn).
        # Escalate: SIGKILL the mpv after 8s — inside the daemon's 10s,
        # so the loop still exits HERE and the bookmark still flushes.
        t = threading.Timer(8, proc.kill)
        t.daemon = True
        t.start()
        _stop_sync_child()  # the per-play sync dies WITH us — otherwise it
        # keeps downloading as an orphan while the NEXT source streams
        # (field 2026-07-18: a 336-episode feed sync survived a switch to
        # Spotify and chewed the shared radio through Vaiana)
    signal.signal(signal.SIGTERM, _stop)

    # Background episode caching. A library entry's cache setting (--cache N,
    # passed by vibbd) decides: 0 = never sync, N = keep the newest N,
    # -1 = keep every episode. Without the flag (cards with raw links, CLI)
    # the legacy behaviour stands: NRK podcasts/series sync their newest 50.
    kind = None
    m = re.match(r"https?://radio\.nrk\.no/podkast/([a-z0-9_-]+)", target, re.I)
    if m:
        kind = "podcast"
    else:
        m = re.match(r"https?://radio\.nrk\.no/serie/([a-z0-9_-]+)/?$", target, re.I)
        if m:
            kind = "series"
    # cache_n: None = legacy (newest 50), 0 = off, N = newest N, -1 = keep all
    sync_args = None
    if m and cache_n != 0:
        n = 50 if cache_n is None else cache_n
        sync_args = ["sync", m.group(1), str(n), kind]
    elif cache_n not in (None, 0) and len(urls) > 1 \
            and target.startswith(("http://", "https://")):
        sync_args = ["sync-feed", target, str(cache_n)]
    # Wait for mpv's IPC socket, then seek to the resume position
    for _ in range(100):
        if proc.poll() is not None:
            sys.exit(proc.returncode or 0)
        try:
            if ipc_get(sock, "playback-time") is not None:
                break
        except OSError:
            pass
        time.sleep(0.2)
    if start_pos:
        # mpv was launched --pause so the episode start never plays out
        # loud; the unpause is a SEPARATE try so a failed seek still
        # unsticks playback (from 0:00 — the old behavior, not silence).
        try:
            ipc(sock, "seek", start_pos, "absolute")
        except OSError:
            log("could not seek to resume position — playing from start")
        try:
            ipc(sock, "set_property", "pause", False)
        except OSError:
            log("could not unpause after the resume seek — "
                "play/pause button will")

    # Background episode caching starts ONLY now — AFTER mpv is up. Launched
    # before the IPC wait it competed with mpv's own file open for the (cold,
    # right-after-boot) SD cache, adding seconds to tap->audio. content.py is
    # stdlib-only; nice-19 keeps it out of mpv's way from here on.
    if sync_args:
        global _sync_child
        _sync_child = subprocess.Popen(
            [sys.executable, content.__file__, *sync_args],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=lambda: os.nice(19))  # never compete
        log(f"background sync started: {' '.join(sync_args)}")

    def survive_dead_audio(stable):
        """When the audio output dies mid-play (BT chip crash, speaker
        powered off), mpv burns through the queue silently — every file
        "ends" within seconds. Pause, roll back to the last episode that
        was actually audible, and wait for the output to come back."""
        log("tracks are flying past with no audio — output looks dead; "
            "pausing")
        try:
            ipc(sock, "set_property", "pause", True)
        except OSError:
            return
        if stable and stable[0] in urls:
            spath, spos = stable
            try:
                ipc(sock, "playlist-play-index", urls.index(spath))
                for _ in range(50):  # wait for the file to load
                    if ipc_get(sock, "path") == spath \
                            and ipc_get(sock, "duration"):
                        break
                    time.sleep(0.2)
                ipc(sock, "set_property", "pause", True)
                ipc(sock, "seek", int(spos), "absolute")
                log(f"rolled back to the last audible episode at "
                    f"{int(spos)}s")
            except OSError:
                pass
        from vibb import bt as _bt
        healed = False
        for i in range(120):  # give the output <=10 min to return
            if audio_ready():
                time.sleep(2)  # let the transport settle
                try:
                    ipc(sock, "set_property", "pause", False)
                except OSError:
                    pass
                log("audio output is back — resuming")
                return
            # The USB->battery brownout can crash the BT controller
            # outright, and nothing else heals it while we sit paused.
            # Kernel shows the crash signature -> recover IMMEDIATELY
            # (bt.py ensure re-attaches the firmware and reconnects the
            # speaker); otherwise wait 30s first — the speaker is probably
            # just switched off, and reconnecting is not ours to force.
            if not healed and (i >= 6 or _bt._hci_crashed()):
                healed = True
                log("audio still gone — running bluetooth recovery")
                try:
                    # capture, don't devnull: recover()'s diagnostics (the
                    # throttled/power state, 'Re-probed BT serdev',
                    # 'Controller is back') were swallowed here — every
                    # crash lost its evidence (field 2026-07-23)
                    r = subprocess.run(
                        [sys.executable, _bt.__file__, "ensure"],
                        capture_output=True, text=True, timeout=240)
                    for line in (r.stdout or "").splitlines():
                        if line.strip():
                            log(f"bt-recovery: {line.strip()}")
                except (OSError, subprocess.TimeoutExpired) as e:
                    log(f"bluetooth recovery attempt failed: {e!r}")
            time.sleep(5)
        log("audio output did not come back — staying paused "
            "(position saved; any play command resumes)")

    # Poll position and persist it until mpv exits; log track changes
    last_np = None
    last_title = None
    last_beat = 0.0
    prev_path, track_started = None, time.monotonic()
    fast_skips, stable = 0, None
    # Bookmark writes are throttled: every 3s tick used to json+rename
    # into STATE_DIR on the SD card — 1200 write bursts/hour for the
    # whole listening session, and a paused mpv kept writing too
    # (energy audit 2026-07-20 #2). Write only when the episode or the
    # pause state changes, or BM_FLUSH_S has passed; the skipped writes
    # park in bm_pending and flush once when mpv exits, so a clean
    # stop/reboot loses nothing and a yanked battery loses <=30s.
    bm_flush_s = float(os.environ.get("VIBB_BOOKMARK_FLUSH", "30"))
    bm_last = [0.0, None, None]  # wall clock of last write, path, paused
    bm_pending = None
    while proc.poll() is None:
        try:
            path = ipc_get(sock, "path")
            paused = ipc_get(sock, "pause")
            now_m = time.monotonic()
            if path and path != prev_path:
                was_first = prev_path is None
                if not was_first and not paused:
                    fast_skips = _count_fast_skip(
                        fast_skips, now_m - track_started)
                prev_path, track_started = path, now_m
                # Re-anchor the rollback point on EVERY track change.
                # Without this, `stable` still names the previous
                # episode, so a fault in the first 15s of episode N
                # rolled the kid into the MIDDLE of episode N-1 (the
                # 15s gate below never got to move it). Anchor to where
                # this track is about to BE, not to 0: the first track
                # is already seeked to start_pos, and an in-session
                # jump seeks to its saved position just below — 0.0
                # would make a fault right after a resume-at-5:00
                # actively rewind the child to the top (QA 2026-08-13).
                # Resolved BEFORE the dead-output check, not after it.
                # The check CONSUMES `stable`, so anchoring at 0.0 here
                # and correcting it further down left one path wrong:
                # a fault at the moment of an in-session jump rolled the
                # child to the TOP of an episode whose saved spot was
                # well inside it. On a podcast that costs minutes; on an
                # audiobook it costs hours, with no user action at all
                # (QA 2026-08-14).
                saved = 0.0
                if not was_first and not no_resume:
                    saved = episode_pos(load_state(key), ids.get(path), path)
                    if saved <= RESUME_MIN_S:
                        saved = 0.0   # never heard, or finished
                stable = (path, start_pos if was_first else saved)
                # dead output = mpv chews through the queue erroring
                # track after track; with the audio path gone there is
                # no reason to wait for skip #3. ANY track change without
                # a usable audio path is proof enough — the 3s poll can
                # see a 15-episode storm as ONE change, so the fast-skip
                # pattern alone missed it (field log 2026-07-17)
                if fast_skips >= 3 or (not was_first
                                       and not audio_ready()):
                    survive_dead_audio(stable)
                    fast_skips, prev_path = 0, None
                    track_started = time.monotonic()
                    continue
                # Jumped to another episode in-session (prev/next): resume it
                # where it was left. The first track is already at start_pos,
                # and a never-heard/finished episode has no saved position, so
                # a natural advance still plays from the top.
                if saved:   # resolved above, and `stable` already holds it
                    try:
                        ipc(sock, "seek", saved, "absolute")
                        log(f"resuming this episode at {int(saved)}s")
                    except OSError:
                        pass
            # Publish which episode is playing + the pause state (vibbd
            # reads the FILE at shutdown — IPC would race mpv's death);
            # written when the track or pause state changes.
            if path and (path, paused) != last_np:
                last_np = (path, paused)
                try:
                    with open(now_file + ".tmp", "w") as f:
                        json.dump({"id": ids.get(path), "url": path,
                                   "title": titles.get(path),
                                   "image": images.get(path),
                                   "paused": bool(paused),
                                   "duration": ipc_get(sock, "duration"),
                                   "target": target}, f)
                    os.replace(now_file + ".tmp", now_file)
                except OSError:
                    pass
            pos = ipc_get(sock, "playback-time")
            # A live stream (radio) has no finite duration — don't bookmark
            # it (its "position" is the live-edge timestamp, not progress).
            dur = ipc_get(sock, "duration")
            live = dur in (None, 0)
            if path and isinstance(pos, (int, float)):
                if not paused and now_m - track_started > 15:
                    stable = (path, pos)  # last spot that audibly played
                # Position is persisted for EVERY entry, including
                # 'always from the start' ones: the power-on session
                # continues where the box was switched off, whatever the
                # entry's per-tap flag says. That flag is enforced on the
                # READ side (clear_state + st=None above), so a tap still
                # starts such an entry at track 1.
                if not live:
                    if (path != bm_last[1] or bool(paused) != bm_last[2]
                            or time.monotonic() - bm_last[0] >= bm_flush_s):
                        save_state(key, path, pos, ids.get(path), dur)
                        bm_last = [time.monotonic(), path, bool(paused)]
                        bm_pending = None
                    else:
                        bm_pending = (key, path, pos, ids.get(path), dur)
                # heartbeat so a quiet-but-playing stream isn't mistaken
                # for frozen (mpv runs silent); every ~30s
                now_m = time.monotonic()
                if now_m - last_beat > 30:
                    last_beat = now_m
                    state = "paused at" if paused else "playing,"
                    log("...playing (live)" if live
                        else f"...{state} {int(pos)}s")
            # Prefer the catalog title (NRK mp3s lack ID3, so mpv's
            # media-title falls back to an unhelpful filename)
            title = (titles.get(path) if path else None) or ipc_get(sock, "media-title")
            if title and title != last_title:
                last_title = title
                log(f"now playing: {title}")
        except OSError:
            pass
        time.sleep(POLL_S)

    if bm_pending:  # flush the last throttled position — nothing is lost
        try:
            save_state(*bm_pending)
        except OSError:
            pass

    # Clear the bookmark ONLY when the queue truly finished by itself.
    # mpv exits 0 on a clean SIGTERM quit too (reboot, daemon restart,
    # /stop) — clearing there wiped the resume position, so "restart the
    # box" looked like "the audiobook is over".
    if proc.returncode == 0 and not terminated:
        clear_state(key)  # whole queue finished — next tap starts fresh
    sys.exit(proc.returncode or 0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("stopped")
        sys.exit(0)
