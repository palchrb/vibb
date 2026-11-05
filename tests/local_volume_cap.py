#!/usr/bin/env python3
"""ONE volume cap for the box, on every output, at every landing.

Owner 2026-09-05 (first Zero): the separate built-in-speaker limit
(`local_fallback_cap`) is gone — `volume_cap`, the child-safety ceiling the
knob already obeys, is the only cap, and it is applied at USE on every
path that lands audio anywhere — a fresh mpv, the live retarget of a
playing one, and a Spotify session's _apply_box_volume (NEW-1) — never
written back (volume.json must keep meaning "what the user chose").
The headphones-die -> speaker fallback therefore lands at the box cap.
tests/spotify_volume_before_play.py pins the Spotify ordering."""
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
from vibb.output import local_volume  # noqa: E402

# 1. the rule itself: min(stored, cap), whatever the output; 0 disables
assert local_volume(90, 35) == 35
assert local_volume(20, 35) == 20, "already quieter: leave it alone"
assert local_volume(90, 0) == 90, "cap 0 = disabled"
assert local_volume(100, 100) == 100
print("1. min(stored, cap) on every output, never raised, 0 disables OK")

# 2. it is THE setting: volume_cap, defaulted, range-checked — and the old
#    built-in-only key is gone (an old settings.json carrying it is ignored)
assert sysinfo.SETTING_SPECS["volume_cap"] == (100, 30, 100)
assert "local_fallback_cap" not in sysinfo.SETTING_SPECS
assert sysinfo.load_settings()["volume_cap"] == 100
sysinfo.update_settings({"volume_cap": 70})
assert sysinfo.load_settings()["volume_cap"] == 70
try:
    sysinfo.update_settings({"volume_cap": 101})
    raise AssertionError("out of range must be rejected")
except ValueError:
    pass
print("2. volume_cap is the one cap: settable, defaulted, range-checked OK")

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
assert "local_volume(" in abv and "volume_cap" in abv, \
    "Spotify must be capped at the landing like mpv is (NEW-1)"
assert abv.index("local_volume(") < abv.index("/player/volume"), \
    "cap the value BEFORE it is POSTed"
print("3b. Spotify's box-volume apply is capped at the landing OK")

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

# 6. the old built-in-only limit is gone from both surfaces, the one cap stays
assert "local_fallback_cap" not in open(
    os.path.join(REPO, "pi/web/app.js"), encoding="utf-8").read()
assert "set-localcap" not in open(
    os.path.join(REPO, "pi/web/index.html"), encoding="utf-8").read()
assert "local_fallback_cap" not in open(
    os.path.join(REPO, "pi/ui.py"), encoding="utf-8").read()
assert "set-cap" in open(
    os.path.join(REPO, "pi/web/index.html"), encoding="utf-8").read()
print("6. one cap in the PWA and on the screen, the speaker-only one gone OK")

print("\nVOLUME CAP OK — one ceiling, every output, every landing.")
