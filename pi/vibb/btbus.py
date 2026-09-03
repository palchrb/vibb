"""Bluetooth TRANSPORT layer — one narrow primitive surface, two backends.

  cli   bluetoothctl / bluealsa-aplay text parsing (the proven path)
  dbus  direct BlueZ + bluealsa D-Bus (PLAN-bt-dbus.md; preferred by
        `auto` since the parity gate passed on the rig 2026-07-07)

Selected once per process via VIBB_BT_BACKEND=cli|dbus|auto (default
auto). Flow logic — pairing retries, stale-key handling, one-output
policy, MAC_FILE/ASOUND routing — lives in bt.py and never sees which
backend ran. Error CLASSIFICATION is the contract here: pair() returns
one of the PAIR_* constants; the cli backend maps regexes, the dbus
backend maps typed org.bluez.Error.* names.

Phase status (PLAN-bt-dbus.md §3): the dbus backend currently covers
the READ primitives (A1); action primitives delegate to the cli
implementation until phase B lands. Importing this module must never
require dbus — all dbus imports are lazy.
"""

import json
import os
import re
import subprocess
import time

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# pair() classification — the contract bt.py's flow logic branches on
PAIR_OK = "ok"
PAIR_ALREADY = "already-paired"
PAIR_AUTH_FAILED = "auth-failed"     # stale key: clearing the bond is right
PAIR_NOT_AVAILABLE = "not-available"  # never seen during scan
PAIR_ERROR = "error"

# remove_device() classification
REMOVE_OK = "ok"
REMOVE_NOT_FOUND = "not-found"       # already gone: treated as success
REMOVE_ERROR = "error"


def log(msg):
    print(msg, flush=True)


def _run(args, timeout=30):
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, _ANSI.sub("", r.stdout + r.stderr)
    except subprocess.TimeoutExpired:
        return 1, "(timed out)"
    except FileNotFoundError as e:
        return 127, str(e)


def _ctl(*args, timeout=30):
    return _run(["bluetoothctl", *args], timeout=timeout)


# --- backend selection -----------------------------------------------------------

_BACKEND = None  # resolved once per process


def backend():
    global _BACKEND
    if _BACKEND is None:
        want = os.environ.get("VIBB_BT_BACKEND", "auto")
        if want == "cli":
            _BACKEND = "cli"  # explicit kill switch
        else:
            # auto prefers dbus since the parity gate passed on the rig
            # (2026-07-07, tests/bt_parity.py: PARITY OK). Every dbus
            # read still degrades to the cli path on any bus failure.
            try:
                import dbus  # noqa: F401 — availability probe only
                _BACKEND = "dbus"
                log(f"bt backend: dbus ({want})")
            except ImportError:
                log("bt backend: dbus unavailable — using bluetoothctl")
                _BACKEND = "cli"
    return _BACKEND


def _dbus_read(fn_dbus, fn_cli, *args):
    """Run the dbus read primitive with the cli one as a safety net —
    a bus hiccup must degrade exactly like a bluetoothctl failure."""
    if backend() == "dbus":
        try:
            return fn_dbus(*args)
        except Exception as e:  # DBusException, missing name, ...
            log(f"bt dbus read failed ({e.__class__.__name__}) — cli fallback")
    return fn_cli(*args)


# --- primitives: adapter ---------------------------------------------------------

def adapter_power_on():
    if backend() == "dbus":
        try:
            _dbus_adapter_set("Powered", True)
            return
        except Exception as e:
            log(f"bt dbus power-on failed ({e.__class__.__name__}) — cli")
    _ctl("power", "on")


def adapter_pairable_on():
    """Without pairable on, BlueZ does a NON-BONDING pairing (key thrown
    away, bond gone after power cycle) — learned on real hardware."""
    if backend() == "dbus":
        try:
            _dbus_adapter_set("Pairable", True)
            return
        except Exception as e:
            log(f"bt dbus pairable failed ({e.__class__.__name__}) — cli")
    _ctl("pairable", "on")


def adapter_powered():
    return _dbus_read(_dbus_adapter_powered, _cli_adapter_powered)


def _cli_adapter_powered():
    _c, out = _ctl("show", timeout=10)
    return "Powered: yes" in out


# --- primitives: device listing / info -------------------------------------------

def paired_devices():
    """[{mac, name}] for every bonded device."""
    return _dbus_read(_dbus_paired_devices, _cli_paired_devices)


def connected_devices():
    return _dbus_read(_dbus_connected_devices, _cli_connected_devices)


