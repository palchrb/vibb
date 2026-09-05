#!/usr/bin/env python3
"""4d — the silent pass (warming) in soloistd, against the shared fake Soloist
(tests/soloist_fakes.py: a scripted RFC 6455 server that also plays the audio
cache) and the fake child. Pins AM-62..81:

  W1 POST /cache/download -> 202 queued; the child restarts on vibb_null
     (argv per start), /status is frozen as stopped with spotify_state live,
     every track is dwelled until WHOLE (the fake writes 128 KiB blocks and a
     one-block prefetch), the pass stops at autoplay, the child comes back on
     the kid's node, the ledger says done -> a second POST is 200 {done}
     without a restart
  W2 a cache that already holds the list: dwell 0, nothing written, done
  W3 the kid presses play mid-pass: abort -> child back on the kid's node ->
     the play lands on the fresh child
  W4 /cache/abort with nothing running is a no-op
  W6 a stalled link: three stalls end the pass 'stalled', nothing marked warmed
  W7 the child dies mid-pass: the restart lands on the kid's node and the pass
     is aborted (never a play into the wrong sink)
  W8 /context/tracks for a context that is not live answers from disk, stale,
     with cached as a count
  W9 poweroff imminent: the pass refuses before touching the child
"""
import json
import os
import signal
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tests"))
sys.path.insert(0, os.path.join(REPO, "pi"))
from soloist_fakes import (CTX, TRACKS, FAKE, start_sidecar, get, post, wait_state,  # noqa: E402
                           graph, install_pw_dump, PWD_FILE)


def health(base):
    return get(base, "/soloist/health")[1]


def wait_for(base, pred, timeout, what):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        h = health(base)
        if pred(h):
            return h
        time.sleep(0.1)
    raise AssertionError(f"{what}: {health(base)}")


def argvs(data):
    out = []
    for n in sorted(x for x in os.listdir(data) if x.startswith("argv-")):
        a = eval(open(os.path.join(data, n)).read())
        out.append(a[a.index("-d") + 1] if "-d" in a else None)
    return out


def fresh_fake(mode="fast"):
    FAKE.status, FAKE.context, FAKE.idx = "idle", None, 0
    FAKE.fetch_mode = mode
    FAKE.cached_uris = set()
    FAKE.fetch_log = []
    FAKE.received.clear()


# ---- W1: the whole pass --------------------------------------------------------
fresh_fake("fast")
install_pw_dump("follow", null=True)      # the stream follows the child: bench node, then vibb_null
p, base, data = start_sidecar()
wait_state(base, "ok")
gen0 = health(base)["gen"]
code, r = post(base, "/cache/download", {"uri": CTX})
assert code == 202 and r["queued"] is True, (code, r)
h = wait_for(base, lambda h: h["node"] == "vibb_null" and h["gen"] == gen0 + 1, 10, "child on vibb_null")
assert h["warming"] and h["warming"]["uri"] == CTX, h
st = get(base, "/status")[1]
assert st["stopped"] is True and st["paused"] is False, "frozen as STOPPED (AM-64)"
assert st["spotify_state"] in ("starting", "ok"), st["spotify_state"]
h = wait_for(base, lambda h: h["warming"] is None and h["warm_last"], 60, "pass finished")
assert h["warm_last"]["result"] == "done", h["warm_last"]
cmds = [(m.get("command"), m.get("volume"), m.get("uri")) for m in FAKE.received]
i_vol = next(i for i, c in enumerate(cmds) if c[0] == "set_volume" and c[1] == 0)
i_play = next(i for i, c in enumerate(cmds) if c[0] == "play" and c[2] == CTX)
assert i_vol < i_play, f"shroud: set_volume 0 before the first play (AM-63): {cmds[:6]}"
assert h["node"] == "vibb_bench_node" and h["gen"] == gen0 + 2, h
assert argvs(data)[-2:] == ["vibb_null", "vibb_bench_node"], argvs(data)
fetched = [u for u, _ in FAKE.fetch_log]
assert sorted(fetched) == sorted(TRACKS), f"every track dwelled to WHOLE: {fetched}"
assert all(n >= 180 * 160 * 125 for _, n in FAKE.fetch_log), FAKE.fetch_log
wait_state(base, "ok")                    # the restored child settles in a beat
st = get(base, "/status")[1]
if not (st["stopped"] is True and st["spotify_state"] == "ok"):
    print("--- health ---", health(base)); print("--- sidecar log ---"); print(open(p.logpath).read()[-4000:])
