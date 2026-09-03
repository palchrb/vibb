#!/usr/bin/env python3
"""I10: the audio policy self-test PROVES the no-fail-over behaviour at
boot instead of trusting a config file in someone else's package
(PLAN-pipewire-soloist §B.6, AM-7, AM-8).

pw_dump() is scripted in the fixed 7-phase order policy_selftest()
documents; every command it forks is recorded. Pinned:

  1. a green graph -> verdict ok, written to POLICY_FILE with the core
     cookie; the probes are cleaned up (metadata deleted, sinks destroyed,
     streams killed)
  2. each SAFETY drift alone -> fail-safety naming it: a targetless stream
     that linked (to the HAT!), a stream re-homed after its target
     vanished, a pinned stream that followed the default sink, a settings
     belt that says true, a HAT gain below 1.0
  3. an RF drift alone (codec not sbc, suspend not 120, a bluez input
     node) -> fail-rf, never fail-safety
  4. no graph -> down
  5. cap_everywhere(): only fail-safety AND the pipewire stack
  6. selftest_due(): never run / cookie changed -> yes; same cookie -> no
     (a crash-looping daemon must not probe every 5 s)
"""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["VIBB_RUN"] = TMP
os.environ["VIBB_AUDIO_POLICY_FILE"] = os.path.join(TMP, "policy.json")
os.environ["VIBB_AUDIO_STACK"] = "pipewire"
sys.path.insert(0, os.path.join(REPO, "pi"))

from vibb import audio  # noqa: E402

audio.time.sleep = lambda s: None
audio.log = lambda *a: None
CMDS = []
WPCTL = {"text": ""}


def fake_run(cmd, timeout=5.0):
    CMDS.append(list(cmd))
    if cmd[:2] == ["wpctl", "settings"]:
        return 0, WPCTL["text"]
    return 0, ""


class P:
    def kill(self):
        CMDS.append(["<kill>"])

    def wait(self, timeout=2):
        pass


audio._run = fake_run
audio._popen = lambda cmd, props: CMDS.append(["<popen>"] + list(cmd)) or P()
GOOD_SETTINGS = "\n".join(
    f"  - Name: {k}\n    Value: false" for k in (
        "linking.allow-moving-streams", "linking.follow-default-target",
        "node.stream.restore-target", "node.stream.restore-props"))


def node(i, name, mclass, params=None, **props):
    p = {"node.name": name, "media.class": mclass}
    p.update(props)
    info = {"props": p}
    if params:
        info["params"] = params
    return {"id": i, "type": "PipeWire:Interface:Node", "info": info}


def link(i, out_id, in_id):
    return {"id": i, "type": "PipeWire:Interface:Link",
            "info": {"output-node-id": out_id, "input-node-id": in_id}}


def base(cookie=1, hat_vol=1.0, codec="sbc", suspend="120", bt_input=False):
    d = [{"id": 0, "type": "PipeWire:Interface:Core", "info": {"cookie": cookie}},
         node(47, "alsa_output.hat", "Audio/Sink",
              params={"Props": [{"mute": False, "channelVolumes": [hat_vol, hat_vol]}]},
              **{"alsa.card_name": "snd_rpi_hifiberry_dac"}),
         node(61, "bluez_output.X.1", "Audio/Sink",
              **{"api.bluez5.address": "2C:FD:B3:FA:DA:04", "api.bluez5.codec": codec,
                 "session.suspend-timeout-seconds": suspend}),
         node(70, "vibb_null", "Audio/Sink")]
    if bt_input:
        d.append(node(62, "bluez_input.X.0", "Audio/Source"))
    return d


STREAM = "Stream/Output/Audio"


def phases(*, targetless_to=None, rescued_to=None, default_to=None, **kw):
    b = base(**kw)
    sink = node(90, "vibb_selftest_sink", "Audio/Sink")
    sink_b = node(91, "vibb_selftest_B", "Audio/Sink")
    p2 = b + [node(80, "vibb-selftest-targetless", STREAM)] + (
        [link(200, 80, targetless_to)] if targetless_to else [])
    p3 = b + [sink]
    p4 = b + [sink, node(81, "vibb-selftest-pinned", STREAM), link(201, 81, 90)]
    p5 = b + [node(81, "vibb-selftest-pinned", STREAM)] + (
        [link(202, 81, rescued_to)] if rescued_to else [])
    p6 = b + [sink, sink_b, node(82, "vibb-selftest-default", STREAM),
              link(203, 82, default_to or 90)]
    return [b, p2, p3, p4, p5, p6, b]


