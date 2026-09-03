"""The PipeWire side of the audio stack (PLAN-pipewire-soloist.md).

Everything vibb needs to know about a PipeWire graph, stdlib only: which
stack this box runs, whether the server is up, which sink node carries
the paired speaker or the HAT, and the asound.conf text that pins the
vibb_bt/vibb_local pcm names to those nodes. Under the bluealsa stack
(the default) nothing here is consulted except stack().

Node names are DISCOVERED from the graph by the device address / card
name, never composed: bluez_output.<MAC>.1 vs .a2dp-sink is a PipeWire
version detail, and the HAT's name embeds a platform path (bench
2026-09-03: alsa_output.platform-107c701400.hdmi.hdmi-stereo).
"""

import json
import os
import re
import subprocess
import time

from vibb.paths import RUN_DIR

STACK_FILE = os.environ.get("VIBB_AUDIO_STACK_FILE", "/etc/vibb/audio-stack")
SOCKET = os.environ.get("VIBB_PW_SOCKET", "/run/pipewire/pipewire-0")
# the HAT is sndrpihifiberry in /proc/asound/cards and snd_rpi_hifiberry_dac
# to PipeWire; a bench without a HAT points this at its HDMI card
LOCAL_CARD = os.environ.get("VIBB_LOCAL_CARD", "hifiberry")
UNRESOLVED = "vibb-unresolved"   # a node that never exists: opens fail at
#                                  hw_params (bench s2d), never at some sink
# The player's "the sink node is not there yet" exit (EX_TEMPFAIL): the
# daemon's crash healer respawns it when the node exists and never
# charges it against the 2-per-boot budget (AM-9).
SINK_WAIT_EXIT = 75
SINK_WAIT_S = float(os.environ.get("VIBB_SINK_WAIT_S", "5"))

_stack = [None]


def log(msg):
    print(f"vibbd: {msg}", flush=True)


def stack():
    """'bluealsa' | 'pipewire' — env first (systemd units carry it), then
    the file install.sh writes, default bluealsa. Read once per process:
    a toggle flip re-runs install.sh, which restarts every service."""
    if _stack[0] is None:
        v = os.environ.get("VIBB_AUDIO_STACK", "")
        if not v:
            try:
                with open(STACK_FILE) as f:
                    v = f.read().strip()
            except OSError:
                v = ""
        _stack[0] = "pipewire" if v == "pipewire" else "bluealsa"
    return _stack[0]


def pw_dump(timeout=3.0):
    """The graph as pw-dump's JSON list; [] on ANY failure (no server,
    no tool, timeout, garbage) — callers treat [] as 'no node'."""
    try:
        r = subprocess.run(["pw-dump"], capture_output=True, text=True,
                           timeout=timeout,
                           env=dict(os.environ, PIPEWIRE_RUNTIME_DIR=os.path.dirname(SOCKET)))
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    try:
        out = json.loads(r.stdout)
    except ValueError:
        return []
    return out if isinstance(out, list) else []


def server_up():
    return os.path.exists(SOCKET) and bool(pw_dump())


def _sinks(dump):
    for obj in dump:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = ((obj.get("info") or {}).get("props") or {})
        if props.get("media.class") == "Audio/Sink" and props.get("node.name"):
            yield props


def find_bt_sink(mac, dump=None):
    """node.name of the Audio/Sink whose api.bluez5.address is `mac`, or
    None. The bluez INPUT node (a phone streaming into the box) shares
    the address but is Audio/Source — never matched."""
    dump = pw_dump() if dump is None else dump
    want = mac.upper()
    for props in _sinks(dump):
        if str(props.get("api.bluez5.address", "")).upper() == want:
            return props["node.name"]
    return None


def find_local_sink(dump=None):
    """node.name of the ALSA sink whose card name contains LOCAL_CARD."""
    dump = pw_dump() if dump is None else dump
    want = LOCAL_CARD.lower()
    for props in _sinks(dump):
        card = f"{props.get('alsa.card_name', '')} {props.get('api.alsa.card.name', '')}"
        if want in card.lower():
            return props["node.name"]
    return None


def sink_ready(output, mac=None, dump=None):
    """Does the sink node for `output` exist right now? The spawn/retarget
    gate under pipewire (D): a pcm pinned to an absent node fails at
    hw_params, so nobody may point a player at it yet."""
    if output == "bt":
        return bool(mac) and find_bt_sink(mac, dump) is not None
    return find_local_sink(dump) is not None


