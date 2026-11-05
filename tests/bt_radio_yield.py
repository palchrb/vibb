#!/usr/bin/env python3
"""Gate btwatchd's radio-yield discipline on the shared 2.4GHz radio:
BLIND pages (boot cadence / backoff timer) hold while wifi is mid-setup
or a network stream/track load is in flight (field 2026-07-18: pages
deauthed wifi twice mid-boot, reason=6, and starved a CDN load to 19s).
User-intent kicks are never gated; a long-absent speaker stops yielding
(starvation belt); every page marks PAGING for the player to wait on;
~4 real boot failures drop the 5s cadence early. Runs WITHOUT dbus/gi:
the bus layer is stubbed — this tests the state machine, not bluez."""
import os
import sys
import tempfile
import time
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["VIBB_RUN"] = TMP
os.environ["VIBB_BT_FILE"] = os.path.join(TMP, "bt-headset")
os.environ["VIBB_BT_LOCKFILE"] = os.path.join(TMP, "bt.lock")
os.environ["VIBB_BT_KICK"] = os.path.join(TMP, "bt-kick")
os.environ["VIBB_WLAN_OPERSTATE"] = os.path.join(TMP, "operstate")
os.environ["VIBB_NET_ROUTE"] = os.path.join(TMP, "route")
MAC = "30:C0:1B:BD:13:B2"
with open(os.environ["VIBB_BT_FILE"], "w") as f:
    f.write(MAC + "\n")
sys.path.insert(0, os.path.join(REPO, "pi"))

# --- stub dbus/gi (btwatchd exec's the poll fallback without them) ----------
SCHEDULED = []  # (ms) per GLib.timeout_add


class _GLib:
    @staticmethod
    def timeout_add(ms, cb, *a):
        SCHEDULED.append(ms)
        return len(SCHEDULED)

    @staticmethod
    def source_remove(_id):
        pass


class _Gio:
    pass


dbus_mod = types.ModuleType("dbus")
dbus_mod.Interface = lambda *a, **k: None
dbus_mod.Boolean = bool
mainloop = types.ModuleType("dbus.mainloop")
glib = types.ModuleType("dbus.mainloop.glib")
glib.DBusGMainLoop = lambda **k: None
mainloop.glib = glib
dbus_mod.mainloop = mainloop
gi = types.ModuleType("gi")
repo = types.ModuleType("gi.repository")
repo.Gio, repo.GLib = _Gio, _GLib
gi.repository = repo
for name, mod in (("dbus", dbus_mod), ("dbus.mainloop", mainloop),
                  ("dbus.mainloop.glib", glib), ("gi", gi),
                  ("gi.repository", repo)):
    sys.modules[name] = mod

import btwatchd  # noqa: E402
from vibb import radio  # noqa: E402

rec = btwatchd.Reconnector(bus=None)
rec._adapter_up = lambda: None
rec._connected = lambda: False
rec._output = lambda dev: None
ATTEMPTS = []
_real_attempt = btwatchd.Reconnector.attempt
rec.attempt = lambda why, **kw: ATTEMPTS.append(why)

HDR = ("Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask"
       "\tMTU\tWindow\tIRTT\n")


def wifi(settled, oper="up"):
    with open(os.environ["VIBB_WLAN_OPERSTATE"], "w") as f:
        f.write(oper + "\n")
    with open(os.environ["VIBB_NET_ROUTE"], "w") as f:
        f.write(HDR + ("wlan0\t00000000\t0102A8C0\t0003\t0\t0\t600"
                       "\t00000000\t0\t0\t0\n" if settled else ""))


# 1. wifi mid-setup inside the boot gate -> hold, even with no busy marker
btwatchd.WIFI_GATE_S = 10 ** 9  # this machine's uptime is 'early boot'
wifi(settled=False)
assert rec._radio_yield() is True
print("1. wifi associating at boot: blind pages hold OK")

# 1b. AM-93: wlan0 DOWN inside the gate (NetworkManager not started yet)
#     -> nothing to protect, the page goes now; 'dormant' (NM working on
#     it: scanning/associating) without a route -> hold
wifi(settled=False, oper="down")
assert rec._radio_yield() is False, "wlan0 down: nothing to deauth, page now"
wifi(settled=False, oper="dormant")
assert rec._radio_yield() is True
wifi(settled=False)
print("1b. wlan0 down at boot: page now; dormant/up without a route: hold OK")

# 2. wifi settled (default route up) -> the gate opens
wifi(settled=True)
assert rec._radio_yield() is False
print("2. wifi settled: gate open OK")

