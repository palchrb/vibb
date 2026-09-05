"""Box settings (validated, consumers re-read live) and system status
(PiSugar battery, disk/cache usage, temperatures). Extracted verbatim
from daemon.py."""

import json
import os
import socket
import subprocess
import threading
import time

from vibb import netmgmt, output
from vibb.paths import CACHE_DIR, SETTINGS_FILE, STATE_DIR


def log(msg):
    print(f"vibbd: {msg}", flush=True)


def shutdown(restart=False):
    """Answer the HTTP request first, then power off."""
    cmd = ["reboot"] if restart else ["poweroff"]
    log(f"{'restart' if restart else 'shutdown'} requested")
    threading.Timer(1.0, lambda: subprocess.run(cmd)).start()
    return {"ok": True, "action": "restart" if restart else "poweroff"}


# --- settings (screen timeout, idle shutdown, volume cap) -----------------------

# Defaults double as the validation table: (default, min, max). 0 disables
# the screen timeout / idle shutdown.
SETTING_SPECS = {
    "screen_timeout_s": (30, 0, 600),
    "screen_brightness": (100, 10, 100),  # % backlight (min 10: never black)
    "idle_shutdown_min": (5, 0, 240),  # screen blanks at 30s; no reason to
                                       # burn the ~100mA idle floor for 30 min
    # volume_cap: the ONE ceiling — on the knob and at every landing on every
    # output (owner 2026-09-05; the built-in-only local_fallback_cap is gone,
    # an old settings.json key is simply ignored)
    "volume_cap": (100, 30, 100),
    "spotify_cache_gb": (20, 1, 100),
    "spotify_bitrate": (160, 96, 320),  # further constrained to BITRATES
    # Continue what was playing when the box was switched off: -1 always
    # (the pre-2026-08-13 boolean behaviour), 0 never, N = only if it
    # stopped less than N hours ago. -1-as-always mirrors the library's
    # cache setting. The OLD boolean key cannot be widened in place: a
    # saved 1 meant "always" and would silently become "1 hour".
    "resume_window_h": (-1, -1, 168),
    "wifi_auto_off_min": (15, 0, 240),  # 0 = never auto-off
    "wifi_ps_bt_off": (0, 0, 1),  # 1 = hold wifi power save OFF while BT
    # audio plays (fewer beacon-wake coex arbitrations against A2DP — the
    # suspected BCM43430 crash trigger). Costs ~15-20% listening runtime,
    # so it's the parent's call; default off.
    "wifi_probe": (1, 0, 1),  # auto-off'd wifi: 1 = re-probe ~10 min,
    # 0 = stay off until the PWA/screen reconnect button
    "simple_nav": (0, 0, 2),  # browse: 0 text menus, 1 flat cover carousel,
                              # 2 category carousel -> per-category carousel
    "storytel_sync": (1, 0, 1),  # 1 = mirror local audiobook positions back
    # to Storytel as a backup (a fresh signed device, one-way out, queued
    # when offline); 0 = the box keeps positions to itself. Default on.
}


# go-librespot streams Ogg Vorbis at exactly these rates — an in-range
# but unknown value (say 200) would make it fail to start
BITRATES = (96, 160, 320)


def load_settings():
    out = {k: spec[0] for k, spec in SETTING_SPECS.items()}
    saved = {}
    try:
        with open(SETTINGS_FILE) as f:
            saved = json.load(f)
        for k, spec in SETTING_SPECS.items():
            if isinstance(saved.get(k), (int, float)):
                out[k] = max(spec[1], min(spec[2], int(saved[k])))
    except (OSError, ValueError):
        pass
    # Legacy resume_on_boot (bool/0/1) -> the hours window. Pure, so it
    # is safe in the hot path and in every process that loads settings
    # on its own; the first PWA save drops the old key for good.
    if not isinstance(saved, dict):
        saved = {}
    if "resume_window_h" not in saved and "resume_on_boot" in saved:
        out["resume_window_h"] = -1 if saved["resume_on_boot"] else 0
    if out["spotify_bitrate"] not in BITRATES:
        out["spotify_bitrate"] = SETTING_SPECS["spotify_bitrate"][0]
    return out


