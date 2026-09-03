#!/usr/bin/env python3
"""NEW-1: Spotify on the HAT is capped like mpv is, and the cap lands
BEFORE /player/play (PLAN-pipewire-soloist.md §F, AM-13).

Until 2026-09-02 `_apply_box_volume` sent volume.json straight to
/player/volume, uncapped, and only AFTER /player/play — so every Spotify
landing on the built-in speaker played its first 0.5-2s at headphone
level. Pinned:

  1. on the local pcm the POSTed volume is min(stored, local_fallback_cap),
     scaled by volume_steps exactly as before (no double scaling)
  2. on the bt pcm it is the stored value, untouched
  3. the FIRST /player/volume precedes /player/play; the post-play apply
     stays as belt
  4. volume.json is never written back
"""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
for k in ("VIBB_RUN", "VIBB_STATE", "VIBB_CACHE"):
    os.environ[k] = TMP
os.environ["VIBB_SETTINGS"] = os.path.join(TMP, "settings.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import player  # noqa: E402

CALLS = []


def fake_go(path, timeout=5, body=None):
    CALLS.append((path, body))
    return b"{}"


def fake_status(timeout=5):
    return {"username": "kid", "volume_steps": 100,
            "track": {"uri": "spotify:track:x", "name": "T",
                      "artist_names": ["A"]}}


player.spotify.go = fake_go
player.spotify.status = fake_status
player.spotify.to_uri = lambda t: "spotify:playlist:p"
player.spotify.read_bookmark = lambda uri: None
player.radio.touch_busy = lambda: None
player.radio.wait_paging_clear = lambda: None
player.time.sleep = lambda s: None

with open(os.path.join(TMP, "volume.json"), "w") as f:
    json.dump({"volume": 90}, f)


def run(pcm):
    CALLS.clear()
    with open(os.path.join(TMP, "output.json"), "w") as f:
        json.dump({"output": "local" if pcm == "vibb_local" else "bt",
                   "pcm": pcm}, f)
    player.play_spotify("https://open.spotify.com/playlist/p")
    return [c for c in CALLS if c[0] in ("/player/volume", "/player/play")]


# 1 + 3: local pcm -> capped, and the first volume POST precedes play
seq = run("vibb_local")
paths = [p for p, _ in seq]
assert paths.index("/player/volume") < paths.index("/player/play"), \
    f"the cap must land before /player/play: {paths}"
vols = [b["volume"] for p, b in seq if p == "/player/volume"]
assert vols and all(v == 35 for v in vols), \
    f"local pcm: min(90, cap 35) scaled by steps 100 = 35, got {vols}"
assert paths.count("/player/volume") >= 2, "the post-play apply stays as belt"
print("1. local pcm: capped to 35, first POST before /player/play OK")

# 2: bt pcm -> untouched
seq = run("vibb_bt")
vols = [b["volume"] for p, b in seq if p == "/player/volume"]
assert vols and all(v == 90 for v in vols), f"headphones keep 90, got {vols}"
print("2. bt pcm: the stored level, uncapped OK")

# 3b: AM-7 — fail-safety caps the bt pcm too
from vibb import audio  # noqa: E402

audio.cap_everywhere = lambda: True
seq = run("vibb_bt")
vols = [b["volume"] for p, b in seq if p == "/player/volume"]
assert vols and all(v == 35 for v in vols), f"fail-safety: headphones capped too, got {vols}"
audio.cap_everywhere = lambda: False
print("3b. fail-safety caps the bt pcm as well OK")

# 4: never written back
with open(os.path.join(TMP, "volume.json")) as f:
    assert json.load(f)["volume"] == 90
print("4. volume.json untouched OK")

print("\nall spotify_volume_before_play checks passed")
