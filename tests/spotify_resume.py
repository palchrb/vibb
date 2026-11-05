#!/usr/bin/env python3
"""Gate Spotify resume bookkeeping: while the box plays, every tick must
snapshot track+position into a PER-CONTEXT bookmark, and nothing else may
touch it. Regressions for the three ways 'it started from the top again'
happened in the field:
  - a phone streaming its own music through the box (Spotify Connect)
    stamped the box context over the phone's track/position,
  - two playlist cards shared ONE bookmark file, wiping each other,
  - (in player.py) a slow track load silently skipped the resume seek."""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(REPO, "pi"))

from vibb import spotify as s  # noqa: E402

PL_A = "spotify:playlist:aaaaaaaaaaaaaaaaaaaaaa"
PL_B = "spotify:playlist:bbbbbbbbbbbbbbbbbbbbbb"


def st(uri="spotify:track:t1", pos=90000, origin="go-librespot",
       paused=False, stopped=False, track=True):
    return {"play_origin": origin, "paused": paused, "stopped": stopped,
            "track": {"uri": uri, "position": pos, "duration": 200000,
                      "name": "T", "artist_names": ["A"],
                      "album_cover_url": None} if track else None}


# 1. a box-initiated playing session is bookkept (position passes through)
bm = s.bookmark_step(st(pos=123456), PL_A)
assert bm and bm["uri"] == "spotify:track:t1" and bm["position"] == 123456
assert bm["context_uri"] == PL_A
print("1. box-initiated playback is bookkept OK")

# 2. paused / stopped / empty sessions leave the bookmark alone
assert s.bookmark_step(st(paused=True), PL_A) is None
assert s.bookmark_step(st(stopped=True), PL_A) is None
assert s.bookmark_step(st(track=False), PL_A) is None
print("2. paused/stopped/empty ticks do not write OK")

# 3. THE PHONE-CLOBBER GUARD: a Connect session started from a phone
# carries the phone's play_origin — it must NOT overwrite the box bookmark
assert s.bookmark_step(st(uri="spotify:track:phone", origin="playlist"),
                       PL_A) is None
print("3. a phone-driven session cannot clobber the bookmark OK")

# 4. back-compat: an older go-librespot without play_origin still bookkeeps
assert s.bookmark_step(st(origin=None), PL_A) is not None
assert s.bookmark_step(st(origin=""), PL_A) is not None
print("4. missing play_origin (old binary) still bookkeeps OK")

# 5. no known context -> nothing to resume against later -> no write
assert s.bookmark_step(st(), None) is None
print("5. context-less playback is not bookmarked OK")

# 6. PER-CONTEXT: playlists A and B keep independent positions
s.save_bookmark(s.bookmark_step(st(uri="spotify:track:a3", pos=300000), PL_A))
s.save_bookmark(s.bookmark_step(st(uri="spotify:track:b7", pos=70000), PL_B))
a, b = s.read_bookmark(PL_A), s.read_bookmark(PL_B)
assert a["uri"] == "spotify:track:a3" and a["position"] == 300000, a
assert b["uri"] == "spotify:track:b7" and b["position"] == 70000, b
print("6. two playlists keep independent bookmarks OK")

# 7. legacy migration: the old single-file bookmark is still readable
# for a context with no per-context file yet
legacy_ctx = "spotify:album:cccccccccccccccccccccc"
with open(s.LEGACY_BM_FILE, "w") as f:
    json.dump({"context_uri": legacy_ctx, "uri": "spotify:track:old",
               "position": 45000}, f)
lg = s.read_bookmark(legacy_ctx)
assert lg and lg["uri"] == "spotify:track:old", lg
# ...but a per-context file wins over the legacy one
assert s.read_bookmark(PL_A)["uri"] == "spotify:track:a3"
# ...and the legacy file NEVER answers for a different context — that
# put another playlist's title/art/position on the now-playing card
assert s.read_bookmark("spotify:album:eeeeeeeeeeeeeeeeeeeeee") is None, \
    "legacy bookmark served for the wrong context"
print("7. legacy bookmark readable for ITS context only, new files win OK")

# 8. clear_bookmark forgets ONE context (stop / --fresh), others survive
s.clear_bookmark(PL_A)
assert s.read_bookmark(PL_A) is None or \
    s.read_bookmark(PL_A).get("uri") != "spotify:track:a3"
assert s.read_bookmark(PL_B)["uri"] == "spotify:track:b7", "B was wiped too"
print("8. clearing one context leaves the other playlist's position OK")

# 9. logout forgets everything
s.clear_all_bookmarks()
assert s.read_bookmark(PL_B) is None
assert not os.path.exists(s.LEGACY_BM_FILE)
print("9. logout clears every bookmark OK")

# 10. replaying the target that is ALREADY loaded continues in place:
# paused -> one /player/resume; playing -> no-op. Never a respawn (which
# reloads the context + seeks — an audible hiccup for nothing).
os.environ["VIBB_LIBRARY"] = os.path.join(os.environ["VIBB_STATE"],
                                            "lib.json")
os.environ.setdefault("VIBB_CACHE", os.environ["VIBB_STATE"])
import daemon  # noqa: E402

calls = []
daemon.go_status = lambda **_k: {"track": {"uri": "t"}, "paused": True,
                            "stopped": False}
