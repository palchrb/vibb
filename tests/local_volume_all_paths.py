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
from vibb import sysinfo  # noqa: E402
sysinfo.update_settings({"volume_cap": 35})   # THE cap (AM-58): every output
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

# 2. set_output(bt): reopen onto headphones -> the same cap (AM-58: one cap)
CALLS.clear()
orch.set_output("bt")
assert volume_posts() == [35], f"headphones land at the box cap too: {volume_posts()}"
print("2. set_output(bt) live reopen -> /player/volume 35 (one cap) OK")

# 3. _go_output_rebuild while the output is local -> one capped POST
CALLS.clear()
orch.set_output("local")
CALLS.clear()
daemon._go_output_rebuild()
assert volume_posts() == [35], f"rebuild onto the HAT must cap: {volume_posts()}"
print("3. _go_output_rebuild on local -> /player/volume 35 OK")

# 4. _go_output_rebuild while the output is bt -> capped the same way
CALLS.clear()
orch.set_output("bt")
CALLS.clear()
daemon._go_output_rebuild()
assert volume_posts() == [35], volume_posts()
print("4. _go_output_rebuild on bt -> /player/volume 35 OK")

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

# 7. AM-58: the policy self-test's verdict no longer moves the cap — the
#    cap is on every output by construction. fail-safety or green, a bt
#    reopen lands at 35 and the live knob cannot exceed 35 either.
import json as _json  # noqa: E402

daemon._audio.POLICY_FILE = os.path.join(os.environ["VIBB_RUN"], "policy.json")
os.environ["VIBB_AUDIO_STACK"] = "pipewire"; daemon._audio._stack[0] = None
daemon._audio.sink_ready = lambda out, mac=None, dump=None: True   # the node exists
for verdict in ("fail-safety", "ok"):
    with open(daemon._audio.POLICY_FILE, "w") as f:
        _json.dump({"verdict": verdict, "safety": ["targetless-linked"] if verdict != "ok" else [], "rf": []}, f)
    daemon._audio._policy_cache["mtime"] = None
    CALLS.clear()
    orch._save_volume(90)
    orch.set_output("bt")
    assert volume_posts() == [35], f"{verdict}: the bt reopen lands at the cap: {volume_posts()}"
    orch.source = "spotify"
    CALLS.clear()
    r = orch.volume(absolute=80)
    assert r["volume"] == 35 and volume_posts() == [35], (verdict, r, volume_posts())
    orch.source = None
print("7. the self-test verdict does not move the cap: 35 everywhere, always OK")

print("\nall local_volume_all_paths checks passed")
