#!/usr/bin/env python3
"""I8: vibb is the single volume authority through its own softvol /
engine volume — it never writes a PipeWire node's volume (a bluez node's
volume becomes AVRCP SetAbsoluteVolume over the air mid-stream, the
channel-ops-while-streaming class), never a mixer. Source pin over pi/."""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAD = re.compile(r"wpctl set-volume|wpctl set-mute|pw-metadata[^\n]*volume|amixer|snd_mixer|"
                 r"pw-cli set-param[^\n]*Props")
hits = []
for root, _dirs, files in os.walk(os.path.join(REPO, "pi")):
    for f in files:
        if f.endswith((".py", ".sh")):
            path = os.path.join(root, f)
            for n, line in enumerate(open(path, encoding="utf-8"), 1):
                if BAD.search(line) and "no_volume_writer" not in line:
                    hits.append(f"{os.path.relpath(path, REPO)}:{n}: {line.strip()}")
assert not hits, "a graph/mixer volume writer appeared:\n" + "\n".join(hits)
print("no wpctl/pw-metadata/mixer volume writer in pi/ OK")
print("\nall no_volume_writer checks passed")
