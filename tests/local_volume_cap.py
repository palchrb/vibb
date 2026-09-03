#!/usr/bin/env python3
"""The built-in speaker gets its own ceiling.

The box keeps ONE volume number, but a pair of kids' headphones and the
HAT's amplifier are not the same loudness — the level a parent sets for
quiet headphones is a room-filling level on the speaker. That matters
because the speaker is exactly what the box lands on in the dark, when a
child has pulled dead headphones off: until now the loudest event this
box could produce was the one action it offers in that moment.

Pins: the cap applies on the local pcm only, at USE and never written
back (volume.json must keep meaning "what the user chose"), on every
path that can put audio on the speaker — a fresh mpv, the live retarget
of a playing one, and (since 2026-09-02, NEW-1) a Spotify session's
_apply_box_volume, which used to reach the HAT uncapped — and 0
disables it. tests/spotify_volume_before_play.py pins the Spotify
ordering (cap before /player/play)."""
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

from vibb import sysinfo  # noqa: E402
from vibb.output import OUTPUT_PCMS, local_volume  # noqa: E402

LOCAL, BT = OUTPUT_PCMS["local"], OUTPUT_PCMS["bt"]

# 1. the rule itself: capped on the speaker, untouched on bluetooth
assert local_volume(90, LOCAL, 35) == 35
assert local_volume(90, BT, 35) == 90, "headphones keep their own level"
assert local_volume(20, LOCAL, 35) == 20, "already quieter: leave it alone"
assert local_volume(90, LOCAL, 0) == 90, "cap 0 = disabled"
assert local_volume(100, LOCAL, 100) == 100
# AM-7: a safety drift in the audio policy caps EVERY output
assert local_volume(90, BT, 35, everywhere=True) == 35
assert local_volume(90, LOCAL, 35, everywhere=True) == 35
assert local_volume(90, BT, 0, everywhere=True) == 90, "cap 0 still disables"
print("1. capped on the speaker only, never raised, 0 disables OK")

# 2. it is a real setting, with a default and a range
assert sysinfo.SETTING_SPECS["local_fallback_cap"] == (35, 0, 100)
assert sysinfo.load_settings()["local_fallback_cap"] == 35
sysinfo.update_settings({"local_fallback_cap": 20})
assert sysinfo.load_settings()["local_fallback_cap"] == 20
try:
    sysinfo.update_settings({"local_fallback_cap": 101})
    raise AssertionError("out of range must be rejected")
except ValueError:
    pass
print("2. settable, defaulted, range-checked OK")

# 3. a fresh mpv on the speaker is capped — and the SAVED knob is not
#    touched, so the headphone level is still there when they come back
import player  # noqa: E402

with open(os.path.join(TMP, "volume.json"), "w") as f:
    json.dump({"volume": 90}, f)
src = open(player.__file__, encoding="utf-8").read()
assert "volume = local_volume(" in src
i_cap = src.index("volume = local_volume(")
i_spawn = src.index("proc = subprocess.Popen(mpv_command(")
assert i_cap < i_spawn, "the cap must be applied before mpv starts"
assert "json.dump({\"volume\"" not in src[i_cap:i_spawn], \
    "the cap must never be written back to volume.json"
with open(os.path.join(TMP, "volume.json")) as f:
    assert json.load(f)["volume"] == 90
print("3. fresh mpv capped at use; the saved knob is untouched OK")

# 3b. the Spotify engine gets the same cap, in the one function every
#     Spotify landing (spawn, blip resume, rebuild) goes through
i_abv = src.index("def _apply_box_volume():")
abv = src[i_abv:src.index("\ndef ", i_abv + 1)]
assert "local_volume(" in abv and "local_fallback_cap" in abv, \
    "Spotify must be capped for the HAT like mpv is (NEW-1)"
assert abv.index("local_volume(") < abv.index("/player/volume"), \
    "cap the value BEFORE it is POSTed"
print("3b. Spotify's box-volume apply is capped for the speaker OK")

# 4. the LIVE retarget — the loudest path, since it moves a playing
#    child onto the amplifier while mpv keeps the headphone softvol
import daemon  # noqa: E402

dsrc = open(daemon.__file__, encoding="utf-8").read()
i_ret = dsrc.index('["set_property", "audio-device", f"alsa/{pcm}"]')
window = dsrc[i_ret:i_ret + 900]   # the comment above it is long
assert "_local_volume(" in window and '"volume", v' in window, \
    "a live retarget must re-apply the cap for the device it landed on"
assert "if mpv_switched:" in window, "only when the retarget succeeded"
print("4. live retarget re-applies the cap for its new device OK")

# 5. the cap reads the SAVED knob, not mpv's current level — otherwise
#    each retarget would ratchet the number down
assert "def _volume_setting(self):" in dsrc
assert "self._volume_setting()" in window
print("5. the cap is computed from the saved knob, so it cannot ratchet OK")

# 6. reachable from both surfaces, or it is unsettable in practice
assert "local_fallback_cap" in open(
    os.path.join(REPO, "pi/web/app.js"), encoding="utf-8").read()
assert "set-localcap" in open(
    os.path.join(REPO, "pi/web/index.html"), encoding="utf-8").read()
assert "local_fallback_cap" in open(
    os.path.join(REPO, "pi/ui.py"), encoding="utf-8").read()
print("6. exposed in the PWA and on the screen OK")

print("\nLOCAL VOLUME CAP OK — the fallback speaker can no longer be the "
      "loudest thing in a dark bedroom.")
