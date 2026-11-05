"""Filesystem locations shared by every component (env-overridable)."""

import json
import os
import time

STATE_DIR = os.environ.get("VIBB_STATE", "/var/lib/vibb/state")
CACHE_DIR = os.environ.get("VIBB_CACHE", "/var/lib/vibb/cache")
# Uploaded section logos — user content, so NOT under CACHE_DIR's pruning
ART_DIR = os.environ.get("VIBB_ART", "/var/lib/vibb/art")
# Audio the parent uploaded from the PWA (own audiobooks, ripped CDs, the
# kids' own recordings). Like ART_DIR this is USER CONTENT and must stay
# outside CACHE_DIR: prune_cache would happily delete a 300MB audiobook
# nobody has another copy of. expand_entries() already plays a folder of
# audio files (mp3/m4a/m4b/ogg/opus/flac/wav) with per-file bookmarks and
# cover.jpg as art, so uploading IS the whole feature.
MEDIA_DIR = os.environ.get("VIBB_MEDIA", "/var/lib/vibb/media")
SETTINGS_FILE = os.environ.get("VIBB_SETTINGS", "/etc/vibb/settings.json")

# Advisory 'a human pressed a button' marker, mtime is the fact — same
# contract as the radio markers (tmpfs, crash-safe, best-effort). The
# UI touches it on input; vibb-idle reads it so the box never powers
# off in a kid's hands while they browse without playing anything.
RUN_DIR = os.environ.get(
    "VIBB_RUN", "/run" if os.access("/run", os.W_OK) else "/tmp")
ACTIVITY_FILE = os.path.join(RUN_DIR, "vibb-ui-activity")
_ACT_TOUCHED = [0.0]


def touch_activity():
    """Record 'someone is using the buttons right now'. Throttled so a
    burst of presses costs one tmpfs write per 10s; failures are
    swallowed — this can only ever delay an auto-shutdown, never break
    playback."""
    now = time.monotonic()
    if _ACT_TOUCHED[0] and now - _ACT_TOUCHED[0] < 10:
        return
    try:
        with open(ACTIVITY_FILE, "w"):
            pass
        _ACT_TOUCHED[0] = now
    except OSError:
        pass


def last_activity():
    """Epoch mtime of the last button press, 0.0 when never/unknown."""
    try:
        return os.path.getmtime(ACTIVITY_FILE)
    except OSError:
        return 0.0


# Shared 'go-librespot was just restarted' marker. go-librespot gets
# restarted from three places on the same BT event — bt.py's ALSA-route
# rewrite, output.py's audio_device retarget, and the daemon's dead-
# device rebuild — each for its own config change. This mtime lets a
# later restart that has NOTHING new to apply skip a redundant second
# bounce (which just re-bursts the shared 2.4GHz radio and re-flaps the
# speaker). Advisory, crash-safe, tmpfs — same contract as the radio
# markers.
# The systemd unit of whichever Spotify engine this box was installed
# with (PLAN-soloistd.md: the engine is an INSTALL-TIME toggle, and the
# daemon's ~30 REST call sites are already engine-blind through
# VIBB_GO_API). The unit NAME was the half that was still hardcoded in
# 14 places; everything goes through go_unit_cmd() now, so swapping the
# engine is an env pair on the units and nothing else.
GO_UNIT = os.environ.get("VIBB_GO_UNIT", "go-librespot")


def go_unit_cmd(*args):
    """systemctl argv for the Spotify engine's unit.

    go_unit_cmd("restart")               -> systemctl restart <unit>
    go_unit_cmd("is-active", "--quiet")  -> systemctl is-active --quiet <unit>
    """
    return ["systemctl", *args, GO_UNIT]


GO_RESTART_FILE = os.path.join(RUN_DIR, "vibb-go-restart")


def note_go_restart():
    """Record that go-librespot was just (re)started, from any path."""
    try:
        with open(GO_RESTART_FILE, "w"):
            pass
    except OSError:
        pass


def go_restarted_within(secs):
    """True if go-librespot was (re)started less than `secs` ago. A
    future mtime (clock jumped back) reads as 'not recent' — a harmless
    extra restart beats wrongly skipping a needed one."""
    try:
        age = time.time() - os.path.getmtime(GO_RESTART_FILE)
    except OSError:
        return False
    return 0 <= age < secs


# Is the wall clock trustworthy THIS boot? The Zero has no RTC, so at
# boot systemd/fake-hwclock restore roughly the time the box was last
# running — i.e. approximately the moment it was switched off. Anything
# that compares wall-clock stamps across a reboot (the resume session)
# must therefore wait for a real correction: the PiSugar RTC load or
# NTP. Same tmpfs-marker contract as the radio/go-restart markers —
# advisory, crash-safe, and per-boot by construction.
CLOCK_OK_FILE = os.path.join(RUN_DIR, "vibb-clock-ok")
NTP_SYNC_FILE = "/run/systemd/timesync/synchronized"


def note_clock_ok():
    """Record that something authoritative just set the wall clock."""
    try:
        with open(CLOCK_OK_FILE, "w"):
            pass
    except OSError:
        pass


def clock_trusted():
    """True once the RTC load or NTP has set the clock this boot."""
    return os.path.exists(CLOCK_OK_FILE) or os.path.exists(NTP_SYNC_FILE)


def read_settings():
    """The raw settings dict ({} when missing/invalid). Validation and
    defaults live in the daemon — consumers treat this as advisory."""
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}
