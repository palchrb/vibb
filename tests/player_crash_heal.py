#!/usr/bin/env python3
"""Gate the crashed-player healer (review 2026-07-18 R5): a player child
that DIES with a nonzero rc — OOM kill, segfault — while something was
audibly playing must be respawned (the 3s bookmark resumes in place),
because the stall watchdog otherwise stands down on a dead child and
the box sits silent with 'playing' on the screen. Guards under test:
never on a clean exit or a deliberate stop, never against the persisted
intent (paused / other target / nothing published), never outside the
BT_RESUME_S no-surprise-audio window, retried while the output is away
but only inside that window, max 2 respawns per boot, and never when
another path already restarted playback."""
import json
import os
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = tempfile.mkdtemp()
os.environ["VIBB_STATE"] = STATE
os.environ["VIBB_LIBRARY"] = os.path.join(STATE, "lib.json")
os.environ.setdefault("VIBB_CACHE", tempfile.mkdtemp())
os.environ["VIBB_STALL_POLL"] = "3600"  # park the import-time watchdog
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

TARGET = "https://feeds.example.com/show"
SPAWNED = []


class FakeChild:
    def __init__(self, rc=None):
        self.rc = rc

    def poll(self):
        return self.rc


def fake_spawn(self, target, **kw):
    SPAWNED.append(target)
    self.child = FakeChild(None)  # the respawned player is alive
    self.child_started = time.monotonic()


daemon.Orchestrator._spawn = fake_spawn
daemon._audio_ready = lambda: True
orch = daemon.ORCH


def publish(target=TARGET, paused=False):
    with open(daemon.NOW_FILE, "w") as f:
        json.dump({"target": target, "paused": paused}, f)


def crash(rc=-9):
    orch.child = FakeChild(rc)
    orch.target, orch.source = TARGET, "mpv"
    orch.child_started = time.monotonic() - 60
    SPAWNED.clear()
    orch._crash_respawns = 0


def wait_for(what, pred, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise SystemExit(f"TIMEOUT waiting for: {what}")


# 1. OOM-killed child + published 'playing' intent -> one respawn
crash()
publish()
since = orch._heal_crashed_child(0.0)
assert SPAWNED == [TARGET], f"crash while playing must respawn: {SPAWNED}"
assert since == 0.0 and orch._crash_respawns == 1
print("1. crashed child while audibly playing respawns once OK")

# 2. clean exit (queue finished / graceful TERM) -> never
crash(rc=0)
assert orch._heal_crashed_child(0.0) == 0.0 and not SPAWNED
print("2. clean exit rc=0 never respawns OK")

# 3. deliberate stop cleared the child -> nothing to heal
crash()
orch.child = None
assert orch._heal_crashed_child(0.0) == 0.0 and not SPAWNED
print("3. deliberate stop (child cleared) never respawns OK")

# 4. the kid had PAUSED -> a respawn would be surprise audio: stand down
crash()
publish(paused=True)
since = orch._heal_crashed_child(0.0)
assert not SPAWNED and since > 0.0
print("4. paused intent never respawns (no surprise audio) OK")

# 5. published intent belongs to ANOTHER target -> stand down
crash()
publish(target="https://other.example.com/x")
assert orch._heal_crashed_child(0.0) > 0.0 and not SPAWNED
print("5. stale now-playing for another target never respawns OK")

# 6. no persisted intent at all -> never guess toward audio
crash()
os.remove(daemon.NOW_FILE)
assert orch._heal_crashed_child(0.0) > 0.0 and not SPAWNED
print("6. missing published intent never respawns OK")

# 7. output away: retry-not-respawn, then respawn when it returns
# (a crash during a 30s speaker blip still heals, like BT blips do)
crash()
publish()
daemon._audio_ready = lambda: False
since = orch._heal_crashed_child(0.0)
assert since > 0.0 and not SPAWNED, "must wait for the output"
since2 = orch._heal_crashed_child(since)
assert since2 == since, "the first-seen-dead stamp must be kept"
daemon._audio_ready = lambda: True
assert orch._heal_crashed_child(since) == 0.0 and SPAWNED == [TARGET]
print("7. output away: retries within the window, heals on return OK")

# 8. crash detected OUTSIDE the BT_RESUME_S window -> stands down for
# good (the ghost state on the screen is the honest fallback)
crash()
publish()
old = time.monotonic() - daemon.BT_RESUME_S - 1
assert orch._heal_crashed_child(old) == old and not SPAWNED
print("8. expired window never respawns OK")

# 9. max 2 per boot: the third crash is left alone
crash()
publish()
orch._crash_respawns = 2
assert orch._heal_crashed_child(0.0) > 0.0 and not SPAWNED
print("9. respawn cap (2 per boot) holds OK")

# 10. spotify source: go-librespot owns the audio, a dead player.py
# child is not a silence signal there
crash()
orch.source = "spotify"
publish()
assert orch._heal_crashed_child(0.0) > 0.0 and not SPAWNED
print("10. spotify source never respawns OK")

# 11. another path (play tap / blip resume) replaced the child between
# the healer's check and its respawn -> no double spawn. The audio probe
# runs between the two locked sections — race an interloper in there.
crash()
publish()


def swap_and_ready():  # a play tap lands mid-probe
    orch.child = FakeChild(None)
    orch.child_started = time.monotonic()
    return True


daemon._audio_ready = swap_and_ready
assert orch._heal_crashed_child(0.0) == 0.0 and not SPAWNED, \
    "a concurrently respawned child must never be doubled"
daemon._audio_ready = lambda: True
print("11. a concurrent respawn is never doubled OK")

# 12. the real watchdog loop drives the healer: one scripted tick with a
# crashed child on disk-published intent -> the loop respawns it
crash()
publish()
gate = threading.Semaphore(0)
daemon._tick = lambda s: gate.acquire()
threading.Thread(target=orch._stall_watchdog, daemon=True).start()
gate.release()  # one poll
wait_for("watchdog-driven respawn", lambda: SPAWNED)
assert SPAWNED == [TARGET]
print("12. the stall watchdog's dead-child branch runs the healer OK")

# 13. rc 75 = the player declined to spawn into a void (pipewire: the
# sink node was not there yet, AM-9). Respawned when the output is ready,
# but NEVER charged to the 2-per-boot budget — two node flaps must not
# silence the box. A real crash right after still charges.
crash(rc=daemon._audio.SINK_WAIT_EXIT)
publish()
assert orch._heal_crashed_child(0.0) == 0.0 and SPAWNED == [TARGET]
assert orch._crash_respawns == 0, "rc 75 is not a crash"
crash(rc=daemon._audio.SINK_WAIT_EXIT)
publish()
orch._crash_respawns = 2                   # budget already spent by real crashes
assert orch._heal_crashed_child(0.0) > 0.0 and not SPAWNED, \
    "an exhausted budget still stands — rc 75 only never SPENDS it"
crash(rc=-9)
publish()
assert orch._heal_crashed_child(0.0) == 0.0 and orch._crash_respawns == 1
print("13. the sink-wait exit (75) respawns without charging the budget OK")

print("PLAYER CRASH HEAL OK — a died-mid-audiobook player comes back on "
      "its own, and nothing ever restarts into a pause, another target, "
      "a missing output, or beyond the no-surprise-audio window.")