def update_settings(changes):
    if not isinstance(changes, dict):
        raise ValueError("settings must be an object")
    for k, v in changes.items():
        if k not in SETTING_SPECS:
            raise ValueError(f"unknown setting {k!r}")
        if not isinstance(v, (int, float)):
            raise ValueError(f"{k} must be a number")
        lo, hi = SETTING_SPECS[k][1], SETTING_SPECS[k][2]
        if not lo <= v <= hi:
            raise ValueError(f"{k} must be {lo}-{hi}")
        if k == "spotify_bitrate" and int(v) not in BITRATES:
            raise ValueError("spotify_bitrate must be one of 96, 160, 320")
    merged = {**load_settings(), **{k: int(v) for k, v in changes.items()}}
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    with open(SETTINGS_FILE + ".tmp", "w") as f:
        json.dump(merged, f, indent=2)
    os.replace(SETTINGS_FILE + ".tmp", SETTINGS_FILE)
    log(f"settings updated: {changes}")
    if "spotify_cache_gb" in changes:
        output.resize_spotify_cache(merged["spotify_cache_gb"])
    if "spotify_bitrate" in changes:
        output.set_spotify_bitrate(merged["spotify_bitrate"])
    return merged



# --- system status (battery, disk, wifi) ----------------------------------------

_PISUGAR_SOCK = [None]  # persistent connection (guarded by the lock)
_PISUGAR_LOCK = threading.Lock()


def _pisugar_drop():
    s, _PISUGAR_SOCK[0] = _PISUGAR_SOCK[0], None
    if s is not None:
        try:
            s.close()
        except OSError:
            pass


def pisugar_get(prop):
    """Query pisugar-server's TCP API, e.g. pisugar_get('battery') -> '84.2'.

    One persistent connection: pisugar-server logs every connect (2x INFO)
    and treats every disconnect as an error ('Response error: Stream
    closed' WARN) — with the PWA battery pill polling, per-request
    connections flooded the journal with 6 lines per refresh."""
    with _PISUGAR_LOCK:
        for attempt in (1, 2):  # second try = fresh connection
            try:
                s = _PISUGAR_SOCK[0]
                if s is None:
                    s = socket.create_connection(("127.0.0.1", 8423),
                                                 timeout=2)
                    _PISUGAR_SOCK[0] = s
                s.settimeout(2)
                s.sendall(f"get {prop}\n".encode())
                data = b""
                while b"\n" not in data:  # reply: "battery: 84.2\n"
                    chunk = s.recv(256)
                    if not chunk:
                        raise OSError("pisugar closed the connection")
                    data += chunk
                text = data.decode(errors="ignore").strip()
                if not text.startswith(prop):
                    # desynced (a stale reply from an earlier timeout) —
                    # drop the connection rather than mismatch answers
                    raise OSError(f"unexpected reply {text[:40]!r}")
                return text.split(":", 1)[1].strip() if ":" in text else None
            except (OSError, IndexError):
                _pisugar_drop()
                if attempt == 2:
                    return None


def _safe_volts(raw):
    """JSON-safe battery voltage; sane Li-Ion range only (nan/inf fail)."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return round(v, 2) if 2.0 <= v <= 6.0 else None


def _safe_amps(raw):
    """JSON-safe battery current (A). PiSugar signs it by direction
    (charge vs discharge); anything outside a plausible pack current is
    a glitch (nan/inf fail the compare)."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return round(v, 2) if -5.0 <= v <= 5.0 else None


