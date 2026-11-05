"""WiFi management via nmcli (RPi OS ships NetworkManager): scan/join/forget,
the setup hotspot with its fresh-box watchdog, and the rfkill on/off toggle.
Extracted verbatim from daemon.py."""

import json
import os
import re
import socket
import subprocess
import threading
import time


def log(msg):
    print(f"vibbd: {msg}", flush=True)


def _run_out(args):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _wifi_blocked():
    """rfkill state via its JSON interface (made for scripts and stable);
    the human text layout is only the fallback."""
    out = _run_out(["rfkill", "-J"])
    try:
        devices = next(iter(json.loads(out).values()))
        for d in devices:
            if d.get("type") == "wlan":
                return "blocked" in (d.get("soft"), d.get("hard"))
    except (ValueError, StopIteration, AttributeError, TypeError):
        pass
    out = _run_out(["rfkill", "list", "wifi"]).lower()
    return "blocked: yes" in out


def wifi_state():
    """(enabled, ssid, ip) — enabled means not rfkill-blocked."""
    enabled = not _wifi_blocked()
    ssid = None
    for line in _run_out(["iw", "dev", "wlan0", "link"]).splitlines():
        if line.strip().startswith("SSID:"):
            ssid = line.split(":", 1)[1].strip()
    ip = (_run_out(["hostname", "-I"]).split() or [None])[0]
    return enabled, ssid, ip


# /system is polled every 30s by the screen (plus any open PWA tab), and
# each answer forked rfkill+iw+hostname+nmcli for values that only
# change when WE change them or on rare DHCP/roam events (architect
# power audit 2026-08-10 #3). Snapshot with a TTL; every mutating path
# below invalidates, so user-visible changes are never stale.
SNAP_TTL_S = float(os.environ.get("VIBB_NET_SNAP_TTL", "120"))
_snap = {"at": 0.0, "val": None}


def invalidate_snapshot():
    _snap["val"] = None


def wifi_snapshot():
    """(enabled, ssid, ip, hotspot_active) — the /system answer, cached."""
    now = time.monotonic()
    if _snap["val"] is None or now - _snap["at"] > SNAP_TTL_S:
        en, ssid, ip = wifi_state()
        _snap["val"] = (en, ssid, ip, hotspot_active())
        _snap["at"] = now
    return _snap["val"]


def _rfkill(enabled):
    invalidate_snapshot()
    try:
        subprocess.run(["rfkill", "unblock" if enabled else "block", "wifi"],
                       timeout=10)
        return None
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)


def set_wifi(enabled):
    """User-facing switch (PWA/screen). Turning wifi ON also grants a fresh
    auto-off grace window; turning it OFF marks the block as deliberate so
    the auto-off prober won't sneak it back on."""
    err = _rfkill(enabled)
    if err:
        return {"error": err}
    log(f"wifi {'unblock' if enabled else 'block'}ed")
    now = time.monotonic()
    if enabled:
        _auto.update(last_ok=now, blocked=False)
    else:
        _auto.update(blocked=False)
    en, ssid, ip = wifi_state()
    return {"enabled": en, "ssid": ssid, "ip": ip}


def wifi_reconnect(window_s=30):
    """On-demand reconnect: unblock the radio and actively wait up to
    window_s for a known network to associate (NetworkManager auto-joins
    once the radio is up). This is the 'get the net back NOW' button —
    the offline-Spotify popup's X — a far better fit than waiting out the
    auto-off prober's 10-minute timer when someone is standing there
    wanting it. Returns {ok, ssid}; None while another wifi op runs.
    The caller quiesces A2DP first: the scan shares the 2.4GHz radio."""
    if not WIFI_LOCK.acquire(blocking=False):
        return None
    try:
        _rfkill(True)
        _auto.update(last_ok=time.monotonic(), blocked=False)
        ssid = None
        deadline = time.monotonic() + window_s
        while time.monotonic() < deadline and not ssid:
            time.sleep(3)
            _en, ssid, _ip = wifi_state()
        log(f"wifi reconnect: {'joined ' + ssid if ssid else 'nothing found'}")
        return {"ok": bool(ssid), "ssid": ssid}
    finally:
        WIFI_LOCK.release()


AVAHI_CONF = os.environ.get("VIBB_AVAHI_CONF",
                            "/etc/avahi/avahi-daemon.conf")