def _cli_device_lines(args):
    _c, out = _ctl(*args, timeout=10)
    if args == ("devices", "Paired") and ("Invalid" in out or "Unknown" in out):
        _c, out = _ctl("paired-devices", timeout=10)  # older bluez
    devices = []
    for line in out.splitlines():
        parts = line.split(" ", 2)
        if len(parts) >= 2 and parts[0] == "Device":
            mac = parts[1]
            _c, info = _ctl("info", mac, timeout=10)
            devices.append({"mac": mac,
                            "name": parts[2] if len(parts) > 2 else mac,
                            "audio": bool(re.search(
                                r"Icon: audio|Audio Sink|0000110b",
                                info, re.I))})
    return devices


def _cli_paired_devices():
    return _cli_device_lines(("devices", "Paired"))


def _cli_connected_devices():
    return _cli_device_lines(("devices", "Connected"))


def device_info(mac):
    """{present, paired, connected, name} — present=False means BlueZ has
    no object for the device at all (never seen / removed)."""
    return _dbus_read(_dbus_device_info, _cli_device_info, mac)


def _cli_device_info(mac):
    _c, info = _ctl("info", mac, timeout=10)
    if not info.strip() or "not available" in info.lower():
        return {"present": False, "paired": False, "connected": False,
                "name": None}
    m = re.search(r"^\s*(?:Alias|Name): (.+)$", info, re.M)
    return {"present": True,
            "paired": "Paired: yes" in info,
            "connected": "Connected: yes" in info,
            "name": m.group(1).strip() if m else None}


# --- primitives: discovery -------------------------------------------------------

def populate_cache(secs):
    """Let BlueZ see the device before pair() — no result needed."""
    if backend() == "dbus":
        try:
            _dbus_discover(secs)
            return
        except Exception as e:
            log(f"bt dbus scan failed ({e.__class__.__name__}) — cli")
    _ctl("--timeout", str(secs), "scan", "on", timeout=secs + 15)


def discover(secs):
    """[{mac, name, audio, rssi}] for devices actually seen THIS window
    (BlueZ's cache of long-gone devices must never leak into pickers).
    rssi is None on the cli backend."""
    return _dbus_read(_dbus_discover, _cli_discover, secs)


def _cli_discover(secs):
    _c, out = _ctl("--timeout", str(secs), "scan", "on", timeout=secs + 15)
    macs = sorted({m.group(1).upper() for m in re.finditer(
        r"Device ((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})", out)})
    found = []
    for mac in macs:
        _c, info = _ctl("info", mac, timeout=10)
        m = re.search(r"^\s*Name: (.+)$", info, re.M)
        name = m.group(1).strip() if m else "(no name)"
        audio = bool(re.search(r"Icon: audio|Audio Sink|0000110b", info, re.I))
        found.append({"mac": mac, "name": name, "audio": audio, "rssi": None})
    return found


# --- primitives: actions ----------------------------------------------------------
# Phase B1: connect/disconnect/trust/remove run over D-Bus (typed
# org.bluez.Error.* names replace the regexes). Phase B2
# (PLAN-bt-b2-pairing.md): pairing over D-Bus with our own Agent1 exists
# behind VIBB_BT_PAIR=dbus — bluetoothctl stays the default until the
# rig matrix passes, and the permanent fallback after.

def pair(mac):
    """(classification, raw_output_for_log). Kill switch: the dbus/Agent1
    path runs only with VIBB_BT_PAIR=dbus until the B2 rig matrix
    passes (PLAN-bt-b2-pairing.md §6); bluetoothctl's built-in agent is
    the proven default and the permanent in-call fallback. Read per call
    so a `systemctl edit` needs only a service restart."""
    if backend() == "dbus" and os.environ.get("VIBB_BT_PAIR") == "dbus":
        try:
            return _dbus_pair(mac)
        except Exception as e:  # infra only; classified errors are the API
            log(f"bt dbus pair failed ({e.__class__.__name__}) — cli fallback")
    return _cli_pair(mac)


def _cli_pair(mac):
    code, out = _ctl("pair", mac, timeout=45)
    if code == 0:
        return PAIR_OK, out
    if re.search("AlreadyExists", out, re.I):
        return PAIR_ALREADY, out
    if re.search("AuthenticationFailed|AuthenticationCanceled|"
                 "AuthenticationRejected|AuthenticationTimeout", out, re.I):
        return PAIR_AUTH_FAILED, out
    if re.search("not available", out, re.I):
        return PAIR_NOT_AVAILABLE, out
    return PAIR_ERROR, out


def trust(mac):
    if backend() == "dbus":
        try:
            _dbus_trust(mac)
            return
        except Exception as e:
            log(f"bt dbus trust failed ({e.__class__.__name__}) — cli")
    _ctl("trust", mac, timeout=15)


