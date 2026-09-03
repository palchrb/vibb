#!/usr/bin/env python3
"""AM-10: asound.conf now has TWO writers under pipewire — bt.py's
_route_alsa (connect/route) and the daemon's ensure_bt_route (btwatchd's
announce) — and neither may ever leave the file empty or truncated: an
empty asound.conf is both pcms gone, every output silent, and nothing
heals it (bt_state_fsync.py). Pinned:

  1. write_asound uses a per-process tmp name — two processes writing at
     once never share one (one truncating the other's data before its
     rename was the exact hole)
  2. 200 concurrent writes from two processes: the file is ALWAYS one of
     the complete texts, never empty, never a mix
  3. ensure_bt_route steps aside (no write, returns False) while another
     process holds the radio lock — it must not block the HTTP thread
  4. ensure_bt_route is idempotent: same node -> no write; a new node ->
     one write; no node in the graph -> no write; a dump that lacks the
     HAT keeps the HAT pin already in the file
"""
import multiprocessing
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["VIBB_ASOUND"] = os.path.join(TMP, "asound.conf")
os.environ["VIBB_BT_LOCKFILE"] = os.path.join(TMP, "bt.lock")
os.environ["VIBB_BT_FILE"] = os.path.join(TMP, "bt-headset")
os.environ["VIBB_STATE"] = TMP
os.environ["VIBB_RUN"] = TMP
os.environ["VIBB_AUDIO_STACK"] = "pipewire"
sys.path.insert(0, os.path.join(REPO, "pi"))

from vibb import audio, bt  # noqa: E402

bt.log = lambda *a: None
audio.log = lambda *a: None
MAC = "AA:BB:CC:DD:EE:FF"
TEXT_A = audio.asound_text(MAC, "bluez_output.A.1", "alsa_output.hat")
TEXT_B = audio.asound_text(MAC, "bluez_output.B.1", "alsa_output.hat")


def hammer(text, n):
    for _ in range(n):
        bt.write_asound(text)


# 1 + 2. two processes, per-pid tmp, never a bad file
src = open(bt.__file__, encoding="utf-8").read()
assert 'f"{ASOUND}.tmp.{os.getpid()}"' in src
pa = multiprocessing.Process(target=hammer, args=(TEXT_A, 200))
pb = multiprocessing.Process(target=hammer, args=(TEXT_B, 200))
pa.start(); pb.start()
bad = 0
seen = 0
while pa.is_alive() or pb.is_alive():
    try:
        body = open(bt.ASOUND).read()
    except OSError:
        continue
    seen += 1
    if body not in (TEXT_A, TEXT_B):
        bad += 1
pa.join(); pb.join()
assert bad == 0, f"{bad} of {seen} reads saw a truncated/mixed asound.conf"
assert open(bt.ASOUND).read() in (TEXT_A, TEXT_B)
assert not [f for f in os.listdir(TMP) if ".tmp" in f], "no tmp left behind"
print(f"1-2. two writers, {seen} concurrent reads, never a bad file OK")

# 3. the lock is honoured without blocking
DUMP = [{"id": 1, "type": "PipeWire:Interface:Node", "info": {"props": {
    "node.name": "bluez_output.C.1", "media.class": "Audio/Sink",
    "api.bluez5.address": MAC}}},
    {"id": 2, "type": "PipeWire:Interface:Node", "info": {"props": {
        "node.name": "alsa_output.hat", "media.class": "Audio/Sink",
        "alsa.card_name": "snd_rpi_hifiberry_dac"}}}]
audio.pw_dump = lambda timeout=3.0: DUMP
held = bt.acquire_process_lock()          # "bt.py owns the radio"
before = open(bt.ASOUND).read()
assert audio.ensure_bt_route(MAC) is False
assert open(bt.ASOUND).read() == before, "no write while another process holds the lock"
held.close()
print("3. ensure_bt_route steps aside while the radio lock is held OK")

# 4. idempotence + HAT pin kept
assert audio.ensure_bt_route(MAC) is True
body = open(bt.ASOUND).read()
assert 'playback_node "bluez_output.C.1"' in body and 'playback_node "alsa_output.hat"' in body
assert audio.ensure_bt_route(MAC) is False, "same node: no write"
DUMP[0]["info"]["props"]["node.name"] = "bluez_output.C.a2dp-sink"
del DUMP[1]                                # this dump lacks the HAT
assert audio.ensure_bt_route(MAC) is True, "renamed node: one write"
body = open(bt.ASOUND).read()
assert 'playback_node "bluez_output.C.a2dp-sink"' in body
assert 'playback_node "alsa_output.hat"' in body, "the HAT pin survives a HAT-less dump"
DUMP.clear()
assert audio.ensure_bt_route(MAC) is False, "no node in the graph: nothing true to write"
assert audio.ensure_bt_route("") is False
os.environ["VIBB_AUDIO_STACK"] = "bluealsa"; audio._stack[0] = None
assert audio.ensure_bt_route(MAC) is False, "never under bluealsa"
print("4. idempotent, rename rewrites once, HAT pin kept, no-node no-op OK")

print("\nall asound_second_writer checks passed")
