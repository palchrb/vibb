#!/usr/bin/env python3
"""I1: every path that lands go-librespot's audio on the HAT re-applies
the local cap (PLAN-pipewire-soloist.md §F, NEW-1).

The mpv paths are pinned by local_volume_cap.py and the Spotify SPAWN by
spotify_volume_before_play.py. Two more paths move a LIVE Spotify session
onto the amplifier without a spawn — the v0.0.7 live reopen in
set_output(local) and _go_output_rebuild's reopen while the output is
local — and until 2026-09-02 both kept the session's headphone volume.
Pinned: after a successful reopen onto vibb_local exactly one
/player/volume POST carries min(stored, cap) scaled by volume_steps;
onto vibb_bt none; volume.json is never written; a failed reopen posts
nothing (the restart path respawns through _apply_box_volume, capped).
"""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
os.environ["VIBB_SETTINGS"] = os.path.join(os.environ["VIBB_STATE"], "se.json")
os.environ["VIBB_LIBRARY"] = os.path.join(os.environ["VIBB_STATE"], "lib.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

orch = daemon.ORCH
CALLS = []
REOPEN = {"ok": True}
daemon.go = lambda path, timeout=5, body=None: CALLS.append((path, body)) or b"{}"
daemon.go_status = lambda **_k: {"volume_steps": 100, "username": "kid",
                                 "track": None}
daemon.reopen_go_output = lambda pcm: REOPEN["ok"]
daemon._retarget_go_librespot = lambda pcm: False
daemon._bt_transport_ready = lambda: True
daemon._i2s_card_present = lambda: True
daemon._kick_bt_connect = lambda: None
daemon._renderer.is_sonos = lambda: False
daemon._tick = lambda s: None
orch._mpv_alive = lambda: False
orch.target, orch.source = None, None
orch._save_volume(90)                      # the headphone level a parent set
daemon._bt.MAC_FILE = os.path.join(os.environ["VIBB_RUN"], "bt-mac")
with open(daemon._bt.MAC_FILE, "w") as f:
    f.write("2C:FD:B3:FA:DA:04")


def volume_posts():
    return [b["volume"] for p, b in CALLS if p == "/player/volume"]


# 1. set_output(local): live reopen -> one capped POST (35 of 100 steps)
CALLS.clear()
out = orch.set_output("local")
assert out["output"] == "local", out
assert volume_posts() == [35], f"reopen onto the HAT must cap: {volume_posts()}"
print("1. set_output(local) live reopen -> /player/volume 35 OK")

# 2. set_output(bt): reopen onto headphones -> no cap POST
CALLS.clear()
orch.set_output("bt")
assert volume_posts() == [], f"headphones keep their level: {volume_posts()}"
print("2. set_output(bt) live reopen -> no volume POST OK")

# 3. _go_output_rebuild while the output is local -> one capped POST
CALLS.clear()
orch.set_output("local")
CALLS.clear()
daemon._go_output_rebuild()
assert volume_posts() == [35], f"rebuild onto the HAT must cap: {volume_posts()}"
print("3. _go_output_rebuild on local -> /player/volume 35 OK")

# 4. _go_output_rebuild while the output is bt -> none
CALLS.clear()
orch.set_output("bt")
CALLS.clear()
daemon._go_output_rebuild()
assert volume_posts() == [], volume_posts()
print("4. _go_output_rebuild on bt -> no volume POST OK")

# 5. a failed reopen posts nothing here (the restart path respawns the
#    player, which caps via _apply_box_volume)
REOPEN["ok"] = False
CALLS.clear()
orch.set_output("local")
assert volume_posts() == [], volume_posts()
REOPEN["ok"] = True
print("5. failed reopen -> no cap POST from the daemon (spawn path caps) OK")

# 6. the saved knob is untouched by all of the above
assert orch._volume_setting() == 90
with open(daemon.VOL_FILE) as f:
    assert json.load(f)["volume"] == 90
print("6. volume.json untouched OK")

print("\nall local_volume_all_paths checks passed")