def resolve_route(mac, tries=10, delay=1.0):
    """(bt_node | None, local_node | None) from the live graph. The BT
    node is waited for — up to `tries` dumps `delay` apart — because
    connect() calls this the moment the transport exists and the node
    follows by milliseconds; the HAT node is whatever the same dump
    shows (absent = no HAT / WirePlumber not up)."""
    import time
    bt_node = local_node = None
    for i in range(max(1, tries)):
        dump = pw_dump()
        local_node = find_local_sink(dump) or local_node
        if mac:
            bt_node = find_bt_sink(mac, dump)
            if bt_node:
                break
        else:
            break
        if i + 1 < tries:
            time.sleep(delay)
    return bt_node, local_node


def recover_units():
    """Units bt.py's crash recovery try-restarts after re-attaching the
    firmware. bluealsa must come back for its A2DP endpoint; WirePlumber
    re-registers its own on org.bluez's NameOwnerChanged (bench B10
    verifies), so under pipewire the answer is nothing — restarting
    pipewire.service would kill every client stream, and a WirePlumber
    bounce is opt-in until the bench says a tier needs it."""
    if stack() != "pipewire":
        return ("bluealsa", "bluealsad")   # name differs across releases
    if os.environ.get("VIBB_BT_HEAL_RESTART_WP") == "1":
        return ("wireplumber",)
    return ()


def pinned_node(text, pcm):
    """The playback_node a pcm block in asound.conf pins, or None."""
    m = re.search(r"pcm\." + re.escape(pcm) + r"\s*\{.*?playback_node\s+\"([^\"]+)\"",
                  text, re.S)
    node = m.group(1) if m else None
    return None if node in (None, UNRESOLVED) else node


def ensure_bt_route(mac):
    """The daemon's writer on btwatchd's announce (AM-10): the speaker's
    node may be NEW (first connect after boot) or RENAMED (package
    upgrade) since asound.conf was written, and the announce is about to
    retarget mpv onto vibb_bt. File ONLY — never a go-librespot reopen;
    the announce path already does its single reopen. One pw-dump per
    announce, never per second. Skips when bt.py owns the radio (it
    writes the route itself) rather than block the HTTP thread. Returns
    True when the file was rewritten."""
    if not mac or stack() != "pipewire":
        return False
    from vibb import bt as _bt  # lazy: bt imports audio lazily too
    try:
        with open(_bt.ASOUND) as f:
            cur = f.read()
    except OSError:
        cur = ""
    bt_node, local_node = resolve_route(mac, tries=1)
    if bt_node is None:
        return False   # not in the graph yet — nothing true to write
    local_node = local_node or pinned_node(cur, "vibb_local")  # keep a
    #   pinned HAT when this dump did not show it (never regress to unresolved)
    text = asound_text(mac, bt_node, local_node)
    if text == cur:
        return False
    lock = _bt.acquire_process_lock(blocking=False)
    if lock is None:
        log("asound route: bt.py owns the radio — it writes the route itself")
        return False
    try:
        _bt.write_asound(text)
        log(f"asound route refreshed on announce: {mac} -> {bt_node}")
        return True
    finally:
        lock.close()


# --- the policy self-test (I10, AM-7, AM-8) -------------------------------------
# The no-fail-over ban is enforced by config in someone else's package now.
# So at boot (and whenever PipeWire restarts) vibb PROVES the behaviour:
# a targetless stream links nowhere, a stream whose target vanishes is
# never re-homed, a pinned stream ignores the default sink, the settings
# say so, and the HAT's graph gain is 1.0. A safety failure flips
# cap_everywhere() — every landing on every output is capped (AM-7) —
# and shows on /status and the screen; nothing is refused (bedtime rule).

POLICY_FILE = os.environ.get("VIBB_AUDIO_POLICY_FILE",
                             os.path.join(RUN_DIR, "vibb-audio-policy"))
SELFTEST_POLL_S = 60.0       # cookie watch cadence (one pw-dump a minute)
SELFTEST_MIN_GAP_S = 300.0   # vibb-daemon is Restart=always/5s: never probe-loop
_policy_cache = {"mtime": None, "val": {}}
_PROBE_ENV = {"PIPEWIRE_RUNTIME_DIR": os.path.dirname(SOCKET)}
_PWCAT = ["pw-cat", "--playback", "--raw", "--format=s16", "--rate=44100",
          "--channels=2", "/dev/zero"]   # silence, never EOF; killed by us


def _run(cmd, timeout=5.0):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, **_PROBE_ENV))
        return r.returncode, r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def _popen(cmd, props):
    env = dict(os.environ, **_PROBE_ENV)
    env["PIPEWIRE_PROPS"] = props
    try:
        return subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError:
        return None


def _kill(p):
    if p is not None:
        try:
            p.kill()
            p.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass


def core_cookie(dump):
    for obj in dump:
        if obj.get("type") == "PipeWire:Interface:Core":
            return (obj.get("info") or {}).get("cookie")
    return None


def _node_named(dump, name):
    for obj in dump:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        if ((obj.get("info") or {}).get("props") or {}).get("node.name") == name:
            return obj
    return None


