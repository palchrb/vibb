#!/usr/bin/env python3
"""Gate the shared-radio yield markers (vibb/radio.py). The Zero 2 W
has ONE 2.4GHz radio: BT pages against an absent speaker deauthed wifi
mid-boot and starved CDN track loads to 19s (field 2026-07-18). The
markers are ADVISORY and mtime-TTL'd — a crashed writer must never
wedge the other side, so everything here also checks the fail-open
paths (stale, future-mtime, unreadable)."""
import os
import sys
import tempfile
import time
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["VIBB_RUN"] = TMP
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
os.environ.setdefault("VIBB_CACHE", tempfile.mkdtemp())
os.environ["VIBB_LIBRARY"] = os.path.join(os.environ["VIBB_STATE"],
                                            "lib.json")
os.environ["VIBB_RADIO_BUSY_TTL"] = "20"
os.environ["VIBB_BT_PAGING_TTL"] = "10"
sys.path.insert(0, os.path.join(REPO, "pi"))

from vibb import radio  # noqa: E402

# 1. no marker file -> not busy, not paging (the common cold path)
assert radio.busy() is False
assert radio.paging() is False
print("1. no markers: radio reads free OK")

# 2. touch -> fresh
radio.touch_busy()
radio.touch_paging()
assert radio.busy() is True
assert radio.paging() is True
print("2. touched markers read fresh OK")

# 3. TTL expiry: backdate the mtime past the TTL -> stale (a crashed
# daemon can never gate btwatchd forever)
old = time.time() - 21
os.utime(radio.BUSY_FILE, (old, old))
assert radio.busy() is False
print("3. mtime past TTL: stale (crashed writer fails open) OK")

# 4. FUTURE mtime (boot RTC/NTP clock jumped backwards after the touch)
# must read stale, not fresh-forever
fut = time.time() + 3600
os.utime(radio.BUSY_FILE, (fut, fut))
assert radio.busy() is False
print("4. future mtime: stale, not fresh-forever OK")

# 5. clear_paging removes the marker; clearing twice is harmless
radio.clear_paging()
assert radio.paging() is False
radio.clear_paging()  # idempotent
print("5. clear_paging: removed, double-clear harmless OK")

# 6. wait_paging_clear: no marker -> returns immediately; a live marker
# -> bounded by the cap, never forever (stale-marker worst case)
t0 = time.monotonic()
radio.wait_paging_clear(cap_s=2)
assert time.monotonic() - t0 < 0.5, "clear marker must not wait"
radio.touch_paging()
t0 = time.monotonic()
radio.wait_paging_clear(cap_s=1)
took = time.monotonic() - t0
assert 0.8 < took < 3, f"cap must bound the wait: {took:.1f}s"
radio.clear_paging()
print("6. wait_paging_clear: instant when clear, capped when not OK")

# 7. wifi_settled: 'operstate up' alone is NOT settled — it flips at
# association, BEFORE the 4-way handshake/DHCP where the boot deauths
# (reason=6) live. Settled needs a default route through wlan0 too.
oper = os.path.join(TMP, "operstate")
route = os.path.join(TMP, "route")
os.environ["VIBB_WLAN_OPERSTATE"] = oper
os.environ["VIBB_NET_ROUTE"] = route
HDR = ("Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask"
       "\tMTU\tWindow\tIRTT\n")
with open(oper, "w") as f:
    f.write("down\n")
with open(route, "w") as f:
    f.write(HDR)
assert radio.wifi_settled() is False
with open(oper, "w") as f:
    f.write("up\n")
assert radio.wifi_settled() is False, "up without a route is mid-DHCP"
with open(route, "w") as f:
    f.write(HDR + "wlan0\t00000000\t0102A8C0\t0003\t0\t0\t600\t00000000"
            "\t0\t0\t0\n")
assert radio.wifi_settled() is True
print("7. wifi_settled: needs operstate up AND a wlan0 default route OK")

# 8. a route via eth0/usb0 only does not count as wifi settled (pages
# would still stomp wlan0's association)
with open(route, "w") as f:
    f.write(HDR + "usb0\t00000000\t0102A8C0\t0003\t0\t0\t600\t00000000"
            "\t0\t0\t0\n")
assert radio.wifi_settled() is False
print("8. non-wlan0 default route: not settled OK")

# 9. fail-open: no wlan0 at all (operstate unreadable) -> settled
# (nothing to protect); route table unreadable -> settled (never gate
# forever on a broken proc read)
os.remove(oper)
assert radio.wifi_settled() is True
with open(oper, "w") as f:
    f.write("up\n")
os.remove(route)
assert radio.wifi_settled() is True
print("9. missing operstate/route files fail open OK")

# 10. AM-93 wifi_assoc_in_flight: the interface UP for NM to work on but
#     no default route yet = the fragile window; 'down' = nobody has
#     touched the radio, nothing to protect; missing files fail open
def _oper(v):
    with open(oper, "w") as f:
        f.write(v + "\n")
def _route(wlan):
    with open(route, "w") as f:
        f.write(HDR + ("wlan0\t00000000\t0102A8C0\t0003\t0\t0\t600\t00000000"
                       "\t0\t0\t0\n" if wlan else ""))
_oper("down"); _route(False)
assert radio.wifi_assoc_in_flight() is False, "down: NM has not started"
_oper("dormant")
assert radio.wifi_assoc_in_flight() is True, "dormant, no route: scanning/associating"
_oper("up")
assert radio.wifi_assoc_in_flight() is True, "up, no route: handshake/DHCP"
_route(True)
assert radio.wifi_assoc_in_flight() is False, "routed: settled"
_oper("unknown"); _route(False)
assert radio.wifi_assoc_in_flight() is True, "an admin-up interface of unknown state counts as in flight"
os.remove(oper)
assert radio.wifi_assoc_in_flight() is False, "no wlan0: nothing to protect"
_oper("up"); os.remove(route)
assert radio.wifi_assoc_in_flight() is False, "unreadable route table fails open"
print("10. wifi_assoc_in_flight: down = free, up/dormant without a route = hold, routed = free OK")

# 10. uptime: real /proc/uptime parses; a broken path fails to inf
# (= never gate on it)
assert radio.uptime() > 0
print("10. uptime reads /proc/uptime OK")

# 11. the player's ordering contract (RF review): a spotify start claims
# BUSY *first*, then waits on PAGING — the reverse would let btwatchd
# slip a fresh page into the gap between the wait and the CDN burst
import player  # noqa: E402

CALLS = []
player.radio = types.SimpleNamespace(
    touch_busy=lambda: CALLS.append("busy"),
    wait_paging_clear=lambda cap_s=6.0: CALLS.append("wait"))


class _Stop(Exception):
    pass


def _stop(_uri):
    raise _Stop  # markers checked — don't run the real session start


player.spotify = types.SimpleNamespace(
    to_uri=lambda t: "spotify:album:x", read_bookmark=_stop)
try:
    player.play_spotify("https://open.spotify.com/album/x")
except _Stop:
    pass
assert CALLS == ["busy", "wait"], \
    f"BUSY must be claimed before the PAGING wait: {CALLS}"
print("11. play_spotify claims BUSY before waiting on PAGING OK")

print("RADIO MARKERS OK — advisory, TTL'd, fail-open in every direction.")