def run(seq, settings=GOOD_SETTINGS):
    it = iter(seq)
    audio.pw_dump = lambda timeout=3.0: next(it)
    WPCTL["text"] = settings
    CMDS.clear()
    audio._policy_cache["mtime"] = None
    return audio.policy_selftest()


# 1. green
v = run(phases())
assert v["verdict"] == "ok" and v["safety"] == [] and v["rf"] == [], v
assert json.load(open(os.environ["VIBB_AUDIO_POLICY_FILE"]))["cookie"] == 1
assert ["pw-metadata", "0", "default.configured.audio.sink", "-d"] in CMDS, "metadata cleaned up"
assert sum(c[:2] == ["pw-cli", "destroy"] for c in CMDS) >= 3, "probe sinks destroyed"
assert CMDS.count(["<kill>"]) == 3, "every probe stream killed"
assert audio.selftest_state()["verdict"] == "ok"
print("1. green graph -> ok, recorded with the cookie, probes cleaned up OK")

# 2. safety drifts, one at a time
v = run(phases(targetless_to=47))
assert v["verdict"] == "fail-safety" and "targetless-linked" in v["safety"], v
v = run(phases(rescued_to=47))
assert v["verdict"] == "fail-safety" and "rescued-after-vanish" in v["safety"], v
v = run(phases(default_to=91))
assert v["verdict"] == "fail-safety" and "followed-default" in v["safety"], v
v = run(phases(), settings=GOOD_SETTINGS.replace(
    "linking.follow-default-target\n    Value: false",
    "linking.follow-default-target\n    Value: true"))
assert v["verdict"] == "fail-safety" and \
    "setting:linking.follow-default-target=true" in v["safety"], v
v = run(phases(hat_vol=0.4))
assert v["verdict"] == "fail-safety" and "hat-gain" in v["safety"], v
assert ["pw-metadata", "0", "default.configured.audio.sink", "-d"] in CMDS, \
    "cleanup happens on failure too"
print("2. each safety drift -> fail-safety naming it OK")

# 3. rf drifts
v = run(phases(codec="aac"))
assert v["verdict"] == "fail-rf" and v["rf"] == ["codec:aac"] and v["safety"] == [], v
v = run(phases(suspend="5"))
assert v["verdict"] == "fail-rf" and "bt-suspend" in v["rf"], v
v = run(phases(bt_input=True))
assert v["verdict"] == "fail-rf" and "bluez-input-node" in v["rf"], v
print("3. rf drifts -> fail-rf, never fail-safety OK")

# 4. down
v = run([[]])
assert v["verdict"] == "down", v
print("4. no graph -> down OK")

# 5. cap_everywhere
run(phases(targetless_to=47))
assert audio.cap_everywhere() is True
run(phases())
assert audio.cap_everywhere() is False
run(phases(targetless_to=47))
os.environ["VIBB_AUDIO_STACK"] = "bluealsa"; audio._stack[0] = None
assert audio.cap_everywhere() is False, "never under bluealsa"
os.environ["VIBB_AUDIO_STACK"] = "pipewire"; audio._stack[0] = None
print("5. cap_everywhere: fail-safety AND pipewire only OK")

# 6. due
run(phases())
assert audio.selftest_due(base(cookie=1)) is False, "same cookie, fresh: not due"
assert audio.selftest_due(base(cookie=2)) is True, "PipeWire restarted: due"
os.remove(os.environ["VIBB_AUDIO_POLICY_FILE"])
audio._policy_cache["mtime"] = None
assert audio.selftest_due(base(cookie=1)) is True, "never run: due"
print("6. selftest_due: never / cookie change -> yes, else no OK")

print("\nall audio_policy_selftest checks passed")
