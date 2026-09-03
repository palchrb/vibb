#!/usr/bin/env python3
"""Event-driven BT reconnect daemon — phase C of PLAN-bt-dbus.md.

Replaces the vibb-bt-reconnect bash poll loop (up to 60s to notice
the speaker) with BlueZ D-Bus signals: when the remembered speaker
powers on — most page us by themselves, the rest appear on the bus —
we call Device1.Connect within seconds.

State machine (event-driven equivalents of the old loop):

  BOOT       bluez/adapter not confirmed ready: power-on + attempt
             every 5s inside a ~120s window (a failure here means
             "too early", not "speaker away")
  STEADY     target connected: zero timers, pure signal wait
  WAITING    target away: attempt on target-path signals; blind
             backoff attempts 20s -> 300s in between (each blind
             attempt is radio page time — most speakers come to US)
  NO_TARGET  nothing remembered: idle on the MAC-file monitor

Scope guards (plan §7):
- No recovery role: firmware-crash healing lives in bt.py/vibbd.
  A dead controller just fails our attempts into backoff.
- No pairing, no scanning, no ALSA routing, and no one-output
  enforcement (that stays inside bt.py connect(); auto-kicking a
  self-connecting second device from here would need debounce against
  reconnect loops — deliberately not taken on in phase C).
  One addition since (owner request 2026-07-27): follow-the-connector —
  a PAIRED audio sink that connects itself while the configured speaker
  is ABSENT is adopted as the active speaker (via vibbd /bt/connect,
  which owns routing). Detection lives here because this daemon already
  watches every Device1 signal; the switch itself still runs in bt.py.
- Cross-process flock (LOCK_NB) before Connect: when bt.py owns the
  radio (pairing, switching) we skip; a signal or timer retries soon.
- bluez restart (NameOwnerChanged) re-enters the BOOT fast window, so
  recover()'s systemctl restart bluetooth yields a fast reconnect.

Fallback: VIBB_BT_BACKEND=cli or missing python3-dbus/python3-gi
exec's the old bash poll loop (installed as vibb-bt-reconnect-poll).
"""

import os
import subprocess
import sys
import threading
import time

