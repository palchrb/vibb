#!/usr/bin/env python3
"""install.sh's wiring of the audio-stack toggle (PLAN-pipewire-soloist
§H/§A, I5, AM-5): source pins, because install.sh only runs as root on
a box.

  1. the toggle is sourced and resolved BEFORE the package loop, and its
     packages join PKGS (bluealsa's stay: masked, never removed)
  2. the bluealsa asound.conf placeholder is written under bluealsa only;
     `audio_stack_route` runs AFTER the vibb package is installed (bt.py
     route needs it) and after step 4 (WirePlumber up)
  3. `audio_stack_apply` replaces the bare bluealsa enable; the keep-alive
     block is a bluealsa-only knob
  4. vibb-bt-reconnect orders after the endpoint owner
     (`audio_stack_endpoint_units`) and every audio client unit — daemon,
     go-librespot, bt-reconnect — carries `audio_stack_unit_env`
     (VIBB_AUDIO_STACK, PIPEWIRE_RUNTIME_DIR, PIPEWIRE_PROPS, VIBB_BT_GATE)
  5. bt-reconnect compares the shadow gate at 1/s (AM-12c)
"""
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(REPO, "pi", "install.sh"), encoding="utf-8").read()

# 1
i_src = src.index('. "$SCRIPT_DIR/audio-stack.sh"')
i_res = src.index("audio_stack_resolve", i_src)
i_pk = src.index("PKGS+=($(audio_stack_packages))", i_res)
i_loop = src.index('for p in "${PKGS[@]}"; do have_pkg')
assert i_src < i_res < i_pk < i_loop, "toggle sourced + resolved before the package loop"
assert "bluez-alsa-utils libasound2-plugin-bluez" in src[:i_loop], \
    "bluealsa's packages stay in PKGS on both stacks"
print("1. toggle sourced, packages joined before the apt loop OK")

# 2
i_ph = src.index("# Placeholder ALSA device: play.sh rewrites this")
assert 'if [[ $AUDIO_STACK == bluealsa ]]; then' in src[i_ph - 300:i_ph], \
    "the bluealsa placeholder must not clobber a pipewire asound.conf"
i_pkg = src.index('for f in "$SCRIPT_DIR"/vibb/*.py; do')
i_route = src.index("\naudio_stack_route\n")   # the call, not the comment naming it
i_apply = src.index("\naudio_stack_apply\n")
assert i_apply < i_pkg < i_route, "route after the package install and after apply"
print("2. placeholder bluealsa-only; route after package + apply OK")

# 3
assert "systemctl enable --now bluealsa.service 2>/dev/null" not in src, \
    "the bare bluealsa enable is audio_stack_apply's job now"
i_ka = src.index("# A2DP transport keep-alive: stock bluealsa tears the transport down")
assert 'if [[ $AUDIO_STACK == bluealsa ]]; then' in src[i_ka - 120:i_ka], \
    "keep-alive is a bluealsa knob"
print("3. apply replaces the enable; keep-alive guarded OK")

# 4
i_rc = src.index("Description=Vibb BT reconnect daemon")
rc_unit = src[i_rc:i_rc + 900]
assert "After=bluetooth.service $(audio_stack_endpoint_units)" in rc_unit
assert "$(audio_stack_unit_env)" in rc_unit
assert "bluealsa.service bluealsad.service" not in rc_unit.split("$(audio_stack")[0]
for marker in ("Description=go-librespot Spotify Connect daemon",
               "Description=Vibb orchestration daemon"):
    i = src.index(marker)
    assert "$(audio_stack_unit_env)" in src[i:i + 2500], f"{marker}: unit env lines"
print("4. endpoint-owner ordering + unit env on daemon/go-librespot/bt-reconnect OK")

# 5
assert "Environment=VIBB_BT_GATE_SHADOW_S=1" in rc_unit
print("5. bt-reconnect shadow cadence 1/s OK")

# 7. the ENGINE toggle wiring (AM-53)
i_eng = src.index("\nspotify_engine_resolve\n")
assert i_eng < i_loop, "engine resolve (and refuse) before anything is touched"
i_app = src.index("\nspotify_engine_apply\n")
assert i_pkg < i_app < src.index('echo "==> [6/8] Enabling services'), \
    "engine apply after the package install, before the services enable"
for marker in ("Description=Vibb orchestration daemon", "Description=Vibb BT reconnect daemon",
               "Description=Vibb idle auto-shutdown", "Description=Vibb media button daemon",
               "Description=Vibb RFID daemon"):
    i = src.index(marker)
    assert "$(spotify_engine_unit_env)" in src[i:i + 2500], f"{marker}: engine env"
assert "$(spotify_engine_go_config_env" in src and "Environment=VIBB_GO_CONFIG=" not in src, \
    "GO_CONFIG only through the conditional helper (none under soloist)"
print("7. engine toggle: resolve early, apply late, env on five units, GO_CONFIG conditional OK")

# the toggle file itself is what install.sh reads at runtime for extras
extra = open(os.path.join(REPO, "pi", "extra.sh"), encoding="utf-8").read()
assert 'AUDIO_STACK="$(cat "${VIBB_AUDIO_STACK_FILE:-/etc/vibb/audio-stack}"' in extra
assert "VIBB_SPOTIFY_ENGINE_FILE:-/etc/vibb/spotify-engine" in extra and \
    "stop go-librespot" not in extra and "start --no-block go-librespot" not in extra, \
    "extra.sh derives the engine unit, never names it"
print("6. extra.sh reads the same stack + engine files OK")

print("\nall install_unit_order checks passed")
