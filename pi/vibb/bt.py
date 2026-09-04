#!/usr/bin/env python3
"""Bluetooth speaker management — play.sh's hard-won BlueZ workarounds.

Step 2 of the bt refactor (PLAN-bt-dbus.md): all transport (which tool
or bus answers a question) lives in vibb/btbus.py behind a narrow
primitive surface with cli|dbus backends. This module owns the FLOW:
pairing retries, stale-key handling, crash recovery, one-output policy,
MAC_FILE/ASOUND routing — in exactly one copy, backend-agnostic.

The workarounds this preserves, all learned on real hardware:
- rfkill soft-block persists across reboots on fresh installs -> unblock
- without `pairable on`, BlueZ does a NON-BONDING pairing (new_link_key
  with store_hint 0): "Pairing successful" but the key is thrown away,
  so the bond is gone after a power cycle
- device info can intermittently come back empty (D-Bus hiccup)
  -> retry before concluding the device is unpaired
- pair failures are classified: AlreadyExists = fine, continue;
  AuthenticationFailed = stale key on the device, and ONLY then is it
  right to clear our bond and pair fresh; "not available" = not seen
- "Connected: yes" right after pairing is just the pairing link — a
  profile connect is still required, and the real "ready" signal is
  bluealsa exposing an A2DP PCM for the device

CLI (used by play.sh and vibbd's /bt endpoints):
  bt.py scan            human-readable device list
  bt.py scan-raw        mac<TAB>name<TAB>yes|no  (audio confirmed?)
  bt.py connect [name]  auto-pair: the single audio device in pairing mode
  bt.py use <MAC>       connect a device (pairs first when unknown)
  bt.py forget <MAC>    drop the bond (clears config if it was active)
  bt.py rename <MAC> [NAME]  set a custom display name (blank = reset);
                        dbus backend only — writes BlueZ Device1.Alias
  bt.py ensure          connect the remembered device, else auto-pair
  bt.py reconnect       tear down + rebuild the configured device's link
  bt.py visible [secs] [adopt]  incoming pairing mode: the box becomes
                        discoverable and accepts a pairing started FROM
                        a car/head unit (default 120s window)
"""

import fcntl
import os
import re
import subprocess
import sys
import threading
import time

