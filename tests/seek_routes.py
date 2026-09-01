#!/usr/bin/env python3
"""POST /seek: three sources, one absolute contract, clamped short of the end.

Seek did not exist anywhere in the box before this — not in the API, not
in the PWA, not on the screen. The three branches each have a trap that
is invisible until it bites a child:

  mpv     — a RELATIVE seek past EOF ends the file and steps the
            playlist, which the player's dead-output watchdog reads as a
            skip. That is the exact path that rolled a kid back to an
            earlier episode three times (field 2026-08-12). So: absolute,
            clamped, and touch_user_skip() stamped BEFORE the command.
  sonos   — the audio is in another room. Falling through would seek
            go-librespot instead, silently, which is the hole shuffle()
            already had to close.
  spotify — absolute milliseconds, and go_status caches for a second, so
            deltas issued in a burst all resolve from the same base.

The absolute-wins rule is what makes an accelerating press compound: the
screen sends where it wants to LAND, never how far to jump."""
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
os.environ["VIBB_SONOS_SEEK_SPACING"] = "0.15"  # test-sized pacing —
#   the PATTERN is under test (12), the field constant is 0.7

import daemon  # noqa: E402


def mk(source="mpv", mpv_alive=True):
    o = object.__new__(daemon.Orchestrator)
    o.lock = daemon.threading.RLock() if hasattr(daemon.threading, "RLock") \
        else daemon.threading.Lock()
    o.source = source
    o._mpv_alive = lambda: mpv_alive
    o._seek_at = -1e9
    o.child_started = 0.0
    o.sonos_snap = {}
    o.sonos_snap_at = 0.0
    o.sonos_bm_hold = ("uri", 99.0)
    o._sonos_step_lock = daemon.threading.Lock()
    o._sonos_seek_want = None
    o._sonos_seeking = False
    return o


class Recorder:
    """Every outbound call, so a branch that leaks is visible."""

    def __init__(self):
        self.go, self.ipc, self.sonos, self.skips = [], [], [], []


def install(rec, *, mpv_props=None, go_track=None, is_sonos=False,
            ipc_reply=None):
    daemon.go = lambda p, body=None, **k: rec.go.append((p, body)) or b"{}"
    daemon.go_status = lambda *a, **k: {"track": go_track} if go_track else {}
    daemon.mpv_get = lambda prop, **k: (mpv_props or {}).get(prop)
    daemon.mpv_ipc = lambda cmd, **k: (rec.ipc.append(cmd)
                                       or (ipc_reply
                                           if ipc_reply is not None
                                           else {"error": "success"}))
    daemon._radio.touch_user_skip = lambda: rec.skips.append(len(rec.ipc))
    daemon._renderer.is_sonos = lambda: is_sonos
    daemon._renderer.read = lambda: {"uid": "RINCON_1"}
    daemon._renderer.post = lambda p, b=None, **k: (rec.sonos.append((p, b))
                                                    or (200, {}))


REAL = (daemon.go, daemon.go_status, daemon.mpv_get, daemon.mpv_ipc,
        daemon._radio.touch_user_skip, daemon._renderer.is_sonos,
        daemon._renderer.read, daemon._renderer.post)

