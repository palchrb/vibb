#!/usr/bin/env python3
"""Gate the power-cut safety of the two BT state files that do NOT
self-heal. tmp+rename alone is not enough on ext4: the rename can reach
disk before the data, and a hard cut then leaves an EMPTY file — field
2026-08-04: a car-trip cut zeroed /etc/vibb/bt-headset right after a
follow-the-connector adopt had rewritten it; the box rebooted with
'btwatchd: target (none)', no BT icon, no remembered speaker. An empty
asound.conf is worse still: both pcms gone, every output silent. The
contract: DATA is fsynced before the rename — losing the rename keeps
the old value, never an empty file."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["VIBB_BT_FILE"] = os.path.join(TMP, "bt-headset")
os.environ["VIBB_ASOUND"] = os.path.join(TMP, "asound.conf")
sys.path.insert(0, os.path.join(REPO, "pi"))

from vibb import bt  # noqa: E402

bt.log = lambda *a: None
SYNCED = []
_real_fsync = os.fsync


def spy(fd):
    SYNCED.append(1)
    return _real_fsync(fd)


os.fsync = spy

# 1. the configured-speaker file
bt._persist_mac("AA:BB:CC:DD:EE:FF")
assert open(bt.MAC_FILE).read().strip() == "AA:BB:CC:DD:EE:FF"
assert len(SYNCED) == 1, "MAC write must fsync the data before the rename"
print("1. configured-speaker file fsyncs before rename OK")

# 2. asound.conf (the ALSA routing both outputs depend on)
bt._route_alsa("AA:BB:CC:DD:EE:FF")
body = open(bt.ASOUND).read()
assert "AA:BB:CC:DD:EE:FF" in body and "vibb_local" in body
assert len(SYNCED) == 2, "asound write must fsync the data before the rename"
# already-routed MAC: no rewrite, no extra fsync (SD wear)
bt._route_alsa("AA:BB:CC:DD:EE:FF")
assert len(SYNCED) == 2, "an unchanged route must not rewrite the file"
print("2. asound.conf fsyncs before rename; unchanged route no-ops OK")

# 3. the same contract under the PipeWire stack: the pcm NAMES survive,
#    the colon MAC stays in the file, the node is READ from the graph,
#    an unchanged route no-ops, a node RENAME rewrites exactly once, and
#    a not-yet-existing node leaves the old file alone
from vibb import audio  # noqa: E402

os.environ["VIBB_AUDIO_STACK"] = "pipewire"
audio._stack[0] = None
NODE = {"bt": "bluez_output.AA_BB_CC_DD_EE_FF.1", "local": "alsa_output.hat"}
audio.resolve_route = lambda mac, tries=10, delay=1.0: (NODE["bt"], NODE["local"])
SYNCED.clear()
bt._route_alsa("AA:BB:CC:DD:EE:FF")
body = open(bt.ASOUND).read()
assert "pcm.vibb_bt {" in body and "pcm.vibb_local {" in body
assert "AA:BB:CC:DD:EE:FF" in body, "the colon MAC stays for the idempotence check"
assert 'playback_node "bluez_output.AA_BB_CC_DD_EE_FF.1"' in body
assert 'playback_node "alsa_output.hat"' in body
assert "bluealsa" not in body
assert len(SYNCED) == 1
bt._route_alsa("AA:BB:CC:DD:EE:FF")
assert len(SYNCED) == 1, "same MAC + same node: no rewrite"
NODE["bt"] = "bluez_output.AA_BB_CC_DD_EE_FF.a2dp-sink"   # a package upgrade
bt._route_alsa("AA:BB:CC:DD:EE:FF")
assert len(SYNCED) == 2, "a node rename is a real route change: one rewrite"
assert 'playback_node "bluez_output.AA_BB_CC_DD_EE_FF.a2dp-sink"' in open(bt.ASOUND).read()
audio.resolve_route = lambda mac, tries=10, delay=1.0: (None, NODE["local"])
bt._route_alsa("AA:BB:CC:DD:EE:FF")
assert len(SYNCED) == 2, "no node yet: the old file stays (never truncated)"
assert not [f for f in os.listdir(TMP) if ".tmp" in f], "no tmp left behind"
os.fsync = _real_fsync
print("3. pipewire: names + MAC kept, node read, rename rewrites once, "
      "absent node leaves the file OK")

# 4. the tmp name carries the pid: two writers never share one
src = open(bt.__file__, encoding="utf-8").read()
assert 'f"{ASOUND}.tmp.{os.getpid()}"' in src, "per-process tmp name (AM-10)"
assert 'ASOUND + ".tmp"' not in src
print("4. per-process tmp name OK")

print("\nall bt_state_fsync checks passed")
