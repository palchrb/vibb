#!/usr/bin/env python3
"""AM-98: what playback interrupted in a sweep round gets ONE more try once
the box has been quiet SYNC_RETRY_QUIET_S — not the next sweep 6 h away.

  1. box busy, then quiet: the interrupted sync runs again, the badge is
     noted, the aborted Spotify lists get one more _warm_entries
  2. box stays busy past SYNC_RETRY_MAX_S: nothing runs (next sweep)
  3. a retry that yields again is not retried a second time
  4. the sweep loop wires it: every SweepYield branch collects, the warm
     batch passes its aborted list, the retry runs before the stamp
"""
import os
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
os.environ["VIBB_LIBRARY"] = os.path.join(os.environ["VIBB_STATE"], "lib.json")
os.environ["VIBB_SYNC_RETRY_QUIET"] = "0.3"
os.environ["VIBB_SYNC_RETRY_MAX"] = "2"
os.environ["VIBB_SYNC_RETRY_POLL"] = "0.05"
os.environ["VIBB_SYNC_STAGGER"] = "0"
os.environ["VIBB_SYNC_BUSY_RECHECK"] = "1"
sys.path.insert(0, os.path.join(REPO, "pi"))
from vibb import library as lib  # noqa: E402

BUSY = [False]
lib.BUSY_CHECK = lambda: BUSY[0]
CALLS = []
YIELD_NEXT = [0]


def fake_sync_one(argv, mod=None):
    CALLS.append(("sync", tuple(argv), mod))
    if YIELD_NEXT[0] > 0:
        YIELD_NEXT[0] -= 1
        raise lib.SweepYield()


def fake_warm(items, aborted=None):
    CALLS.append(("warm", tuple(items)))
    return {u: True for u, *_ in items}


lib._sync_one = fake_sync_one
lib._warm_entries = fake_warm
BADGED = []
lib._mark_new_seen = lambda t, nid: BADGED.append(t)
lib.content.newest_episode_id = lambda t: "ep9"

E = {"name": "Show", "target": "https://example.com/feed.xml"}
W = [("spotify:playlist:p", "List", "https://open.spotify.com/playlist/p")]

# 1. busy for 0.5 s, then quiet -> one retry of each
BUSY[0] = True
threading.Timer(0.5, lambda: BUSY.__setitem__(0, False)).start()
t0 = time.monotonic()
lib._retry_interrupted([(["sync", E["target"], "50"], None, E, True)], list(W))
dt = time.monotonic() - t0
assert ("sync", ("sync", E["target"], "50"), None) in CALLS, CALLS
assert ("warm", tuple(W)) in CALLS, CALLS
assert BADGED == [E["target"]], BADGED
assert 0.7 <= dt < 2.0, f"waited for the quiet stretch ({dt:.2f}s)"
print("1. busy then quiet: the interrupted sync and the aborted lists get one more try OK")

# 2. never quiet -> gives up within SYNC_RETRY_MAX_S, nothing runs
CALLS.clear(); BUSY[0] = True
t0 = time.monotonic()
lib._retry_interrupted([(["sync", E["target"], "50"], None, E, True)], [])
assert not CALLS, CALLS
assert 1.8 <= time.monotonic() - t0 < 4, "bounded by SYNC_RETRY_MAX_S"
BUSY[0] = False
print("2. box stays busy: nothing retried, bounded wait OK")

# 3. the retry yields again -> logged, no second retry
CALLS.clear(); YIELD_NEXT[0] = 1
lib._retry_interrupted([(["sync", E["target"], "50"], None, E, True)], [])
assert [c for c in CALLS if c[0] == "sync"] == [("sync", ("sync", E["target"], "50"), None)], CALLS
print("3. a retry that yields again is not retried twice OK")

# 4. wiring in the sweep loop (source pins)
src = open(os.path.join(REPO, "pi", "vibb", "library.py")).read()
i_loop = src.index("def _cache_sweeper():")
body = src[i_loop:]
assert body.count("interrupted.append(") == 3, "art-heal, storytel and podcast branches all collect"
i_warm = body.index("_warm_entries(warm_batch, aborted=warm_retry)")
i_retry = body.index("_retry_interrupted(interrupted, warm_retry)")
i_stamp = body.index("_stamp_sweep()")
assert i_warm < i_retry < i_stamp, "retry after the batch, before the stamp"
print("4. the sweep loop collects every yield and retries before stamping OK")
print("\nall sweep_retry_after_yield checks passed")
