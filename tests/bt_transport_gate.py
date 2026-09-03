#!/usr/bin/env python3
"""The A2DP gate goes stack-neutral: org.bluez.MediaTransport1 instead of
bluealsa's PCM1 (PLAN-pipewire-soloist.md §D, AM-12).

BlueZ creates the transport object whoever owns the endpoint — bluealsa
today, PipeWire after the migration — so `a2dp_pcm_present(mac)` keeps
its signature and every one of its ~11 call sites, while the meaning
moves from "bluealsa negotiated a codec" to "the peer accepted our A2DP
SetConfiguration". Pinned here:

  1. the rule: an A2DP-SOURCE transport (0000110a) for the MAC counts, in
     any state; a SINK one (0000110b — a phone streaming INTO the box)
     never does; another device's transport never does
  2. the busctl fallback parses `--json=short` (variant wrappers) and a
     garbled reply reads as "no transport", never as an exception
  3. shadow mode: the default answers with the bluealsa PCM, compares the
     transport at most every SHADOW_S, and logs a disagreement ONCE with
     its direction, then its duration when it ends — the 1/s /status
     readers must not double the bus traffic (daemon.py:2998 history)
  4. VIBB_BT_GATE=transport answers with the transport alone
  5. (rig only) against fake_bluezd over a private bus: SetPcm creates a
     source transport (parity fixtures keep meaning "ready"), a sink-only
     transport is False, DropTransport flips it back
"""
import json
import os
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ.pop("VIBB_BT_GATE", None)

from vibb import btbus  # noqa: E402

JR = "2C:FD:B3:5B:1C:BA"
GO = "30:C0:1B:BD:13:B2"
SRC, SNK = btbus.A2DP_SOURCE_UUID, "0000110b-0000-1000-8000-00805f9b34fb"


def tree(**transports):
    """{mac: uuid} -> an ObjectManager tree like BlueZ's."""
    objs = {"/org/bluez/hci0": {"org.bluez.Adapter1": {"Powered": True}}}
    for mac, uuid in transports.items():
        mac = mac.replace("_", ":")
        dev = "/org/bluez/hci0/dev_" + mac.replace(":", "_")
        objs[dev] = {"org.bluez.Device1": {"Connected": True}}
        objs[dev + "/sep1/fd0"] = {"org.bluez.MediaTransport1": {
            "Device": dev, "UUID": uuid, "State": "idle", "Codec": 0}}
    return objs


# 1. the rule
assert btbus.transport_in_managed(tree(**{JR.replace(":", "_"): SRC}), JR)
assert not btbus.transport_in_managed(tree(**{JR.replace(":", "_"): SNK}), JR), \
    "a phone streaming INTO the box is not our transport"
assert not btbus.transport_in_managed(tree(**{JR.replace(":", "_"): SRC}), GO), \
    "another device's transport never counts"
assert not btbus.transport_in_managed(tree(), JR)
assert btbus.transport_in_managed(tree(**{JR.replace(":", "_"): SRC}), JR.lower()), \
    "the MAC argument's case must not matter (BlueZ paths are upper-case)"
print("1. source transport for the MAC counts; sink/other/none do not OK")

# 2. the busctl fallback's parser
dev = "/org/bluez/hci0/dev_" + JR.replace(":", "_")
reply = {"type": "a{oa{sa{sv}}}", "data": [{
    dev: {"org.bluez.Device1": {"Connected": {"type": "b", "data": True}}},
    dev + "/sep1/fd0": {"org.bluez.MediaTransport1": {
        "UUID": {"type": "s", "data": SRC},
        "State": {"type": "s", "data": "active"},
        "Volume": {"type": "q", "data": 100}}}}]}
objs = btbus.busctl_objects(json.dumps(reply))
assert objs[dev + "/sep1/fd0"]["org.bluez.MediaTransport1"]["UUID"] == SRC
assert btbus.transport_in_managed(objs, JR)
assert btbus.busctl_objects("garbage") == {}
assert btbus.busctl_objects('{"data": "nope"}') == {}
assert btbus.busctl_objects('{"data": [[]]}') == {}
print("2. busctl --json=short parsed; garbage reads as no transport OK")

# 3. shadow mode (the default): pcm answers, transport is compared at most
#    every SHADOW_S, disagreement logged once + on flip + on end
assert btbus.GATE_MODE == "shadow", "shadow is the default until the flip"
logs = []
btbus.log = lambda m: logs.append(m)
btbus._BACKEND = "dbus"
pcm, tr, reads = {"v": True}, {"v": False}, {"n": 0}


def fake_tr(mac):
    reads["n"] += 1
    return tr["v"]