def _linked_sinks(dump, node_id):
    """Names of the nodes a node's output links reach."""
    names = {obj["id"]: ((obj.get("info") or {}).get("props") or {}).get("node.name")
             for obj in dump if obj.get("type") == "PipeWire:Interface:Node"}
    out = set()
    for obj in dump:
        if obj.get("type") != "PipeWire:Interface:Link":
            continue
        info = obj.get("info") or {}
        if info.get("output-node-id") == node_id:
            out.add(names.get(info.get("input-node-id")) or "?")
    return out


def _settings():
    """{key: value} out of `wpctl settings` (0.5.8: '- Name: k' / 'Value: v')."""
    _rc, text = _run(["wpctl", "settings"], timeout=5)
    out, key = {}, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("- Name:"):
            key = line.split(":", 1)[1].strip()
        elif line.startswith("Value:") and key:
            out[key] = line.split(":", 1)[1].strip().lower()
            key = None
    return out


def _probe_stream(name, target=None):
    """A silent pw-cat stream named `name`, pinned to `target` or targetless."""
    props = f'{{ node.name = "{name}" application.name = "vibb-selftest" }}'
    cmd = list(_PWCAT)
    if target:
        cmd = cmd[:1] + ["--target", target] + cmd[1:]
    return _popen(cmd, props)


def _mk_sink(name):
    _run(["pw-cli", "create-node", "adapter",
          f"{{ factory.name=support.null-audio-sink node.name={name} "
          "media.class=Audio/Sink object.linger=true audio.position=[FL FR] "
          "monitor.channel-volumes=false }"], timeout=5)


def _rm_sink(dump, name):
    n = _node_named(dump, name)
    if n is not None:
        _run(["pw-cli", "destroy", str(n["id"])], timeout=5)


def policy_selftest():
    """Run the probes; write + return the verdict. pw_dump() is called in a
    FIXED order (the unit test scripts it): 1 the graph, 2 targetless
    probe live, 3 the null sink created, 4 the pinned probe live, 5 the
    sink destroyed, 6 the default probe live, 7 final graph."""
    safety, rf = [], []
    d = pw_dump()                                                    # 1
    if not d:
        return _write_verdict({"verdict": "down", "safety": [], "rf": [],
                               "cookie": None})
    cookie = core_cookie(d)
    real_sinks = ("bluez_output.", "alsa_output.", "vibb_null")

    # (1) a TARGETLESS stream must link nowhere (find-default/find-best off)
    p = _probe_stream("vibb-selftest-targetless")
    time.sleep(1.0)
    d = pw_dump()                                                    # 2
    n = _node_named(d, "vibb-selftest-targetless")
    if n is None or _linked_sinks(d, n["id"]):
        safety.append("targetless-linked" if n is not None else "probe-missing")
    _kill(p)

    # (2) a stream whose target VANISHES is destroyed, never re-homed
    _mk_sink("vibb_selftest_sink")
    time.sleep(0.5)
    d = pw_dump()                                                    # 3
    p = _probe_stream("vibb-selftest-pinned", "vibb_selftest_sink")
    time.sleep(1.0)
    d = pw_dump()                                                    # 4
    n = _node_named(d, "vibb-selftest-pinned")
    if n is None or _linked_sinks(d, n["id"]) != {"vibb_selftest_sink"}:
        safety.append("pinned-not-linked")
    _rm_sink(d, "vibb_selftest_sink")
    time.sleep(1.5)
    d = pw_dump()                                                    # 5
    n = _node_named(d, "vibb-selftest-pinned")
    if n is not None and any(s.startswith(real_sinks) or s == "?"
                             for s in _linked_sinks(d, n["id"])):
        safety.append("rescued-after-vanish")
    _kill(p)

    # (3) a pinned stream ignores the (configured) default sink
    _mk_sink("vibb_selftest_sink"); _mk_sink("vibb_selftest_B")
    _run(["pw-metadata", "0", "default.configured.audio.sink",
          '{ "name": "vibb_selftest_B" }'], timeout=5)
    p = _probe_stream("vibb-selftest-default", "vibb_selftest_sink")
    time.sleep(1.0)
    d = pw_dump()                                                    # 6
    n = _node_named(d, "vibb-selftest-default")
    if n is None or _linked_sinks(d, n["id"]) != {"vibb_selftest_sink"}:
        safety.append("followed-default")
    _kill(p)
    _run(["pw-metadata", "0", "default.configured.audio.sink", "-d"], timeout=5)
    _rm_sink(d, "vibb_selftest_sink"); _rm_sink(d, "vibb_selftest_B")

    # (4) the settings belt
    s = _settings()
    for k in ("linking.allow-moving-streams", "linking.follow-default-target",
              "node.stream.restore-target", "node.stream.restore-props"):
        if s.get(k) != "false":
            safety.append(f"setting:{k}={s.get(k)}")

    # (5) HAT gain 1.0 / unmuted; (RF) codec, suspend, no input nodes
    d = pw_dump()                                                    # 7
    local = find_local_sink(d)
    for obj in d:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        info = obj.get("info") or {}
        props = info.get("props") or {}
        name = props.get("node.name", "")
        if name == local:
            for pr in (info.get("params") or {}).get("Props") or []:
                vols = pr.get("channelVolumes") or []
                if pr.get("mute") or any(abs(v - 1.0) > 0.01 for v in vols):
                    safety.append("hat-gain")
                    break
        if name.startswith("bluez_output."):
            if props.get("api.bluez5.codec", "sbc") != "sbc":
                rf.append(f"codec:{props.get('api.bluez5.codec')}")
            if str(props.get("session.suspend-timeout-seconds", "")) != "120":
                rf.append("bt-suspend")
        if name.startswith("bluez_input."):
            rf.append("bluez-input-node")

    verdict = "fail-safety" if safety else ("fail-rf" if rf else "ok")
    return _write_verdict({"verdict": verdict, "safety": safety, "rf": rf,
                           "cookie": cookie})