def mdns_host():
    """The box's stable '<name>.local' address.

    install.sh writes the chosen box name as avahi's host-name, so this
    survives every IP change — which is exactly why the PWA link uses it
    rather than an address: the browser stores the API token PER ORIGIN,
    so a token handed out against an IP is lost the moment DHCP moves the
    box or it comes up as its own hotspot. One stable origin means you
    link a phone once, ever.

    It resolves in BOTH modes: mDNS on the home LAN, and in hotspot mode
    the captive-portal resolver answers every name with the box's own
    address (dnsmasq-shared 'address=/#/10.42.0.1')."""
    name = ""
    try:
        with open(AVAHI_CONF) as f:
            for line in f:
                line = line.strip()
                if line.startswith("host-name="):
                    name = line.split("=", 1)[1].strip()
                    break
    except OSError:
        pass
    if not name:
        name = (socket.gethostname() or "vibb").split(".")[0]
    return f"{name}.local"


# --- wifi management (nmcli — RPi OS's NetworkManager) -----------------------------

WIFI_LOCK = threading.Lock()  # one scan/connect at a time
HOTSPOT_CON = "vibb-hotspot"
HOTSPOT_SSID = os.environ.get("VIBB_HOTSPOT_SSID") \
    or f"Vibb-{socket.gethostname()}"
# WPA-PSK is 8..63 chars: the old default 'vibb123' (7) made nmcli refuse
# every hotspot with "is not valid WPA PSK" (first Zero boot 2026-09-05)
HOTSPOT_PSK = os.environ.get("VIBB_HOTSPOT_PSK", "vibb1234")
WATCHDOG_DELAY_S = int(os.environ.get("VIBB_WIFI_WATCHDOG_DELAY", "45"))
# wifi auto-off: a disconnected wpa_supplicant scan-loops constantly
# (~10-20mA — 5-10% of playback draw); after wifi_auto_off_min without a
# known network we rfkill-block, then briefly probe every PROBE_INTERVAL
# so a parent's hotspot at the cabin is still found within ~10 minutes.
PROBE_INTERVAL_S = int(os.environ.get("VIBB_WIFI_PROBE_INTERVAL", "600"))
PROBE_WINDOW_S = int(os.environ.get("VIBB_WIFI_PROBE_WINDOW", "30"))
_auto = {"last_ok": 0.0, "blocked": False, "next_probe": 0.0,
         "probe_held": False}
# vibbd installs the real check at startup: hold the periodic probe
# while music streams over bluetooth. NM's scan shares the Zero 2 W's
# 2.4GHz radio with A2DP — mid-playback it stutters the audio and is the
# documented firmware-crash trigger (bt.py recover()). The probe is
# DEFERRED, not skipped: it fires on the first pass after playback stops.
probe_hold = [lambda: False]
# vibbd installs a callback here too: a code-driven SWITCH between
# networks (PWA "join this wifi" while already online) strands long-lived
# TCP connections — go-librespot's AP/dealer/spclient die silently and it
# spends minutes in 30-60s timeout storms that wedge its local API, which
# /status and /playpause then block on (field log 2026-07-17: frozen UI).
# The daemon's callback restarts it — ~5s and deterministic. Not needed on
# the wifi-was-off paths: no internet means the supervisor already parked
# go-librespot, and it starts fresh when connectivity returns.
net_changed = [lambda: None]
_last_scan = {"networks": [], "at": 0.0}  # wlan0 can't scan while in AP mode