def connect_device(mac):
    """(ok, detail) — one attempt; retries are flow logic in bt.py."""
    if backend() == "dbus":
        try:
            return _dbus_connect_device(mac)
        except Exception as e:  # non-DBus failure only; errors are the API
            log(f"bt dbus connect failed ({e.__class__.__name__}) — cli")
    code, out = _ctl("connect", mac, timeout=30)
    return code == 0, out


def disconnect_device(mac):
    if backend() == "dbus":
        try:
            return _dbus_disconnect_device(mac)
        except Exception as e:
            log(f"bt dbus disconnect failed ({e.__class__.__name__}) — cli")
    code, out = _ctl("disconnect", mac, timeout=15)
    return code == 0, out


def remove_device(mac):
    if backend() == "dbus":
        try:
            return _dbus_remove_device(mac)
        except Exception as e:
            log(f"bt dbus remove failed ({e.__class__.__name__}) — cli")
    code, out = _ctl("remove", mac, timeout=15)
    if code == 0:
        return REMOVE_OK, out
    if "not available" in out.lower():
        return REMOVE_NOT_FOUND, out
    return REMOVE_ERROR, out


def set_alias(mac, name):
    """Give a bonded device a custom display name via BlueZ
    Device1.Alias. Every listing already reads Alias, so the name flows
    to the PWA and the device screen with no display changes. An empty
    name CLEARS the alias — BlueZ then falls back to the device's real
    Name. dbus-native: bluetoothctl has no reliable one-shot per-device
    alias set, so the cli backend reports that instead of guessing.
    Returns (ok, effective_name_or_message)."""
    if backend() == "dbus":
        try:
            return _dbus_set_alias(mac, name)
        except Exception as e:
            name = getattr(e, "get_dbus_name", lambda: "")() or ""
            if "UnknownObject" in name or "DoesNotExist" in name:
                return False, "no such device"
            log(f"bt dbus set-alias failed ({e.__class__.__name__})")
            return False, f"rename failed: {e.__class__.__name__}"
    return False, "renaming a speaker needs the dbus backend"


def _dbus_set_alias(mac, name):
    import dbus
    props = dbus.Interface(_bus().get_object(_BLUEZ, _dev_path(mac)),
                           "org.freedesktop.DBus.Properties")
    props.Set("org.bluez.Device1", "Alias", dbus.String(name), timeout=10)
    # read back: an empty set makes BlueZ restore the remote Name, so the
    # caller (and the log) reports the name that actually took effect
    return True, str(props.Get("org.bluez.Device1", "Alias", timeout=10))


# error-name mapping: pure functions, unit-testable without a bus --------

def _map_connect_error(name, msg):
    """AlreadyConnected IS success (matches the cli flow's tolerance);
    everything else is a classified failure with the typed name kept for
    the log (bluez >=5.62 puts 'br-connection-page-timeout' etc. in msg)."""
    if name.endswith(".AlreadyConnected"):
        return True, "already connected"
    return False, f"{name}: {msg}"


def _map_disconnect_error(name, msg):
    if name.endswith(".NotConnected"):
        return True, "not connected"
    return False, f"{name}: {msg}"


def _map_remove_error(name, msg):
    if name.endswith(".DoesNotExist") or "UnknownObject" in name:
        return REMOVE_NOT_FOUND, f"{name}: {msg}"
    return REMOVE_ERROR, f"{name}: {msg}"


def _map_pair_error(name, msg):
    """Typed org.bluez.Error.* -> the PAIR_* contract. All four
    Authentication* variants mean 'stale key on one side' and route to
    bt.py's clear-and-retry-once branch (the cli regex already matched
    all four). ConnectionAttemptFailed (page timeout) and a missing
    device object both mean 'not seen' — the flow's scan-again message.
    NoReply is our own 60s budget expiring."""
    if name.endswith(".AlreadyExists"):
        return PAIR_ALREADY, f"{name}: {msg}"
    if (name.endswith(".AuthenticationFailed")
            or name.endswith(".AuthenticationCanceled")
            or name.endswith(".AuthenticationRejected")
            or name.endswith(".AuthenticationTimeout")):
        return PAIR_AUTH_FAILED, f"{name}: {msg}"
    if (name.endswith(".ConnectionAttemptFailed")
            or "UnknownObject" in name or "UnknownMethod" in name):
        return PAIR_NOT_AVAILABLE, f"{name}: {msg}"
    return PAIR_ERROR, f"{name}: {msg}"


