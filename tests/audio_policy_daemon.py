#!/usr/bin/env python3
"""The daemon's side of the policy self-test (PLAN-pipewire-soloist §B.6,
AM-8): where it runs, what it exposes, and that bluealsa boxes see none
of it.

  1. /status carries audio_policy under pipewire (the verdict, or
     'pending' before the first run) and OMITS it under bluealsa
  2. a bt.py recovery never runs the self-test (AM-92: the probes only
     competed for CPU during the reconnect + resume)
  3. POST /audio/selftest starts a run off the request thread (202) and
     answers 409 under bluealsa
  4. no boot watcher thread (AM-92: on request only); one run at a
     time (a second trigger while one runs is dropped)
"""
import json
import os
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["VIBB_STATE"] = TMP
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
os.environ["VIBB_SETTINGS"] = os.path.join(TMP, "se.json")
os.environ["VIBB_LIBRARY"] = os.path.join(TMP, "lib.json")
os.environ["VIBB_AUDIO_POLICY_FILE"] = os.path.join(TMP, "policy.json")
os.environ["VIBB_AUDIO_STACK"] = "pipewire"
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

orch = daemon.ORCH
orch._mpv_alive = lambda: False
orch.target, orch.source = None, None
daemon.go_status = lambda **_k: {}
daemon.current_output = lambda **_k: {"output": "local", "pcm": "vibb_local"}
RUNS = []


def fake_selftest():
    RUNS.append(time.monotonic())
    time.sleep(0.2)
    return {"verdict": "ok"}


daemon._audio.policy_selftest = fake_selftest


def set_stack(s):
    os.environ["VIBB_AUDIO_STACK"] = s
    daemon._audio._stack[0] = None


# 1. /status
st = orch.status()
assert st.get("audio_policy") == "pending", st.get("audio_policy")
with open(os.environ["VIBB_AUDIO_POLICY_FILE"], "w") as f:
    json.dump({"verdict": "fail-safety", "safety": ["hat-gain"], "rf": []}, f)
daemon._audio._policy_cache["mtime"] = None
assert orch.status().get("audio_policy") == "fail-safety"
set_stack("bluealsa")
assert "audio_policy" not in orch.status(), "bluealsa boxes emit nothing"
set_stack("pipewire")
print("1. /status.audio_policy under pipewire only OK")

# 2. bt recovery never runs it (AM-92)


class R:
    def __init__(self, rc):
        self.returncode, self.stdout = rc, ""


daemon.subprocess.run = lambda *a, **k: R(0)
RUNS.clear()
assert daemon._bt_recover("ensure") is True
time.sleep(0.5)
assert len(RUNS) == 0, "a recovery does not run the self-test (AM-92)"
daemon.subprocess.run = lambda *a, **k: R(1)
assert daemon._bt_recover("ensure") is False
time.sleep(0.3)
assert len(RUNS) == 0
set_stack("pipewire")
print("2. bt recovery -> no self-test (AM-92) OK")

# 3. the POST route (source pin + the runner's single-flight)
src = open(daemon.__file__, encoding="utf-8").read()
i = src.index('elif self.path == "/audio/selftest":')
body = src[i:i + 700]
assert "audio-stack-not-pipewire" in body and "202" in body and "_audio_policy_run" in body
i_gate = src.index("def _require_token")
assert i > i_gate, "lives in the POST handler"
RUNS.clear()
t1 = threading.Thread(target=daemon._audio_policy_run, args=("a",))
t2 = threading.Thread(target=daemon._audio_policy_run, args=("b",))
t1.start(); time.sleep(0.05); t2.start(); t1.join(); t2.join()
assert len(RUNS) == 1, "one run at a time; the second trigger is dropped"
print("3. POST /audio/selftest route + single-flight runner OK")

# 4. the watcher thread only under pipewire
assert "_audio_policy_watch" not in src, "no boot watcher: the self-test runs on request only (AM-92)"
print("4. no boot watcher thread (on request only) OK")

print("\nall audio_policy_daemon checks passed")
