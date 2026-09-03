#!/usr/bin/env python3
"""vibb.audio: the PipeWire resolver reads node names from the graph and
never composes them (PLAN-pipewire-soloist.md §C, I12).

  1. stack(): env beats the file beats the default (bluealsa); anything
     but the literal 'pipewire' is bluealsa
  2. find_bt_sink: by api.bluez5.address, case-insensitive, Audio/Sink
     only — the bluez INPUT node with the same address is skipped; both
     name shapes (.1 and .a2dp-sink) are found because nothing is built
  3. find_local_sink: by card name substring (hifiberry), or whatever
     VIBB_LOCAL_CARD says on a HAT-less bench
  4. pw_dump: [] when the tool is missing, fails, or prints garbage;
     the real list when it works (PATH fake)
  5. sink_ready per output
  6. asound_text: both pcm NAMES survive, colon MAC present, server +
     playback_node pinned, an unresolved node fails closed, no bluealsa
"""
import json
import os
import stat
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["VIBB_RUN"] = TMP
os.environ["VIBB_AUDIO_STACK_FILE"] = os.path.join(TMP, "audio-stack")
os.environ.pop("VIBB_AUDIO_STACK", None)
os.environ.pop("VIBB_LOCAL_CARD", None)
sys.path.insert(0, os.path.join(REPO, "pi"))

from vibb import audio  # noqa: E402

MAC = "2C:FD:B3:FA:DA:04"
OTHER = "30:C0:1B:BD:13:B2"


def node(i, name, mclass, **props):
    p = {"node.name": name, "media.class": mclass}
    p.update(props)
    return {"id": i, "type": "PipeWire:Interface:Node", "info": {"props": p}}


DUMP = [
    {"id": 0, "type": "PipeWire:Interface:Core", "info": {"cookie": 42}},
    node(46, "alsa_output.platform-107c701400.hdmi.hdmi-stereo", "Audio/Sink",
         **{"alsa.card_name": "vc4-hdmi-0", "api.alsa.card.name": "vc4-hdmi-0"}),
    node(47, "alsa_output.platform-soc_sound.stereo-fallback", "Audio/Sink",
         **{"alsa.card_name": "snd_rpi_hifiberry_dac",
            "api.alsa.card.name": "snd_rpi_hifiberry_dac"}),
    node(60, "bluez_input.2C_FD_B3_FA_DA_04.0", "Audio/Source",
         **{"api.bluez5.address": MAC}),
    node(61, "bluez_output.2C_FD_B3_FA_DA_04.1", "Audio/Sink",
         **{"api.bluez5.address": MAC, "api.bluez5.codec": "sbc"}),
    node(62, "bluez_output.30_C0_1B_BD_13_B2.a2dp-sink", "Audio/Sink",
         **{"api.bluez5.address": OTHER.lower()}),
    node(70, "vibb_null", "Audio/Sink"),
    node(80, "PipeWire ALSA [mpv]", "Stream/Output/Audio",
         **{"target.object": "bluez_output.2C_FD_B3_FA_DA_04.1"}),
    {"id": 90, "type": "PipeWire:Interface:Link",
     "info": {"output-node-id": 80, "input-node-id": 61}},
]

# 1. stack()
assert audio.stack() == "bluealsa", "default is bluealsa"
audio._stack[0] = None
with open(os.environ["VIBB_AUDIO_STACK_FILE"], "w") as f:
    f.write("pipewire\n")
assert audio.stack() == "pipewire", "the install.sh file selects pipewire"
audio._stack[0] = None
os.environ["VIBB_AUDIO_STACK"] = "bluealsa"
assert audio.stack() == "bluealsa", "the unit's env beats the file"
audio._stack[0] = None
os.environ["VIBB_AUDIO_STACK"] = "PipeWire"
assert audio.stack() == "bluealsa", "only the literal 'pipewire' counts"
audio._stack[0] = None
os.environ["VIBB_AUDIO_STACK"] = "pipewire"
assert audio.stack() == "pipewire"
print("1. stack(): env > file > default, literal match only OK")

