#!/usr/bin/env python3
"""Gate play_spotify's landing poll + retry against go-librespot's api.

v0.2.2 BLOCKED: /player/play answered once the track had loaded, the
bookmark seek and the resume had run. An aborted request usually KEPT
executing server-side (field 2026-07-18 20:26: a 'dropped' /next landed
15s later) — so on a client timeout the player had to CHECK whether
the request landed before re-POSTing, or it reloaded the whole context
behind the first request (double CDN fetch, RF review 2026-07-18).

Since fork v0.2.3 the reply comes once the context is RESOLVED, and the
load/seek/resume land behind it: a 200 proves nothing audible yet. So
EVERY attempt ends in the same landing poll — a track with `buffering`
off (the bookmark's uri when resuming) — and a `stopped` verdict
(v0.2.4: a failed load reads stopped, never buffering forever) or an
exhausted budget counts as a failed attempt and retries. The old
questions stay answered: a timed-out request that landed is never
re-POSTed; a genuinely dead one retries fast (8s + 1s beats the old
15s + 3s by ~9s per attempt); a 409 (a newer command owns playback)
is left alone."""
import os
import sys
import tempfile
import types
import urllib.error

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_LIBRARY"] = os.path.join(os.environ["VIBB_STATE"],
                                            "lib.json")
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(REPO, "pi"))

import player  # noqa: E402

CTX = "spotify:album:A"
BM_URI = "spotify:track:T"
OLD = "spotify:track:OLD"
# a status script: each /status read pops the head while more than one
# answer is queued, the last one sticks
STATUS = []
GO, SCRIPT = [], []
BOOKMARK = {"on": True}


def _status(**_k):
    st = STATUS.pop(0) if len(STATUS) > 1 else (STATUS[0] if STATUS else {})
    if st is None:
        raise OSError("api busy")
    return dict(st)


def loaded(uri, position=30000):
    return {"username": "u", "track": {"uri": uri, "position": position},
            "buffering": False, "stopped": False}


LOADING = {"username": "u", "track": None, "buffering": True,
           "stopped": False}
STOPPED = {"username": "u", "track": None, "buffering": False,
           "stopped": True}
PLAY_BODY = {}


def go(path, timeout=15, body=None):
    GO.append(path)
    if path != "/player/play":
        return  # (no seek/resume any more — position rides the play call)
    PLAY_BODY.clear()
    PLAY_BODY.update(body or {})
    act = SCRIPT.pop(0)
    if act == "ok":                      # 200, and the load lands at once
        STATUS[:] = [loaded(BM_URI)]
        return
    if act == "ok-loading-lands":        # 200, three loading reads, lands
        STATUS[:] = [LOADING, LOADING, LOADING, loaded(BM_URI)]
        return
    if act == "ok-stopped":              # 200, then the load FAILED
        STATUS[:] = [LOADING, STOPPED, STOPPED]
        return
    if act == "ok-same-from-top":        # no bookmark: replay lands on the
        STATUS[:] = [loaded(OLD, 300)]   # same uri, from the top
        return
    if act == "409":
        raise urllib.error.HTTPError("http://x/player/play", 409,
                                     "Conflict", {}, None)
    if act == "timeout-lands":
        # the CLIENT gave up but the server finished the load anyway
        STATUS[:] = [None, loaded(BM_URI)]
    raise OSError("timed out")


player.radio = types.SimpleNamespace(touch_busy=lambda: None,
                                     wait_paging_clear=lambda cap_s=6: None)
player.spotify = types.SimpleNamespace(
    to_uri=lambda t: CTX,
    clear_bookmark=lambda uri: None,
    read_bookmark=lambda uri: ({"context_uri": CTX, "uri": BM_URI,
                                "position": 30000} if BOOKMARK["on"]
                               else None),
    status=_status, status_strict=_status, go=go)
