#!/usr/bin/env python3
"""Gate the cache sweep's busy predicate (AM-103, owner 2026-10-20): the
sweep yields only to audio that NEEDS a scarce link — a stream over the
box's own radio (Spotify, mpv on a remote url, a start/skip in flight, an
api too blocked to tell) or any audio out over a BT speaker (A2DP under
wifi load = the BT firmware-crash class). A synced podcast or a Storytel
book on the HAT, and a Sonos session (the speaker streams, the box only
polls), leave the radio free: the sweep carries on and a half-downloaded
book is not killed by a bedtime story from the cache. Before, every kind
of sound paused the sweep (_audible_now)."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_LIBRARY"] = os.path.join(os.environ["VIBB_STATE"], "lib.json")
os.environ["VIBB_RUN"] = tempfile.mkdtemp()  # no stale radio markers
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

orch = daemon.ORCH
MPV = {"pause": False, "path": "/cache/show/e1.mp3"}
OUT = {"output": "local", "pcm": "vibb_local"}
daemon.mpv_get = lambda p: MPV.get(p)
daemon.current_output = lambda: dict(OUT)
daemon.spotify_playing = lambda: False
daemon._go_unit_active = lambda: False
orch._mpv_alive = lambda: True
orch.source = "mpv"

# 1. a cached file on the HAT: audible, no radio -> the sweep carries on
assert daemon._audible_now() is True
assert daemon._sweep_busy_now() is False
print("1. cached podcast/book on the built-in speaker: not busy OK")

# 2. the same file out over BT: coex -> busy
OUT["output"] = "bt"
assert daemon._sweep_busy_now() is True
OUT["output"] = "local"
print("2. cached file over a BT speaker: busy OK")

# 3. mpv streaming a remote url: busy whatever the output
MPV["path"] = "https://podkast.nrk.no/x/e1.mp3"
assert daemon._sweep_busy_now() is True
MPV["path"] = "/cache/show/e1.mp3"
print("3. remote url in mpv: busy OK")

# 4. paused: nothing audible, nothing streaming -> not busy
MPV["pause"] = True
assert daemon._sweep_busy_now() is False
MPV["pause"] = False
print("4. paused: not busy OK")

# 5. Spotify playing: a stream -> busy, even on the HAT
daemon.spotify_playing = lambda: True
assert daemon._sweep_busy_now() is True
daemon.spotify_playing = lambda: False
print("5. spotify playing: busy OK")

# 6. api blocked + unit running = probably mid-load = cannot tell -> busy
def _boom():
    raise OSError("down")
daemon.spotify_playing = _boom
daemon._go_unit_active = lambda: True
assert daemon._sweep_busy_now() is True
daemon._go_unit_active = lambda: False
daemon.spotify_playing = lambda: False
print("6. blocked api + running unit: busy (unknown is busy) OK")

# 7. a fresh BUSY marker (a tap/skip in flight) -> busy
daemon._radio.touch_busy()
assert daemon._sweep_busy_now() is True
os.remove(daemon._radio.BUSY_FILE) if hasattr(daemon._radio, "BUSY_FILE") else None
daemon._radio.busy = lambda: False
print("7. a start/skip in flight (BUSY marker): busy OK")

# 8. Sonos session playing: the speaker streams, the box only polls
orch.source = "sonos"
orch._sonos_fresh = lambda: {"transport": "PLAYING", "reachable": True, "ours": True}
assert daemon._audible_now() is True       # the card says playing...
assert daemon._sweep_busy_now() is False   # ...but the radio is free
print("8. sonos playing: not busy OK")

# 9. the daemon wires THIS predicate as the sweep's gate
src = open(os.path.join(REPO, "pi", "daemon.py")).read()
assert "_library.BUSY_CHECK = _sweep_busy_now" in src
print("9. BUSY_CHECK is the radio-aware gate OK")

print("SWEEP BUSY GATE OK — the sweep yields to streams and BT, never to a cached file on the HAT or a Sonos.")