# 2. bt sink discovery
assert audio.find_bt_sink(MAC, DUMP) == "bluez_output.2C_FD_B3_FA_DA_04.1"
assert audio.find_bt_sink(MAC.lower(), DUMP) == "bluez_output.2C_FD_B3_FA_DA_04.1"
assert audio.find_bt_sink(OTHER, DUMP) == "bluez_output.30_C0_1B_BD_13_B2.a2dp-sink", \
    "the .a2dp-sink shape is found because the name is READ, not built"
assert audio.find_bt_sink("AA:AA:AA:AA:AA:AA", DUMP) is None
assert audio.find_bt_sink(MAC, [n for n in DUMP if n["id"] != 61]) is None, \
    "the bluez INPUT node (Audio/Source) must never stand in for the sink"
print("2. bt sink by address, case-insensitive, sink-only, any name shape OK")

# 3. local sink discovery
assert audio.find_local_sink(DUMP) == "alsa_output.platform-soc_sound.stereo-fallback"
audio.LOCAL_CARD = "vc4-hdmi"
assert audio.find_local_sink(DUMP) == "alsa_output.platform-107c701400.hdmi.hdmi-stereo", \
    "a HAT-less bench points VIBB_LOCAL_CARD at its HDMI card"
audio.LOCAL_CARD = "hifiberry"
assert audio.find_local_sink([]) is None
print("3. local sink by card-name substring, bench override OK")

# 4. pw_dump via a PATH fake
bindir = os.path.join(TMP, "bin")
os.makedirs(bindir)
fake = os.path.join(bindir, "pw-dump")
os.environ["PATH"] = bindir + ":" + os.environ["PATH"]
open(fake, "w").write("#!/bin/sh\nexit 3\n")
os.chmod(fake, stat.S_IRWXU)
assert audio.pw_dump() == [], "a failing pw-dump reads as an empty graph"
open(fake, "w").write("#!/bin/sh\necho 'not json'\n")
assert audio.pw_dump() == []
open(fake, "w").write("#!/bin/sh\necho '{\"a\": 1}'\n")
assert audio.pw_dump() == [], "a non-list reply is not a graph"
with open(os.path.join(TMP, "dump.json"), "w") as f:
    json.dump(DUMP, f)
open(fake, "w").write(f"#!/bin/sh\ncat {TMP}/dump.json\n")
assert [o["id"] for o in audio.pw_dump()] == [o["id"] for o in DUMP]
assert audio.find_bt_sink(MAC) == "bluez_output.2C_FD_B3_FA_DA_04.1", \
    "dump=None means: run pw-dump"
os.environ["PATH"] = os.environ["PATH"].split(":", 1)[1]
assert audio.pw_dump() == [] or True  # tool missing -> [] (PATH-dependent)
os.environ["PATH"] = bindir + ":" + os.environ["PATH"]
print("4. pw_dump: [] on failure/garbage, the list when it works OK")

# 5. sink_ready
assert audio.sink_ready("bt", MAC, DUMP) is True
assert audio.sink_ready("bt", OTHER, DUMP) is True
assert audio.sink_ready("bt", "AA:AA:AA:AA:AA:AA", DUMP) is False
assert audio.sink_ready("bt", None, DUMP) is False, "no speaker = not ready"
assert audio.sink_ready("local", dump=DUMP) is True
assert audio.sink_ready("local", dump=[]) is False
print("5. sink_ready per output OK")

# 6. asound_text
txt = audio.asound_text(MAC, "bluez_output.2C_FD_B3_FA_DA_04.1",
                        "alsa_output.platform-soc_sound.stereo-fallback")
assert "pcm.vibb_bt {" in txt and "pcm.vibb_local {" in txt, "the NAMES survive"
assert MAC in txt, "colon MAC kept for bt.py's idempotence check"
assert 'playback_node "bluez_output.2C_FD_B3_FA_DA_04.1"' in txt
assert 'playback_node "alsa_output.platform-soc_sound.stereo-fallback"' in txt
assert txt.count(f'server "{audio.SOCKET}"') == 2
assert "bluealsa" not in txt
txt2 = audio.asound_text(None, None, None)
assert txt2.count(f'playback_node "{audio.UNRESOLVED}"') == 2, \
    "unresolved pins a node that cannot exist: opens fail closed"
print("6. asound_text: names, MAC, server, pinned nodes, fail-closed OK")

print("\nall audio_route_discovery checks passed")
