#!/usr/bin/env python3
"""Gate the mid-burst status card (field 2026-07-23 22:16, mash test):
during a v0.0.8 debounced skip the fork's /status answers with
pending_track_uri and NO track for a beat. That trackless-but-non-empty
answer skipped the hold-cache branch (which only fired on a fully empty
response) and flashed 'Nothing playing' on the screen mid-mash.

Contract: a trackless /status WITH an in-flight signal (pending skip,
busy marker, recent slow control) holds the last good card for the load
window — and still passes pending_track_uri through. A trackless answer
with NO signal keeps the old behavior (a deliberate stop shows
immediately)."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
os.environ["VIBB_LIBRARY"] = os.path.join(os.environ["VIBB_STATE"],
                                            "lib.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

orch = daemon.ORCH
orch._mpv_alive = lambda: False
orch.source, orch.target = "spotify", "https://open.spotify.com/playlist/x"
daemon.current_output = lambda **_k: {"output": "local"}
daemon._radio.busy = lambda: False
orch._spot_cmd_timeout_at = -1e9

LIVE = {"track": {"uri": "spotify:track:a", "name": "Blue Monday '88",
                  "artist_names": ["New Order"], "album_name": "Substance",
                  "album_cover_url": "https://i.scdn.co/x",
                  "duration": 244720, "position": 1000},
        "paused": False, "stopped": False}

# 0. a live session fills the hold cache
daemon.go_status = lambda **_k: LIVE
st = orch.status()
assert st["spotify"]["track"] == "Blue Monday '88", st["spotify"]

# 1. mid-settle: trackless answer WITH pending_track_uri -> the card
#    holds the last track AND passes the pending uri through
daemon.go_status = lambda **_k: {"pending_track_uri": "spotify:track:b"}
st = orch.status()
assert st["spotify"]["track"] == "Blue Monday '88", \
    f"mid-burst card must hold the last track, got {st['spotify']}"
assert st["spotify"]["pending_track_uri"] == "spotify:track:b", \
    "the pending skip target must survive the card hold"
assert st["title"] == "Blue Monday '88", \
    f"the screen card must not read 'Nothing playing' mid-burst: {st['title']}"
print("1. trackless + pending skip: card held, pending passed through OK")

# 1b. v0.1.0: the fork KNOWS the pending target's metadata — the card
#     shows where the kid is GOING (name + cover + fresh progress), and
#     next_track's cover is exposed for the UI's art prewarm
daemon.go_status = lambda **_k: {
    "pending_track_uri": "spotify:track:c",
    "pending_track": {"name": "True Faith", "artist_names": ["New Order"],
                      "album_name": "Substance", "duration": 353000,
                      "album_cover_url": "https://i.scdn.co/pending"},
    "next_track": {"name": "1963", "album_cover_url":
                   "https://i.scdn.co/next"}}
st = orch.status()
assert st["title"] == "True Faith", \
    f"the card must show the skip TARGET during a burst: {st['title']}"
assert st["artwork"] == "https://i.scdn.co/pending", st["artwork"]
assert st["position"] == 0 and st["playing"] is True, \
    (st["position"], st["playing"])
assert st["spotify"]["track"] == "True Faith", st["spotify"]
assert st["spotify"]["next_artwork"] == "https://i.scdn.co/next", \
    "next_track cover must be exposed for the UI art prewarm"
print("1b. pending_track metadata: card shows the target + next cover OK")

# 1c. fork v0.2.3+: pending_track_uri is gone; EVERY load — the hold, a
#     cold fetch, a PHONE skip no BUSY marker covers — reads track:null +
#     buffering:true. Same hold, no 'Nothing playing'.
daemon.go_status = lambda **_k: LIVE
orch.status()
daemon.go_status = lambda **_k: {"buffering": True, "stopped": False,
                                 "paused": False}
st = orch.status()
assert st["title"] == "Blue Monday '88" and \
    st["spotify"]["track"] == "Blue Monday '88", (st["title"], st["spotify"])
assert st["spotify"]["pending_track_uri"] is None, st["spotify"]
print("1c. trackless + buffering: card held OK")

# 1d. v0.2.4: a FAILED load reads stopped (buffering may linger a read)
#     -> that is a dead session, the card clears at once
daemon.go_status = lambda **_k: {"buffering": True, "stopped": True}
st = orch.status()
assert st["spotify"]["track"] is None, \
    f"stopped wins over buffering — no hold: {st['spotify']}"
print("1d. buffering but stopped: no hold OK")

# 1e. v0.2.4 names the target as pending_track ONLY (uri inside) — the
#     card shows where the kid is going and pending_track_uri is derived
daemon.go_status = lambda **_k: LIVE
orch.status()
daemon.go_status = lambda **_k: {
    "buffering": True, "stopped": False,
    "pending_track": {"uri": "spotify:track:d", "name": "Bizarre Love Triangle",
                      "artist_names": ["New Order"], "album_name": "Substance",
                      "duration": 401000,
                      "album_cover_url": "https://i.scdn.co/blt"}}
st = orch.status()
assert st["title"] == "Bizarre Love Triangle", st["title"]
assert st["spotify"]["pending_track_uri"] == "spotify:track:d", st["spotify"]
assert st["artwork"] == "https://i.scdn.co/blt", st["artwork"]
print("1e. v0.2.4 pending_track without pending_track_uri: card + uri OK")

# 2. fully empty answer (transient timeout) still holds briefly (old rule)
daemon.go_status = lambda **_k: {}
st = orch.status()
assert st["spotify"]["track"] == "Blue Monday '88", st["spotify"]
print("2. fully empty answer: short hold still applies OK")

# 3. trackless answer with NO in-flight signal: no hold — a deliberate
#    stop must show immediately, not linger for the load window
daemon.go_status = lambda **_k: {"stopped": True}
st = orch.status()
assert st["spotify"]["track"] is None, \
    f"a signal-less trackless answer must NOT hold the card: {st['spotify']}"
print("3. trackless without any in-flight signal: card clears OK")

print("\nall spot_status_pending_hold checks passed")