player._apply_box_volume = lambda: None
# the budgets are the field values; shrink them for the test clock
player.PLAY_LAND_S = 0.6
player.PLAY_LAND_TIMEOUT_S = 0.4
player.PLAY_LAND_POLL_S = 0.02
SLEEPS = []
player.time.sleep = lambda s: SLEEPS.append(s) if s >= 1 else None


def run(script, pre_track, bookmark=True):
    GO.clear()
    SLEEPS.clear()
    BOOKMARK["on"] = bookmark
    SCRIPT[:] = script
    STATUS[:] = [loaded(pre_track) if pre_track else STOPPED]
    player.play_spotify("https://open.spotify.com/album/A")
    return [p for p in GO if p == "/player/play"]


# 1. timeout but the request LANDED (track loaded) -> NO re-POST; the
# bookmark position rides the play body (fork v0.0.5, no separate
# seek/resume), so resume still lands exactly where we left off
plays = run(["timeout-lands"], pre_track=None)
assert len(plays) == 1, f"landed request must not be re-POSTed: {GO}"
assert PLAY_BODY.get("position") == 30000, PLAY_BODY
assert "/player/seek" not in GO and "/player/resume" not in GO, GO
print("1. timed-out play that landed: no duplicate load, position in body OK")

# 2. timeout and the server genuinely failed (track unchanged) -> ONE
# retry, which succeeds against the warm server (position still carried)
plays = run(["timeout-dead", "ok"], pre_track=OLD)
assert len(plays) == 2, f"a dead attempt must be retried: {GO}"
assert PLAY_BODY.get("position") == 30000, PLAY_BODY
assert SLEEPS == [1], SLEEPS
print("2. genuinely failed play: one fast retry, position rides the play OK")

# 3. never lands, all attempts dead -> exits nonzero with the hint
try:
    run(["timeout-dead"] * 3, pre_track=None)
    raise AssertionError("must exit when the api never answers")
except SystemExit as e:
    assert e.code == 1, e.code
print("3. api never answers: clean exit 1 (supervisor's problem now) OK")

# 4. the timeout constants are env-tunable and default to the fast 8s
assert player.PLAY_TIMEOUT_S == 8.0
assert float(os.environ.get("VIBB_SPOT_PLAY_LAND", "8")) == 8.0
print("4. first-attempt timeout defaults to 8s OK")

# 5. v0.2.3: a 200 with the load still running behind it — the player
# waits the load out (loading reads are not 'stopped') and lands once
plays = run(["ok-loading-lands"], pre_track=OLD)
assert len(plays) == 1, f"an accepted load in flight is waited out: {GO}"
assert SLEEPS == [], SLEEPS
print("5. accepted, loading, lands: one play, no retry OK")

# 6. v0.2.4: a 200 whose load then FAILS reads stopped -> counted as a
# failed attempt -> retried at once (not exit 0 with nothing playing)
plays = run(["ok-stopped", "ok"], pre_track=None)
assert len(plays) == 2, f"a failed load after a 200 must retry: {GO}"
print("6. accepted then stopped: retried OK")

# 7. a 409 = a newer command (second tap, the phone) owns playback now:
# nothing to retry, no exit
plays = run(["409"], pre_track=OLD)
assert len(plays) == 1 and SLEEPS == [], (GO, SLEEPS)
print("7. superseded (409): left alone OK")

# 8. no bookmark, same context replayed from the top: the landing is the
# SAME uri as before the play — a fresh position tells it apart from a
# stale read, so no needless re-POST
plays = run(["ok-same-from-top"], pre_track=OLD, bookmark=False)
assert len(plays) == 1, f"a from-the-top replay on the same uri lands: {GO}"
print("8. same-uri replay from the top: landed on position OK")

print("SPOT PLAY RETRY OK — every attempt ends in the landing poll: landed "
      "requests are never doubled, dead ones retry fast, stopped means "
      "failed, 409 means someone newer owns playback.")
