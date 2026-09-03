#!/usr/bin/env python3
"""btwatchd's announce under pipewire (AM-10/AM-11): the route pin is
refreshed FIRST, file only, and the deferred mpv switch waits for the
sink node as well as the transport.

  1. set_output(bt, fallback=True) with output already bt: exactly one
     ensure_bt_route call, one pw-dump, one live go-librespot reopen —
     the same reopen count as today
  2. transport up but node absent: no mpv retarget (would fail at
     hw_params and start the advance storm), route untouched
  3. a user switch (fallback=False) never calls ensure_bt_route, and its
     mpv retarget is gated the same way
  4. under bluealsa none of this runs: no pw-dump, no ensure_bt_route
"""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["VIBB_STATE"] = TMP
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
os.environ["VIBB_SETTINGS"] = os.path.join(TMP, "se.json")
os.environ["VIBB_LIBRARY"] = os.path.join(TMP, "lib.json")
os.environ["VIBB_BT_FILE"] = os.path.join(TMP, "bt-headset")
os.environ["VIBB_AUDIO_STACK"] = "pipewire"
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

orch = daemon.ORCH
MAC = "2C:FD:B3:FA:DA:04"
with open(os.environ["VIBB_BT_FILE"], "w") as f:
    f.write(MAC)
CALLS = {"ensure": [], "dump": 0, "reopen": 0, "mpv": []}
NODE = {"present": True}
daemon._audio.ensure_bt_route = lambda mac: CALLS["ensure"].append(mac) or True


def fake_dump(timeout=3.0):
    CALLS["dump"] += 1
    if not NODE["present"]:
        return []
    return [{"id": 1, "type": "PipeWire:Interface:Node", "info": {"props": {
        "node.name": "bluez_output.X.1", "media.class": "Audio/Sink",
        "api.bluez5.address": MAC}}}]


daemon._audio.pw_dump = fake_dump


def fake_reopen(pcm):
    CALLS["reopen"] += 1
    return True


daemon.reopen_go_output = fake_reopen
daemon.go_status = lambda **_k: {"username": "kid"}
daemon.btbus.a2dp_pcm_present = lambda mac: True
daemon._kick_bt_connect = lambda: None
daemon._renderer.is_sonos = lambda: False
daemon.mpv_ipc = lambda cmd: CALLS["mpv"].append(cmd) or {"error": "success"}
orch._mpv_alive = lambda: True
orch.child, orch.target, orch.source = None, None, None


def reset():
    CALLS.update(ensure=[], dump=0, reopen=0, mpv=[])


def set_output_file(o):
    with open(os.path.join(TMP, "output.json"), "w") as f:
        json.dump({"output": o, "pcm": "vibb_" + o}, f)


# 1. the announce with output already bt
set_output_file("bt"); reset()
out = orch.set_output("bt", fallback=True)
assert out.get("unchanged") is True, out
assert CALLS["ensure"] == [MAC], "exactly one route refresh, before the switch"
assert CALLS["dump"] == 1, f"one pw-dump for the node gate, got {CALLS['dump']}"
assert CALLS["reopen"] == 1, "the one live reopen, as today"
assert any(c[:2] == ["set_property", "audio-device"] for c in CALLS["mpv"]), \
    "deferred mpv switch applied when transport AND node exist"
print("1. announce: one route refresh, one dump, one reopen, mpv switched OK")

# 2. transport up, node absent
NODE["present"] = False; reset()
orch.set_output("bt", fallback=True)
assert CALLS["ensure"] == [MAC]
assert not any(c[:2] == ["set_property", "audio-device"] for c in CALLS["mpv"]), \
    "no node -> no mpv retarget (it would fail at hw_params)"
assert CALLS["reopen"] == 0, "no node -> no reopen either"
NODE["present"] = True
print("2. transport without node: no retarget, no reopen OK")

# 3. a user switch from local to bt
set_output_file("local"); reset()
orch.set_output("bt")
assert CALLS["ensure"] == [], "a user switch is not an announce"
assert any(c[:2] == ["set_property", "audio-device"] for c in CALLS["mpv"])
NODE["present"] = False; set_output_file("local"); reset()
orch.set_output("bt")
assert not any(c[:2] == ["set_property", "audio-device"] for c in CALLS["mpv"]), \
    "user switch: the live retarget waits for the node too"
NODE["present"] = True
print("3. user switch: no route refresh; retarget gated on the node OK")

# 4. bluealsa: none of it
os.environ["VIBB_AUDIO_STACK"] = "bluealsa"; daemon._audio._stack[0] = None
set_output_file("bt"); reset()
orch.set_output("bt", fallback=True)
assert CALLS["ensure"] == [] and CALLS["dump"] == 0
assert CALLS["reopen"] == 1 and any(c[:2] == ["set_property", "audio-device"] for c in CALLS["mpv"])
print("4. bluealsa: no route refresh, no dump, today's behaviour OK")

print("\nall output_announce_route checks passed")
