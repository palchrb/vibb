#!/usr/bin/env python3
"""The sweeper's soloist branch (4d: AM-37, AM-67, AM-70) — library._warm_entry
against a fake sidecar (the HTTP calls stubbed in place):

  1. queued -> the sweeper STAYS with the pass: BUSY and the warming marker
     are touched every poll; done -> the precache state is stamped
  2. the box turns audible mid-pass -> ONE /cache/abort; the pass ends
     'aborted' -> NOT stamped (an album kind stays due for the next sweep)
  3. the sidecar says done at once -> stamped without a poll loop
  4. under soloist a new Spotify entry defaults to cache: 1 (AM-45)
  5. two due lists are queued first and share one poll loop; the precache
     state is stamped per uri from health.warm_done (AM-87)
"""
import json
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = tempfile.mkdtemp(); STATE = tempfile.mkdtemp()
os.environ["VIBB_RUN"] = RUN
os.environ["VIBB_STATE"] = STATE
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_LIBRARY"] = os.path.join(STATE, "lib.json")
os.environ["VIBB_GO_UNIT"] = "vibb-soloistd"
os.environ["VIBB_WARM_POLL_S"] = "0.05"
sys.path.insert(0, os.path.join(REPO, "pi"))

from vibb import library as lib  # noqa: E402
from vibb import radio  # noqa: E402

CALLS = []
HEALTH = []          # a queue of health answers; the last one repeats
BUSY = [False]
lib.BUSY_CHECK = lambda: BUSY[0]
lib.WARM_START = lambda: CALLS.append("ps-kick")


def fake_go(path, timeout=5, body=None):
    CALLS.append((path, body))
    if path == "/cache/download":
        return json.dumps({"queued": True} if not DONE_AT_ONCE[0] else {"done": True, "warmed": 6}).encode()
    return b"{}"


def fake_health():
    h = HEALTH.pop(0) if len(HEALTH) > 1 else HEALTH[0]
    if BUSY[0] and h.get("warming"):
        pass
    return h


DONE_AT_ONCE = [False]
lib.spotify.go = fake_go
lib._warm_health = fake_health
lib._radio.wait_paging_clear = lambda *a, **k: None
URI = "spotify:album:a1"           # an album: due only while never stamped

# 1. queued, then done
HEALTH[:] = [{"warming": {"uri": URI}}, {"warming": {"uri": URI}},
             {"warming": None, "warm_last": {"uri": URI, "result": "done"}}]
assert lib._precache_due(URI) is True
ok = lib._warm_entry(URI, "Album")
assert ok is True and ("/cache/download", {"uri": URI}) in CALLS and "ps-kick" in CALLS, CALLS
assert radio.busy() and radio.warming(), "BUSY + warming touched while the pass ran"
assert lib._precache_due(URI) is False, "stamped on done"
print("1. queued -> stayed with the pass, markers touched, stamped on done OK")

# 2. busy mid-pass -> one abort, not stamped
CALLS.clear()
URI2 = "spotify:album:a2"
HEALTH[:] = [{"warming": {"uri": URI2}}, {"warming": {"uri": URI2}}, {"warming": {"uri": URI2}},
             {"warming": None, "warm_last": {"uri": URI2, "result": "aborted: abort"}}]
BUSY[0] = True
ok = lib._warm_entry(URI2, "Album 2")
BUSY[0] = False
assert ok is False
assert [c for c in CALLS if c[0] == "/cache/abort"] == [("/cache/abort", None)], CALLS
assert lib._precache_due(URI2) is True, "an aborted pass stays due (AM-70)"
print("2. audible mid-pass -> one abort, the entry stays due OK")

# 3. done at once
CALLS.clear(); DONE_AT_ONCE[0] = True
URI3 = "spotify:album:a3"
HEALTH[:] = [{"warming": None}]
assert lib._warm_entry(URI3, "Album 3") is True and lib._precache_due(URI3) is False
assert all(c[0] != "/cache/abort" for c in CALLS)
print("3. done at once -> stamped, no loop OK")

# 5. AM-87: a sweep's due lists go into ONE pass; results per uri from warm_done
CALLS.clear(); DONE_AT_ONCE[0] = False
U5, U6 = "spotify:album:a5", "spotify:album:a6"
HEALTH[:] = [{"warming": {"uri": U5}}, {"warming": {"uri": U6}},
             {"warming": None, "warm_done": {U5: "done", U6: "partial"},
              "warm_last": {"uri": U6, "result": "partial"}}]
res = lib._warm_entries([(U5, "A5"), (U6, "A6")])
assert res == {U5: True, U6: False}, res
assert [c for c in CALLS if c[0] == "/cache/download"] == [("/cache/download", {"uri": U5}),
                                                          ("/cache/download", {"uri": U6})], CALLS
assert CALLS.count("ps-kick") == 1, CALLS
assert lib._precache_due(U5) is False and lib._precache_due(U6) is True
print("5. two due lists: both queued first, one poll loop, stamped per uri from warm_done OK")

# 4. AM-45: the default under soloist
assert lib._default_cache("https://open.spotify.com/playlist/x") == 1
assert lib._default_cache("https://radio.nrk.no/podkast/x") == 0
print("4. Spotify entries default to cache: 1 under soloist OK")

print("\nall sweep_warm_gate checks passed")