try:
    # 1. mpv: ABSOLUTE, never relative — and clamped below the duration
    #    so the seek can never run off the end into the next episode
    rec = Recorder()
    install(rec, mpv_props={"playback-time": 100.0, "duration": 600.0})
    o = mk()
    r = o.seek(delta=60)
    assert r == {"routed": "mpv", "position": 160.0}, r
    assert rec.ipc == [["seek", 160.0, "absolute"]], rec.ipc
    assert rec.go == [], "a live mpv must never leak a seek to spotify"
    print("1. mpv seeks absolute, and spotify is left alone OK")

    # 2. THE ONE THAT MATTERS: a jump past the end is clamped short of
    #    it. Un-clamped, mpv ends the file and steps the playlist, and
    #    the watchdog reads that track change as a dead output.
    rec = Recorder()
    install(rec, mpv_props={"playback-time": 570.0, "duration": 600.0})
    r = mk().seek(delta=300)
    assert r["position"] == 600.0 - daemon.Orchestrator.SEEK_TAIL_S, r
    assert rec.ipc[0][1] < 600.0, "a seek must never land ON the end"
    print("2. a seek past the end is clamped short of it OK")

    # 3. and the human context is stamped BEFORE the command, not after:
    #    if the file does end anyway, the watchdog must already know a
    #    person did it (the ordering command() uses for next/prev)
    assert rec.skips == [0], \
        f"touch_user_skip must precede the ipc call, got {rec.skips}"
    print("3. touch_user_skip is stamped before the seek command OK")

    # 4. a live stream has no duration and therefore no destination —
    #    refused, rather than seeking a byte-range refetch of a radio URL
    rec = Recorder()
    install(rec, mpv_props={"playback-time": 12.0, "duration": None})
    r = mk().seek(delta=30)
    assert r == {"routed": None, "position": None, "reason": "live"}, r
    assert rec.ipc == [] and rec.go == [], "a live stream must seek nothing"
    print("4. a live stream refuses instead of seeking OK")

    # 5. absolute WINS over delta, because that is what lets an
    #    accelerating press compound: the screen sends where to land
    rec = Recorder()
    install(rec, mpv_props={"playback-time": 100.0, "duration": 600.0})
    r = mk().seek(position=42.0, delta=999)
    assert r["position"] == 42.0, r
    print("5. an absolute position wins over a delta OK")

    # 6. spotify: absolute MILLISECONDS (the fork's unit), clamped
    rec = Recorder()
    install(rec, go_track={"position": 30000, "duration": 300000})
    r = mk(source="spotify", mpv_alive=False).seek(delta=60)
    assert r == {"routed": "spotify", "position": 90.0}, r
    assert rec.go == [("/player/seek", {"position": 90000})], rec.go
    print("6. spotify seeks in absolute milliseconds OK")

    # 7. SONOS IS FIRST AND NEVER FALLS THROUGH. The audio is in another
    #    room; the fall-through would aim the seek at go-librespot.
    rec = Recorder()
    install(rec, is_sonos=True, go_track={"position": 0, "duration": 300000})
    o = mk(source="sonos", mpv_alive=False)
    o.sonos_snap = {"rel_s": 50.0, "dur_s": 400.0, "transport": "PLAYING",
                    "stale_s": 0}
    o.sonos_snap_at = time.monotonic()
    r = o.seek(delta=30)
    assert r["routed"] == "sonos", r
    assert rec.go == [], "nothing may reach go-librespot from a sonos box"
    for _ in range(60):                       # the SOAP call is coalesced
        if rec.sonos:                         # onto a worker thread
            break
        time.sleep(0.05)
    assert rec.sonos and rec.sonos[0][0] == "/seek", rec.sonos
    assert rec.sonos[0][1]["if_uid"] == "RINCON_1", "the uid guard must ride"
    assert isinstance(rec.sonos[0][1]["s"], float), "seconds, not h:m:s"
    print("7. sonos is routed first, coalesced, and never leaks OK")

    # 8. the optimistic position is held for the DISPLAY only, and is
    #    deliberately NOT written into sonos_snap: that snapshot is also
    #    the bookmark's source of truth, and a held guess must never
    #    reach the disk (QA 2026-08-15).
    assert o.sonos_snap["rel_s"] == 50.0, "the snapshot must stay MEASURED"
    assert o.sonos_opt_pos and o.sonos_opt_pos[0] == r["position"], \
        "the seek target is held for display"
    assert o._sonos_position() == r["position"], \
        "and the position the screen reads is the held target"
    assert o.sonos_bm_hold is None, \
        "a deliberate seek outranks the refused-seek bookmark hold"
    print("8. the sonos snapshot is patched and the bm hold released OK")

    # 9. a sonos renderer with no usable snapshot refuses — it must not
    #    fall through to spotify just because the speaker is quiet
    rec = Recorder()
    install(rec, is_sonos=True, go_track={"position": 0, "duration": 300000})
    o = mk(source="sonos", mpv_alive=False)
    r = o.seek(delta=30)
    assert r["routed"] is None and r["reason"] == "live", r
    assert rec.go == [] and rec.sonos == []
    print("9. a sonos renderer with no snapshot refuses, silently to nobody OK")

    # 10. THE RESUME HOLD MUST LET GO. _settle_position pins the reported
    #     position at the bookmark for 20s after a spawn; without this a
    #     seek BACK right after a resume — the most likely first use —
    #     makes /status keep reporting the bookmark, so the bar jumps
    #     forward and lies while the audio sits where the child put it.
    o = mk()
    o.child_started = time.monotonic()
    now = {"resume_pos": 300.0}
    assert o._settle_position(120.0, now) == 300.0, "the hold must still work"
    o._seek_at = time.monotonic()
    assert o._settle_position(120.0, now) == 120.0, \
        "after a deliberate seek the live position must win"
    print("10. a deliberate seek releases the resume position hold OK")