def _safe_pct(raw):
    """A JSON-safe battery percentage: pisugar can return nan/inf while
    the charger toggles, and json.dumps(nan) is invalid JSON."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return round(v, 1) if -1 <= v <= 200 else None  # nan/inf fail the compare


_PCT_HIST = []  # last readings — the percent is voltage-modelled and sags
                # a few points under load; median stops the visible bounce


def _smoothed_pct(pct, plugged):
    if pct is None or plugged:
        # charging moves fast and monotonically — show it raw
        _PCT_HIST.clear()
        return pct
    _PCT_HIST.append(pct)
    del _PCT_HIST[:-3]
    return sorted(_PCT_HIST)[len(_PCT_HIST) // 2]


BATT_RUNTIME_FILE = os.path.join(STATE_DIR, "on-battery-runtime.json")


CHARGE_RESET_PCT = int(os.environ.get("VIBB_CHARGE_RESET_PCT", "25"))
# a battery level that ROSE this much ACROSS AN OFF PERIOD means it was
# charged while powered off. Only trusted on the first tick after boot.
# 25 (not 10): a Li-Ion pack's voltage RELAXES upward when the load drops
# (heavy use at shutdown -> resting at boot), and the voltage-modelled
# percent can climb 10-15% with no charge at all — the field-reported
# "runtime resets for no reason" was this relaxation crossing a low
# threshold. A real off-charge is a much bigger jump.
CHARGE_CONFIRM_TICKS = int(os.environ.get("VIBB_CHARGE_CONFIRM", "2"))
# plugged/charging must read true this many ticks in a row before we
# reset: PiSugar occasionally reports a single spurious 'plugged', and
# one bad read used to wipe the whole counter mid-session.


# In-memory runtime counter between disk flushes: a JSON write+rename
# EVERY 60s tick kept the SD from sleeping and wore flash for a counter
# whose consumers tolerate minutes of staleness (review P4). Memory is
# authoritative while the daemon runs; disk is flushed every
# BATT_FLUSH_S or on a >=1% battery change, so a hard power cut loses
# at most that much accounting.
_RUNTIME_MEM = None  # (accum, last_pct) once the tracker has run
BATT_FLUSH_S = float(os.environ.get("VIBB_BATT_FLUSH", "300"))


def _load_runtime():
    """(accum_seconds, last_pct) — memory first, else disk, else None."""
    if _RUNTIME_MEM is not None:
        return _RUNTIME_MEM
    try:
        with open(BATT_RUNTIME_FILE) as f:
            d = json.load(f)
        return max(0, int(d["accum"])), d.get("last_pct")
    except (OSError, ValueError, KeyError, TypeError):
        return None, None


def _battery_runtime():
    """Accumulated POWERED-ON seconds since the last charge, or None while
    on the charger / without a PiSugar. The box can be switched off in
    between — wall-clock would count sleep as usage, so a daemon thread
    accumulates actual uptime instead (persisted across restarts)."""
    return _load_runtime()[0]


HOLD = "hold"  # unconfirmed charge: neither reset nor accumulate this tick


def _runtime_step(delta, charging_now, confirmed, rose, prev_accum, pct):
    """One tick's decision (pure). Returns:
      None            reset the counter (confirmed charge, or a charge
                      across the off period seen on the first boot tick)
      HOLD            a charging read that is NOT yet confirmed over
                      CHARGE_CONFIRM_TICKS — hold the counter still so one
                      spurious 'plugged' can't wipe it
      (accum, pct)    on battery: accumulate the elapsed time
    The debounce (charging_now/confirmed) and the off-period rise (rose)
    are computed by the caller from the raw PiSugar reads."""
    if rose:
        return None
    if charging_now:
        return None if confirmed else HOLD
    return int((prev_accum or 0) + delta), pct


# Last charger reading from the runtime tracker's 60s tick — a free
# cache for other daemon policies (the wifi-ps governor's charger rule)
# so they don't add yet another pisugar poller.
_PLUGGED = [None, 0.0]  # (bool, monotonic stamp of the reading)


def plugged_cached(max_age_s=180.0):
    """The tracker's last charger reading; None when unknown or stale
    (tracker not running yet, or a box without a PiSugar)."""
    val, at = _PLUGGED
    if val is None or time.monotonic() - at > max_age_s:
        return None
    return val


def _battery_runtime_tracker():
    """60s ticks: while on battery, add the elapsed powered-on time to the
    persisted counter; reset it whenever the box is (or was) charging.

    Charging is detected three ways so the counter can't run away: the
    charger is plugged in, the pack is actively charging, OR the battery
    level has risen since we last looked. That last signal is what catches
    a charge that happened while the box was switched OFF (this thread
    wasn't running to see 'plugged'), which otherwise let the counter add
    session onto session into implausible totals."""
    last = time.monotonic()
    first_tick = True  # the boot-vs-last-session comparison happens once
    charge_ticks = 0   # consecutive ticks reading plugged/charging
    _flushed_at = 0.0  # last disk flush (first real step always flushes)
    _flushed_pct = None
    while True:
        time.sleep(60)
        try:
            now = time.monotonic()
            delta, last = now - last, now
            plugged = pisugar_get("battery_power_plugged")
            if plugged is None:
                continue  # no pisugar on this box (or a transient read miss)
            _PLUGGED[0], _PLUGGED[1] = plugged == "true", now
            charging = pisugar_get("battery_charging")
            pct = _safe_pct(pisugar_get("battery"))
            prev_accum, prev_pct = _load_runtime()
            charging_now = plugged == "true" or charging == "true"
            charge_ticks = charge_ticks + 1 if charging_now else 0
            confirmed = charge_ticks >= CHARGE_CONFIRM_TICKS
            rose = (first_tick and pct is not None and prev_pct is not None
                    and pct > prev_pct + CHARGE_RESET_PCT)
            step = _runtime_step(delta, charging_now, confirmed, rose,
                                 prev_accum, pct)
            first_tick = False
            if step is HOLD:
                continue  # unconfirmed charge — leave the counter untouched
            global _RUNTIME_MEM
            if step is None:
                _RUNTIME_MEM = None  # reset: back to disk-truth (absent)
                try:
                    os.remove(BATT_RUNTIME_FILE)
                except OSError:
                    pass
                continue
            accum, last_pct = step
            _RUNTIME_MEM = (int(accum), last_pct)
            # flush to disk only every BATT_FLUSH_S or on a >=1% battery
            # move — not every tick (SD wear + sleep, review P4)
            pct_moved = (last_pct is not None and _flushed_pct is not None
                         and abs(last_pct - _flushed_pct) >= 1)
            if now - _flushed_at < BATT_FLUSH_S and not pct_moved:
                continue
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(BATT_RUNTIME_FILE + ".tmp", "w") as f:
                json.dump({"accum": int(accum), "last_pct": last_pct}, f)
            os.replace(BATT_RUNTIME_FILE + ".tmp", BATT_RUNTIME_FILE)
            _flushed_at, _flushed_pct = now, last_pct
        except Exception as e:
            log(f"battery runtime tracker error: {e!r}")


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


# Cache-size TTL cache: /system is polled every 30s by the screen and
# 60s by the PWA for the battery pill — but os.walk over a 20GB spotify
# cache is tens of thousands of syscalls and periodic SD wakes for a
# number that changes only when the sweeper/pruner runs (review P3).
_DIR_SIZE_CACHE = {}  # path -> (measured_at, size)
DIR_SIZE_TTL_S = float(os.environ.get("VIBB_DIR_SIZE_TTL", "600"))


def _dir_size_cached(path):
    now = time.monotonic()
    hit = _DIR_SIZE_CACHE.get(path)
    if hit and now - hit[0] < DIR_SIZE_TTL_S:
        return hit[1]
    size = _dir_size(path)
    _DIR_SIZE_CACHE[path] = (now, size)
    return size


def invalidate_dir_sizes():
    """The sweeper/pruner just changed a cache — measure fresh next time."""
    _DIR_SIZE_CACHE.clear()




def system_status():
    batt = pisugar_get("battery")
    volts = pisugar_get("battery_v")
    amps = pisugar_get("battery_i")
    plugged = pisugar_get("battery_power_plugged")
    disk = None
    try:
        import shutil
        du = shutil.disk_usage(CACHE_DIR if os.path.isdir(CACHE_DIR) else "/")
        disk = {"total": du.total, "free": du.free}
    except OSError:
        pass
    caches = {}
    for name, p in (("podcasts", CACHE_DIR),
                    ("spotify", "/var/lib/vibb/spotify-cache")):
        if os.path.isdir(p):
            caches[name] = _dir_size_cached(p)
    temp = None
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            temp = round(int(f.read().strip()) / 1000, 1)
    except (OSError, ValueError):
        pass
    # snapshot, not live forks: /system is a 30s screen poll and the
    # wifi answer only changes on our own ops (which invalidate) —
    # architect power audit 2026-08-10 #3
    enabled, ssid, ip, hotspot = netmgmt.wifi_snapshot()
    on_battery_s = None
    if batt is not None and plugged != "true":
        on_battery_s = _battery_runtime()
    return {"battery": _smoothed_pct(_safe_pct(batt), plugged == "true"),
            "battery_v": _safe_volts(volts),
            "battery_i": _safe_amps(amps),
            "on_battery_s": on_battery_s,
            "plugged": plugged == "true",
            "disk": disk, "caches": caches, "cpu_temp": temp,
            "wifi": {"enabled": enabled, "ssid": ssid, "ip": ip,
                     "hotspot": hotspot,
                     "hotspot_ssid": netmgmt.HOTSPOT_SSID},
            "hostname": socket.gethostname()}


