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
import subprocess

STACK_FILE = os.environ.get("VIBB_AUDIO_STACK_FILE", "/etc/vibb/audio-stack")
SOCKET = os.environ.get("VIBB_PW_SOCKET", "/run/pipewire/pipewire-0")
# the HAT is sndrpihifiberry in /proc/asound/cards and snd_rpi_hifiberry_dac
# to PipeWire; a bench without a HAT points this at its HDMI card
LOCAL_CARD = os.environ.get("VIBB_LOCAL_CARD", "hifiberry")
UNRESOLVED = "vibb-unresolved"   # a node that never exists: opens fail at
#                                  hw_params (bench s2d), never at some sink

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
'''
