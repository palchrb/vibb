#!/usr/bin/env python3
"""Gate the idle auto-shutdown: playback resets the countdown, button
presses reset it too (a kid browsing without playing must never have
the box die in their hands), a paused/stopped box counts down and
powers off at the limit, 'never' (0) both disables AND stops the count
from accumulating behind the parent's back — flipping auto-off back on
after hours of tomgang must start a FRESH countdown, not power off
within the minute — and a future-mtime activity marker (clock jump) is
no signal, exactly like the radio markers."""
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = tempfile.mkdtemp()
os.environ["VIBB_RUN"] = RUN
os.environ["VIBB_SETTINGS"] = os.path.join(RUN, "settings.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import json  # noqa: E402

import idle  # noqa: E402
from vibb import paths  # noqa: E402

CALLS = []
idle.subprocess.run = lambda argv, **kw: CALLS.append(argv[0])
PLAYING = [True]
idle.daemon_playing = lambda: PLAYING[0]
SSH = [False]
idle.ssh_active = lambda: SSH[0]  # hermetic — no real `ss` in the gate


def set_limit(minutes):
    with open(os.environ["VIBB_SETTINGS"], "w") as f:
        json.dump({"idle_shutdown_min": minutes}, f)


set_limit(2)  # 2 min limit -> third idle cycle (120s) powers off

# 1. playback keeps resetting the countdown
assert idle._cycle(999) == 0
print("1. active playback resets the countdown OK")

# 2. paused/stopped: counts down and powers off at the limit
PLAYING[0] = False
n = idle._cycle(0)
assert n == idle.CHECK_S and CALLS == []
assert idle._cycle(n) is None  # 120s idle == the 2 min limit
# The backup runs FIRST, on the way down: the box has been idle for the
# whole timeout, so nothing is playing, the radio is free and the session's
# bookmarks are fresh. That is the moment worth backing up at — no periodic
# timer can guess it (owner 2026-08-17). Its own gates make almost every
# shutdown a no-op.
# systemctl, not python: a child of vibb-idle inherits vibb-idle's cgroup,
# so running the module directly bypassed the memory ceiling and CPU pinning
# that vibb-backup.service carries — on the one path that actually runs.
assert CALLS == ["systemctl", "logger", "poweroff"], CALLS
print("2. idle box backs up via the capped unit, then powers off OK")

# 2b. and a backup that fails or hangs must NEVER keep the box awake —
#     a dead network would otherwise burn battery at every shutdown.
CALLS.clear()
_real_run = idle.subprocess.run


def _boom(argv, **kw):
    CALLS.append(argv[0])
    if argv[0] == "systemctl":
        raise idle.subprocess.TimeoutExpired("backup", 180)


idle.subprocess.run = _boom
try:
    assert idle._cycle(n) is None, "a failed backup must not stop the poweroff"
    assert CALLS == ["systemctl", "logger", "poweroff"], CALLS
finally:
    idle.subprocess.run = _real_run
print("2b. a hung or failed backup never blocks the shutdown OK")

# 3. a fresh button press counts as activity — browsing hands never
# have the box shut down under them
CALLS.clear()
paths._ACT_TOUCHED[0] = 0.0
paths.touch_activity()
assert idle._cycle(999) == 0 and CALLS == []
print("3. fresh button press resets the countdown OK")

# 4. a stale press does not: age it beyond the freshness window
old = time.time() - idle.ACTIVITY_FRESH_S - 5
os.utime(paths.ACTIVITY_FILE, (old, old))
assert idle._cycle(0) == idle.CHECK_S
print("4. stale button press no longer counts OK")

# 5. a future-mtime marker (clock jumped backwards) is no signal
future = time.time() + 3600
os.utime(paths.ACTIVITY_FILE, (future, future))
assert idle._cycle(0) == idle.CHECK_S
print("5. future-mtime marker (clock jump) is ignored OK")

# 6. 'never' (0): no poweroff AND no accumulation — hours of tomgang
# then flipping auto-off back on starts fresh, not instant shutdown
os.remove(paths.ACTIVITY_FILE)
set_limit(0)
assert idle._cycle(999999) == 0 and CALLS == []
set_limit(2)
assert idle._cycle(0) == idle.CHECK_S and CALLS == []
print("6. 'never' disables and drains the counter — re-enable starts fresh OK")

# 7. daemon down -> the direct source probes decide
set_limit(30)
idle.daemon_playing = lambda: None
idle.spotify.playing = lambda: True
idle.mpv.playing = lambda: False
assert idle._cycle(60) == 0
idle.spotify.playing = lambda: False
assert idle._cycle(60) == 2 * idle.CHECK_S
print("7. daemon down falls back to direct source probes OK")

# 8. the marker helpers themselves: throttled writes, epoch mtime
paths._ACT_TOUCHED[0] = 0.0
paths.touch_activity()
first = paths.last_activity()
assert first > 0
time.sleep(0.05)
paths.touch_activity()  # throttled — must NOT rewrite within 10s
assert paths.last_activity() == first
print("8. activity marker writes once per burst (throttled) OK")

# 9. an ssh login HOLDS auto-off: powering the box off under someone
#    debugging cost an evening (field 2026-08-03 — the 5-min idle fired
#    mid-journalctl and the wedged pisugar poweroff needed a hard cut).
#    Logout resumes the countdown from zero.
PLAYING[0] = False
SSH[0] = True
CALLS.clear()
stale = time.time() - idle.ACTIVITY_FRESH_S - 5  # test 8 touched the
os.utime(paths.ACTIVITY_FILE, (stale, stale))    # marker — age it out
assert idle._cycle(999999) == 0 and CALLS == [], \
    "an active ssh session must hold the countdown at zero"
SSH[0] = False
assert idle._cycle(0) == idle.CHECK_S, "logout must resume counting"
print("9. active ssh session holds auto-off; logout resumes OK")

print("IDLE SHUTDOWN OK — playback or hands on the box keep it alive, "
      "'never' never counts, and it dies exactly at the parent's limit.")

# 9. the Soloist updater's slot on the same way down (PLAN-pipewire-soloist
#    D1/AM-50): only when its unit exists (the soloist engine), AFTER the
#    backup (irreplaceable first), bounded, and a hung updater never
#    blocks the poweroff either
import tempfile as _tf  # noqa: E402

unit = os.path.join(_tf.mkdtemp(), "vibb-soloist-update.service")
open(unit, "w").write("[Service]\n")
idle.UPDATE_UNIT = unit
CALLS2 = []
idle.subprocess.run = lambda argv, **kw: CALLS2.append(" ".join(argv[:4]))
PLAYING[0] = False
SSH[0] = False
set_limit(2)
n = idle._cycle(0)
assert idle._cycle(n) is None
assert CALLS2[:2] == ["systemctl start --wait vibb-backup.service",
                      "systemctl start --wait vibb-soloist-update.service"], CALLS2
assert CALLS2[-1] == "poweroff"
CALLS2.clear()


def _hang(argv, **kw):
    CALLS2.append(" ".join(argv[:4]))
    if "vibb-soloist-update.service" in argv:
        raise idle.subprocess.TimeoutExpired("update", idle.UPDATE_MAX_S)


idle.subprocess.run = _hang
n = idle._cycle(0)
assert idle._cycle(n) is None and CALLS2[-1] == "poweroff", CALLS2
idle.UPDATE_UNIT = "/nonexistent/vibb-soloist-update.service"
CALLS2.clear()
idle.subprocess.run = lambda argv, **kw: CALLS2.append(" ".join(argv[:4]))
n = idle._cycle(0)
assert idle._cycle(n) is None
assert not any("soloist-update" in c for c in CALLS2), "no unit (go-librespot box): no slot"
print("9. updater slot: after the backup, only when its unit exists, a hang never blocks poweroff OK")