_here = os.path.dirname(os.path.abspath(__file__))
for _p in (_here, "/usr/local/lib/vibb-py"):
    if os.path.isdir(os.path.join(_p, "vibb")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
        break

POLL_FALLBACK = os.environ.get("VIBB_RECON_POLL",
                               "/usr/local/bin/vibb-bt-reconnect-poll")


def log(msg):
    print(f"btwatchd: {msg}", flush=True)


def _fallback(reason):
    if os.access(POLL_FALLBACK, os.X_OK):
        log(f"{reason} — falling back to the poll loop")
        os.execv(POLL_FALLBACK, [POLL_FALLBACK])
    log(f"{reason} and no poll fallback installed — exiting")
    sys.exit(3)


if os.environ.get("VIBB_BT_BACKEND") == "cli":
    _fallback("VIBB_BT_BACKEND=cli")
try:
    import dbus
    import dbus.mainloop.glib
    from gi.repository import Gio, GLib
except ImportError as _e:
    _fallback(f"dbus/gi unavailable ({_e})")

from vibb import boxapi  # noqa: E402
from vibb import radio as _radio  # noqa: E402 — shared-radio yield
from vibb.bt import (BT_QUIET_FILE, KICK_FILE, MAC_FILE,  # noqa: E402
                       acquire_process_lock)

# timings are env-tunable so the test harness can run in seconds
BOOT_RETRY_S = float(os.environ.get("VIBB_RECON_BOOT_RETRY", "5"))
BOOT_WINDOW_S = float(os.environ.get("VIBB_RECON_BOOT_WINDOW", "120"))
BACKOFF_MIN_S = float(os.environ.get("VIBB_RECON_BACKOFF_MIN", "20"))
BACKOFF_MAX_S = float(os.environ.get("VIBB_RECON_BACKOFF_MAX", "300"))
DROP_RETRY_S = float(os.environ.get("VIBB_RECON_DROP_RETRY", "3"))
# a FRESH drop means someone is probably standing right there
# power-cycling the speaker — and a powered-on classic-BT speaker that
# doesn't page US is invisible to events, so our own pages are the only
# discovery. Keep them coming every ~15s through the blip window
# (matches vibbd's auto-resume), then decay as before. Nothing is
# playing then (the lost path stopped it), so the radio is free.
RECENT_DROP_S = float(os.environ.get("VIBB_RECON_RECENT", "150"))
RECENT_RETRY_S = float(os.environ.get("VIBB_RECON_RECENT_RETRY", "15"))
# The ladder used to floor at BACKOFF_MAX forever: a speaker switched
# off for the night still got a ~5s blind page-TX every 5 minutes, all
# of it on the shared 2.4GHz radio (energy audit 2026-07-20 #5). After
# this long away we stop scheduling blind pages entirely — every
# revival path is event-driven and already in place: the speaker paging
# US (inbound reconnect), RSSI/appearance evidence, the play-press kick
# file, a retarget, and boot.
ABSENT_AFTER_S = float(os.environ.get("VIBB_RECON_ABSENT_AFTER", "3600"))
DEBOUNCE_S = float(os.environ.get("VIBB_RECON_DEBOUNCE", "5"))
LOCK_RETRY_S = float(os.environ.get("VIBB_RECON_LOCK_RETRY", "10"))
CONNECT_TIMEOUT_S = 30
# Radio yield: blind pages hold while wifi is mid-setup at boot or a
# network stream/track load is in flight (advisory marker from vibbd).
# Only BLIND (timer/boot) pages — user-intent kicks and speaker-initiated
# signals are never gated. See vibb/radio.py for the rationale.
YIELD_RETRY_S = float(os.environ.get("VIBB_RECON_YIELD_RETRY", "4"))
YIELD_GIVEUP_S = float(os.environ.get("VIBB_RECON_YIELD_GIVEUP", "120"))
# 45, not 15: NetworkManager doesn't even bring wlan0 up until ~27s of
# uptime on this box, so a 15s gate expired BEFORE the association it
# was meant to protect (field 2026-07-18 20:00: boot pages at 23s and
# 33s landed in the assoc window). Free once wifi settles (route up);
# an offline cabin boot delays blind pages <=45s — kicks bypass.
WIFI_GATE_S = float(os.environ.get("VIBB_RECON_WIFI_GATE", "45"))
# ~4 failed boot pages = the speaker is off; the 5s boot cadence would
# otherwise burn ~24 pages against nothing, exactly while wifi comes up
BOOT_FAIL_LIMIT = int(os.environ.get("VIBB_RECON_BOOT_FAILS", "4"))
# Follow-the-connector (field 2026-07-27): the Skoda head unit paged US
# and brought up AVRCP while the box still pointed at the JBL sitting at
# home — metadata flowed to the car's display, audio kept opening the
# JBL's dead PCM. A paired audio sink connecting itself IS user intent
# (ignition on, headset power button); when the configured speaker is
# absent, adopt the connector. The confirm delay lets a manual switch
# in progress win the race (bt.py connects first, writes the MAC file
# after) so adoption becomes a no-op instead of a duplicate connect.
ADOPT_CONFIRM_S = float(os.environ.get("VIBB_RECON_ADOPT_CONFIRM", "3"))
ADOPT_DEBOUNCE_S = float(os.environ.get("VIBB_RECON_ADOPT_DEBOUNCE", "60"))
AUDIO_SINK_UUID = "0000110b-0000-1000-8000-00805f9b34fb"
# When the target IS alive, a second audio sink connecting itself is not
# adopted — and not left parked either: a parallel ACL with the Skoda's
# AVRCP polling on it during live A2DP is exactly the channel-ops-while-
# streaming dose this chip's firmware crashes under (owner 2026-07-27:
# never car + headset connected at once). Disconnect the newcomer ONCE;
# if it pages right back (some head units retry hard) let it park — a
# quiet parked link beats fighting a reconnect loop with page storms.
# The kick memory decays, so a fresh trip hours later gets a fresh kick.
KICK_RETRY_S = float(os.environ.get("VIBB_RECON_KICK_RETRY", "3600"))
# A peer that ACCEPTS the ACL page but never lets the audio channel up
# (AVDTP refused: the car head unit whose single A2DP slot CarPlay
# holds, or a headset with a stale session from before a reboot) used
# to loop connect->drop every 3-7s for minutes — the ACL success reset
# the ladder each round, so neither the 20->300s backoff nor the 1h
# park could ever engage (field 2026-08-04, Skoda + JBL; the only code
# that escalates lives in _attempt_failed, which never runs when the
# page itself succeeds). Success is now COMMITTED only when the A2DP
# PCM exists (or the nudge fallback announces); a drop before that is
# a refusal that climbs the ladder, and after this many consecutive
# refusals the pages park entirely. Revival: a kick (play press /
# output switch), a retarget, an inbound connect, or boot — NOT nearby
# evidence, because the refusing car is nearby and chatty by
# definition. Threshold is a field heuristic — tune on the box via the
# env, not by redeploying.
REFUSAL_PARK_N = int(os.environ.get("VIBB_RECON_REFUSAL_PARK", "5"))

BLUEZ = "org.bluez"
ADAPTER_PATH = "/org/bluez/hci0"


def dev_path(mac):
    return ADAPTER_PATH + "/dev_" + mac.upper().replace(":", "_")


def read_target():
    try:
        mac = open(MAC_FILE).read().strip().upper()
        return mac or None
    except OSError:
        return None


class Reconnector:
    def __init__(self, bus):
        self.bus = bus
        self.target = read_target()
        self.state = "BOOT"
        self.backoff = BACKOFF_MIN_S
        self.timer = None            # at most ONE pending GLib timer
        self.connecting = None       # mac of the in-flight Connect
        self.last_attempt = 0.0
        self.lock = None             # flock held across the Connect
        self.boot_deadline = time.monotonic() + BOOT_WINDOW_S
        self.monitor = None          # Gio ref — GC would stop events
        self.kick_monitor = None     # ditto, for the connect-now kick
        self.announced = None        # last output we told vibbd about
        self.disconnected_since = None  # when the target went away
        self._pcm_waiting = False    # a bt announcement awaits the PCM
        self._nudged = False         # one A2DP nudge per steady period
        self._committed = False      # this steady period reached audio
        self.refusals = 0            # consecutive steadies with no audio
        self.boot_fails = 0          # real page failures this boot window
        self._yield_logged = False   # log the first radio-yield only
        self.adopt_seen = {}         # mac -> monotonic of last adopt look
        self.kicked = {}             # mac -> monotonic of last polite kick

    # --- bus plumbing ------------------------------------------------------

    def subscribe(self):
        """No sender filter: only bluez emits these interfaces, and an
        unfiltered match survives bluez restarts with no re-subscribe."""
        add = self.bus.add_signal_receiver
        add(self._props_changed, signal_name="PropertiesChanged",
            dbus_interface="org.freedesktop.DBus.Properties",
            path_keyword="path")
        add(self._ifaces_added, signal_name="InterfacesAdded",
            dbus_interface="org.freedesktop.DBus.ObjectManager")
        add(self._name_owner, signal_name="NameOwnerChanged",
            dbus_interface="org.freedesktop.DBus", arg0=BLUEZ)

    def watch_mac_file(self):
        f = Gio.File.new_for_path(MAC_FILE)
        self.monitor = f.monitor(Gio.FileMonitorFlags.NONE, None)
        self.monitor.connect("changed", self._mac_file_changed)

    def watch_kick_file(self):
        f = Gio.File.new_for_path(KICK_FILE)
        self.kick_monitor = f.monitor(Gio.FileMonitorFlags.NONE, None)
        self.kick_monitor.connect("changed", self._kicked)

    # --- signal handlers ---------------------------------------------------

    def _props_changed(self, iface, changed, _invalidated, path=None):
        if str(iface) == "org.bluez.Adapter1":
            if changed.get("Powered"):
                self.attempt("adapter powered")
        elif (str(iface) == "org.bluez.Device1" and self.target
                and str(path) == dev_path(self.target)):
            if "Connected" in changed:
                if changed["Connected"]:
                    self.enter_steady("device connected")
                else:
                    self.state = "WAITING"
                    self._nudged = False  # fresh nudge for the next link
                    committed, self._committed = self._committed, False
                    if self.disconnected_since is None:
                        self.disconnected_since = time.monotonic()
                    self._notify_lost()
                    if committed:
                        # audio worked on this link: a blip reconnects
                        # fast; a powered-off speaker fails one page and
                        # starts the backoff ladder
                        self.backoff = BACKOFF_MIN_S
                        self.schedule(DROP_RETRY_S, "target dropped")
                    else:
                        # the peer accepted the page but audio never
                        # came up — an AVDTP refusal, not a blip (see
                        # REFUSAL_PARK_N). Retrying fast re-collides
                        # with whatever refused (a busy car slot, a
                        # stale headset session that needs its ~20s
                        # supervision timeout to clear), so this climbs
                        # the ladder instead — and never resets it.
                        self.refusals += 1
                        if self.refusals >= REFUSAL_PARK_N:
                            log(f"{self.refusals} connects in a row "
                                "with no audio — the peer accepts the "
                                "link but refuses the audio channel; "
                                "parking pages (a play press, "
                                "retarget, inbound connect or boot "
                                "revives them)")
                        else:
                            self.schedule(
                                self.backoff,
                                f"connected but no audio "
                                f"(x{self.refusals})")
                            self.backoff = min(self.backoff * 2,
                                               BACKOFF_MAX_S)
            elif self.state == "WAITING" and not self.refusals:
                # RSSI etc. — evidence the device is nearby right now.
                # Gated on refusals: a refusing peer emits properties
                # continually, and letting them reset the ladder would
                # defeat the escalation in exactly the field scenario.
                self.backoff = BACKOFF_MIN_S
                self.attempt("target seen", debounce=True)
        elif (str(iface) == "org.bluez.Device1" and changed.get("Connected")
                and str(path).startswith(ADAPTER_PATH + "/dev_")):
            # a device we did NOT page connected (they page us) and it is
            # not the target — maybe follow it (see ADOPT_* above)
            self._inbound_connected(str(path))

    def _ifaces_added(self, path, _ifaces):
        # refusal-gated like the evidence branch: bluez re-announcing a
        # refusing peer's object is not a reason to page it again
        if self.target and str(path) == dev_path(self.target) \
                and not self.refusals:
            self.backoff = BACKOFF_MIN_S
            self.attempt("target appeared")

    def _name_owner(self, _name, _old, new):
        if str(new):
            log("bluez is up — entering the fast window")
            self.enter_boot()
        else:
            log("bluez went away — going quiet until it returns")
            self.cancel_timer()
            self.state = "BOOT"

    def _kicked(self, *_args):
        """vibbd touched the kick file: the user just switched the
        output to bt while the speaker is disconnected — connect NOW
        instead of waiting out the backoff ladder. attempt() handles the
        harmless cases (already connected, in flight, lock busy); the
        debounce absorbs Gio's multiple events per touch."""
        if not self.target:
            return
        if not self.refusals:
            # a fresh user intent resets the ladder — but NOT while the
            # peer is refusing audio: the kick file's writers include
            # every transport control, and the car's own AVRCP commands
            # reach it through the mpris bridge, so an unconditional
            # reset here would let the refusing peer un-park itself.
            # The immediate attempt below still runs either way — a
            # play press always gets its page; it just doesn't wipe
            # the refusal bookkeeping (a success will).
            self.backoff = BACKOFF_MIN_S
            # Re-base the RECENT_DROP fast window on the kick so a
            # post-heal retry runs the 15s cadence for the full 150s
            # from NOW (a slow heal otherwise burns the window that
            # started at the original drop and lands in the patient
            # ladder). But only when we're ALREADY OUTSIDE that window:
            # an unconditional re-base let every button press with the
            # headset off restart the 150s×15s paging AND reset the 1h
            # ABSENT parking clock, so a kid mashing buttons kept the
            # box paging ~4/min forever (energy/RF audit 2026-07-24
            # #3). Inside the window a kick's immediate attempt() below
            # is enough; the stamp stays where it was.
            now = time.monotonic()
            if self.disconnected_since is None \
                    or now - self.disconnected_since > RECENT_DROP_S:
                self.disconnected_since = now
        self.attempt("output switched to bt", debounce=True)

    def _mac_file_changed(self, *_args):
        new = read_target()
        if new == self.target:
            return
        log(f"target changed: {self.target or '(none)'} -> {new or '(none)'}")
        self.target = new
        self.backoff = BACKOFF_MIN_S
        self.disconnected_since = None
        self.refusals = 0  # a fresh device deserves a fresh window
        self._committed = False
        self.cancel_timer()
        if new is None:
            self.state = "NO_TARGET"
            self._output("local")  # speaker forgotten -> built-in
        else:
            self.state = "WAITING"
            self.attempt("retarget")

    # --- state transitions ---------------------------------------------------

    def enter_boot(self):
        self.state = "BOOT"
        self.boot_deadline = time.monotonic() + BOOT_WINDOW_S
        self.boot_fails = 0
        self.refusals = 0  # fresh bluez, fresh refusal window
        self._committed = False
        self.cancel_timer()
        self._boot_tick()

    def _boot_tick(self):
        if self.state != "BOOT":
            return
        self._adapter_up()
        if self.connecting:
            return
        if self._radio_yield():
            self.schedule(YIELD_RETRY_S, None)  # radio busy — hold the page
            return
        self.attempt("boot")
        # next step is scheduled by the attempt's outcome handlers

    def _radio_yield(self):
        """Should a BLIND page hold right now? Yes while wifi is in its
        fragile boot window (association/DHCP — pages there deauthed
        wifi, field 2026-07-18) or a network stream/track load is in
        flight. Bounded: a long-absent speaker stops yielding after
        YIELD_GIVEUP_S so markers can never starve reconnect."""
        if _radio.uptime() < WIFI_GATE_S and not _radio.wifi_settled():
            hold = True
        elif (self.disconnected_since is not None
                and time.monotonic() - self.disconnected_since
                > YIELD_GIVEUP_S):
            hold = False  # starvation belt: reconnect beats politeness
        else:
            hold = _radio.busy()
        if hold and not self._yield_logged:
            self._yield_logged = True
            log("radio busy (wifi setup / track load) — holding the page")
        elif not hold:
            self._yield_logged = False
        return hold

    def enter_steady(self, why):
        if self.state != "STEADY":
            log(f"steady: {self.target} ({why})")
        self.state = "STEADY"
        # NO success bookkeeping here: this fires on the bare ACL, and
        # a peer that accepts the page but refuses AVDTP reaches steady
        # every 3-7s — resetting the ladder/away timer here is what let
        # the field loop run forever. _commit_bt does it once audio is
        # real.
        self.cancel_timer()
        if not self._pcm_waiting:
            self._pcm_waiting = True
            self._pcm_tries = 10
            self._await_pcm()

    def _commit_bt(self):
        """The one place a connection counts as a SUCCESS: we are
        announcing bt for this steady period, either because the A2DP
        PCM exists or because the nudge fallback committed (a peer that
        held a stable ACL through the whole 10s wait is not the 1-3s
        refusal pattern). Only now do the ladder, the away timer and
        the refusal count reset."""
        if self.state == "STEADY":
            self._committed = True
            self.refusals = 0
            self.backoff = BACKOFF_MIN_S
            self.disconnected_since = None
        self._output("bt")

    def _await_pcm(self):
        """Announce bt only once the A2DP PCM actually exists: the
        announcement restarts go-librespot, and doing that while AVDTP
        is still configuring TORE FRESH CONNECTIONS DOWN (field log:
        SelectCodec 'Resource temporarily unavailable' -> transport
        freed -> disconnect -> fallback to local -> reconnect -> ...,
        an output flap loop that also made mpv skip episodes)."""
        if self.state != "STEADY":
            self._pcm_waiting = False
            return
        try:
            from vibb import btbus
            ready = btbus.a2dp_pcm_present(self.target)
            if ready:
                from vibb import audio
                if audio.stack() == "pipewire":
                    # the transport precedes its sink node by ms, and a
                    # pcm pinned to an absent node fails at hw_params —
                    # the announce retargets mpv onto that pcm, so wait
                    # for the node too (PLAN-pipewire-soloist §D)
                    ready = audio.sink_ready("bt", self.target)
        except Exception:
            ready = True  # can't tell — announce rather than stall
        if ready:
            self._pcm_waiting = False
            self._nudged = False
            self._commit_bt()
            return
        if self._pcm_tries <= 0:
            self._pcm_waiting = False
            if not self._nudged:
                # Connected but no audio transport: some speakers' own
                # reconnect brings only the control link (AVRCP) and the
                # A2DP profile never comes up — the box then sits
                # 'connected but silent' until someone presses connect
                # (field log 2026-07-17 19:02). Device1.Connect on an
                # already-connected device connects the MISSING profiles.
                # Exactly one nudge per steady period, then fall back to
                # today's announce-anyway.
                self._nudged = True
                self._nudge_a2dp()
            else:
                self._commit_bt()  # last resort: pre-nudge behavior
            return
        self._pcm_tries -= 1
        GLib.timeout_add(1000, self._await_pcm_tick)

    def _await_pcm_tick(self):
        self._await_pcm()
        return False

    def _nudge_a2dp(self):
        """Force the missing A2DP profile up on an already-connected
        device. Same guards as any attempt: never while another connect
        is in flight, never without the cross-process flock (bt.py may
        own the radio). Success re-enters steady, which re-arms the PCM
        wait; failure announces anyway (the pre-nudge behavior)."""
        if self.state != "STEADY" or self.connecting:
            self._output("bt")
            return
        lock = acquire_process_lock(blocking=False)
        if lock is None:
            self._output("bt")  # bt.py owns the radio — let it finish
            return
        self.connecting = self.target
        self.lock = lock
        _radio.touch_paging()
        log(f"connected but no A2DP transport — nudging profiles "
            f"({self.target})")
        try:
            dev = dbus.Interface(
                self.bus.get_object(BLUEZ, dev_path(self.target),
                                    introspect=False),
                "org.bluez.Device1")
            dev.Connect(reply_handler=self._nudge_ok,
                        error_handler=self._nudge_err,
                        timeout=CONNECT_TIMEOUT_S)
        except Exception:
            self._finish_attempt()
            self._output("bt")

    def _nudge_ok(self):
        self._finish_attempt()
        self.enter_steady("a2dp nudged")  # re-arms the PCM wait

    def _nudge_err(self, err):
        self._finish_attempt()
        try:
            nm = err.get_dbus_name()
        except Exception:
            nm = err.__class__.__name__
        log(f"a2dp nudge failed ({nm}) — announcing anyway")
        self._commit_bt()

    def _output(self, device):
        """Follow-the-speaker output policy: connected -> bt, confirmed
        away/forgotten -> built-in (vibbd skips the fallback when no
        I2S card exists, so BT-only boxes are unaffected). Announced at
        most once per transition — flapping links can't restart
        go-librespot in a loop."""
        self._want_output = device
        if device == self.announced:
            return
        try:
            r = boxapi.post("/output", {"device": device, "fallback": True},
                            timeout=5)
        except Exception as e:
            # vibbd not up yet (boot: we connect the speaker before the
            # daemon listens) — retry until the announcement lands
            log(f"output -> {device} not applied ({e.__class__.__name__}) "
                f"— retrying in 10s")
            GLib.timeout_add(10000, self._output_retry)
            return
        self.announced = device
        if r.get("skipped"):
            log(f"output -> {device} skipped ({r['skipped']})")
        elif not r.get("unchanged"):
            log(f"output -> {device} (speaker "
                f"{'connected' if device == 'bt' else 'away'})")

    def _output_retry(self):
        want = getattr(self, "_want_output", None)
        if want and want != self.announced:
            self._output(want)
        return False

    def _notify_lost(self):
        """Tell vibbd the transport just died. mpv reacts to a dead
        ALSA device by ERRORING each episode and auto-advancing — field
        log 2026-07-17: ~15 episodes skipped in 3s before the output
        fallback caught up (the stall watchdog can't see it: the
        position isn't frozen, it's flying). The daemon stops playback
        (bookmark survives) and puts the choice on the screen. A hint,
        not a command: fire-and-forget, no retry — the daemon re-checks
        output + player state itself, and never on the radio path."""
        try:
            boxapi.post("/bt/lost", {}, timeout=3)
        except Exception as e:
            log(f"lost-notify failed ({e.__class__.__name__}) — "
                "the output fallback remains the backstop")

    # --- follow-the-connector (adopt) ----------------------------------------

    def _inbound_connected(self, path):
        """A non-target device's Connected flipped true. Debounce per
        mac, then confirm after a short delay off the signal path."""
        mac = path.rsplit("/dev_", 1)[-1].replace("_", ":").upper()
        if len(mac) != 17 or mac == self.target:
            return
        now = time.monotonic()
        last = self.adopt_seen.get(mac)
        if last is not None and now - last < ADOPT_DEBOUNCE_S:
            return
        self.adopt_seen[mac] = now
        GLib.timeout_add(int(ADOPT_CONFIRM_S * 1000),
                         self._adopt_confirm, mac)

    def _adopt_confirm(self, mac):
        if mac == (read_target() or self.target):
            return False  # a manual switch already landed here — no-op
        props = self._dev_props(mac)
        uuids = [str(u).lower() for u in (props or {}).get("UUIDs") or []]
        if (not props or not props.get("Paired")
                or not props.get("Connected")
                or AUDIO_SINK_UUID not in uuids):
            return False  # gone again / not bonded / not a speaker
        if self.target and self._connected():
            # The configured speaker is ALIVE — it keeps the audio. The
            # Skoda connects itself whenever the ignition turns while a
            # kid listens on the headset in the back seat; stealing the
            # stream onto the car's speakers then would be the new bug.
            # But don't leave the newcomer parked next to the live link
            # either — one polite kick per KICK_RETRY_S (see above).
            last = self.kicked.get(mac)
            if last is None or time.monotonic() - last > KICK_RETRY_S:
                self.kicked[mac] = time.monotonic()
                log(f"{mac} connected while {self.target} is active — "
                    "disconnecting it (one link at a time)")
                self._kick(mac)
            return False
        log(f"paired speaker {mac} connected by itself while "
            f"{self.target or 'no speaker'} is away — adopting it")
        threading.Thread(target=self._adopt_post, args=(mac,),
                         daemon=True).start()
        return False  # one-shot timer

    def _adopt_post(self, mac):
        """vibbd owns the switch (quiesce, alias rewrite, resume) —
        same path as picking the speaker in the PWA. Off the GLib loop:
        /bt/connect legitimately runs for minutes when it heals."""
        try:
            boxapi.post("/bt/connect", {"mac": mac}, timeout=250)
        except Exception as e:
            log(f"adopt of {mac} failed ({e.__class__.__name__}) — "
                "target unchanged")

    def _dev_props(self, mac):
        try:
            p = dbus.Interface(
                self.bus.get_object(BLUEZ, dev_path(mac), introspect=False),
                "org.freedesktop.DBus.Properties")
            return p.GetAll("org.bluez.Device1", timeout=5)
        except Exception:
            return None

    def _kick(self, mac):
        """Async Disconnect of a non-target device — a hangup on an
        existing ACL, not a page, so no radio flock needed. Failure is
        fine: the link we tried to drop just stays parked."""
        try:
            dev = dbus.Interface(
                self.bus.get_object(BLUEZ, dev_path(mac), introspect=False),
                "org.bluez.Device1")
            dev.Disconnect(
                reply_handler=lambda: None,
                error_handler=lambda e: log(
                    f"kick of {mac} failed "
                    f"({getattr(e, 'get_dbus_name', lambda: e)()})"),
                timeout=CONNECT_TIMEOUT_S)
        except Exception as e:
            log(f"kick of {mac} failed ({e.__class__.__name__})")

    # --- the attempt ---------------------------------------------------------

    def attempt(self, why, debounce=False):
        if not self.target:
            self.state = "NO_TARGET"
            return
        if self.connecting:
            return
        now = time.monotonic()
        if debounce and now - self.last_attempt < DEBOUNCE_S:
            return
        if self._connected():
            self.enter_steady("already connected")
            return
        lock = acquire_process_lock(blocking=False)
        if lock is None:
            # bt.py owns the radio (pairing/switching) — don't stack a
            # page on top of it; retry shortly
            self.schedule(LOCK_RETRY_S, None)
            return
        self.last_attempt = now
        self.connecting = self.target
        self.lock = lock
        self.cancel_timer()
        _radio.touch_paging()  # a page is going on the air — player waits
        log(f"connecting {self.target} ({why})")
        try:
            # fresh proxy per attempt (never cached across bluez restarts);
            # introspect=False keeps proxy creation non-blocking
            dev = dbus.Interface(
                self.bus.get_object(BLUEZ, dev_path(self.target),
                                    introspect=False),
                "org.bluez.Device1")
            dev.Connect(reply_handler=self._connect_ok,
                        error_handler=self._connect_err,
                        timeout=CONNECT_TIMEOUT_S)
        except Exception as e:
            self._attempt_failed(e.__class__.__name__)

    def _connect_ok(self):
        was = self._finish_attempt()
        if was != self.target:
            # retargeted while the old connect was in flight — the
            # steady state we just reached is for the WRONG device
            self.attempt("retarget (stale connect)")
            return
        self.enter_steady("connected")

    def _connect_err(self, err):
        name = getattr(err, "get_dbus_name", lambda: "")() or ""
        if name.endswith(".AlreadyConnected"):
            self._connect_ok()
        else:
            self._attempt_failed(name or str(err))

    def _attempt_failed(self, detail):
        was = self._finish_attempt()
        if was is not None and was != self.target:
            self.attempt("retarget (stale connect)")
            return
        if self.state == "STEADY" or self._connected():
            # a racing attempt lost to a successful connection (the
            # speaker paged us while we paged it) — nothing is wrong,
            # and touching the output here flapped it mid-playback
            return
        if self.state == "BOOT":
            # NotReady = the adapter isn't powered yet, i.e. the page
            # never went ON the air — that's boot machinery warming up,
            # not evidence the speaker is off, so it doesn't count
            if "NotReady" not in detail:
                self.boot_fails += 1
            if self.boot_fails >= BOOT_FAIL_LIMIT:
                # the speaker is really off/away: stop the 5s boot
                # cadence early (it would burn a page every 5s exactly
                # while wifi associates) and take the backoff ladder
                log(f"{self.boot_fails} boot pages failed — the speaker "
                    "looks off; switching to the patient ladder")
                self.state = "WAITING"
            elif time.monotonic() < self.boot_deadline:
                self.schedule(BOOT_RETRY_S, None)
                return
            else:
                self.state = "WAITING"
        log(f"connect failed ({detail}) — next blind attempt in "
            f"{int(self.backoff)}s")
        if self.disconnected_since is None:
            self.disconnected_since = time.monotonic()
        # NO automatic switch to the built-in speaker (owner decision
        # 2026-08-13). It used to flip the output here once the speaker
        # had been away FALLBACK_S. Nothing played through it at the
        # time — the fault handling had already killed mpv — but the
        # NEXT thing to start audio did: a play press, a boot resume, a
        # blip resume, all landing on the HAT amplifier at a volume the
        # parent set for quiet headphones. On a bedtime box, going
        # silent is the right failure: the story continuing out loud in
        # a dark room is not a recovery, it is a fright. The speaker is
        # still one press away — the play-against-an-absent-speaker
        # popup offers it, with a person present and the screen lit.
        # (A FORGOTTEN speaker still falls back at _on_target_changed:
        # with no bt target at all, local is the only output there is.)
        away_s = time.monotonic() - self.disconnected_since
        if away_s >= ABSENT_AFTER_S:
            log(f"speaker away {int(away_s / 60)} min — parking blind pages "
                "(an inbound page, nearby evidence or a play press wakes "
                "them instantly)")
            return  # stay in WAITING, just stop paying for politeness
        fresh = away_s < RECENT_DROP_S
        self.schedule(min(self.backoff, RECENT_RETRY_S) if fresh
                      else self.backoff, None)
        self.backoff = min(self.backoff * 2, BACKOFF_MAX_S)

    def _finish_attempt(self):
        _radio.clear_paging()  # the single funnel every attempt exits by
        was, self.connecting = self.connecting, None
        lock, self.lock = self.lock, None
        if lock is not None:
            try:
                lock.close()  # flock releases with the fd
            except OSError:
                pass
        return was

    # --- helpers -------------------------------------------------------------

    def _connected(self):
        try:
            props = dbus.Interface(
                self.bus.get_object(BLUEZ, dev_path(self.target),
                                    introspect=False),
                "org.freedesktop.DBus.Properties")
            return bool(props.Get("org.bluez.Device1", "Connected",
                                  timeout=5))
        except Exception:
            return False  # no object / bluez down — attempt will tell

    def _adapter_up(self):
        """BOOT only — mirrors the bash loop's 'power on' retries. Without
        Pairable the eventual pairing would be non-bonding (bt.py lore)."""
        try:
            props = dbus.Interface(self.bus.get_object(BLUEZ, ADAPTER_PATH,
                                                       introspect=False),
                                   "org.freedesktop.DBus.Properties")
            for prop in ("Powered", "Pairable"):
                props.Set("org.bluez.Adapter1", prop, dbus.Boolean(True),
                          timeout=5)
        except Exception:
            pass  # bluez not up yet — that's what the boot window is for

    def schedule(self, secs, why):
        self.cancel_timer()
        if os.path.exists(BT_QUIET_FILE):
            # The user explicitly chose the built-in speaker — park blind
            # reconnect pages (they saturate the shared 2.4GHz radio and can
            # provoke the controller). Event-driven revival still fires: the
            # switch-to-bt kick, the speaker paging us, RSSI/appearance. A
            # speaker DROP never sets the marker (btwatchd's own fallback),
            # so drop-recovery keeps its full ladder.
            if why:
                log(f"{why} — parked (user chose the built-in speaker)")
            return
        if why:
            log(f"{why} — retry in {int(secs)}s")
        self.timer = GLib.timeout_add(int(secs * 1000), self._timer_fire)

    def _timer_fire(self):
        self.timer = None
        if self.state == "BOOT":
            self._boot_tick()
        elif self.state == "WAITING":
            if time.monotonic() < self.boot_deadline:
                # dropped out of BOOT early (boot_fails) but the adapter
                # bring-up retries still belong to the boot window
                self._adapter_up()
            if self._radio_yield():
                self.schedule(YIELD_RETRY_S, None)
                return False
            self.attempt("timer")
        return False  # one-shot; outcomes schedule the next one

    def cancel_timer(self):
        if self.timer is not None:
            GLib.source_remove(self.timer)
            self.timer = None


def main():
    # rfkill runs BEFORE any bus traffic (plan pitfall 10): a blocked
    # radio makes bluez unresponsive, and the block persists reboots
    subprocess.run(["rfkill", "unblock", "bluetooth"], capture_output=True)
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    addr = (os.environ.get("VIBB_DBUS_ADDRESS")
            or os.environ.get("DBUS_SYSTEM_BUS_ADDRESS"))
    try:
        bus = dbus.bus.BusConnection(addr) if addr else dbus.SystemBus()
    except Exception as e:
        _fallback(f"cannot reach the system bus ({e.__class__.__name__})")
    bus.set_exit_on_disconnect(True)  # dbus-daemon restart -> systemd respawn
    r = Reconnector(bus)
    r.subscribe()
    r.watch_mac_file()
    r.watch_kick_file()
    log(f"event-driven reconnect up — target {r.target or '(none)'}")
    r.enter_boot()
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