assert st["stopped"] is True and st["spotify_state"] == "ok", "freeze lifted, fresh child idle"
# three fresh children, each 'not logged in' for a beat while restoring the
# session: 'starting' inside the grace, never needs-pair (the daemon fast-fails on it)
assert "needs-pair" not in open(p.logpath).read(), open(p.logpath).read()[-1500:]
ledger = json.load(open(os.path.join(data, "vibb", "ledger.json")))
assert sorted(ledger[CTX]["warmed"]) == sorted(TRACKS) and ledger[CTX]["result"] == "done", ledger[CTX]
assert ledger[CTX]["complete"] is True and ledger[CTX]["fingerprint"]
# done + verified: the second ask costs nothing — no restart, no child touched
code, r = post(base, "/cache/download", {"uri": CTX})
assert code == 200 and r["done"] is True, (code, r)
time.sleep(0.5)
assert health(base)["gen"] == gen0 + 2
print("W1. pass: null sink, frozen stopped, every track whole, autoplay stop, restore, ledger done OK")

# ---- W8: the remembered list answers for a context that is not live --------------
# (Soloist restores its session across a restart — 'restoring session' in the
# Zero's log — so the context can still be live after the pass; make it not)
FAKE.status, FAKE.context = "idle", None
FAKE.send(FAKE.state()); time.sleep(0.3)
code, lst = get(base, "/context/tracks?uri=" + CTX)
assert lst["stale"] is True and lst["cached"] == lst["length"] == len(TRACKS), lst
assert [t["uri"] for t in lst["tracks"]] == TRACKS and lst["tracks"][0]["track"]["name"] == "T0"
print("W8. listing from disk for a non-live context: stale, counted, ordered OK")
p.terminate(); p.wait(5)

# ---- W2: everything already in the cache: dwell 0, nothing written --------------
fresh_fake("fast")
p, base, data = start_sidecar()          # (start_sidecar resets the fake's cache view)
FAKE.cached_uris = set(TRACKS)
wait_state(base, "ok")
t0 = time.monotonic()
code, r = post(base, "/cache/download", {"uri": CTX})
assert code == 202
h = wait_for(base, lambda h: h["warming"] is None and h["warm_last"], 40, "cached pass")
assert h["warm_last"]["result"] == "done" and FAKE.fetch_log == [], (h["warm_last"], FAKE.fetch_log)
assert time.monotonic() - t0 < 25, "already-cached tracks cost the settle wait only"
print("W2. cached list: done without a byte written OK")
p.terminate(); p.wait(5)

