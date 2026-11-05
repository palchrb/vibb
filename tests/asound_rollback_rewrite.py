#!/usr/bin/env python3
"""AM-95 (1): a rollback to bluealsa must REWRITE /etc/asound.conf — the
PipeWire text carries the MAC too ("# bt speaker <MAC>"), and the old
idempotence check ('MAC already in the file') left both pcms pointing at a
masked server: a silent box on both outputs.

  1. PipeWire-format file with the MAC + stack bluealsa -> rewritten to
     'type bluealsa'
  2. a second call is a no-op (idempotent within the format)
"""
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["VIBB_ASOUND"] = os.path.join(TMP, "asound.conf")
os.environ["VIBB_AUDIO_STACK"] = "bluealsa"
os.environ["VIBB_STATE"] = TMP
os.environ["VIBB_GO_API"] = "http://127.0.0.1:1"
sys.path.insert(0, os.path.join(REPO, "pi"))
with open(os.path.join(TMP, "output.json"), "w") as f:
    f.write('{"output": "local", "pcm": "vibb_local"}')

from vibb import bt  # noqa: E402

MAC = "2C:FD:B3:5B:1C:BA"
with open(os.environ["VIBB_ASOUND"], "w") as f:
    f.write(f"# bt speaker {MAC}\npcm.vibb_bt {{\n    type pipewire\n    playback_node \"bluez_output.x.1\"\n}}\n"
            "pcm.vibb_local {\n    type pipewire\n    playback_node \"alsa_output.y\"\n}\n")
bt._route_alsa(MAC)
txt = open(os.environ["VIBB_ASOUND"]).read()
assert "type bluealsa" in txt and "type pipewire" not in txt and MAC in txt, txt
print("1. rollback rewrites a PipeWire-format asound.conf to bluealsa OK")

mt = os.stat(os.environ["VIBB_ASOUND"]).st_mtime_ns
time.sleep(0.05)
bt._route_alsa(MAC)
assert os.stat(os.environ["VIBB_ASOUND"]).st_mtime_ns == mt, "idempotent within the format"
print("2. same format + same MAC: untouched OK")
print("\nall asound_rollback_rewrite checks passed")
