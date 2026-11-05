#!/usr/bin/env python3
"""NEW-2: Spotify is CONFIRMED paused before mpv starts, and a wedged
go-librespot is restarted rather than spawned over (PLAN-pipewire-soloist
§G, AM-14).

bluealsa's exclusive pcm was the accidental guarantee against two
sources; PipeWire mixes. Pinned:

  1. a pause that lands (status paused / no track) -> True, no restart
  2. a refused connection (not running) -> True at once, no restart
  3. a slow pause (status still playing for a while) -> waits, True, no
     restart
  4. an API that answers but never pauses -> False after the budget, no
     restart (the log says 'spawning anyway')
  5. TIMEOUTS through the budget (the wedge) -> `systemctl --no-block
     try-restart go-librespot` exactly once + the cross-process restart
     marker, then True
  6. play_mpv calls it BEFORE the mpv Popen
"""
import os
import sys
import tempfile
import urllib.error

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
for k in ("VIBB_RUN", "VIBB_STATE", "VIBB_CACHE"):
    os.environ[k] = TMP
os.environ["VIBB_SETTINGS"] = os.path.join(TMP, "settings.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import player  # noqa: E402
from vibb import paths  # noqa: E402

player.time.sleep = lambda s: None
RUNS, GO = [], []
player.subprocess.run = lambda args, **kw: RUNS.append(list(args))
STATUS = {"seq": []}


def fake_go(path, timeout=5, body=None):
    GO.append(path)
    if STATUS.get("go_raises"):
        raise STATUS["go_raises"]
    return b"{}"


def fake_status(timeout=2):
    item = STATUS["seq"].pop(0) if len(STATUS["seq"]) > 1 else STATUS["seq"][0]
    if isinstance(item, Exception):
        raise item
    return dict(item)


player.spotify.go = fake_go
player.spotify.status_strict = fake_status
PLAYING = {"track": {"uri": "spotify:track:x"}, "paused": False}
PAUSED = {"track": {"uri": "spotify:track:x"}, "paused": True}
NOTRACK = {}
refused = urllib.error.URLError(ConnectionRefusedError(111, "refused"))
timed_out = TimeoutError("timed out")


def reset(seq, go_raises=None):
    STATUS["seq"] = list(seq)
    STATUS["go_raises"] = go_raises
    RUNS.clear()
    GO.clear()
    if os.path.exists(paths.GO_RESTART_FILE):
        os.remove(paths.GO_RESTART_FILE)


# 1. pause lands
reset([PAUSED])
assert player._confirm_spotify_paused(budget_s=0.3) is True
assert RUNS == [] and GO == ["/player/pause"]
reset([NOTRACK])
assert player._confirm_spotify_paused(budget_s=0.3) is True and RUNS == []
print("1. paused / no track -> True, no restart OK")

# 2. refused = not running
reset([PLAYING], go_raises=refused)
assert player._confirm_spotify_paused(budget_s=0.3) is True
assert RUNS == [] and not os.path.exists(paths.GO_RESTART_FILE)
print("2. connection refused -> True at once, no restart OK")

# 3. slow pause
reset([PLAYING, PLAYING, PAUSED])
assert player._confirm_spotify_paused(budget_s=5) is True
assert RUNS == [] and len(GO) == 3, GO
print("3. slow pause -> confirmed on the third look, no restart OK")

# 4. answers but never pauses
reset([PLAYING])
assert player._confirm_spotify_paused(budget_s=0.2) is False
assert RUNS == [], "a responsive API is never restarted"
print("4. never pauses -> False, no restart OK")

# 5. the wedge: timeouts through the budget
reset([timed_out, timed_out, timed_out, NOTRACK], go_raises=timed_out)
assert player._confirm_spotify_paused(budget_s=0.2) is True
assert RUNS == [["systemctl", "--no-block", "try-restart", "go-librespot"]], RUNS
assert os.path.exists(paths.GO_RESTART_FILE), "the daemon's dedup marker"
print("5. timeouts -> one --no-block try-restart + marker, then True OK")

# 6. before the mpv Popen
src = open(player.__file__, encoding="utf-8").read()
i_conf = src.index("_confirm_spotify_paused()  # never two sources")
i_play = src.rfind("\ndef ", 0, i_conf)          # the mpv play function
i_spawn = src.index("proc = subprocess.Popen(mpv_command(", i_play)
assert i_conf < i_spawn, "confirm before the spawn"
assert src.count('spotify.go("/player/pause", timeout=1)') == 1, \
    "the fire-and-forget pause exists only inside the confirm loop"
print("6. the mpv play path confirms before the Popen OK")

print("\nall spotify_pause_confirm checks passed")