# ---- W3: the kid presses play mid-pass ----------------------------------------------
fresh_fake("slow")
install_pw_dump("follow", null=True)
p, base, data = start_sidecar()
wait_state(base, "ok")
gen0 = health(base)["gen"]
post(base, "/cache/download", {"uri": CTX})
wait_for(base, lambda h: h["node"] == "vibb_null" and h["warming"], 10, "pass owns the child")
time.sleep(1.0)
FAKE.received.clear()
t0 = time.monotonic()
code, r = post(base, "/player/play", {"uri": CTX})
dt = time.monotonic() - t0
assert code == 200 and r["ok"], (code, r)
h = health(base)
assert h["warming"] is None and h["node"] == "vibb_bench_node", h
assert h["warm_last"]["result"].startswith("aborted"), h["warm_last"]
assert argvs(data)[-1] == "vibb_bench_node" and argvs(data)[-2] == "vibb_null", argvs(data)
names = [m["command"] for m in FAKE.received]
assert "play" in names and names.index("play") > 0 and FAKE.status == "playing", names
assert dt < 15, f"the kid waited {dt:.1f}s"
st = get(base, "/status")[1]
assert st["stopped"] is False and st["track"]["uri"] == TRACKS[0], st
print(f"W3. play mid-pass: aborted, restored, the play landed in {dt:.1f}s OK")
# ---- W4: abort with nothing running ---------------------------------------------------
assert post(base, "/cache/abort")[1] == {"aborted": False}
print("W4. /cache/abort without a pass is a no-op OK")
p.terminate(); p.wait(5)

# ---- W6: a stalled link ------------------------------------------------------------
fresh_fake("stall")
install_pw_dump("follow", null=True)
p, base, data = start_sidecar()
wait_state(base, "ok")
post(base, "/cache/download", {"uri": CTX})
h = wait_for(base, lambda h: h["warming"] is None and h["warm_last"], 60, "stalled pass")
assert h["warm_last"]["result"] == "stalled", h["warm_last"]
ledger = json.load(open(os.path.join(data, "vibb", "ledger.json")))
assert ledger[CTX]["warmed"] == [], "a stub is never marked warmed"
assert h["node"] == "vibb_bench_node", "restored after a stalled pass"
print("W6. stalled fetches: three strikes end the pass, nothing marked warmed, restored OK")
p.terminate(); p.wait(5)

# ---- W7: the child dies mid-pass ---------------------------------------------------
fresh_fake("slow")
install_pw_dump("follow", null=True)
p, base, data = start_sidecar()
wait_state(base, "ok")
post(base, "/cache/download", {"uri": CTX})
h = wait_for(base, lambda h: h["node"] == "vibb_null" and h["warming"], 10, "pass owns the child")
pid = h["child"]
os.kill(pid, signal.SIGKILL)
try:
    h = wait_for(base, lambda h: h["warming"] is None and h["node"] == "vibb_bench_node" and h["child"]
                 and h["child"] != pid, 30, "restart on the kid's node after the crash")
    assert h["warm_last"]["result"].startswith("aborted"), h["warm_last"]
    for _ in range(50):                      # the child writes its argv a beat after health moves
        if argvs(data)[-1] == "vibb_bench_node":
            break
        time.sleep(0.1)
    assert argvs(data)[-1] == "vibb_bench_node", argvs(data)
    assert argvs(data).count("vibb_bench_node") == 2, f"one restore, never a duplicate child: {argvs(data)}"
except AssertionError:
    print("--- sidecar log ---"); print(open(p.logpath).read()[-3000:]); raise
print("W7. child died mid-pass: pass aborted, restart on the kid's node, never a play into it OK")
p.terminate(); p.wait(5)

# ---- W9: poweroff imminent ------------------------------------------------------
fresh_fake("fast")
install_pw_dump("follow", null=True)
p, base, data = start_sidecar()
wait_state(base, "ok")
run_dir = [l for l in open("/proc/%d/environ" % p.pid, "rb").read().split(b"\0") if l.startswith(b"VIBB_RUN=")][0][9:].decode()
open(os.path.join(run_dir, "poweroff-imminent"), "w").write("x")
gen0 = health(base)["gen"]
post(base, "/cache/download", {"uri": CTX})
h = wait_for(base, lambda h: h["warming"] is None and h["warm_last"], 10, "refused")
assert h["warm_last"]["result"] == "poweroff" and h["gen"] == gen0, h
print("W9. poweroff imminent: the pass refuses, the child is untouched OK")
p.terminate(); p.wait(5)

print("\nall soloist_warm checks passed")
