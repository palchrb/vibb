#!/usr/bin/env python3
"""AM-9: under pipewire the player waits a bounded moment for the sink
node its pcm is pinned to, then FAILS CLOSED with exit 75 — never
'spawn anyway' (a pcm pinned to an absent node fails at hw_params, and
that is the advance storm).

  1. bluealsa: no wait, no graph read
  2. pipewire, node present: returns at once
  3. pipewire, node absent through the budget: SystemExit 75, polling
     every 0.5 s, no mpv Popen
  4. the wait sits right before the mpv Popen AND before /player/play
"""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
for k in ("VIBB_RUN", "VIBB_STATE", "VIBB_CACHE"):
    os.environ[k] = TMP
os.environ["VIBB_SETTINGS"] = os.path.join(TMP, "settings.json")
os.environ["VIBB_BT_FILE"] = os.path.join(TMP, "bt-headset")
os.environ["VIBB_SINK_WAIT_S"] = "0.6"
os.environ.pop("VIBB_AUDIO_STACK", None)
sys.path.insert(0, os.path.join(REPO, "pi"))

import player  # noqa: E402
from vibb import audio  # noqa: E402

with open(os.environ["VIBB_BT_FILE"], "w") as f:
    f.write("2C:FD:B3:FA:DA:04")
ASKED = []
NODE = {"v": True}
audio.sink_ready = lambda out, mac=None, dump=None: ASKED.append((out, mac)) or NODE["v"]
SLEPT = []
player.time.sleep = lambda s: SLEPT.append(s)


def set_stack(s):
    os.environ["VIBB_AUDIO_STACK"] = s
    audio._stack[0] = None


# 1. bluealsa
set_stack("bluealsa")
player._wait_sink("vibb_bt")
assert ASKED == [], "bluealsa never reads the graph"
print("1. bluealsa: no wait OK")

# 2. pipewire, node present
set_stack("pipewire")
player._wait_sink("vibb_bt")
assert ASKED == [("bt", "2C:FD:B3:FA:DA:04")] and SLEPT == []
ASKED.clear()
player._wait_sink("vibb_local")
assert ASKED == [("local", "2C:FD:B3:FA:DA:04")]
print("2. pipewire, node present: returns at once OK")

# 3. node absent: exit 75 after the budget
ASKED.clear(); SLEPT.clear()
NODE["v"] = False
try:
    player._wait_sink("vibb_bt")
    raise AssertionError("must not return without a node")
except SystemExit as e:
    assert e.code == audio.SINK_WAIT_EXIT == 75, e.code
assert len(ASKED) >= 2 and all(s == 0.5 for s in SLEPT), (ASKED, SLEPT)
print("3. node absent: polls, then exit 75 OK")

# 4. placement
src = open(player.__file__, encoding="utf-8").read()
i_wait = src.index("_wait_sink(pcm)")
i_spawn = src.index("proc = subprocess.Popen(mpv_command(")
assert i_wait < i_spawn and "subprocess.Popen(mpv_command(" not in src[i_wait:i_spawn]
i_play = src.index("def play_spotify(")
i_w2 = src.index("_wait_sink(output_pcm())", i_play)
i_post = src.index('spotify.go("/player/play"', i_play)
assert i_w2 < i_post, "before /player/play too"
print("4. the wait precedes the mpv Popen and /player/play OK")

print("\nall player_sink_wait_fail_closed checks passed")
