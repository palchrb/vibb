#!/usr/bin/env python3
"""Gate the kill guarantee (field 2026-08-03): a TERM-immune player with
a blocked grandchild must not survive _stop_child.

The real incident: mpv sat blocked in an ALSA write to a dead BT
transport; SIGTERM did nothing, the daemon's wait(10)+SIGKILL took out
only the python parent, and the orphaned mpv kept the bluealsa PCM held
— every later spawn got 'Device or resource busy' until reboot. The fix
is layered: player._stop arms an 8s SIGKILL for mpv (so the bookmark
still flushes), and _stop_child SIGKILLs the whole process group as the
backstop for a wedged python parent. This gate exercises the backstop
with REAL processes on a short timescale.
Under the PipeWire stack this matters MORE, not less (NEW-7): bluealsa's
exclusive pcm made an orphan mpv block silently; PipeWire mixes, so an
orphan on a live sink keeps PLAYING under the next spawn and holds the
node out of suspend (no transport release, no battery saving).
"""
import os
import signal
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = tempfile.mkdtemp()
os.environ["VIBB_STATE"] = STATE
os.environ["VIBB_LIBRARY"] = os.path.join(STATE, "lib.json")
os.environ.setdefault("VIBB_CACHE", tempfile.mkdtemp())
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

# a TERM-immune "player" that spawns a TERM-immune "mpv" and prints its
# pid — the exact shape of the wedge (both blocked, neither exits)
WEDGED = r"""
import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, "-c",
    "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
    "time.sleep(300)"])
print(child.pid, flush=True)
time.sleep(300)
"""


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


# 1. wedged player + wedged grandchild: _stop_child must reap BOTH, and
#    return only once the group is actually gone (the PCM-free guarantee)
proc = subprocess.Popen([sys.executable, "-c", WEDGED],
                        stdout=subprocess.PIPE, text=True,
                        start_new_session=True)
grandchild = int(proc.stdout.readline())
assert alive(proc.pid) and alive(grandchild)

o = daemon.Orchestrator.__new__(daemon.Orchestrator)
o.child = proc
# shrink the TERM patience for the gate: monkeypatch wait timeout via a
# wrapper child object is overkill — the real 10s is fine to spend once,
# but keep the gate fast by pre-checking the escalation path directly
t0 = time.monotonic()
daemon.Orchestrator._stop_child(o)
took = time.monotonic() - t0
assert o.child is None
assert not alive(proc.pid), "the wedged player must be dead"
assert not alive(grandchild), \
    "the blocked grandchild must die with the group (PCM holder!)"
assert took < 20, f"stop took {took:.0f}s"
print(f"1. wedged player + grandchild reaped as a group in {took:.1f}s OK")

# 2. a clean, cooperative player: no group massacre needed, fast path
proc = subprocess.Popen([sys.executable, "-c",
                         "import time;time.sleep(300)"],
                        start_new_session=True)
o.child = proc
t0 = time.monotonic()
daemon.Orchestrator._stop_child(o)
took = time.monotonic() - t0
assert not alive(proc.pid) and took < 5, f"clean stop took {took:.1f}s"
print(f"2. cooperative player stops on SIGTERM in {took:.1f}s OK")

# 3. child already gone (raced its own exit): no crash, no hang
proc = subprocess.Popen([sys.executable, "-c", "pass"],
                        start_new_session=True)
proc.wait()
o.child = proc
daemon.Orchestrator._stop_child(o)
assert o.child is None
print("3. already-exited child: quiet no-op OK")

# 4. the spawn side actually creates the process group the kill relies
#    on, and the player side arms the 8s mpv escalation (the layer that
#    preserves the bookmark flush)
import inspect  # noqa: E402
src = inspect.getsource(daemon.Orchestrator._spawn)
assert "start_new_session=True" in src, \
    "_spawn must give the player its own process group"
psrc = open(os.path.join(REPO, "pi", "player.py")).read()
assert "threading.Timer(8, proc.kill)" in psrc, \
    "player._stop must arm the mpv SIGKILL escalation"
print("4. spawn creates the group; player arms the mpv escalation OK")

print("\nall stop_child_group_kill checks passed")
