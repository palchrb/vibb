#!/usr/bin/env python3
"""Gate the spotify control path. Two eras of contract, both enforced:

Old scars (field 2026-07-18 15:43-15:44): a /next whose status probe
timed out fell through to the replay fallback and RESTARTED the album
from 0:00, and queued slow controls fired late and out of order. Those
protections live on in the LOCKED path (playpause + the fast-path
fallback): an unreachable-but-running go-librespot never replays; only
playpause (or a STABLY empty session) replays.

New contract (go-librespot v0.0.8 fast-skip): next/prev on a live
spotify session are forwarded IMMEDIATELY (no busy-drop, no lock held
across the HTTP round) so the fork's debounce sees the true press
cadence — the old busy-gate spaced presses ~1s apart, each a fresh full
load, which defeated the debounce and re-created the 429 storm (field
2026-07-23 21:52: 10 loads in 9s from a prev mash). A FAILING fast-path
(dead session / unreachable api) falls back to the locked path, where
all the old guards still decide."""
import os
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_LIBRARY"] = os.path.join(os.environ["VIBB_STATE"],
                                            "lib.json")
os.environ["VIBB_EMPTY_RECHECK"] = "0.05"  # fast transient-empty recheck
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

orch = daemon.ORCH
daemon._kick_bt_connect = lambda: None
daemon._radio.touch_busy = lambda: None
orch._mpv_alive = lambda: False
orch.source = "spotify"
orch.target = "https://open.spotify.com/album/4rxfprnLYz3592ZGaeqcON"
SPAWNED = []
orch._spawn = lambda target, **kw: SPAWNED.append(target)
orch._ensure_spotify_backend = lambda: True
CMDS = []
daemon.spotify_command = daemon.spotify_skip = lambda a: CMDS.append(a)


def _unreachable(**_k):
    raise OSError("timed out")


def _cmd_unreachable(a):
    raise OSError("timed out")


def skip(action):
    """Fire a control and wait out the async fast-path/fallback."""
    r = orch.command(action)
    time.sleep(0.4)
    return r


# 1. api unreachable + unit RUNNING: the fast-path fails, the locked
# fallback sees busy-not-dead — no replay, no spawn. playpause (locked
# path) drops as busy.
daemon.go_status = _unreachable
daemon._go_unit_active = lambda: True
daemon.spotify_command = daemon.spotify_skip = _cmd_unreachable
for a in ("next", "prev"):
    r = skip(a)
    assert r.get("fast"), (a, r)
r = orch.command("playpause")
assert r.get("busy") is True, r
assert SPAWNED == [], SPAWNED
print("1. busy api (unit running): no replay from any press OK")

# 2. api unreachable + unit DOWN: next/prev fall back and stay dead
# buttons (a skip must never restart an album); playpause replays.
daemon._go_unit_active = lambda: False
orch._spot_cmd_timeout_at = -1e9
skip("next")
skip("prev")
assert SPAWNED == [], SPAWNED
r = orch.command("playpause")
assert r["routed"] == "resume" and SPAWNED == [orch.target], (r, SPAWNED)
print("2. dead session: playpause replays, next/prev stay dead buttons OK")

# 2b. album ran off its end: the api ANSWERS and says the session is
# cleanly empty — the fast-path errors (nothing to skip in), and the
# locked fallback WRAPS (replays) instead of going dead (field: next on
# the last Coco track did nothing)
SPAWNED.clear()
orch._spot_cmd_timeout_at = -1e9
daemon.go_status = lambda **k: {}          # reachable, cleanly empty
skip("next")
assert SPAWNED == [orch.target], SPAWNED
print("2b. stable empty session: next wraps (replays the target) OK")

# 2b-ii. mid-load: v0.0.8 accepts a skip during a load (it queues and
# defers it) — the fast-path just forwards. No replay, no recheck dance.
SPAWNED.clear()
CMDS.clear()
daemon.spotify_command = daemon.spotify_skip = lambda a: CMDS.append(a)
r = skip("next")
assert r.get("fast") and CMDS == ["next"], (r, CMDS)
assert SPAWNED == [], f"a forwarded skip must never replay: {SPAWNED}"
print("2b-ii. mid-load skip: forwarded to the fork's debounce, no replay OK")

# 2b-iv. the control call itself timing out: slow ≠ dead — the skip
# usually still lands inside go-librespot, so the fast path stamps the
# hold window and STOPS (no fallback, no re-send, no replay). Field
# 2026-07-23 22:16:46: the old fallback read the mid-settle session as
# 'empty' and replayed the whole playlist.
SPAWNED.clear()
orch._spot_cmd_timeout_at = -1e9


def _cmd_boom(a):
    raise TimeoutError("timed out")


daemon.spotify_command = daemon.spotify_skip = _cmd_boom
daemon.go_status = lambda **k: {"track": {"uri": "spotify:track:x"},
                                "paused": False, "stopped": False}
skip("next")
assert orch._spot_cmd_timeout_at > 0, "timeout must stamp the hold window"
assert SPAWNED == [], SPAWNED
orch._spot_cmd_timeout_at = -1e9
daemon.spotify_command = daemon.spotify_skip = lambda a: CMDS.append(a)
print("2b-iv. timed-out control -> hold stamped, no fallback, no replay OK")

