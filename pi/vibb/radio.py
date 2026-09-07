"""Advisory radio-yield markers for the shared 2.4GHz front-end.

The Zero 2 W time-slices ONE radio between wifi and BT. Steady-state
A2DP + wifi streaming coexist fine (firmware slots at ms scale) — what
collides catastrophically are the MACROSCOPIC discretionary bursts we
control the timing of: BT paging a (possibly absent) speaker vs wifi
association/DHCP or a CDN track load (field 2026-07-18: paging deauthed
wifi twice mid-boot; loads starved to 19s). The rule these markers
implement: whoever is doing something time-critical owns the radio; the
side that CAN wait a few seconds, waits.

Two markers, both advisory, both mtime-TTL'd (a crashed writer can never
wedge the other side — everything fails open):

- BUSY   (vibb-radio-busy): the daemon/player touch it when network-
  heavy audio work starts (spotify session/track loads, stream spawns).
  btwatchd defers BLIND pages while it is fresh. User-intent connects
  are never gated.
- PAGING (vibb-bt-paging): btwatchd touches it around each connect
  attempt; the spotify player waits a bounded few seconds for it to
  clear before starting CDN-heavy session work.

Ordering matters (RF review): a starter touches BUSY *first*, then
waits on PAGING — so btwatchd can't slip a fresh page into the gap.
Both waits are bounded; no hold-and-wait, no deadlock."""

import os
import socket
import time

_RUN = os.environ.get(
    "VIBB_RUN", "/run" if os.access("/run", os.W_OK) else "/tmp")
BUSY_FILE = os.path.join(_RUN, "vibb-radio-busy")
PAGING_FILE = os.path.join(_RUN, "vibb-bt-paging")
BUSY_TTL_S = float(os.environ.get("VIBB_RADIO_BUSY_TTL", "20"))
PAGING_TTL_S = float(os.environ.get("VIBB_BT_PAGING_TTL", "10"))
SKIP_FILE = os.path.join(_RUN, "vibb-user-skip")
# WARMING (vibb-warming): the daemon's sweeper touches it every <=10 s while a
# soloistd pass runs (4d, AM-68). btwatchd's blind pages hold on it BEFORE the
# starvation belt: a page under a fetching pass is "BT paging + wifi", the
# thrice-observed flap, and has nothing to gain. Bounded by its TTL.
WARMING_FILE = os.path.join(_RUN, "vibb-warming")
WARMING_TTL_S = float(os.environ.get("VIBB_WARMING_TTL", "30"))
SKIP_TTL_S = float(os.environ.get("VIBB_USER_SKIP_TTL", "12"))


def _touch(path):
    try:
        with open(path, "a"):
            pass
        os.utime(path, None)
    except OSError:
        pass  # advisory — never break audio over a marker


def _fresh(path, ttl):
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return False
    # a future mtime (the boot RTC/NTP clock jump ran backwards) must
    # read STALE, not fresh-forever
    return 0 <= age < ttl


def touch_busy():
    """Network-heavy audio work is starting (track load, stream spawn)."""
    _touch(BUSY_FILE)


def busy():
    return _fresh(BUSY_FILE, BUSY_TTL_S)


def touch_warming():
    """A soloistd pass is running (touched by the sweeper's poll loop)."""
    _touch(WARMING_FILE)


def warming():
    return _fresh(WARMING_FILE, WARMING_TTL_S)


def touch_user_skip():
    """A HUMAN just skipped a track (daemon's next/prev to mpv). The
    player's dead-output watchdog reads this: rapid track changes right
    after a user skip are a finger, not a dying sink."""
    _touch(SKIP_FILE)


def user_skip_fresh():
    return _fresh(SKIP_FILE, SKIP_TTL_S)


def touch_paging():
    """A BT connect attempt is going on the air."""
    _touch(PAGING_FILE)


def clear_paging():
    try:
        os.remove(PAGING_FILE)
    except OSError:
        pass


def paging():
    return _fresh(PAGING_FILE, PAGING_TTL_S)


def wait_paging_clear(cap_s=6.0):
    """Let an in-flight BT page finish before CDN work — bounded: one
    default page attempt is ~5.1s, so 6s covers it with margin, and a
    stale marker (crashed btwatchd) costs at most this cap."""
    deadline = time.monotonic() + cap_s
    while paging() and time.monotonic() < deadline:
        time.sleep(0.5)


# One probe address for the whole box (daemon, player, content) —
# env-overridable so tests never touch the real network (review M3/Q2)
PROBE_ADDR = os.environ.get("VIBB_PROBE_ADDR", "1.1.1.1:443")


def internet_up(timeout=2.0):
    """Actual-internet probe (not just wifi association): plain IP, no
    DNS to hang on."""
    host, _, port = PROBE_ADDR.rpartition(":")
    try:
        socket.create_connection((host, int(port)), timeout=timeout).close()
        return True
    except (OSError, ValueError):
        return False


def uptime():
    """Seconds since BOOT (not process start — btwatchd restarts)."""
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError):
        return float("inf")  # can't tell — never gate on it


def _wlan0_operstate():
    """'up' | 'dormant' | 'down' | ... or None when there is no wlan0."""
    state_path = os.environ.get("VIBB_WLAN_OPERSTATE",
                                "/sys/class/net/wlan0/operstate")
    try:
        with open(state_path) as f:
            return f.read().strip()
    except OSError:
        return None


def _wlan0_routed():
    """A default route through wlan0? None when the table is unreadable."""
    route_path = os.environ.get("VIBB_NET_ROUTE", "/proc/net/route")
    try:
        with open(route_path) as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) > 1 and parts[0] == "wlan0" \
                        and parts[1] == "00000000":
                    return True
        return False
    except OSError:
        return None


def wifi_assoc_in_flight():
    """Is wlan0 in the fragile part of its setup RIGHT NOW — the interface
    up for NetworkManager/wpa_supplicant to work on (scan, associate,
    4-way handshake, DHCP) but no default route yet? That is where the
    boot deauths (reason=6, field 2026-07-18) lived. 'down' means nobody
    has touched the radio yet: nothing to protect, a page is free (AM-93:
    NetworkManager starts ~20 s late on Trixie and 'hold until settled'
    made BT wait for it). Fails open: no wlan0, no table -> False."""
    st = _wlan0_operstate()
    if st is None or st in ("down", "notpresent", "lowerlayerdown"):
        return False
    routed = _wlan0_routed()
    return routed is False


def wifi_settled():
    """Is wifi past its fragile window? 'operstate up' alone flips at
    association — BEFORE the 4-way handshake and DHCP, which is exactly
    where the boot deauths (reason=6) live — so require a default route
    through wlan0 too. No wlan0 at all reads settled: nothing to
    protect."""
    st = _wlan0_operstate()
    if st is None:
        return True  # no wlan0 — wifi isn't in the picture
    if st != "up":
        return False
    routed = _wlan0_routed()
    if routed is None:
        return True  # unreadable — fail open, don't gate forever
    return routed
