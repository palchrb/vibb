#!/usr/bin/env python3
"""Vibb idle auto-shutdown (installed as vibb-idle, on by default).

Powers the box off after N minutes without ACTIVITY, to save battery
when it's been left on and forgotten. The PiSugar's physical button
powers it back on (cold boot ~25-35s).

Activity is either of:
  - playback: go-librespot actively playing (Spotify) OR mpv running
    and not paused. A paused player counts as idle, so pausing and
    walking away eventually shuts down too.
  - hands on the box: the UI touches an advisory marker on every
    button press (paths.touch_activity), so a kid browsing the
    carousel without starting anything never has the box die mid-use.

The timeout comes from vibbd's settings.json (idle_shutdown_min,
re-read every cycle so the settings menu applies live; 0 = disabled).
While disabled NOTHING accumulates — flipping the setting back on
starts a fresh countdown instead of powering off within the minute.
The CLI argument is the fallback when no settings file exists.

Usage: idle.py [minutes]   (default 5)
"""

import os
import re
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, "/usr/local/lib/vibb-py"):
    if os.path.isdir(os.path.join(_p, "vibb")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
        break
from vibb import boxapi, mpv, renderer, spotify  # noqa: E402
from vibb.paths import RUN_DIR, last_activity, read_settings  # noqa: E402

IDLE_MIN = int(sys.argv[1]) if len(sys.argv) > 1 else 5
CHECK_S = 60


def describe(minutes):
    """'0' means DISABLED — say so. The old line printed 'will power
    off after 0 min', which read as an instant-shutdown bug and sent
    the owner hunting in the wrong daemon (field 2026-07-29: the real
    culprit was a pisugar-server tap shell)."""
    if minutes <= 0:
        return "idle auto-shutdown DISABLED (idle_shutdown_min=0)"
    return (f"will power off ~{minutes} min after playback and button "
            "presses stop (60s sampling + one cycle of button grace)")
# A button press younger than this counts as 'in use'. Two check
# periods: a press can land anywhere between samples, and one extra
# cycle of grace is cheaper than a box dying in a kid's hands.
ACTIVITY_FRESH_S = CHECK_S * 2


def idle_minutes():
    v = read_settings().get("idle_shutdown_min")
    return int(v) if isinstance(v, (int, float)) else IDLE_MIN


def daemon_playing():
    """Unified answer from the orchestration daemon, None if it's down."""
    try:
        return bool(boxapi.get("/status", timeout=5).get("playing"))
    except (OSError, ValueError):
        return None


def sonos_playing():
    """Third direct probe for the daemon-down window: a Sonos rendering
    OUR session must hold auto-off — powering the box off kills the
    controller and the bookmark while the music plays on in the corner
    (QA review 2026-08-09; the round-1 'idle needs zero changes' was
    only true with vibbd up). Only a CONFIRMED playing state holds:
    sidecar down or stale answers False, or a dead sidecar would pin
    the box awake forever."""
    if not renderer.is_sonos():
        return False
    try:
        snap = renderer.get("/state", timeout=3)
    except (OSError, ValueError):
        return False
    stale = snap.get("stale_s")
    return (snap.get("transport") == "PLAYING" and snap.get("ours")
            and stale is not None and stale < 30)


# A live ssh session whose traffic has been nothing but keepalive noise
# for this long is a terminal someone walked away from — release the
# hold and let the box sleep. Typing, reading output, journalctl -f all
# clear SSH_QUIET_BYTES easily and reset the clock, so a real working
# session holds for hours.
SSH_QUIET_RELEASE_S = 30 * 60
# Movement below this per check counts as silence, not a human: sshd's
# ClientAlive probes (install.sh drop-in, 1/min) ride the encrypted
# channel and move ~200-400 bytes/min in each direction even when
# nobody types. One keystroke's echo plus a prompt redraw, or a single
# line of output, clears 1KB.
SSH_QUIET_BYTES = 1024
_ssh = {"n": None, "bytes": None, "quiet_s": 0}


def ssh_active():
    """Anyone WORKING on the box over ssh? An active session means a
    human is on the box — powering off under them cost a debugging
    evening (field 2026-08-03: the 5-min idle fired mid-journalctl,
    and the wedged pisugar poweroff then needed a hard cut). utmp is
    gone on trixie (systemd built with -UTMP), so `who` is blind —
    established TCP on the sshd port is the signal instead.

    2026-08-10 (power audit #1): bare ESTABLISHED was too strong a
    hold. A laptop that SUSPENDS mid-session leaves its socket
    established until the kernel keepalive reaps it — ~2h15m of full
    idle draw, every time a lid closes on a debug session. Two-part
    fix: install.sh drops ClientAlive into sshd so a DEAD peer is
    closed within ~4 min, and for a LIVE peer nobody is touching (a
    forgotten terminal on a desktop that never sleeps) the per-socket
    byte counters gate the hold: below SSH_QUIET_BYTES per check it is
    keepalive chatter, and SSH_QUIET_RELEASE_S of that releases the
    box. Counters unparseable -> plain ESTABLISHED hold (the sshd
    drop-in still reaps dead peers); errors mean 'unknown': fail
    toward the OLD behavior (shutdown proceeds), never toward a box
    that can't sleep because a probe broke."""
    try:
        out = subprocess.run(
            ["ss", "-Htni", "state", "established", "( sport = :22 )"],
            capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired, AttributeError):
        return False
    if not out or not out.strip():
        _ssh["n"], _ssh["bytes"], _ssh["quiet_s"] = None, None, 0
        return False
    counters = re.findall(r"bytes_(?:acked|received):(\d+)", out)
    if not counters:
        return True  # no tcp_info on this kernel — hold as before
    n, total = len(counters), sum(int(c) for c in counters)
    moved = (_ssh["n"] != n or _ssh["bytes"] is None
             or total - _ssh["bytes"] >= SSH_QUIET_BYTES)
    _ssh["n"], _ssh["bytes"] = n, total
    if moved:
        _ssh["quiet_s"] = 0
        return True
    _ssh["quiet_s"] += CHECK_S
    return _ssh["quiet_s"] < SSH_QUIET_RELEASE_S


def _cycle(idle):
    """One check: the new idle-seconds count, or None after poweroff."""
    active = daemon_playing()
    if active is None:  # daemon down — check the sources directly
        active = spotify.playing() or mpv.playing() or sonos_playing()
    if not active:
        age = time.time() - last_activity()
        # A negative age is a clock jump (boot RTC/NTP) — same as the
        # radio markers, treat it as no signal rather than fresh.
        if 0 <= age < ACTIVITY_FRESH_S:
            active = True  # someone is pressing buttons — in use
    if not active and ssh_active():
        active = True  # a human is on the box over ssh — hold auto-off
    idle = 0 if active else idle + CHECK_S
    limit = idle_minutes()
    if limit <= 0:
        return 0  # disabled: never accumulate behind the parent's back
    if idle >= limit * 60:
        _backup_before_off()
        subprocess.run(["logger",
                        f"vibb-idle: idle {limit}min, powering off"])
        subprocess.run(["poweroff"])
        return None
    return idle


BACKUP_MAX_S = int(os.environ.get("VIBB_IDLE_BACKUP_MAX", "180"))
# The Soloist updater's slot on the same way down (PLAN-pipewire-soloist
# D1/AM-50): after the backup (irreplaceable first), its own budget, and
# backup + update < the 600 s window of the poweroff-imminent marker.
UPDATE_MAX_S = int(os.environ.get("VIBB_IDLE_UPDATE_MAX", "120"))
UPDATE_UNIT = os.environ.get("VIBB_SOLOIST_UPDATE_UNIT",
                             "/etc/systemd/system/vibb-soloist-update.service")


def _backup_before_off():
    """Back up on the way down — the moment the owner actually wants.

    This is the natural end of a listening session: the box has been idle
    for the whole timeout, so nothing is playing, the 2.4GHz radio is free,
    and the bookmarks from the session that just ended are on disk. Far
    better than any periodic schedule, which can only ever guess at a quiet
    moment.

    Bounded and best-effort by construction. vibb.backup's own gates decide
    whether there is anything to do (unconfigured, clock not trusted yet,
    already backed up today, no link) and almost every shutdown is a no-op
    costing milliseconds. A run that hangs is killed at BACKUP_MAX_S so a
    dead network can never keep the box awake burning battery — the box
    powers off regardless, and the next shutdown tries again.
    """
    try:
        # Tell the backup this is the on-the-way-down run: the box has
        # been idle for its whole timeout, so its busy-wait must stand
        # down — a silent-but-connected BT speaker otherwise reads as
        # busy and the run dies waiting, killed by our own timeout below
        # (field journal 2026-08-20: 190s then TERM, every shutdown).
        # tmpfs: gone at boot, so the timer path never sees it.
        with open(os.path.join(RUN_DIR, "poweroff-imminent"), "w") as f:
            f.write(str(time.time()))
        # Start the UNIT, don't spawn the module. A child of vibb-idle
        # inherits vibb-idle's cgroup, which has no limits — so the memory
        # ceiling, CPU pinning and cache dir that vibb-backup.service
        # carries would all have been silently bypassed on the one path
        # that actually runs (architect review 2026-08-17). Going through
        # systemd puts the run where its policy lives.
        subprocess.run(
            ["systemctl", "start", "--wait", "vibb-backup.service"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=BACKUP_MAX_S)
    except Exception:
        pass   # a failed backup must never block the shutdown
    if os.path.exists(UPDATE_UNIT):
        # soloist engine only (the unit exists only then): a zero-byte
        # ETag check most days, a bounded download when there is a new
        # build; the sidecar never restarts the child on this path
        try:
            subprocess.run(
                ["systemctl", "start", "--wait", "vibb-soloist-update.service"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=UPDATE_MAX_S)
        except Exception:
            pass


def main():
    idle = 0
    print(f"vibb-idle: {describe(idle_minutes())} "
          "(live from settings.json)", flush=True)
    while True:
        idle = _cycle(idle)
        if idle is None:
            return
        time.sleep(CHECK_S)


if __name__ == "__main__":
    main()