# --- primitives: the A2DP transport gate ------------------------------------------
# The box's universal "audio can really happen" oracle (audio_ready, the
# btwatchd commit gate, /status's icon, set_output's deferred switch...).
# Two implementations behind ONE signature, selected by VIBB_BT_GATE:
#   pcm        bluealsa's org.bluealsa.PCM1 — the proven path, bluealsa-only
#   transport  BlueZ's own org.bluez.MediaTransport1 for the device carrying
#              the HOST's A2DP Source UUID = the peer accepted our
#              SetConfiguration. Backend-neutral: BlueZ creates it whoever
#              owns the endpoint (bluealsa today, PipeWire tomorrow). A
#              0000110b transport (a phone streaming INTO the box) never counts.
#   shadow     answer with pcm, compare against transport at most every 10s
#              and log each disagreement with direction + duration. Flip
#              criteria (PLAN-pipewire-soloist.md AM-12): a week with no
#              disagreement lasting two compares, none in the
#              transport=False/pcm=True direction.
# The bluealsa PCM appears when bluealsa has negotiated a codec; the
# transport when BlueZ has configured the endpoint — hundreds of ms apart
# on a connect, which is exactly what shadow mode measures before the flip.
GATE_MODE = os.environ.get("VIBB_BT_GATE", "shadow")
A2DP_SOURCE_UUID = "0000110a-0000-1000-8000-00805f9b34fb"
SHADOW_S = 10.0
_shadow = {"at": 0.0, "since": None, "dir": None}


def a2dp_pcm_present(mac):
    """The real 'audio ready' signal: the A2DP transport to `mac` exists."""
    if GATE_MODE == "transport":
        return a2dp_transport_present(mac)
    if backend() == "dbus":
        try:
            present = _dbus_a2dp_pcm_present(mac)
        except Exception as e:
            log(f"bt dbus pcm check failed ({e.__class__.__name__}) — cli")
        else:
            if GATE_MODE == "shadow":
                _shadow_compare(mac, present)
            return present
    return _cli_a2dp_pcm_present(mac)


def a2dp_transport_present(mac):
    """MediaTransport1 (A2DP source side) for `mac` — any state counts:
    idle/pending/active all mean the peer accepted the configuration."""
    if backend() == "dbus":
        try:
            return _dbus_a2dp_transport_present(mac)
        except Exception as e:
            log(f"bt dbus transport check failed ({e.__class__.__name__}) — cli")
    return _cli_a2dp_transport_present(mac)


def _shadow_compare(mac, pcm):
    now = time.monotonic()
    if now - _shadow["at"] < SHADOW_S:
        return  # the 1/s readers must not double the bus traffic
    _shadow["at"] = now
    try:
        tr = _dbus_a2dp_transport_present(mac)
    except Exception as e:
        log(f"bt gate shadow: transport read failed ({e.__class__.__name__})")
        return
    if tr == pcm:
        if _shadow["since"] is not None:
            log(f"bt gate: agree again after {now - _shadow['since']:.0f}s "
                f"({_shadow['dir']})")
            _shadow["since"], _shadow["dir"] = None, None
        return
    d = f"transport={tr} pcm={pcm}"
    if _shadow["since"] is None:
        _shadow["since"], _shadow["dir"] = now, d
        log(f"bt gate: DISAGREE {d} (started)")
    elif _shadow["dir"] != d:
        _shadow["dir"] = d
        log(f"bt gate: DISAGREE flipped to {d}")


def _cli_a2dp_pcm_present(mac):
    _c, pcm = _run(["bluealsa-aplay", "-L"], timeout=10)
    return mac.lower() in pcm.lower()


def _cli_a2dp_transport_present(mac):
    """busctl (systemd, always present) — the transport objects have no
    bluetoothctl text surface."""
    _c, out = _run(["busctl", "--system", "--json=short", "call", "org.bluez",
                    "/", "org.freedesktop.DBus.ObjectManager",
                    "GetManagedObjects"], timeout=10)
    return transport_in_managed(busctl_objects(out), mac)