def _nmcli(*args, timeout=60):
    try:
        r = subprocess.run(["nmcli", *args], capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except FileNotFoundError:
        return 127, "nmcli not found — this box does not use NetworkManager"
    except subprocess.TimeoutExpired:
        return 1, "nmcli timed out"


def _nm_unescape(s):
    """nmcli -t escapes ':' and '\\' in field values."""
    return s.replace("\\\\", "\0").replace("\\:", ":").replace("\0", "\\")


def _known_wifi_names():
    _code, out = _nmcli("-t", "-f", "NAME,TYPE", "connection", "show",
                        timeout=10)
    known = set()
    for line in out.splitlines():
        name, _, ctype = line.rpartition(":")
        if ctype == "802-11-wireless":
            known.add(_nm_unescape(name))
    return known


def hotspot_active():
    _code, out = _nmcli("-t", "-f", "NAME", "connection", "show", "--active",
                        timeout=10)
    return HOTSPOT_CON in [_nm_unescape(x) for x in out.splitlines()]


# AP mode beacons 10x/s with wifi power save off (~40-70mA) — a hotspot
# nobody is connected to must not run until the battery dies. The
# watchdog stops it after this long with zero associated clients; an
# idle-stop also stands down the fresh-box auto-AP until the next boot
# or an explicit start (else the watchdog would bring it right back).
HOTSPOT_IDLE_OFF_S = int(os.environ.get("VIBB_HOTSPOT_IDLE_OFF", 45 * 60))
_hs = {"last_client": 0.0, "idle_stopped": False}


def _hotspot_stations():
    """Associated AP clients — 'iw dev wlan0 station dump' prints one
    'Station <mac>' block per client. Unreadable -> assume someone is
    there (fail open: the hotspot stays up, never dies mid-setup)."""
    try:
        r = subprocess.run(["iw", "dev", "wlan0", "station", "dump"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return 1
        return sum(1 for ln in r.stdout.splitlines()
                   if ln.startswith("Station "))
    except (OSError, subprocess.TimeoutExpired):
        return 1


def start_hotspot():
    """Bring up the setup AP. Scans first — the radio can't scan in AP mode,
    so the portal's network picker serves this cached list."""
    invalidate_snapshot()
    _hs.update(last_client=time.monotonic(), idle_stopped=False)
    sc = wifi_scan()
    if sc and sc.get("ok") and sc.get("networks"):
        _last_scan.update(networks=sc["networks"], at=time.time())
    code, out = _nmcli("dev", "wifi", "hotspot", "ifname", "wlan0",
                       "con-name", HOTSPOT_CON, "ssid", HOTSPOT_SSID,
                       "password", HOTSPOT_PSK, timeout=30)
    log(f"hotspot {HOTSPOT_SSID}: {'up' if code == 0 else 'FAILED: ' + out.splitlines()[-1] if out else 'FAILED'}")
    return code == 0


def stop_hotspot():
    invalidate_snapshot()
    _nmcli("connection", "down", HOTSPOT_CON, timeout=15)
    _nmcli("connection", "delete", "id", HOTSPOT_CON, timeout=15)
    log("hotspot stopped")


def _link_up():
    try:
        with open("/sys/class/net/wlan0/operstate") as f:
            return f.read().strip() == "up"
    except OSError:
        return False


def _wifi_watchdog():
    """Two jobs, both keyed on 'the link is down':

    Fresh-box onboarding: no saved wifi network and nothing connected
    -> start the setup hotspot. Boxes WITH saved networks never auto-AP
    (a cabin trip must not burn battery on a pointless hotspot) — there
    the PWA/screen button starts it explicitly.

    Wifi auto-off: a box with saved networks that can't find any of them
    scan-loops for nothing; after wifi_auto_off_min (0 = never) we block
    the radio, then re-probe for PROBE_WINDOW_S every PROBE_INTERVAL_S and
    stay on only when a known network actually takes us in. Turning wifi
    on via PWA/screen (set_wifi) always grants a fresh grace window; a
    manual 'wifi off' is never probed back on. Never triggers during
    playback of streams by construction — streaming means the link is up.
    Offline playback (cached content over bluetooth) is protected too:
    probe_hold defers the probe until the music stops."""
    from vibb.sysinfo import load_settings
    time.sleep(WATCHDOG_DELAY_S)
    _auto["last_ok"] = time.monotonic()
    while True:
        try:
            # Cheap first: one sysfs read. Only when the link is down do we
            # pay for the rfkill/iw/nmcli subprocess probes — a battery box
            # must not spawn processes every 30s around the clock.
            now = time.monotonic()
            if _link_up():
                _auto.update(last_ok=now, blocked=False)
                # In AP mode the link reads 'up' too — pay one iw fork
                # per 30s only while the hotspot runs, and stop it when
                # nobody has been connected for HOTSPOT_IDLE_OFF_S.
                if hotspot_active():
                    if _hotspot_stations() > 0:
                        _hs["last_client"] = now
                    elif now - _hs["last_client"] > HOTSPOT_IDLE_OFF_S:
                        log(f"setup hotspot idle {HOTSPOT_IDLE_OFF_S // 60} "
                            "min with no clients — stopping to save battery")
                        _hs["idle_stopped"] = True
                        stop_hotspot()
                time.sleep(30)
                continue
            enabled, ssid, _ip = wifi_state()
            if enabled:
                if ssid or hotspot_active():
                    _auto.update(last_ok=now, blocked=False)
                elif not _known_wifi_names():
                    if _hs["idle_stopped"]:
                        pass  # already gave up once — wait for boot/button
                    else:
                        log("no saved wifi + not connected — starting setup "
                            "hotspot")
                        start_hotspot()
                else:
                    s = load_settings()
                    auto_min = s.get("wifi_auto_off_min", 0)
                    if auto_min and now - _auto["last_ok"] > auto_min * 60:
                        log(f"no known network for {auto_min} min — wifi off "
                            + (f"(probing every {PROBE_INTERVAL_S // 60} min)"
                               if s.get("wifi_probe", 1) else
                               "(probing disabled — reconnect manually)"))
                        _rfkill(False)
                        _auto.update(blocked=True, probe_held=False,
                                     next_probe=now + PROBE_INTERVAL_S)
            elif _auto["blocked"] and now >= _auto["next_probe"]:
                if not load_settings().get("wifi_probe", 1):
                    # the parent turned probing off (PWA setting): the
                    # radio stays down until an explicit reconnect
                    # (set_wifi) — no scans at all, so bt playback can
                    # never be disturbed, and zero standby battery spend
                    time.sleep(30)
                    continue
                if probe_hold[0]():
                    # see probe_hold above: no 2.4GHz scan mid-A2DP
                    if not _auto["probe_held"]:
                        _auto["probe_held"] = True
                        log("wifi probe: held — music playing over "
                            "bluetooth (probing when it stops)")
                    time.sleep(30)
                    continue
                _auto["probe_held"] = False
                log("wifi probe: looking for known networks")
                _rfkill(True)  # NetworkManager scans + auto-joins known nets
                found = None
                deadline = time.monotonic() + PROBE_WINDOW_S
                while time.monotonic() < deadline and not found:
                    time.sleep(5)
                    _en, found, _ = wifi_state()
                if found:
                    log(f"wifi probe: reconnected to {found!r}")
                    _auto.update(last_ok=time.monotonic(), blocked=False)
                else:
                    log("wifi probe: nothing known nearby — off again")
                    _rfkill(False)
                    _auto["next_probe"] = time.monotonic() + PROBE_INTERVAL_S
        except Exception as e:
            log(f"wifi watchdog error: {e!r}")
        time.sleep(30)


def wifi_scan():
    """Nearby networks, strongest first. None = busy."""
    if not WIFI_LOCK.acquire(blocking=False):
        return None
    try:
        if hotspot_active():  # AP mode: serve the pre-hotspot scan
            return {"ok": True, "cached": True, "hotspot": True,
                    "networks": _last_scan["networks"]}
        code, out = _nmcli("-t", "-f", "IN-USE,SIGNAL,SECURITY,SSID",
                           "dev", "wifi", "list", "--rescan", "yes",
                           timeout=30)
        if code != 0:
            return {"ok": False, "networks": [],
                    "output": out.splitlines()[-1] if out else "scan failed"}
        nets = {}
        for line in out.splitlines():
            parts = line.split(":", 3)  # SSID last -> its colons survive
            if len(parts) != 4:
                continue
            in_use, signal, security, ssid = parts
            ssid = _nm_unescape(ssid)
            if not ssid:
                continue  # hidden network
            entry = {"ssid": ssid,
                     "signal": int(signal) if signal.isdigit() else 0,
                     "secured": bool(security and security != "--"),
                     "in_use": in_use == "*"}
            cur = nets.get(ssid)  # several BSSIDs -> keep the strongest
            if cur is None or entry["signal"] > cur["signal"]:
                if cur and cur["in_use"]:
                    entry["in_use"] = True
                nets[ssid] = entry
        known = _known_wifi_names()
        for n in nets.values():
            n["known"] = n["ssid"] in known
        return {"ok": True,
                "networks": sorted(nets.values(),
                                   key=lambda n: (-n["in_use"], -n["signal"]))}
    finally:
        WIFI_LOCK.release()


def wifi_connect(ssid, password=None):
    """Join a network (uses the saved profile when one exists). None = busy."""
    invalidate_snapshot()
    if not WIFI_LOCK.acquire(blocking=False):
        return None
    try:
        was_hotspot = hotspot_active()
        if was_hotspot:
            log("leaving the setup hotspot to join a network...")
            _nmcli("connection", "down", HOTSPOT_CON, timeout=15)
        if password:
            code, out = _nmcli("dev", "wifi", "connect", ssid,
                               "password", password, timeout=75)
        elif ssid in _known_wifi_names():
            code, out = _nmcli("connection", "up", "id", ssid, timeout=75)
        else:
            code, out = _nmcli("dev", "wifi", "connect", ssid, timeout=75)
        tail = "\n".join(out.splitlines()[-3:])
        log(f"wifi connect {ssid!r} -> exit {code}")
        if code != 0 and was_hotspot:
            # Let the user retry from the portal instead of stranding them
            _nmcli("connection", "up", HOTSPOT_CON, timeout=30) \
                if _hotspot_profile_exists() else start_hotspot()
            tail += "\nsetup hotspot restored — reconnect and retry"
        enabled, cur, ip = wifi_state()
        if code == 0:
            _tune_profile(cur or ssid)
            try:
                net_changed[0]()  # see the hook comment above
            except Exception as e:
                log(f"net-changed hook failed: {e!r}")
        return {"ok": code == 0, "output": tail, "ssid": cur, "ip": ip}
    finally:
        WIFI_LOCK.release()


def _tune_profile(name):
    """Best-effort NM tuning for every profile the portal/PWA creates
    (install.sh does the same for profiles present at install time):

    - clear the default background scan (simple:30:-70 — a full
      off-channel sweep every 30s at weak signal: A2DP stutter and
      battery burn on the shared radio);
    - disable IPv6 on the connection. The box is IPv4-only end to end
      (daemon binds 0.0.0.0, go-librespot/pisugar on 127.0.0.1, avahi
      advertises vibb.local over IPv4). With only a link-local fe80::
      and no global route, go-librespot tried Spotify over IPv6 at boot
      and fatal-crashed 'network is unreachable' before systemd's
      restart recovered it on IPv4 (field 2026-07-20 08:18:34). Killing
      IPv6 on the interface removes the whole trap — Go never sees an
      IPv6 address to try. Applies on the next activation (like the
      bgscan strip); dual-stack networks just use IPv4 there too.

    Both settle in one nmcli call. Older NM lacking a property exits
    nonzero — fine, the box loses nothing either way."""
    _nmcli("connection", "modify", "id", name,
           "802-11-wireless.bgscan", "",
           "ipv6.method", "disabled", timeout=10)


def _hotspot_profile_exists():
    _c, out = _nmcli("-t", "-f", "NAME", "connection", "show", timeout=10)
    return HOTSPOT_CON in [_nm_unescape(x) for x in out.splitlines()]


def wifi_add(ssid, password=None):
    """Save a network profile WITHOUT the network being in range —
    pre-provision the cabin/grandparent wifi before travelling there.
    NetworkManager auto-joins when it first sees it (and the auto-off
    prober finds it too). None = busy."""
    invalidate_snapshot()
    if not WIFI_LOCK.acquire(blocking=False):
        return None
    try:
        if password is not None and not 8 <= len(password) <= 63:
            return {"ok": False,
                    "output": "WPA password must be 8-63 characters"}
        known = ssid in _known_wifi_names()
        if known and password:
            code, out = _nmcli("connection", "modify", "id", ssid,
                               "802-11-wireless-security.key-mgmt", "wpa-psk",
                               "802-11-wireless-security.psk", password,
                               timeout=15)
            action = "password updated"
        elif known:
            return {"ok": False, "output": f"{ssid!r} is already saved"}
        else:
            args = ["connection", "add", "type", "wifi", "ifname", "wlan0",
                    "con-name", ssid, "ssid", ssid]
            if password:
                args += ["802-11-wireless-security.key-mgmt", "wpa-psk",
                         "802-11-wireless-security.psk", password]
            code, out = _nmcli(*args, timeout=15)
            action = "saved"
        log(f"wifi add {ssid!r} -> exit {code}")
        if code != 0:
            return {"ok": False,
                    "output": out.splitlines()[-1] if out else "nmcli failed"}
        _tune_profile(ssid)
        return {"ok": True,
                "output": f"{action} — the box joins it automatically "
                          f"when in range"}
    finally:
        WIFI_LOCK.release()


def wifi_forget(ssid):
    invalidate_snapshot()
    if not WIFI_LOCK.acquire(blocking=False):
        return None
    try:
        code, out = _nmcli("connection", "delete", "id", ssid, timeout=15)
        log(f"wifi forget {ssid!r} -> exit {code}")
        return {"ok": code == 0,
                "output": "\n".join(out.splitlines()[-2:])}
    finally:
        WIFI_LOCK.release()