# 3. a fresh busy marker (track load / stream spawn) -> hold
radio.touch_busy()
assert rec._radio_yield() is True
print("3. network audio in flight: blind pages hold OK")

# 4. starvation belt: the speaker has been away a LONG time -> markers
# no longer gate (reconnect beats politeness; the marker writer may loop)
rec.disconnected_since = time.monotonic() - btwatchd.YIELD_GIVEUP_S - 1
assert rec._radio_yield() is False
rec.disconnected_since = None
assert rec._radio_yield() is True  # fresh drop still yields
print("4. long-absent speaker stops yielding (no starvation) OK")

# 5. _boot_tick under a busy radio: NO page, a short retry is scheduled
rec.state = "BOOT"
SCHEDULED.clear()
rec._boot_tick()
assert not ATTEMPTS, ATTEMPTS
assert SCHEDULED == [int(btwatchd.YIELD_RETRY_S * 1000)], SCHEDULED
print("5. boot tick defers under a busy radio (rescheduled) OK")

# 6. the WAITING timer defers too — and fires once the radio frees up
rec.state = "WAITING"
SCHEDULED.clear()
rec._timer_fire()
assert not ATTEMPTS and SCHEDULED, (ATTEMPTS, SCHEDULED)
old = time.time() - radio.BUSY_TTL_S - 1
os.utime(radio.BUSY_FILE, (old, old))  # marker went stale
rec._timer_fire()
assert ATTEMPTS == ["timer"], ATTEMPTS
print("6. waiting timer defers, then pages when the radio frees OK")

# 7. a REAL attempt marks PAGING for the player to wait on, and
# _finish_attempt (the single exit funnel) clears it
rec.attempt = types.MethodType(_real_attempt, rec)


class _Dev:
    def Connect(self, **kw):
        pass  # in flight — reply comes async


class _Bus:
    def get_object(self, *a, **k):
        return None


btwatchd.dbus.Interface = lambda *a, **k: _Dev()
rec.bus = _Bus()
rec.state = "WAITING"
rec.connecting = None
assert radio.paging() is False
rec.attempt("test page")
assert rec.connecting == MAC, "the stubbed Connect must be in flight"
assert radio.paging() is True, "an on-air page must be marked"
rec._finish_attempt()
assert radio.paging() is False, "finishing must clear the marker"
print("7. attempt marks PAGING, the finish funnel clears it OK")

# 8. boot-fail early drop: real failures stop the 5s boot cadence at the
# limit (the speaker is off — don't burn pages while wifi associates)...
rec.state = "BOOT"
rec.boot_fails = 0
rec.boot_deadline = time.monotonic() + 999
for i in range(btwatchd.BOOT_FAIL_LIMIT):
    assert rec.state == "BOOT", f"dropped early at {i}"
    rec._attempt_failed("Timeout")
assert rec.state == "WAITING", "must drop to WAITING at the fail limit"
print("8. real boot failures drop the 5s cadence early OK")

# 9. ...but NotReady (adapter still powering — the page never went on
# the air) does NOT count toward the limit
rec.state = "BOOT"
rec.boot_fails = 0
for _ in range(btwatchd.BOOT_FAIL_LIMIT * 2):
    rec._attempt_failed("org.bluez.Error.NotReady")
assert rec.state == "BOOT", "NotReady is 'too early', not 'speaker off'"
assert rec.boot_fails == 0, rec.boot_fails
print("9. NotReady failures never count toward the early drop OK")

# 10. enter_boot resets the counter (a bluez restart gets a fresh window)
rec.boot_fails = 3
rec.enter_boot()
assert rec.boot_fails == 0
print("10. a fresh boot window resets the fail counter OK")

print("BT RADIO YIELD OK — blind pages wait their turn, the player "
      "sees pages coming, and nobody starves.")

# 5. a soloistd pass is fetching (4d, AM-68): the blind page holds even for a
#    long-absent speaker — until the marker's TTL runs out
rec.disconnected_since = time.monotonic() - btwatchd.YIELD_GIVEUP_S - 1
radio.touch_warming()
assert rec._radio_yield() is True, "warming beats the starvation belt"
old = time.time() - radio.WARMING_TTL_S - 1
os.utime(radio.WARMING_FILE, (old, old))          # the sweeper stopped touching it
assert rec._radio_yield() is False, "a stale warming marker holds nothing"
rec.disconnected_since = None
print("5. a soloistd pass holds the page, bounded by the marker's TTL OK")

