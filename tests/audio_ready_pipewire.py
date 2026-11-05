#!/usr/bin/env python3
"""audio_ready() under the PipeWire stack = transport AND sink node
(PLAN-pipewire-soloist.md §D, NEW-3).

A pcm pinned to an absent node fails at hw_params, the BT transport
precedes its node by milliseconds, and the HAT node exists only once
WirePlumber is up — so 'able to make sound' gains a second condition
under pipewire and keeps exactly today's meaning under bluealsa.

  1. bluealsa: bt = the transport gate alone; local = the I2S card alone
  2. pipewire: bt = transport AND node; local = card AND node
  3. no speaker configured stays 'nothing to wait for' on both stacks
  4. btwatchd's commit gate (_await_pcm) waits for the node too, only
     under pipewire (source pin: it cannot import here)
"""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
for k in ("VIBB_RUN", "VIBB_STATE", "VIBB_CACHE"):
    os.environ[k] = TMP
os.environ["VIBB_BT_FILE"] = os.path.join(TMP, "bt-headset")
os.environ["VIBB_AUDIO_STACK_FILE"] = os.path.join(TMP, "audio-stack")
os.environ.pop("VIBB_AUDIO_STACK", None)
sys.path.insert(0, os.path.join(REPO, "pi"))

# btbus imports dbus lazily — imports headless
from vibb import audio, btbus, output  # noqa: E402

MAC = "2C:FD:B3:FA:DA:04"
STATE = {"transport": True, "node_bt": True, "node_local": True, "card": True}
btbus.a2dp_pcm_present = lambda mac: STATE["transport"]
output._i2s_card_present = lambda: STATE["card"]
audio.sink_ready = lambda out, mac=None, dump=None: (
    STATE["node_bt"] if out == "bt" else STATE["node_local"])


def set_output(o):
    with open(os.path.join(TMP, "output.json"), "w") as f:
        json.dump({"output": o, "pcm": "vibb_" + o}, f)


def set_stack(s):
    os.environ["VIBB_AUDIO_STACK"] = s
    audio._stack[0] = None


with open(os.environ["VIBB_BT_FILE"], "w") as f:
    f.write(MAC)

# 1. bluealsa: today's meaning
set_stack("bluealsa")
set_output("bt")
STATE.update(transport=True, node_bt=False)
assert output.audio_ready() is True, "bluealsa: the transport alone decides"
STATE.update(transport=False, node_bt=True)
assert output.audio_ready() is False
set_output("local")
STATE.update(card=True, node_local=False)
assert output.audio_ready() is True, "bluealsa: the I2S card alone decides"
STATE.update(card=False, node_local=True)
assert output.audio_ready() is False
print("1. bluealsa: transport / card alone, as today OK")

# 2. pipewire: node required too
set_stack("pipewire")
set_output("bt")
STATE.update(transport=True, node_bt=False)
assert output.audio_ready() is False, "pipewire: transport without node = not ready"
STATE.update(transport=True, node_bt=True)
assert output.audio_ready() is True
STATE.update(transport=False, node_bt=True)
assert output.audio_ready() is False, "a node without a transport is a ghost"
set_output("local")
STATE.update(card=True, node_local=False)
assert output.audio_ready() is False, "pipewire: HAT card present but no node yet (NEW-3)"
STATE.update(card=True, node_local=True)
assert output.audio_ready() is True
print("2. pipewire: transport AND node / card AND node OK")

# 3. no speaker configured
set_output("bt")
os.remove(os.environ["VIBB_BT_FILE"])
for s in ("bluealsa", "pipewire"):
    set_stack(s)
    assert output.audio_ready() is True, f"{s}: no speaker = nothing to wait for"
print("3. no speaker configured -> True on both stacks OK")

# 4. btwatchd's commit gate
src = open(os.path.join(REPO, "pi", "btwatchd.py"), encoding="utf-8").read()
i = src.index("def _await_pcm(self):")
body = src[i:src.index("def _await_pcm_tick", i)]
assert 'audio.sink_ready("bt", self.target)' in body, \
    "the announce must wait for the sink node under pipewire"
assert 'audio.stack() == "pipewire"' in body, "...and only under pipewire"
assert body.index("a2dp_pcm_present") < body.index("sink_ready"), \
    "transport first, node second"
print("4. btwatchd _await_pcm: node wait behind the stack check OK")

print("\nall audio_ready_pipewire checks passed")
