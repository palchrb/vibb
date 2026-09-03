#!/usr/bin/env python3
"""Pin mpv's launch argv. The startup 'trims' (--ao=alsa, --no-config,
--load-scripts=no, --no-ytdl) are safe to ADD for a faster cold start, but
the audio-critical flags must NEVER be dropped by a future trim:
--audio-samplerate=44100 / --audio-channels=stereo force the 44.1kHz stereo
resample without which low-bitrate audiobooks play SILENTLY over A2DP
(player.py's own comment records the field bug). This gate makes that
regression impossible to land silently.
--audio-buffer=0.5 under PipeWire (plan §I): its CLIENT-side job survives —
the decoder runs half a second ahead of the sink through pipewire-alsa's
ring — but the graph->radio hop is governed by the quantum and the bluez5
node latency, tuned in the WirePlumber fragment, not here.
"""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(REPO, "pi"))

import player  # noqa: E402

cmd = player.mpv_command(["/cache/show/e1.mp3", "https://x/e2.mp3"],
                         40, "/run/vibb-mpv.sock", "vibb_bt")

# 1. the two flags that keep audiobooks audible over BT are present
assert "--audio-samplerate=44100" in cmd, "44.1kHz resample flag missing"
assert "--audio-channels=stereo" in cmd, "stereo resample flag missing"
print("1. the 44.1kHz/stereo resample flags are present OK")

# 2. output routing + volume + ipc + the queue all make it through
assert "--audio-device=alsa/vibb_bt" in cmd, "output pcm not routed"
assert "--volume=40" in cmd, "box volume not applied"
assert "--input-ipc-server=/run/vibb-mpv.sock" in cmd, "ipc socket missing"
assert cmd[-2:] == ["/cache/show/e1.mp3", "https://x/e2.mp3"], "queue lost"
print("2. output pcm, volume, ipc socket and the queue are all passed OK")

# 3. the startup trims are present (fast cold start) and harmless
for f in ("--ao=alsa", "--no-config", "--load-scripts=no", "--no-ytdl"):
    assert f in cmd, f"startup trim {f} missing"
assert "--no-video" in cmd and "--really-quiet" in cmd
print("3. startup trims present (ao=alsa, no-config, no scripts, no ytdl) OK")

# 4. the 0.5s audio ring (2026-07-29): over A2DP the default ~0.2s
# request gave a ~100ms device buffer — every coex hiccup on the shared
# radio clicked audibly. NB: this is mpv's real ring, NOT bluealsa's
# 'delay' knob (which only adjusts the REPORTED delay, a common
# misreading).
assert "--audio-buffer=0.5" in cmd, "the RF-gap audio cushion is missing"
print("4. 0.5s audio buffer flag present OK")

# 5. bookmark resume launches PAUSED (silent until the seek lands —
# field 2026-07-30: episode start played audibly for a few seconds
# before the jump); a fresh start must NOT carry the flag. Deliberately
# --pause + IPC seek, NOT a per-file --start group: playlist wraps
# (next at the end, double-prev) re-enter slot 0 and a --start group
# would re-apply the stale bookmark mid-playthrough.
paused_cmd = player.mpv_command(["/cache/show/e1.mp3"], 40,
                                "/run/vibb-mpv.sock", "vibb_bt",
                                paused=True)
assert "--pause" in paused_cmd, "bookmark resume must load silent"
assert paused_cmd.index("--pause") < paused_cmd.index("/cache/show/e1.mp3"), \
    "--pause must precede the queue (global option)"
assert "--pause" not in cmd, "a fresh start must not launch paused"
assert "--start" not in " ".join(paused_cmd), \
    "never a --start group (playlist wraps re-apply it)"
print("5. resume loads paused, fresh start does not OK")

print("MPV LAUNCH FLAGS OK — audio-critical flags pinned; trims are additive.")
