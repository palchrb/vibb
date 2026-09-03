#!/usr/bin/env python3
"""I2: no landing path exists that vibb did not choose — every audio
client carries a PINNED target, and every libpipewire client carries
dont-reconnect + dont-fallback (PLAN-pipewire-soloist §B, AM-5).

  1. mpv's argv names its pcm (alsa/vibb_bt|vibb_local — the pcm IS the
     route); go-librespot's config template names audio_device: vibb_bt
  2. the client fragment install.sh writes carries node.dont-reconnect
     and node.dont-fallback; the unit env belt carries the same as
     PIPEWIRE_PROPS for the daemon (mpv inherits), go-librespot and
     bt-reconnect
  3. under pipewire an unpaired vibb_bt pins a name that cannot exist —
     never plug->null (which silently 'plays')
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
from vibb import audio  # noqa: E402

player = open(os.path.join(REPO, "pi", "player.py"), encoding="utf-8").read()
install = open(os.path.join(REPO, "pi", "install.sh"), encoding="utf-8").read()
stack = open(os.path.join(REPO, "pi", "audio-stack.sh"), encoding="utf-8").read()

# 1
assert 'f"--audio-device=alsa/{pcm}"' in player, "mpv opens the NAMED pcm"
assert "audio_backend: alsa" in install and "audio_device: vibb_bt" in install
print("1. mpv + go-librespot address the pinned pcm names OK")

# 2
i = stack.index("client.conf.d/10-vibb.conf")
frag = stack[i:i + 700]
assert "node.dont-reconnect = true" in frag and "node.dont-fallback  = true" in frag
assert "Environment=PIPEWIRE_PROPS={ node.dont-reconnect=true node.dont-fallback=true }" in stack
for unit in ("Description=Vibb orchestration daemon",
             "Description=go-librespot Spotify Connect daemon",
             "Description=Vibb BT reconnect daemon"):
    j = install.index(unit)
    assert "$(audio_stack_unit_env)" in install[j:j + 2500], unit
print("2. client fragment + PIPEWIRE_PROPS belt on all three units OK")

# 3
txt = audio.asound_text(None, None, None)
assert txt.count(f'playback_node "{audio.UNRESOLVED}"') == 2 and '"null"' not in txt
print("3. unpaired pcm pins a name that cannot exist, never null OK")

print("\nall audio_client_props checks passed")