btbus._dbus_a2dp_pcm_present = lambda mac: pcm["v"]
btbus._dbus_a2dp_transport_present = fake_tr
btbus._shadow.update({"at": 0.0, "since": None, "dir": None})
assert btbus.a2dp_pcm_present(JR) is True, "the answer is the PCM's"
assert reads["n"] == 1 and any("DISAGREE transport=False pcm=True (started)"
                               in m for m in logs), logs
for _ in range(5):
    btbus.a2dp_pcm_present(JR)
assert reads["n"] == 1, "rate-limited: no transport read inside SHADOW_S"
btbus._shadow["at"] = 0.0          # SHADOW_S elapsed
btbus.a2dp_pcm_present(JR)
assert reads["n"] == 2 and sum("DISAGREE" in m for m in logs) == 1, \
    "a continuing disagreement is logged once, not per compare"
btbus._shadow["at"] = 0.0
tr["v"] = True
btbus.a2dp_pcm_present(JR)
assert any("agree again after" in m for m in logs), logs
assert btbus._shadow["since"] is None
btbus._shadow["at"] = 0.0
pcm["v"], tr["v"] = False, True    # the other direction (a real finding)
assert btbus.a2dp_pcm_present(JR) is False
assert any("DISAGREE transport=True pcm=False (started)" in m for m in logs)
print("3. shadow: PCM answers, one compare per SHADOW_S, one log per episode OK")

# 4. transport mode answers with the transport alone
btbus.GATE_MODE = "transport"
pcm["v"], tr["v"] = False, True
assert btbus.a2dp_pcm_present(JR) is True
tr["v"] = False
assert btbus.a2dp_pcm_present(JR) is False
btbus.GATE_MODE = "shadow"
print("4. VIBB_BT_GATE=transport: the transport is the answer OK")

# 5. the live half, on the rig: fake_bluezd over a private bus
probe = subprocess.run([sys.executable, "-c", "import dbus, gi"],
                       capture_output=True)
if probe.returncode != 0 or not shutil.which("dbus-daemon") \
        or not shutil.which("dbus-send"):
    print("5. SKIP live fake_bluezd half (python3-dbus/gi/dbus-daemon "
          "missing here — runs on the rig)")
    print("\nall bt_transport_gate checks passed (offline half)")
    sys.exit(0)

addr = subprocess.run(["dbus-daemon", "--session", "--print-address",
                       "--fork"], capture_output=True, text=True,
                      check=True).stdout.strip()
env = dict(os.environ, DBUS_SYSTEM_BUS_ADDRESS=addr)
fake = subprocess.Popen([sys.executable,
                         os.path.join(REPO, "tests", "fake_bluezd.py")],
                        env=env, stdout=subprocess.PIPE, text=True)
assert "ready" in fake.stdout.readline()
call = ["dbus-send", "--bus=" + addr, "--print-reply", "--dest=org.bluez",
        "--type=method_call", "/org/vibb/mock"]


def mock(method, *args):
    subprocess.run(call + ["org.vibb.Mock." + method] + list(args),
                   check=True, capture_output=True)


def ask(mode):
    code = f"""
import json, sys
sys.path.insert(0, {json.dumps(os.path.join(REPO, "pi"))})
from vibb import btbus
print(json.dumps({{"pcm": btbus.a2dp_pcm_present({json.dumps(JR)}),
                   "tr": btbus.a2dp_transport_present({json.dumps(JR)}),
                   "backend": btbus.backend()}}))
"""
    e = dict(env, VIBB_BT_BACKEND="dbus", VIBB_BT_GATE=mode)
    r = subprocess.run([sys.executable, "-c", code], env=e,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


try:
    mock("AddDevice", f"string:{JR}", "string:JBL JR310BT",
         "boolean:true", "boolean:true", "int16:0")
    a = ask("shadow")
    assert a["backend"] == "dbus" and a == {"pcm": False, "tr": False,
                                            "backend": "dbus"}, a
    mock("SetPcm", f"string:{JR}", "boolean:true")
    a = ask("shadow")
    assert a["pcm"] is True and a["tr"] is True, \
        f"SetPcm must also create the source transport (parity): {a}"
    assert ask("transport")["pcm"] is True
    mock("DropTransport", f"string:{JR}")
    a = ask("transport")
    assert a["pcm"] is False and a["tr"] is False, a
    mock("SetTransport", f"string:{JR}", f"string:{SNK}", "string:active")
    a = ask("transport")
    assert a["tr"] is False, f"a sink-side transport must not count: {a}"
    mock("SetTransport", f"string:{JR}", f"string:{SRC}", "string:pending")
    assert ask("transport")["tr"] is True, "pending counts (configured)"
    print("5. live fake_bluezd: SetPcm -> transport, sink-only False, "
          "pending True OK")
finally:
    fake.terminate()

print("\nall bt_transport_gate checks passed")