# 2b-v. mid-settle emptiness: the api answers WITHOUT a track but WITH
# v0.0.8's pending_track_uri — the session is alive mid-skip, so the
# locked path (reached via a genuine connection failure) must treat it
# as LIVE, never as empty-replay
SPAWNED.clear()
orch._spot_cmd_timeout_at = -1e9


def _cmd_refused(a):
    raise ConnectionRefusedError("refused")


daemon.spotify_command = daemon.spotify_skip = _cmd_refused
daemon.go_status = lambda **k: {"pending_track_uri": "spotify:track:y"}
skip("next")
assert SPAWNED == [], \
    f"a pending skip means the session is alive — never replay: {SPAWNED}"
orch._spot_cmd_timeout_at = -1e9
daemon.spotify_command = daemon.spotify_skip = lambda a: CMDS.append(a)
print("2b-v. trackless-with-pending session: treated as live, no replay OK")

# 2b-vi. fork v0.2.3+: pending_track_uri is gone — EVERY load (the
# debounce hold, a cold fetch, a phone's skip) reads track:null +
# buffering:true. Same rule: alive, never empty-replay. And since v0.2.4
# a FAILED load reads `stopped` (never buffering forever), so playpause
# may trust buffering too — while `stopped` still wins over a stale
# buffering flag: that session is dead and rule 4 must replay.
SPAWNED.clear()
orch._spot_cmd_timeout_at = -1e9
daemon.spotify_command = daemon.spotify_skip = _cmd_refused
daemon.go_status = lambda **k: {"buffering": True, "stopped": False,
                                "paused": False}
skip("next")
assert SPAWNED == [], \
    f"a buffering session is loading, not empty — never replay: {SPAWNED}"
CMDS.clear()
daemon.spotify_command = daemon.spotify_skip = lambda a: CMDS.append(a)
r = orch.command("playpause")
assert r.get("routed") == "spotify" and CMDS == ["playpause"], (r, CMDS)
assert SPAWNED == [], f"playpause on a loading session forwards: {SPAWNED}"
daemon.go_status = lambda **k: {"buffering": True, "stopped": True}
CMDS.clear()
r = orch.command("playpause")
assert SPAWNED == [orch.target] and CMDS == [], \
    f"stopped wins over buffering: a dead session replays: {SPAWNED, CMDS}"
SPAWNED.clear()
orch._spot_cmd_timeout_at = -1e9
print("2b-vi. buffering session: live for next AND playpause; stopped wins OK")

# 2c. a finished mpv queue replays on next too (go-librespot state is
# irrelevant for a podcast box — even unreachable must not block it)
SPAWNED.clear()
orch.source = "mpv"
daemon.go_status = _unreachable
r = orch.command("next")
assert r["routed"] == "resume" and SPAWNED == [orch.target], (r, SPAWNED)
orch.source = "spotify"
print("2c. finished podcast queue: next replays regardless of spotify OK")

# 3. live session: next takes the fast path straight to go-librespot
SPAWNED.clear()
CMDS.clear()
daemon.go_status = lambda **k: {"track": {"uri": "spotify:track:x"},
                                "paused": False, "stopped": False,
                                "play_origin": "go-librespot"}
r = skip("next")
assert r.get("fast") and CMDS == ["next"], (r, CMDS)
assert SPAWNED == [], SPAWNED
print("3. live session: next fast-paths to spotify OK")

# 4. THE POINT: a rapid burst reaches go-librespot in full — no drops,
# no ~1s serialization — so the fork's debounce can coalesce it
CMDS.clear()


def _slow(a):
    CMDS.append(a)
    time.sleep(0.2)  # a leading-edge load in flight


daemon.spotify_command = daemon.spotify_skip = _slow
for _ in range(5):
    orch.command("next")
    time.sleep(0.03)  # ~30Hz mash, well inside the old busy window
time.sleep(1.5)
assert len(CMDS) == 5, f"all burst presses must be forwarded, got {len(CMDS)}"
assert SPAWNED == [], SPAWNED
print("4. a 5-press burst is forwarded in full (debounce sees the burst) OK")

# 5. playpause during a held lock still drops busy (the old protection
# stands for the controls that stay on the locked path)
CMDS.clear()
daemon.spotify_command = daemon.spotify_skip = lambda a: CMDS.append(a)
release = threading.Event()
grabbed = threading.Event()


def hog():
    with orch.lock:
        grabbed.set()
        release.wait(10)


t = threading.Thread(target=hog, daemon=True)
t.start()
grabbed.wait(5)
t0 = time.monotonic()
r = orch.command("playpause")
took = time.monotonic() - t0
release.set()
t.join(5)
assert r.get("busy") is True, r
assert took < 3, f"busy drop took {took:.1f}s — must not queue"
print(f"5. playpause during a running control: dropped in {took:.1f}s OK")

print("\nSPOTIFY CONTROLS OK — the fast path forwards the burst, and the "
      "locked fallback still owns replay safety.")