daemon.go = lambda path, **k: calls.append(path)
orch = daemon.ORCH
orch.target, orch.source = "https://open.spotify.com/playlist/xyz", "spotify"
orch._spawn = lambda *a, **k: (_ for _ in ()).throw(
    AssertionError("same-target replay must not respawn"))
r = orch.play("https://open.spotify.com/playlist/xyz")
assert r.get("resumed") and calls == ["/player/resume"], (r, calls)
calls.clear()
daemon.go_status = lambda **_k: {"track": {"uri": "t"}, "paused": False,
                            "stopped": False}
r = orch.play("https://open.spotify.com/playlist/xyz")
assert r.get("resumed") and calls == [], (r, calls)
print("10. same-target spotify replay = unpause/no-op, never a respawn OK")

# 11. the play-side accept rules: an EARLY position keeps the TRACK and
# only drops the seek — rejecting the whole bookmark restarted the whole
# playlist when replaying within a song's first 20s (field: an output
# switch early in a track sent the queue back to the top)
import player  # noqa: E402

bm = {"context_uri": PL_A, "uri": "spotify:track:5", "position": 9000}
out = player.accept_spot_bookmark(bm, PL_A)
assert out and out["uri"] == "spotify:track:5" and out["position"] == 0, out
assert bm["position"] == 9000, "input dict must not be mutated"
out = player.accept_spot_bookmark({**bm, "position": 90000}, PL_A)
assert out["position"] == 90000, "a deep position must pass through"
assert player.accept_spot_bookmark(bm, PL_B) is None, "wrong context kept"
assert player.accept_spot_bookmark({"context_uri": PL_A}, PL_A) is None
assert player.accept_spot_bookmark(None, PL_A) is None
print("11. early bookmark keeps the track (queue never restarts) OK")

# 12. exact resume (an output switch, not a re-tap): even a position
# below the threshold is honored to the millisecond — the music was
# audibly at 0:09 a second ago; coming back at 0:00 reads as a restart
out = player.accept_spot_bookmark(bm, PL_A, exact=True)
assert out["position"] == 9000, out
print("12. --exact honors a sub-threshold position (output switch) OK")

# 12b. go-librespot parked by the offline supervisor: a play tap must
# fail FAST with a clear error when there is no internet (not spawn a
# player that waits 30s for a session that cannot come), and must START
# the parked unit itself when the net is back (the supervisor's 60s tick
# is far too slow for a button press)
import subprocess as _sp

orch2 = daemon.ORCH
orch2.target = orch2.source = None
orch2._spawn = lambda *a, **k: spawns.append(a)
orch2._stop_child = lambda: None
spawns = []
daemon.subprocess.run = lambda *a, **k: type("R", (), {"returncode": 3})()
daemon._internet_up = lambda: False
r = orch2.play("https://open.spotify.com/playlist/offline1")
assert r.get("error") == "no-internet" and spawns == [], (r, spawns)
print("12b. parked + offline: fast error, no zombie player OK")

started = []
daemon.subprocess.run = lambda cmd, **k: (
    started.append(cmd),
    type("R", (), {"returncode": 3 if "is-active" in cmd else 0})())[1]
daemon._internet_up = lambda: True
daemon.go_status = lambda **_k: {}
r = orch2.play("https://open.spotify.com/playlist/online1")
assert r.get("error") is None and len(spawns) == 1, (r, spawns)
assert any("start" in c for c in started), "parked unit never started"
print("12c. parked + net back: unit started by the tap itself OK")
daemon.subprocess.run = _sp.run

# 13. prev follows mpv semantics: deep into the track -> restart it
# (seek 0); near the start -> the actual previous track (double-press
# forced when go-librespot's own prev only rewound)
calls2, stati = [], []
s.go = lambda path, timeout=5, body=None: calls2.append((path, body))
s.status = lambda timeout=5: stati.pop(0)


class TimeShim:
    """No-op sleep scoped to the spotify module's `time` attribute —
    never the shared time module: daemon's live background threads use
    it too and would spin (QA review Q2, a real flake)."""
    sleep = staticmethod(lambda n: None)

    def __getattr__(self, name):
        import time as _time
        return getattr(_time, name)


s.time = TimeShim()

stati[:] = [{"track": {"uri": "spotify:track:a", "position": 90000}}]
s.command("prev")
assert calls2 == [("/player/seek", {"position": 0})], calls2
calls2.clear()
stati[:] = [{"track": {"uri": "spotify:track:a", "position": 3000}},
            {"track": {"uri": "spotify:track:a", "position": 100}}]
s.command("prev")
assert calls2 == [("/player/prev", None), ("/player/prev", None)], calls2
calls2.clear()
stati[:] = [{"track": {"uri": "spotify:track:a", "position": 3000}},
            {"track": {"uri": "spotify:track:PREV", "position": 0}}]
s.command("prev")
assert calls2 == [("/player/prev", None)], calls2
print("13. prev: deep=restart track, early=previous track (mpv parity) OK")

print("SPOTIFY RESUME OK — per-context bookmarks, phone can't corrupt them.")
# the imported daemon leaves daemon threads that log at interpreter
# shutdown; under suite load one held the stdout lock and CPython aborted
# ("_enter_buffered_busy", suite 2026-09-07) AFTER every check had passed —
# leave without running finalizers
sys.stdout.flush(); sys.stderr.flush()
os._exit(0)