# The vibb package sits next to this script in the repo, or under
# /usr/local/lib/vibb-py when installed (this file doubles as a CLI).
_here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_here, "/usr/local/lib/vibb-py"):
    if os.path.isdir(os.path.join(_p, "vibb")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
        break
from vibb import btbus  # noqa: E402
from vibb.btbus import _run, log  # noqa: E402 — shared helpers
from vibb.paths import STATE_DIR, go_unit_cmd, note_go_restart  # noqa: E402

MAC_FILE = os.environ.get("VIBB_BT_FILE", "/etc/vibb/bt-headset")
# vibbd touches this when the user switches output to bt while the
# speaker is disconnected — btwatchd watches it and connects immediately
# instead of waiting out its blind-retry backoff (up to 300s)
KICK_FILE = os.environ.get("VIBB_BT_KICK",
                           os.path.join(STATE_DIR, "bt-connect-kick"))
# Set when the USER explicitly chose the built-in speaker (hold-X / PWA,
# fallback=False). btwatchd parks its blind reconnect pages while it exists,
# so the box stops chasing a speaker the user deliberately turned off. A
# speaker DROP is btwatchd's own fallback=True, which never sets it — so
# drop-recovery is unaffected. Any transition back to the bt output clears it.
BT_QUIET_FILE = os.environ.get("VIBB_BT_QUIET",
                               os.path.join(STATE_DIR, "bt-quiet"))
ASOUND = os.environ.get("VIBB_ASOUND", "/etc/asound.conf")
SCAN_SECS = int(os.environ.get("VIBB_SCAN_SECS", "20"))
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

# One radio-touching operation across ALL processes: BlueZ serializes per
# device only — it will happily start an A2DP connect while another
# process is mid-pair, which is the documented firmware crasher on the
# Zero 2 W. flock auto-releases on process death (no stale-lock cleanup).
LOCK_FILE = os.environ.get("VIBB_BT_LOCKFILE") or (
    "/run/vibb/bt.lock" if os.access("/run", os.W_OK)
    else "/tmp/vibb-bt.lock")


def acquire_process_lock(blocking=True):
    """Returns the open lock file (keep the reference!) or None."""
    try:
        os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
        f = open(LOCK_FILE, "w")
        fcntl.flock(f, fcntl.LOCK_EX if blocking else
                    fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        return None


def bt_up():
    """Radio can be rfkill-blocked (persists across reboots on a fresh
    install), which makes every scan come up empty. rfkill and the crash
    check run BEFORE any bus traffic — bluez can't answer while the
    controller is blocked or the firmware is down."""
    _run(["rfkill", "unblock", "bluetooth"], timeout=10)
    btbus.adapter_power_on()
    if not controller_ok():
        recover()
    btbus.adapter_pairable_on()  # bonding pairing — see module docstring


_HCICONFIG_WARNED = [False]


def _hci_up():
    """Is the controller actually running? Asks the KERNEL (one ioctl) —
    bluez's 'Powered: yes' is stale state after a firmware crash."""
    code, out = _run(["hciconfig", "hci0"], timeout=10)
    if code == 127 and not _HCICONFIG_WARNED[0]:
        # Trixie's bluez may drop the deprecated tools; detection then
        # falls back to the kernel-journal signature alone (still works,
        # just loses the running-controller fast path).
        _HCICONFIG_WARNED[0] = True
        log("hciconfig missing — crash detection uses the kernel log only")
    return "UP RUNNING" in out


def hci_tx_bytes():
    """The controller's TX byte counter — ground truth for 'does anything
    actually leave the radio'. A2DP playback moves ~35kB/s, so a flat
    counter while the player claims to play means the transport is a
    zombie (bluez still says connected, writes go nowhere). None when
    unavailable (hciconfig dropped, no adapter)."""
    code, out = _run(["hciconfig", "hci0"], timeout=10)
    m = re.search(r"TX bytes:(\d+)", out) if code == 0 else None
    return int(m.group(1)) if m else None


def _hci_crashed():
    """Crashed = the controller is down AND the kernel reported the
    firmware crash recently — OR the chip's COMMAND path is timing out
    even though the interface still says UP.

    The second form is the milder wedge from the field 2026-07-27 21:47:
    no hardware-error line, adapter flag UP, but `hci0: command 0x0406
    tx timeout` repeating for over a minute — so no healer fired and
    reconnection fought br-connection-busy instead of re-probing the
    firmware. Repeated command timeouts prove unresponsiveness no
    matter what the UP flag claims: HCI commands are answered by the
    chip itself, peers can't delay them. `link tx timeout` deliberately
    does NOT count — that is a peer walking out of range, which the
    normal reconnect machinery owns.

    The log query is time-bounded to the last two minutes — a crash
    from hours ago can't trigger recovery, and an rfkill-blocked or
    merely powered-down adapter (down, no signature) doesn't either.
    The log IS the right source: the kernel has no sysfs flag for
    either event. Cost note: this now reads the journal on every probe
    (the UP fast path can't rule out the command-timeout wedge); one
    bounded journalctl read per play-press probe, deduped by the
    healer's lock+cooldown."""
    code, out = _run(["journalctl", "-k", "-S", "-120s", "-o", "cat",
                      "--no-pager"], timeout=10)
    if code != 0 or not out.strip():
        _c, out = _run(["dmesg"], timeout=10)  # fallback: journal missing
        out = "\n".join(out.splitlines()[-80:])
    if len(re.findall(r"command 0x[0-9a-f]+ tx timeout", out)) >= 3:
        return True  # command path dead — UP flag or not
    if _hci_up():
        return False
    return "hci0: hardware error" in out or "Opcode 0x0c03 failed" in out


def controller_ok():
    return btbus.adapter_powered()


# The serdev bus registers in sysfs as 'serial' (kernel serdev_bus_type
# .name = "serial"), NOT 'serdev' — the old path made the re-probe a silent
# no-op in the field (2026-07-22: recover()'s rfkill cycles ran, the
# 'Re-probed' line never appeared, hard wedges survived until a cold power
# cycle). Both are probed for robustness across kernels; env-tunable for
# the test harness.
_SERDEV_BASES = tuple(
    p for p in os.environ.get(
        "VIBB_SERDEV_BASES",
        "/sys/bus/serial/drivers:/sys/bus/serdev/drivers").split(":") if p)


def _reattach_firmware():
    """Reload the BT controller firmware. Older Raspberry Pi OS attached
    the chip via hciuart.service; Bookworm+ does it in-kernel (serdev), so
    restarting the (nonexistent) service is a silent no-op there.
    Re-probing the serdev device (e.g. serial0-0 under the hci_uart_bcm
    driver) makes hci_uart power-cycle the chip and re-upload the firmware
    patchram — the same unbind/bind path Home Assistant OS ships for this
    exact BCM43xx wedge."""
    code, _out = _run(["systemctl", "restart", "hciuart"], timeout=60)
    if code == 0:
        return True
    for bus in _SERDEV_BASES:
        try:
            drivers = os.listdir(bus)
        except OSError:
            continue
        for drv in drivers:
            base = os.path.join(bus, drv)
            try:
                devs = [d for d in os.listdir(base)
                        if d.startswith("serial")]
            except OSError:
                continue
            for dev in devs:
                try:
                    with open(os.path.join(base, "unbind"), "w") as f:
                        f.write(dev)
                    time.sleep(1)
                    with open(os.path.join(base, "bind"), "w") as f:
                        f.write(dev)
                    log(f"==> Re-probed BT serdev {dev} ({drv}) — "
                        f"firmware reloaded")
                    return True
                except OSError as e:
                    log(f"serdev re-probe of {dev} failed: {e}")
    log("no firmware re-attach path found (no hciuart unit, no serdev)")
    return False


def recover():
    """The Zero 2 W's BT controller can crash outright (kernel logs
    'Bluetooth: hci0: hardware error 0x00', typically under 2.4GHz
    wifi/BT coexistence load); after that every HCI command times out
    ('Opcode 0x0c03 failed: -110') until the firmware is re-attached —
    a reboot used to be the only cure. Re-init the whole chain instead:
    hciuart re-uploads the firmware, then bluetooth + bluealsa return."""
    log("==> Bluetooth controller looks dead — re-attaching firmware...")
    # Undervoltage evidence: a Zero 2 W on a marginal supply classically
    # browns out the RADIO section while the SoC runs on — capture the
    # throttle flags at every crash so the field data accrues by itself.
    # Anything but throttled=0x0 is a power lead, not a firmware one.
    code, out = _run(["vcgencmd", "get_throttled"], timeout=10)
    if code == 0 and out.strip():
        log(f"==> power state at crash: {out.strip()}")
    _run(["systemctl", "stop", "bluetooth"], timeout=30)
    _reattach_firmware()
    _run(["systemctl", "start", "bluetooth"], timeout=30)
    from vibb import audio  # lazy: keeps the cli path import-light
    for unit in audio.recover_units():  # bluealsa; nothing under pipewire
        _run(["systemctl", "try-restart", unit], timeout=30)
    time.sleep(3)
    _run(["rfkill", "unblock", "bluetooth"], timeout=10)
    btbus.adapter_power_on()
    ok = controller_ok()
    if not ok:
        # stubborn wedge (field log: bluez restart alone leaves the kernel
        # looping 'hardware error 0x00' every 2s): power-cycle the radio
        # via rfkill around a second firmware re-attach
        log("==> Still down — radio power-cycle + second re-attach...")
        _run(["rfkill", "block", "bluetooth"], timeout=10)
        try:
            time.sleep(2)
            _reattach_firmware()
        finally:
            # the radio must never STAY blocked: anything raised between
            # block and unblock would leave it down until reboot with no
            # healer able to reach it (review 2026-07-18 R6)
            _run(["rfkill", "unblock", "bluetooth"], timeout=10)
        time.sleep(3)
        btbus.adapter_power_on()
        ok = controller_ok()
    if not ok:
        # Tier 3 (field 2026-07-22: two hard crashes where the serdev
        # re-attach alone left the -110 reset loop running): a full driver
        # module reload re-probes the serdev from scratch — stronger than
        # unbind/bind when the DRIVER state itself is wedged. Best-effort:
        # modprobe -r fails harmlessly if something still holds the module.
        log("==> Still down — reloading the BT driver module (hci_uart)...")
        _run(["systemctl", "stop", "bluetooth"], timeout=30)
        _run(["modprobe", "-r", "hci_uart"], timeout=30)
        time.sleep(1)
        _run(["modprobe", "hci_uart"], timeout=30)
        time.sleep(3)
        _run(["systemctl", "start", "bluetooth"], timeout=30)
        _run(["rfkill", "unblock", "bluetooth"], timeout=10)
        time.sleep(2)
        btbus.adapter_power_on()
        ok = controller_ok()
    log("==> Controller is back." if ok
        else "==> Controller still down — a power cycle may be needed.")
    return ok


def discover(scan_secs=None):
    """Scan and return [{mac, name, audio, rssi}] for devices actually
    seen. 'audio': False just means "could not confirm audio" — UUID info
    is unreliable for unpaired devices. rssi (dbus backend only) is for
    sorting/display, never for pairing decisions."""
    bt_up()
    secs = scan_secs or SCAN_SECS
    log(f"Scanning {secs}s — put the speaker/headset in pairing mode now...")
    return btbus.discover(secs)


def _print_devices(devices):
    for d in devices:
        log(f"  {d['mac']}  {d['name']}" + ("   [audio]" if d["audio"] else ""))


def _cache_secs(default):
    """Discovery-before-pair budget. Env-tunable (VIBB_BT_CACHE_SECS)
    so the test harness's fake bluez doesn't cost real wall-clock."""
    return int(os.environ.get("VIBB_BT_CACHE_SECS") or default)


def _paired_after_retry(mac):
    """Device info can intermittently come back empty (D-Bus hiccup) —
    retry before concluding the device is unpaired."""
    for _ in range(3):
        info = btbus.device_info(mac)
        if info["present"]:
            return info["paired"]
        time.sleep(1)
    return False


def connect(mac):
    """Pair (if needed) + trust + A2DP connect + wait for the audio
    transport + route ALSA output. The full play.sh connect_headset flow."""
    if _hci_crashed():
        # recover FIRST: connect attempts against a crashed controller
        # each hang toward their timeout — this is the recovery latency
        recover()
    bt_up()

    if not _paired_after_retry(mac):
        # Unknown/unpaired device: BlueZ must discover it before pairing works
        log(f"==> {mac} is not paired — scanning for it (pairing mode helps)...")
        btbus.populate_cache(_cache_secs(12))
        verdict, pair_out = btbus.pair(mac)
        log(pair_out.strip())
        if verdict == btbus.PAIR_ALREADY:
            # The bond exists after all (the info check lied) — continue
            log("==> Already paired — continuing.")
        elif verdict == btbus.PAIR_AUTH_FAILED:
            # Auth failure = the device holds a stale key. ONLY here is
            # it right to clear our bond and pair fresh.
            log("==> Stale key on the device — clearing bond and retrying once...")
            btbus.remove_device(mac)
            time.sleep(2)
            btbus.populate_cache(_cache_secs(10))
            verdict2, out2 = btbus.pair(mac)
            log(out2.strip())
            if verdict2 not in (btbus.PAIR_OK, btbus.PAIR_ALREADY):
                return False
        elif verdict == btbus.PAIR_NOT_AVAILABLE:
            log("Device not seen during scan. Is it powered on, close to "
                "the Pi, and in pairing mode? Then retry the pairing.")
            return False
        elif verdict != btbus.PAIR_OK:
            return False
        # trust on every successful path — incl. AlreadyExists, where the
        # old code skipped it (speaker-initiated reconnects need Trusted)
        btbus.trust(mac)

    # Always request a profile connect: right after pairing the device shows
    # "Connected: yes" from the pairing link itself, but A2DP is not up yet.
    log(f"==> Connecting (A2DP) to {mac}...")
    ok = False
    for _ in range(3):
        ok, out = btbus.connect_device(mac)
        log(out.strip())
        if ok:
            break
        time.sleep(3)
    if not ok and _hci_crashed():
        # the connect attempt itself can crash the controller firmware
        # (A2DP + paging coexistence) — re-attach and try once more
        if recover():
            ok, out = btbus.connect_device(mac)
            log(out.strip())
    if not ok:
        log("Could not connect. If pairing keeps failing, try interactively:")
        log(f"  bluetoothctl  ->  scan on / pair {mac} / trust {mac} / connect {mac}")
        return False

    # The real "connected" test: bluealsa must expose an A2DP PCM
    log("==> Waiting for audio transport...")
    ready = False
    for _ in range(15):
        if btbus.a2dp_pcm_present(mac):
            ready = True
            break
        time.sleep(1)
    if ready:
        log("==> Audio transport ready.")
    else:
        log("WARNING: bluetooth connected, but no A2DP audio transport appeared.")
        from vibb import audio
        log("Debug with: wpctl status   and   journalctl -u wireplumber -n 20"
            if audio.stack() == "pipewire" else
            "Debug with: bluealsa-aplay -L   and   journalctl -u bluealsa -n 20")

    os.makedirs(os.path.dirname(MAC_FILE), exist_ok=True)
    _persist_mac(mac)
    _route_alsa(mac)
    _disconnect_others(mac)
    return True


def _persist_mac(mac):
    """Write the configured-speaker file so it SURVIVES a power cut.
    tmp+rename alone was not enough: on ext4 the rename can reach disk
    before the data, and a hard cut then leaves an EMPTY file — field
    2026-08-04: the box rebooted from a car-trip cut with 'btwatchd:
    target (none)', no BT icon, no remembered speaker (the file had
    just been rewritten by the follow-the-connector adopt). fsync the
    DATA before the rename; if the rename itself is lost, the old MAC
    survives — a fine fallback, unlike an empty file."""
    with open(MAC_FILE + ".tmp", "w") as f:
        f.write(mac + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(MAC_FILE + ".tmp", MAC_FILE)


def _disconnect_others(mac):
    """One output at a time. Headsets connect back on their own when
    powered on, so after switching, the old device would sit 'connected'
    next to the new one while audio follows only the configured device —
    confusing, and two live A2DP links strain the radio for nothing."""
    for d in btbus.connected_devices():
        if d["mac"].upper() != mac.upper():
            log(f"==> Disconnecting {d['name']} (one output at a time)")
            btbus.disconnect_device(d["mac"])


# Debian's pipewire-alsa maps ALSA 'default' to PipeWire through this file;
# on a box rolled back to bluealsa an apt upgrade may recreate it, so the
# bluealsa template pins 'default' to the HAT whenever it exists (AM-15).
PW_DEFAULT_CONF = os.environ.get("VIBB_ALSA_PW_DEFAULT",
                                 "/etc/alsa/conf.d/99-pipewire-default.conf")


def _bluealsa_asound_text(mac):
    default = ""
    if os.path.exists(PW_DEFAULT_CONF):
        default = '''# pipewire-alsa's 'default' override is installed: keep default on the HAT
pcm.!default {
    type plug
    slave.pcm "hw:sndrpihifiberry"
}
'''
    return f'''# Managed by vibb (bt.py)
pcm.vibb_bt {{
    type plug
    slave.pcm {{
        type bluealsa
        device "{mac}"
        profile "a2dp"
    }}
}}
# Built-in/HAT speaker (Pirate Audio / Amp SHIM, MAX98357A over I2S).
# Needs dtoverlay=hifiberry-dac (sudo vibb-power hat-audio-on) + reboot.
pcm.vibb_local {{
    type plug
    slave.pcm "hw:sndrpihifiberry"
}}
''' + default


def write_asound(text):
    """tmp+fsync+rename: a brown-out mid-write would otherwise truncate
    asound.conf — BOTH pcms gone, every output silent, and nothing heals
    it (review 2026-07-18 R3). The tmp name carries the pid: under
    pipewire the daemon's announce path is a second writer (AM-10), and
    two processes sharing one tmp could rename a truncated file into
    place — the exact hole the fsync closed."""
    tmp = f"{ASOUND}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())  # same power-cut hole as _persist_mac
    os.replace(tmp, ASOUND)


def _route_alsa(mac):
    """Point the vibb_bt ALSA device at this headset (vibb_local for
    the HAT speaker is kept alongside). Under pipewire the pcm NAMES stay
    and each is pinned to a node DISCOVERED from the graph (never
    composed: the name shape is a PipeWire version detail); the colon
    MAC stays in the file so the idempotence check below still works,
    and a node rename (package upgrade) rewrites exactly once."""
    from vibb import audio  # lazy: keeps the cli path import-light
    bt_node = None
    if audio.stack() == "pipewire":
        bt_node, local_node = audio.resolve_route(mac)
        if bt_node is None:
            log(f"==> no PipeWire sink node for {mac} yet — route not rewritten")
            return
        text = audio.asound_text(mac, bt_node, local_node)
    else:
        text = _bluealsa_asound_text(mac)
    try:
        with open(ASOUND) as f:
            cur = f.read()
        if mac in cur and (bt_node is None or bt_node in cur):
            return
    except OSError:
        pass
    write_asound(text)
    log(f"==> ALSA output routed to {mac}"
        + (f" (node {bt_node})" if bt_node else ""))
    # The new vibb_bt->MAC mapping only matters to a RUNNING go-librespot
    # if bt is its CURRENT output — then reopen it live (v0.0.7) so ALSA
    # re-reads asound.conf and picks up the new headset with no restart and
    # no Spotify re-auth. If audio is on the built-in speaker, the running
    # process never opens vibb_bt, so leave it (and its playback)
    # untouched — the mapping applies when the output next switches to bt.
    # Fall back to a restart on a pre-v0.0.7 binary that lacks the endpoint.
    from vibb.output import current_output, reopen_go_output  # lazy: cycle
    if current_output().get("output") != "bt":
        return
    if reopen_go_output("vibb_bt"):
        log("==> go-librespot output reopened live on the new headset")
        return
    log("==> restarting go-librespot to apply the new route...")
    _run(go_unit_cmd("restart"), timeout=30)
    # tell the daemon's dead-device rebuild this reconnect already got a
    # fresh go-librespot — so it doesn't bounce it a second time
    note_go_restart()


def route():
    """install.sh's `bt.py route`: (re)write asound.conf for the current
    stack from the remembered speaker. Under pipewire the HAT pcm must be
    pinned even with no speaker paired, and an unpaired vibb_bt pins a
    node that cannot exist (fails closed at hw_params) instead of the
    old plug->null placeholder that silently 'plays'. Under bluealsa
    install.sh owns the placeholder; with a MAC this is today's rewrite."""
    from vibb import audio
    try:
        mac = open(MAC_FILE).read().strip()
    except OSError:
        mac = ""
    if audio.stack() != "pipewire":
        if mac:
            _route_alsa(mac)
        return True
    bt_node, local_node = audio.resolve_route(mac or None, tries=5)
    if mac and bt_node is None:
        log(f"==> speaker {mac} has no sink node right now — vibb_bt pinned "
            "to an unresolved name until it connects")
    if local_node is None:
        log("==> no HAT sink node — is WirePlumber up and the HAT enabled?")
    write_asound(audio.asound_text(mac, bt_node, local_node))
    log(f"==> asound.conf written for pipewire (bt {bt_node or '-'}, "
        f"local {local_node or '-'})")
    return True


def pair_auto(name_filter=None):
    """Find a device automatically (optionally filtered by name), connect.
    Safety rule: auto-pair only when there is EXACTLY ONE candidate —
    never pick by signal strength (a neighbour's speaker can be closer)."""
    seen = discover()
    if not seen:
        log("No bluetooth devices seen at all. Check that the device is in "
            "pairing mode and close to the Pi, then try again.")
        return False
    if name_filter:
        cands = [d for d in seen if name_filter.lower() in d["name"].lower()]
        if not cands:
            log(f"Nothing matching '{name_filter}'. Devices seen during scan:")
            _print_devices(seen)
            return False
    else:
        cands = [d for d in seen if d["audio"]]
        if not cands:
            log("Saw these devices, but none confirmed as audio (some speakers "
                "only advertise their audio profile after pairing):")
            _print_devices(seen)
            log('Pick yours by name: connect "<name>"')
            return False
    if len(cands) > 1:
        log("Multiple candidates found:")
        _print_devices(cands)
        log('Pick one by name: connect "<name>"')
        return False
    d = cands[0]
    log(f"==> Found device: {d['name']} ({d['mac']})")
    return connect(d["mac"])


def forget(mac):
    bt_up()  # a wedged controller made remove fail silently
    btbus.disconnect_device(mac)
    verdict, out = btbus.remove_device(mac)
    if verdict == btbus.REMOVE_ERROR:
        tail = out.strip().splitlines()[-1] if out.strip() else "unknown error"
        log(f"Could not remove {mac}: {tail}")
        return False
    try:
        active = open(MAC_FILE).read().strip()
    except OSError:
        active = ""
    if active == mac:
        os.remove(MAC_FILE)
        log("==> That was the active device — pair or pick another one.")
    log(f"==> Forgot {mac}")
    return True


def ensure():
    """Connect whatever we know about: remembered device, else auto-pair."""
    try:
        mac = open(MAC_FILE).read().strip()
    except OSError:
        mac = ""
    return connect(mac) if mac else pair_auto()


def visible(secs=120, adopt=False):
    """Incoming pairing mode for cars/head units: the box becomes
    discoverable and auto-accepts the pairing THEY initiate (speakers
    pair the other way round — see connect()). New bonds come back
    trusted (transport detail: an untrusted device's A2DP authorization
    would arrive after our agent is gone). Policy: report-only — the
    parent taps 'Use as speaker' in the PWA, which runs the full
    battle-tested connect() adopt path; auto-adopt stays available as
    the explicit `adopt` arg (PLAN-bt-b2-pairing.md D6: whatever paired
    during the window must not silently seize the kid's audio)."""
    bt_up()
    log(f"==> Box is visible for {secs}s — start the pairing from the "
        f"car's Bluetooth menu now...")
    new = btbus.pairing_window(secs)
    for d in new:
        log(f"==> Paired: {d['name']} ({d['mac']})")
    if not new:
        log("No device paired during the window. Start the pairing from "
            "the car while the box is visible, then try again.")
        return False
    if adopt and len(new) == 1:
        return connect(new[0]["mac"])
    log("==> Pick it as the speaker in the app to route audio there.")
    return True


def reconnect():
    """Tear the configured device's link down, then rebuild it. This is
    the zombie-transport cure (vibbd's stall watchdog: position moves,
    radio TX flat): bluez still claims connected there, so ensure()'s
    plain connect would no-op against the lying state — only an explicit
    disconnect actually kills the dead transport."""
    try:
        mac = open(MAC_FILE).read().strip()
    except OSError:
        mac = ""
    if not mac:
        return ensure()
    log(f"==> Rebuilding the link to {mac} (disconnect + connect)...")
    btbus.disconnect_device(mac)
    time.sleep(2)  # let bluez finish the teardown before paging again
    return connect(mac)


# --- daemon-facing API (vibbd's /bt endpoints call these) ----------------------

BT_LOCK = threading.Lock()  # one pairing/connect operation at a time


def bt_cli():
    """argv prefix for the BT helper subprocess. VIBB_PLAY injects a fake
    CLI in tests; otherwise this very file is executed as a script."""
    override = os.environ.get("VIBB_PLAY")
    if override:
        return ["bash", override]
    return [sys.executable, os.path.abspath(__file__)]


def bt_status():
    configured = None
    try:
        with open(MAC_FILE) as f:
            configured = f.read().strip() or None
    except OSError:
        pass
    devices = {}
    for d in btbus.paired_devices():
        devices[d["mac"]] = {"mac": d["mac"], "name": d["name"],
                             # audio=False is a gamepad/phone/etc — the
                             # PWA must not offer it as a speaker
                             "audio": bool(d.get("audio", True)),
                             "paired": True, "connected": False}
    for d in btbus.connected_devices():
        if d["mac"] in devices:
            devices[d["mac"]]["connected"] = True
    return {"configured": configured, "pairing": BT_LOCK.locked(),
            "devices": sorted(devices.values(),
                              key=lambda d: d["name"].lower())}


def bt_action(args, timeout):
    """Run a bt.py CLI command; None = another operation is in flight."""
    if not BT_LOCK.acquire(blocking=False):
        return None
    try:
        r = subprocess.run([*bt_cli(), *args], capture_output=True,
                           text=True, timeout=timeout)
        out = (r.stdout + "\n" + r.stderr).strip()
        tail = "\n".join(out.splitlines()[-6:])
        log(f"bt {' '.join(args)} -> exit {r.returncode}")
        result = {"ok": r.returncode == 0, "output": tail}
    except (OSError, subprocess.TimeoutExpired) as e:
        result = {"ok": False, "output": f"failed: {e}"}
    finally:
        BT_LOCK.release()
    return {**result, **bt_status()}


def bt_scan():
    """~20s discovery; devices in pairing mode show up here. None = busy."""
    if not BT_LOCK.acquire(blocking=False):
        return None
    try:
        r = subprocess.run([*bt_cli(), "scan-raw"],
                           capture_output=True, text=True, timeout=60)
        found = []
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and MAC_RE.match(parts[0]):
                found.append({"mac": parts[0], "name": parts[1],
                              "audio": len(parts) > 2 and parts[2] == "yes"})
        # audio devices first, then by name
        found.sort(key=lambda d: (not d["audio"], d["name"].lower()))
        log(f"bt scan -> {len(found)} device(s)")
        return {"ok": True, "found": found}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "found": [], "output": f"failed: {e}"}
    finally:
        BT_LOCK.release()


# commands that touch the radio hold the cross-process lock
_RADIO_CMDS = {"connect", "use", "forget", "disconnect", "ensure",
               "reconnect", "recover", "scan", "scan-raw", "visible",
               "route"}  # route: the asound.conf writer lock (AM-10)


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    # visible must not QUEUE behind another radio op (a silently delayed
    # 2-minute window is worse than "try again"); everything else waits
    if cmd == "visible":
        lock = acquire_process_lock(blocking=False)  # noqa: F841
        if lock is None:
            log("another bluetooth operation is running — try again "
                "in a moment")
            return 1
    else:
        lock = acquire_process_lock() if cmd in _RADIO_CMDS else None  # noqa: F841
    if cmd == "scan":
        devices = discover()
        log("Devices seen during scan:")
        _print_devices(devices)
        return 0
    if cmd == "scan-raw":
        for d in discover():
            print(f"{d['mac']}\t{d['name']}\t{'yes' if d['audio'] else 'no'}")
        return 0
    if cmd == "connect":
        return 0 if pair_auto(args[1] if len(args) > 1 else None) else 1
    if cmd in ("use", "forget", "disconnect"):
        if len(args) < 2 or not MAC_RE.match(args[1]):
            print(f"usage: bt.py {cmd} <MAC>", file=sys.stderr)
            return 1
        if cmd == "disconnect":
            ok, out = btbus.disconnect_device(args[1])
            log(out.strip().splitlines()[-1] if out.strip() else "")
            return 0 if ok else 1
        fn = connect if cmd == "use" else forget
        return 0 if fn(args[1]) else 1
    if cmd == "rename":
        # not a radio op (a plain property write) — no lock, safe during
        # playback, so it's absent from _RADIO_CMDS above
        if len(args) < 2 or not MAC_RE.match(args[1]):
            print("usage: bt.py rename <MAC> [NAME]", file=sys.stderr)
            return 1
        ok, out = btbus.set_alias(args[1], args[2] if len(args) > 2 else "")
        log(out.strip() if out else "")
        return 0 if ok else 1
    if cmd == "ensure":
        return 0 if ensure() else 1
    if cmd == "reconnect":
        return 0 if reconnect() else 1
    if cmd == "visible":
        secs = int(args[1]) if len(args) > 1 and args[1].isdigit() else 120
        secs = min(max(secs, 10), 300)
        try:
            return 0 if visible(secs, adopt="adopt" in args[2:]) else 1
        except RuntimeError as e:  # dbus/gi unavailable — additive feature,
            log(str(e))            # the box just behaves as before
            return 2
    if cmd == "recover":
        return 0 if recover() else 1
    if cmd == "route":
        return 0 if route() else 1
    print(__doc__.split("CLI", 1)[1], file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
