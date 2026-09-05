#!/usr/bin/env python3
"""I7: the WirePlumber fragment install.sh writes — parsed from
pi/audio-stack.sh with its variables at their defaults. Pinned: suspend
120 on bluez nodes ONLY (the keep-alive successor, SEVERE-1), 5 on the
HAT; sbc only, no XQ, no mSBC; roles follow WP_ROLES; hw-volume is left
at PipeWire's own default and must never be set (AM-35: the key is a
quirk-list override, and 1.4.2 already covers A2DP_SINK); no dummy AVRCP player
(vibb-mpris is the only MediaPlayer1); the profile is the shipped
main-embedded with the two linking hooks disabled (AM-22/23)."""
import os
import re
import subprocess
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SH = os.path.join(REPO, "pi", "audio-stack.sh")


def render(**env):
    root = tempfile.mkdtemp()
    script = f"""
write_if_changed() {{ mkdir -p "$(dirname "$1")"; cat > "$1"; }}
AUDIO_STACK=pipewire
. {SH}
_as_write_fragments
cat "$VIBB_FS_ROOT/etc/wireplumber/wireplumber.conf.d/50-vibb.conf"
"""
    e = dict(os.environ, VIBB_FS_ROOT=root, **env)
    return subprocess.run(["bash", "-c", script], env=e, capture_output=True,
                          text=True, check=True).stdout


frag = render()
bluez_rule = re.search(r'node\.name = "~bluez_output\.\*".*?\}\s*\}', frag, re.S).group(0)
alsa_rule = re.search(r'node\.name = "~alsa_output\.\*".*?\}\s*\}', frag, re.S).group(0)
assert "session.suspend-timeout-seconds = 120" in bluez_rule
assert "session.suspend-timeout-seconds = 5" in alsa_rule
assert "120" not in alsa_rule, "the HAT keeps the fast release"
print("1. suspend 120 on bluez nodes only, 5 on the HAT OK")

for line in ("bluez5.codecs             = [ sbc ]", "bluez5.enable-sbc-xq      = false",
             "bluez5.enable-msbc        = false", "bluez5.dummy-avrcp-player = false",
             "bluez5.roles              = [ a2dp_source ]",
             "bluez5.autoswitch-profile = false", "bluetooth.autoswitch-to-headset-profile = false", "device.routes.default-sink-volume       = 1.0"):
    assert line in frag, line
assert not re.search(r"^\s*bluez5\.(enable-)?hw-volume\s*=", frag, re.M), \
    "AM-35: hw-volume is a quirk-list override — setting it either way is wrong"
print("2. sbc only, no XQ/mSBC, hw-volume left at its default, local source role (AM-57), "
      "no dummy player OK")

flipped = render(WP_ROLES="a2dp_sink")
assert "bluez5.roles              = [ a2dp_sink ]" in flipped
print("3. WP_ROLES flips in one place OK")

assert "main-embedded = {" in frag
assert "hooks.linking.target.find-default = disabled" in frag
assert "hooks.linking.target.find-best    = disabled" in frag
assert not re.search(r"policy\.linking\.standard|policy\.default-nodes|node\.stream\.restore\s*=|device\.restore\s*=", frag), \
    "only names from the distro's provides inventory"
print("4. shipped main-embedded profile + the two linking hooks off OK")

print("\nall wp_policy_fragment checks passed")