def _write_verdict(v):
    v["at"] = time.time()
    try:
        os.makedirs(os.path.dirname(POLICY_FILE), exist_ok=True)
        tmp = f"{POLICY_FILE}.tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump(v, f)
        os.replace(tmp, POLICY_FILE)
    except OSError as e:
        log(f"audio policy: could not record the verdict ({e!r})")
    _policy_cache["mtime"] = None
    if v["verdict"] != "ok":
        log(f"AUDIO POLICY {v['verdict'].upper()}: safety={v['safety']} rf={v['rf']}")
    else:
        log("audio policy self-test: ok")
    return v


def selftest_state():
    """The last verdict, {} when none. mtime-cached: the 1/s readers and
    every cap site call this and must never parse a file per call."""
    try:
        mt = os.stat(POLICY_FILE).st_mtime
    except OSError:
        return {}
    if _policy_cache["mtime"] != mt:
        try:
            with open(POLICY_FILE) as f:
                _policy_cache["val"] = json.load(f)
        except (OSError, ValueError):
            _policy_cache["val"] = {}
        _policy_cache["mtime"] = mt
    return _policy_cache["val"]


def cap_everywhere():
    """AM-7: a safety-class drift means a stream might reach the HAT
    without vibb choosing it — so until the next green run every landing
    on EVERY output is capped. False without a verdict, and always under
    bluealsa (no policy engine to drift)."""
    return stack() == "pipewire" and selftest_state().get("verdict") == "fail-safety"


def selftest_due(dump):
    """Never run: yes. PipeWire restarted (core cookie changed): yes.
    Otherwise not inside SELFTEST_MIN_GAP_S of the last run."""
    st = selftest_state()
    if not st:
        return True
    cookie = core_cookie(dump)
    if cookie is not None and st.get("cookie") != cookie:
        return True
    return time.time() - float(st.get("at") or 0) > SELFTEST_MIN_GAP_S and \
        st.get("verdict") == "down"


def asound_text(mac, bt_node, local_node):
    """/etc/asound.conf under pipewire: the same two pcm NAMES the whole
    box addresses (output.py OUTPUT_PCMS), each pinned to a discovered
    node. The colon MAC stays in the file so bt.py's idempotence check
    and the node-name check both work; an unresolved node pins a name
    that cannot exist, so an open fails closed instead of landing on
    whatever sink is default."""
    bt_node = bt_node or UNRESOLVED
    local_node = local_node or UNRESOLVED
    return f'''# Managed by vibb (bt.py) — stack: pipewire
# bt speaker {mac or "-"}  (node names discovered from the graph, never composed)
pcm.vibb_bt {{
    type pipewire
    server "{SOCKET}"
    playback_node "{bt_node}"
    hint.description "vibb: BT speaker"
}}
# Built-in/HAT speaker (Pirate Audio / Amp SHIM, MAX98357A over I2S).
pcm.vibb_local {{
    type pipewire
    server "{SOCKET}"
    playback_node "{local_node}"
    hint.description "vibb: built-in speaker"
}}
# ALSA 'default' is CLOSED (a node that cannot exist: opens fail at
# hw_params) unless vibb-extra opens it onto the HAT for an extra — so a
# stray client never lands on whatever sink WirePlumber calls default.
pcm.vibb_closed {{
    type pipewire
    server "{SOCKET}"
    playback_node "vibb-closed-never"
}}
pcm.!default {{
    type plug
    slave.pcm {{
        @func getenv
        vars [ VIBB_ALSA_DEFAULT ]
        default "vibb_closed"
    }}
}}
'''