def busctl_objects(text):
    """The {path: {iface: {prop: value}}} tree out of `busctl --json=short`
    (every variant arrives as {"type": .., "data": ..}); {} on any parse
    failure, so a garbled reply reads as 'no transport'."""
    try:
        objs = json.loads(text)["data"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return {}

    def plain(v):
        if isinstance(v, dict) and "data" in v and "type" in v:
            return plain(v["data"])
        return v
    try:
        return {p: {i: {k: plain(v) for k, v in props.items()}
                    for i, props in ifaces.items()}
                for p, ifaces in objs.items()}
    except AttributeError:
        return {}


def transport_in_managed(objs, mac):
    """Pure: does an ObjectManager tree hold an A2DP-source transport to
    `mac`? Shared by the dbus and busctl paths so both read the same rule."""
    frag = "/dev_" + mac.upper().replace(":", "_") + "/"
    for path, ifaces in objs.items():
        tr = ifaces.get("org.bluez.MediaTransport1")
        if tr is None or frag not in str(path):
            continue
        if str(tr.get("UUID", "")).lower() == A2DP_SOURCE_UUID:
            return True
    return False


# --- dbus backend ----------------------------------------------------------------
# Read primitives only (phase A1). All imports lazy; every entry point is
# wrapped by callers so any DBusException degrades to the cli path.
# Verified against BlueZ 5.82 API; exact bluealsa path grammar is on the
# rig checklist (PLAN-bt-dbus.md §6).

_BLUEZ = "org.bluez"
_ADAPTER_PATH = "/org/bluez/hci0"
_BLUEALSA = "org.bluealsa"


def _bus():
    import dbus
    addr = (os.environ.get("VIBB_DBUS_ADDRESS")
            or os.environ.get("DBUS_SYSTEM_BUS_ADDRESS"))
    if addr:
        # explicit connection: the test harness's private bus must win
        # even where libdbus ignores the env (setuid, scrubbing, ...)
        return dbus.bus.BusConnection(addr)
    return dbus.SystemBus()


def _managed(service, path="/"):
    import dbus
    om = dbus.Interface(_bus().get_object(service, path),
                        "org.freedesktop.DBus.ObjectManager")
    return om.GetManagedObjects(timeout=10)


def _dbus_adapter_props():
    import dbus
    return dbus.Interface(_bus().get_object(_BLUEZ, _ADAPTER_PATH),
                          "org.freedesktop.DBus.Properties")


def _dbus_adapter_set(prop, value):
    import dbus
    _dbus_adapter_props().Set("org.bluez.Adapter1", prop,
                              dbus.Boolean(value), timeout=10)


def _dbus_adapter_powered():
    return bool(_dbus_adapter_props().Get("org.bluez.Adapter1", "Powered",
                                          timeout=10))


def _dbus_device_list(prop):
    out = []
    for _path, ifaces in _managed(_BLUEZ).items():
        dev = ifaces.get("org.bluez.Device1")
        if dev and bool(dev.get(prop)):
            mac = str(dev.get("Address", "")).upper()
            out.append({"mac": mac, "name": str(dev.get("Alias") or mac),
                        # a paired GAMEPAD is not a speaker: the PWA
                        # listed every bond under 'Bluetooth speaker',
                        # one tap away from routing audio into a
                        # controller (field 2026-08-04)
                        "audio": _dbus_is_audio(dev)})
    return sorted(out, key=lambda d: d["mac"])


def _dbus_paired_devices():
    return _dbus_device_list("Paired")


def _dbus_connected_devices():
    return _dbus_device_list("Connected")


def _dev_path(mac):
    return _ADAPTER_PATH + "/dev_" + mac.upper().replace(":", "_")


def _dbus_device_info(mac):
    import dbus
    try:
        props = dbus.Interface(_bus().get_object(_BLUEZ, _dev_path(mac)),
                               "org.freedesktop.DBus.Properties")
        d = props.GetAll("org.bluez.Device1", timeout=10)
    except dbus.exceptions.DBusException as e:
        name = e.get_dbus_name() or ""
        # real bluez: UnknownObject; dbus-python fakes: UnknownMethod —
        # both mean "no such device object", and absence is authoritative
        if "UnknownObject" in name or "UnknownMethod" in name:
            return {"present": False, "paired": False, "connected": False,
                    "name": None}
        raise
    return {"present": True,
            "paired": bool(d.get("Paired")),
            "connected": bool(d.get("Connected")),
            "name": str(d.get("Alias")) if d.get("Alias") else None}


_AUDIO_SINK_UUID = "0000110b-0000-1000-8000-00805f9b34fb"


def _dbus_is_audio(dev):
    icon = str(dev.get("Icon", ""))
    if icon.startswith("audio"):
        return True
    if _AUDIO_SINK_UUID in [str(u).lower() for u in dev.get("UUIDs", [])]:
        return True
    # Class major device class 0x04 = audio/video — works pre-SDP, when
    # UUIDs are still empty for unpaired devices
    try:
        return (int(dev.get("Class", 0)) >> 8) & 0x1F == 0x04
    except (TypeError, ValueError):
        return False


def _dbus_discover(secs):
    """Loop-free discovery: RSSI is only present on devices seen during
    an active discovery, so 'has RSSI' (or 'path appeared after start')
    gates out BlueZ's cache of long-gone devices without needing signal
    subscriptions (those come with the phase C daemon)."""
    import dbus
    adapter = dbus.Interface(_bus().get_object(_BLUEZ, _ADAPTER_PATH),
                             "org.bluez.Adapter1")
    before = set(_managed(_BLUEZ).keys())
    try:
        adapter.SetDiscoveryFilter({"Transport": "bredr"}, timeout=10)
    except dbus.exceptions.DBusException:
        pass  # filter is best-effort (another client may be scanning)
    started = True
    try:
        adapter.StartDiscovery(timeout=10)
    except dbus.exceptions.DBusException as e:
        if "InProgress" not in (e.get_dbus_name() or ""):
            raise
        started = False  # ride along on the other client's discovery
    seen = {}
    deadline = time.monotonic() + secs
    try:
        while time.monotonic() < deadline:
            time.sleep(2)
            for path, ifaces in _managed(_BLUEZ).items():
                dev = ifaces.get("org.bluez.Device1")
                if not dev:
                    continue
                fresh = "RSSI" in dev or path not in before
                if not fresh:
                    continue
                mac = str(dev.get("Address", "")).upper()
                seen[mac] = {
                    "mac": mac,
                    "name": str(dev.get("Alias") or mac),
                    "audio": _dbus_is_audio(dev),
                    "rssi": int(dev["RSSI"]) if "RSSI" in dev else None,
                }
    finally:
        if started:
            try:
                adapter.StopDiscovery(timeout=10)
            except Exception:
                pass  # discovery dies with our connection anyway
    # strongest signal first, unknown-RSSI last (sort/display only —
    # pairing safety rules live in bt.py and never auto-pick by RSSI)
    return sorted(seen.values(),
                  key=lambda d: -(d["rssi"] if d["rssi"] is not None else -999))


def _dbus_device_iface(mac):
    import dbus
    return dbus.Interface(_bus().get_object(_BLUEZ, _dev_path(mac)),
                          "org.bluez.Device1")


def _dbus_trust(mac):
    import dbus
    props = dbus.Interface(_bus().get_object(_BLUEZ, _dev_path(mac)),
                           "org.freedesktop.DBus.Properties")
    props.Set("org.bluez.Device1", "Trusted", dbus.Boolean(True), timeout=15)


def _dbus_connect_device(mac):
    import dbus
    try:
        # Device1.Connect — exactly what bluetoothctl calls (NOT
        # ConnectProfile). Explicit timeout: dbus-python's 25s default
        # would silently undercut the cli path's 30s budget.
        _dbus_device_iface(mac).Connect(timeout=30)
        return True, "Connection successful"
    except dbus.exceptions.DBusException as e:
        return _map_connect_error(e.get_dbus_name() or "", str(e))


def _dbus_disconnect_device(mac):
    import dbus
    try:
        _dbus_device_iface(mac).Disconnect(timeout=15)
        return True, "Successful disconnected"
    except dbus.exceptions.DBusException as e:
        return _map_disconnect_error(e.get_dbus_name() or "", str(e))


def _dbus_remove_device(mac):
    import dbus
    adapter = dbus.Interface(_bus().get_object(_BLUEZ, _ADAPTER_PATH),
                             "org.bluez.Adapter1")
    try:
        adapter.RemoveDevice(dbus.ObjectPath(_dev_path(mac)), timeout=15)
        return REMOVE_OK, "Device has been removed"
    except dbus.exceptions.DBusException as e:
        return _map_remove_error(e.get_dbus_name() or "", str(e))


def _dbus_a2dp_transport_present(mac):
    """org.bluez.MediaTransport1 under /org/bluez/hci0/dev_<MAC>/sepN/fdN
    with the host's A2DP Source UUID — see the gate comment above."""
    return transport_in_managed(_managed(_BLUEZ, "/"), mac)


def _dbus_a2dp_pcm_present(mac):
    """bluealsa exposes PCM1 objects under /org/bluealsa; ours is the
    a2dp source->sink for the device. Presence IS the ready signal."""
    frag = "/dev_" + mac.upper().replace(":", "_") + "/"
    for path, ifaces in _managed(_BLUEALSA, "/org/bluealsa").items():
        pcm = ifaces.get("org.bluealsa.PCM1")
        if pcm is None or frag not in str(path):
            continue
        if str(pcm.get("Mode", "sink")) == "sink":
            return True
    return False


# --- dbus backend: pairing agent (B2 + incoming pairing mode) ---------------------
# PLAN-bt-b2-pairing.md. Everything here runs on a DEDICATED private bus
# connection with its own GLib mainloop, created per call and closed in
# finally: (1) the process's shared SystemBus() singleton was created
# loop-less and dbus-python cannot attach a loop afterwards, so it can
# never export the agent object; (2) bluez auto-unregisters agents whose
# connection dies — closing the connection IS the cleanup, even after a
# crash. Never cache this connection: retry paths (stale-key re-pair)
# must build a fresh one so a wedged agent can't survive.

_AGENT_PATH = "/org/vibb/agent"
_AGENT_CAPABILITY = "NoInputNoOutput"


def _mac_from_path(path):
    return str(path).rsplit("dev_", 1)[-1].replace("_", ":")


def _agent_bus():
    import dbus
    import dbus.bus
    from dbus.mainloop.glib import DBusGMainLoop
    ml = DBusGMainLoop()  # per-connection; never the process default loop
    addr = (os.environ.get("VIBB_DBUS_ADDRESS")
            or os.environ.get("DBUS_SYSTEM_BUS_ADDRESS"))
    if addr:
        return dbus.bus.BusConnection(addr, mainloop=ml)
    return dbus.SystemBus(private=True, mainloop=ml)


def _make_agent(bus, on_event=None):
    """org.bluez.Agent1 that auto-accepts (NoInputNoOutput): JBL-class
    speakers negotiate Just-Works (no callback fires at all), legacy
    devices get PIN 0000 / passkey 0, cars hit RequestConfirmation and
    AuthorizeService. The dbus signatures must be EXACT — a mismatch
    presents as bluez failing the pairing as if no agent were registered,
    which misdiagnoses as an SSP problem. Rejecting would be raising
    org.bluez.Error.Rejected; never used — this agent only exists while a
    pairing was explicitly requested (a pair call or a visible window)."""
    import dbus
    import dbus.service

    def note(kind):
        log(f"bt agent: {kind}")
        if on_event:
            on_event(kind)

    class _Agent(dbus.service.Object):
        @dbus.service.method("org.bluez.Agent1",
                             in_signature="", out_signature="")
        def Release(self):
            note("Release")

        @dbus.service.method("org.bluez.Agent1",
                             in_signature="o", out_signature="s")
        def RequestPinCode(self, device):
            note("RequestPinCode")
            return "0000"

        @dbus.service.method("org.bluez.Agent1",
                             in_signature="o", out_signature="u")
        def RequestPasskey(self, device):
            note("RequestPasskey")
            return dbus.UInt32(0)

        @dbus.service.method("org.bluez.Agent1",
                             in_signature="ouq", out_signature="")
        def DisplayPasskey(self, device, passkey, entered):
            note("DisplayPasskey")

        @dbus.service.method("org.bluez.Agent1",
                             in_signature="os", out_signature="")
        def DisplayPinCode(self, device, pincode):
            note("DisplayPinCode")

        @dbus.service.method("org.bluez.Agent1",
                             in_signature="ou", out_signature="")
        def RequestConfirmation(self, device, passkey):
            note("RequestConfirmation")  # returning = accept

        @dbus.service.method("org.bluez.Agent1",
                             in_signature="o", out_signature="")
        def RequestAuthorization(self, device):
            note("RequestAuthorization")

        @dbus.service.method("org.bluez.Agent1",
                             in_signature="os", out_signature="")
        def AuthorizeService(self, device, uuid):
            note("AuthorizeService")

        @dbus.service.method("org.bluez.Agent1",
                             in_signature="", out_signature="")
        def Cancel(self):
            note("Cancel")

    return _Agent(bus, _AGENT_PATH)


def _agent_manager(bus):
    import dbus
    return dbus.Interface(bus.get_object(_BLUEZ, "/org/bluez"),
                          "org.bluez.AgentManager1")


def _register_agent(bus, default=False):
    """RequestDefaultAgent only for the incoming window: outgoing Pair()
    automatically uses the agent registered by the same connection, and a
    PERMANENT default agent would be a permanently-open pairing door
    (PLAN-bt-b2-pairing.md D8)."""
    mgr = _agent_manager(bus)
    try:
        mgr.RegisterAgent(_AGENT_PATH, _AGENT_CAPABILITY, timeout=10)
    except Exception as e:
        # near-impossible on a fresh private connection; tolerate the
        # known-benign case, fail loudly on anything else
        if "AlreadyExists" not in str(
                getattr(e, "get_dbus_name", lambda: "")()):
            raise
    if default:
        mgr.RequestDefaultAgent(_AGENT_PATH, timeout=10)


def _unregister_agent(bus):
    try:
        _agent_manager(bus).UnregisterAgent(_AGENT_PATH, timeout=5)
    except Exception:
        pass  # best-effort; closing the connection is the real cleanup


def _dbus_pair(mac):
    """One outgoing Pair() with our own agent. Async + mainloop is NOT
    optional: agent callbacks are incoming calls on this connection, and
    only a running loop dispatches them — a blocking Pair() deadlocks on
    every legacy-PIN device (PLAN-bt-dbus.md §9.1). Explicit timeout=60:
    dbus-python's 25s default silently undercuts real SSP handshakes."""
    import dbus
    from gi.repository import GLib
    bus = _agent_bus()
    agent = _make_agent(bus)
    try:
        _register_agent(bus)
        dev = dbus.Interface(
            bus.get_object(_BLUEZ, _dev_path(mac), introspect=False),
            "org.bluez.Device1")
        loop = GLib.MainLoop()
        result = []  # set exactly once by whichever handler fires

        def ok():
            result.append((PAIR_OK, "Pairing successful"))
            loop.quit()

        def err(e):
            result.append(_map_pair_error(e.get_dbus_name() or "", str(e)))
            loop.quit()

        dev.Pair(reply_handler=ok, error_handler=err, timeout=60)
        # belt and braces: on timeout dbus-python fires err() with
        # NoReply, but a lost reply must never hang the process
        guard = GLib.timeout_add_seconds(75, loop.quit)
        loop.run()
        if result:  # guard never fired (it would have been the quitter)
            GLib.source_remove(guard)
        return result[0] if result else (PAIR_ERROR,
                                         "pair timed out (no reply)")
    finally:
        _unregister_agent(bus)
        try:
            agent.remove_from_connection()
        except Exception:
            pass
        try:
            bus.close()
        except Exception:
            pass


def pairing_window(secs):
    """Incoming pairing mode: make the box discoverable and be the
    DEFAULT agent so a car/head unit can pair US (they drive; speakers
    pair the other way round). DiscoverableTimeout is set BEFORE
    Discoverable so bluez itself turns visibility off whatever happens
    to this process — SIGKILL mid-window can never leave the box
    permanently visible (dead-man switch). New bonds are trusted INSIDE
    the window: the peer's A2DP service authorization lands after our
    agent is gone, and Trusted bypasses it. Exactly one bond per window
    (closes ~3s after the first, lingering so that authorization
    completes). Returns [{mac, name}]. Raises RuntimeError with a human
    message when the dbus stack is unavailable — bt.py turns that into
    exit 2; there is deliberately no cli fallback (bluetoothctl's
    interactive agent is the wrong tool unattended)."""
    try:
        import dbus
        from gi.repository import GLib
        bus = _agent_bus()
    except ImportError as e:
        raise RuntimeError("pairing mode needs the dbus backend "
                           "(python3-dbus + python3-gi)") from e
    except Exception as e:
        raise RuntimeError("pairing mode: cannot reach the system bus "
                           f"({e.__class__.__name__})") from e
    newly = {}
    agent = _make_agent(bus)
    props = dbus.Interface(bus.get_object(_BLUEZ, _ADAPTER_PATH),
                           "org.freedesktop.DBus.Properties")
    try:
        _register_agent(bus, default=True)
        # bt_up() ran in the flow; Pairable again is belt and braces
        # (without it bluez does a NON-BONDING pairing — bt.py docstring)
        props.Set("org.bluez.Adapter1", "Pairable",
                  dbus.Boolean(True), timeout=10)
        props.Set("org.bluez.Adapter1", "DiscoverableTimeout",
                  dbus.UInt32(secs), timeout=10)  # the dead-man switch
        props.Set("org.bluez.Adapter1", "Discoverable",
                  dbus.Boolean(True), timeout=10)
        loop = GLib.MainLoop()

        def bonded(path):
            try:
                all_p = dbus.Interface(
                    _bus().get_object(_BLUEZ, path, introspect=False),
                    "org.freedesktop.DBus.Properties").GetAll(
                        "org.bluez.Device1", timeout=10)
            except Exception:
                all_p = {}
            mac = str(all_p.get("Address") or _mac_from_path(path)).upper()
            if mac in newly:
                return
            name = str(all_p.get("Alias") or mac)
            newly[mac] = {"mac": mac, "name": name}
            try:
                _dbus_trust(mac)  # inside the window — see docstring
            except Exception as e:
                log(f"bt visible: trust failed ({e.__class__.__name__})")
            GLib.timeout_add_seconds(3, loop.quit)  # linger, then done

        def on_props_changed(iface, changed, _invalidated, path=None):
            if str(iface) == "org.bluez.Device1" and changed.get("Paired"):
                bonded(path)

        def on_ifaces_added(path, ifaces):
            if (ifaces.get("org.bluez.Device1") or {}).get("Paired"):
                bonded(path)

        bus.add_signal_receiver(
            on_props_changed, signal_name="PropertiesChanged",
            dbus_interface="org.freedesktop.DBus.Properties",
            path_keyword="path")
        bus.add_signal_receiver(
            on_ifaces_added, signal_name="InterfacesAdded",
            dbus_interface="org.freedesktop.DBus.ObjectManager")
        GLib.timeout_add_seconds(secs, loop.quit)  # window end
        loop.run()
        return sorted(newly.values(), key=lambda d: d["mac"])
    finally:
        try:  # explicit off; the DiscoverableTimeout self-clears anyway
            props.Set("org.bluez.Adapter1", "Discoverable",
                      dbus.Boolean(False), timeout=10)
        except Exception:
            pass
        _unregister_agent(bus)
        try:
            agent.remove_from_connection()
        except Exception:
            pass
        try:
            bus.close()
        except Exception:
            pass