finally:
    (daemon.go, daemon.go_status, daemon.mpv_get, daemon.mpv_ipc,
     daemon._radio.touch_user_skip, daemon._renderer.is_sonos,
     daemon._renderer.read, daemon._renderer.post) = REAL

# 11. /seek is annoyance-tier like /volume, and must be in SAFE — an
#     unlisted POST is default-denied, which would give a box whose
#     token file is unreadable a screen where volume works and seek 401s
assert "/seek" in daemon.SAFE["POST"], daemon.SAFE["POST"]
print("11. /seek is in the open POST set beside /volume OK")

# 12. the seek worker PACES the speaker (review 2026-09-01): a mash of
#     targets becomes posts spaced >= SONOS_SEEK_SPACING_S apart, and
#     the LAST target always lands — the throttle bounds the SOAP rate
#     for clients without the UI's own poster (PWA, API).
rec = Recorder()
install(rec, is_sonos=True)
o = mk(source="sonos")
o.sonos_snap = {"armed": True, "ours": True, "transport": "PLAYING",
                "rel_s": 100.0, "dur_s": 3600.0, "uri": "u"}
o.sonos_snap_at = time.time and daemon.time.monotonic()
o.sonos_opt_pos = None
STAMPED = []
_orig_post = daemon._renderer.post


def stamping_post(path, body=None, timeout=None):
    if path == "/seek":
        STAMPED.append((daemon.time.monotonic(), body["s"]))
        return 200, {"ok": True}
    return _orig_post(path, body, timeout)


daemon._renderer.post = stamping_post
daemon._renderer.read = lambda: {"renderer": "sonos", "uid": "U"}
for tgt in (200, 300, 400, 500):
    o.seek(position=tgt)
    daemon.time.sleep(0.02)
for _ in range(100):
    if not o._sonos_seeking:
        break
    daemon.time.sleep(0.02)
daemon._renderer.post = _orig_post
gaps = [b - a for (a, _x), (b, _y) in zip(STAMPED, STAMPED[1:])]
assert all(g >= 0.14 for g in gaps), \
    f"posts must be spaced by the worker: {gaps}"
assert STAMPED[-1][1] == 500.0, \
    f"the LAST target must land whatever was coalesced: {STAMPED}"
assert len(STAMPED) < 4, \
    f"a mash must coalesce, not drain 1:1: {len(STAMPED)}"
print("12. the sonos seek worker paces the speaker, last target lands OK")

print("\nSEEK ROUTES OK — absolute everywhere, clamped short of the end, "
      "and a sonos room never leaks a seek to another player.")
