#!/usr/bin/env python3
"""soloistd, driven for real: the REAL pi/soloistd.py process, a FAKE
Soloist (a stdlib RFC6455 server scripted from tests/soloist_contract.py's
WS side) and a fake `soloist` binary that behaves like the child (writes
ws.addr/ws.port, prints the expiry line, sleeps — or exits 10).

  1. no API key -> needs-key, no child, /status stopped with no username
  2. with a key: child up, WS mirrored, auth -> ok; /status carries every
     contract field; username is synthesized (Soloist has none);
     days_left parsed from the child's own line; the child got -d <node>
  3. /player/play {uri, skip_to_uri, position}: the resume walk under the
     shroud — set_volume 0, play, pause, skip_next until the item matches,
     seek, play, volume restored; lands on the target at the position;
     play_origin is the box's for that context, 'remote' for another
  4. controls map 1:1; pending_track_uri set by next and cleared by
     track_changed; /context/tracks lists the ACTIVE context from
     get_queue (context rows only, autoplay dropped) in the contract shape;
     another uri -> ready and empty; /cache/* -> 404
  5. exit 10 latches: state expired, latch file persisted, NO restart; a
     fresh sidecar with the latch present never spawns the child
  6. a plain crash -> offline, then a bounded-backoff restart
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tests"))
sys.path.insert(0, os.path.join(REPO, "pi"))
from soloist_fakes import (C, TMP, CTX, TRACKS, FAKE, start_sidecar, get, post,  # noqa: E402
                           wait_state, graph, install_pw_dump, PWD_FILE)

# 1. needs-key
p, base, data = start_sidecar(key="")
h = wait_state(base, "needs-key")
assert h["child"] is None
st = get(base, "/status")[1]
assert st["stopped"] is True and st["username"] is None and st["spotify_state"] == "needs-key"
p.terminate(); p.wait(5)
print("1. no key -> needs-key, no child OK")

# 2. ok path
p, base, data = start_sidecar()
h = wait_state(base, "ok")
assert h["days_left"] == 42 and h["ws"] is True
# the banner minus the child's log stamp: stable across children (AM-84)
assert h["build"] == "soloist 1.3.8.13 build 1788609705 (20260905) (g5c3a2053ac) (linux/aarch64)", h["build"]
argv = eval(open(os.path.join(data, "argv.json")).read())
assert argv[argv.index("-d") + 1] == "vibb_bench_node" and argv[argv.index("-k") + 1] == "k" \
    and argv[argv.index("-n") + 1] == "Vibb (test)" and "-z" in argv, argv
st = get(base, "/status")[1]
assert C.STATUS_FIELDS <= set(st), set(st)
assert st["username"] == "FakeBox" and st["volume_steps"] == 100 and st["stopped"] is True
print("2. key -> child up, mirrored, ok; contract fields; username synthesized; -d node OK")

# 3. the resume walk
FAKE.received.clear()
code, r = post(base, "/player/play", {"uri": CTX, "skip_to_uri": TRACKS[3], "position": 45000})
assert code == 200, r
cmds = [(m["command"], m.get("volume"), m.get("position_ms")) for m in FAKE.received]
# the walk's CONTROL sequence: the get_queue snapshot at the start (AM-60) is
# a query riding along, not a control step
cmds = [c for c in cmds if c[0] != "get_queue"]
names = [c[0] for c in cmds]
assert names[0] == "set_volume" and cmds[0][1] == 0, cmds
assert names[1] == "play" and names[2] == "pause", names
assert names.count("skip_next") == 3, names
assert "seek" in names and cmds[names.index("seek")][2] == 45000
assert names[-2] == "play" and names[-1] == "set_volume" and cmds[-1][1] == 40, cmds
time.sleep(0.3)
st = get(base, "/status")[1]
assert st["track"]["uri"] == TRACKS[3] and 45000 <= st["track"]["position"] < 47000, st["track"]
assert st["paused"] is False and st["stopped"] is False and st["play_origin"] == "go-librespot"
assert st["track"]["name"] == "T3" and st["track"]["artist_names"] == ["A"] and st["track"]["duration"] == 180000
print("3. resume walk under the shroud lands on the target at the position; box origin OK")
# AM-60: the walk skipped past t0..t2 and the fake's history keeps only 2, yet
# the listing is the WHOLE list — remembered at the context's start
code, walked = get(base, "/context/tracks?uri=" + CTX)
assert [x["uri"] for x in walked["tracks"]] == TRACKS, [x["uri"] for x in walked["tracks"]]
print("3a. after a resume walk to track 4 the listing is still the whole list (remembered at start) OK")

# 4. controls, pending, listing, cache 404s
FAKE.received.clear()
post(base, "/player/pause"); post(base, "/player/resume"); post(base, "/player/seek", {"position": 1000})
post(base, "/player/volume", {"volume": 55}); post(base, "/player/shuffle_context", {"shuffle_context": True})
names = [m["command"] for m in FAKE.received]
assert names == ["pause", "play", "seek", "set_volume", "set_shuffle"], names
code, r = post(base, "/player/next"); time.sleep(0.2)
assert get(base, "/status")[1]["track"]["uri"] == TRACKS[4] and get(base, "/status")[1]["pending_track_uri"] is None
code, listing = get(base, "/context/tracks?uri=" + CTX)
assert C.LISTING_FIELDS <= set(listing) and listing["ready"]
assert listing["cached"] == len(listing["tracks"]) == listing["length"], "cached is a COUNT (AM-74)"
uris = [t["uri"] for t in listing["tracks"]]
assert uris == TRACKS, uris                       # previous + current + upcoming, autoplay dropped
assert C.LISTING_ITEM <= set(listing["tracks"][0]) and listing["tracks"][0]["track"]["name"] == "T0"
code, other = get(base, "/context/tracks?uri=spotify:playlist:other")
assert other["ready"] and other["tracks"] == [] and other["length"] == 0 and other["cached"] == 0
# 4b. a Soloist that does not answer get_queue (Zero 2026-09-05: the Sonos
#     hand-off's listing timed out at the daemon's 5 s): the ask is bounded
#     and the last good listing for the context is served — fast
FAKE.mute_queue = True
t0 = time.monotonic()
code, again = get(base, "/context/tracks?uri=" + CTX)
dt = time.monotonic() - t0
assert code == 200 and again["ready"] and again["cached"] and [x["uri"] for x in again["tracks"]] == TRACKS, again
assert dt < 4.8, f"the bounded ask must beat the daemon's 5 s: {dt:.1f}s"
#     a context never listed before, still no answer: not ready, still fast
FAKE.context = "spotify:playlist:fresh"; FAKE.send(FAKE.state()); time.sleep(0.2)
t0 = time.monotonic()
code, fresh = get(base, "/context/tracks?uri=spotify:playlist:fresh")
dt = time.monotonic() - t0
assert code == 200 and fresh["ready"] is False and fresh["tracks"] == [], fresh
assert dt < 4.8, f"{dt:.1f}s"
FAKE.mute_queue = False
FAKE.context = CTX; FAKE.send(FAKE.state()); time.sleep(0.2)
print("4b. a slow Soloist: the last good listing, bounded; unknown context: not-ready, bounded OK")
assert get(base, "/status")[1]["play_origin"] == "go-librespot"
FAKE.context = "spotify:album:phone"; FAKE.send(FAKE.state()); time.sleep(0.2)
assert get(base, "/status")[1]["play_origin"] == "remote", "a context the box did not start = the phone"
try:
    urllib.request.urlopen(base + "/cache/snapshot?uri=x", timeout=5); raise AssertionError("must 404")
except urllib.error.HTTPError as e:
    assert e.code == 404
# /cache/download is the warm (4d): queued at once; without a vibb_null sink in
# the graph (this fake pw-dump has none) the pass refuses BEFORE touching the
# child — same generation, same pid, no restart (AM-63)
gen0 = get(base, "/soloist/health")[1]["gen"]; pid0 = get(base, "/soloist/health")[1]["child"]
code, r = post(base, "/cache/download", {"uri": CTX})
assert code == 202 and r.get("queued") is True, (code, r)
for _ in range(50):
    h = get(base, "/soloist/health")[1]
    if h["warming"] is None and h["warm_last"]:
        break
    time.sleep(0.1)
assert h["warm_last"]["result"].startswith("aborted: no vibb_null"), h["warm_last"]
assert h["gen"] == gen0 and h["child"] == pid0, "no null sink: the child is never touched"
p.terminate(); p.wait(5)
print("4. controls 1:1, pending cleared, listing shape, remote origin, snapshot 404, download refused without vibb_null OK")

# 5. exit 10 latches
p, base, data = start_sidecar(mode="exit10")
wait_state(base, "expired", timeout=10)
assert os.path.exists(os.path.join(data, "build-expired.latch"))
time.sleep(1.5)
assert get(base, "/soloist/health")[1]["child"] is None, "an expired build is never restarted"
p.terminate(); p.wait(5)
p, base, _ = start_sidecar(mode="run", data=data)      # same data dir: the latch persists
h = wait_state(base, "expired", timeout=5)
assert h["child"] is None and not os.path.exists(os.path.join(data, "argv.json.new"))
p.terminate(); p.wait(5)
print("5. exit 10: latched, persisted, never respawned OK")

# 6. a crash restarts with backoff
p, base, data = start_sidecar(mode="crash")
wait_state(base, "offline", timeout=10)
out = ""
for _ in range(40):
    time.sleep(0.1)
    if get(base, "/soloist/health")[1]["child"] is not None:
        break
else:
    raise AssertionError("no restart after a crash")
p.terminate(); p.wait(5); out = open(p.logpath).read()   # the harness logs to a file now
assert "restarting in 0.5s" in out, out[-500:]
print("6. a crash -> offline -> bounded-backoff restart OK")

# 7. AM-16: the bind check. A fake pw-dump on PATH shows the soloist
#    stream linked (a) to the pinned node -> bound, state ok; (b) to
#    another sink -> on the first playing event the child is paused,
#    killed, and the state is audio-unbound (fail closed)
FAKE.status, FAKE.context = "idle", None          # the shared fake: a fresh session
install_pw_dump(1)
p, base, data = start_sidecar()
h = wait_state(base, "ok")
for _ in range(30):
    if get(base, "/soloist/health")[1]["bound"] is True:
        break
    time.sleep(0.1)
assert get(base, "/soloist/health")[1]["bound"] is True, "linked to the pinned node = bound"
post(base, "/player/play", {"uri": CTX}); time.sleep(2.6)
assert get(base, "/soloist/health")[1]["state"] == "ok"
p.terminate(); p.wait(5)
FAKE.status, FAKE.context = "idle", None
# the real shape: no stream node until the child plays (lazy) — the start
# check proves nothing, the first playing event's authoritative one decides
open(PWD_FILE, "w").write(json.dumps([o for o in graph(1) if o["id"] not in (9, 20)]))
p, base, data = start_sidecar()
wait_state(base, "ok")
assert get(base, "/soloist/health")[1]["bound"] is None, "nothing proven before the stream exists"
open(PWD_FILE, "w").write(json.dumps(graph(2)))            # the stream lands on the HDMI sink instead
FAKE.received.clear()
post(base, "/player/play", {"uri": CTX})
h = wait_state(base, "audio-unbound", timeout=8)
assert h["child"] is None and h["bound"] is False, h
assert any(m["command"] == "pause" for m in FAKE.received), "a mis-bound child is paused before it is killed"
p.terminate(); p.wait(5)
print("7. bind check: pinned node -> bound; another sink -> paused, killed, audio-unbound OK")

# 8. /soloist/updated (AM-52): a fresh build clears the exit-10 latch and
#    restarts the child — now when idle, deferred while playing, never on
#    the way down (poweroff-imminent marker)
FAKE.status, FAKE.context = "idle", None
open(PWD_FILE, "w").write(json.dumps(graph(1)))
p, base, data = start_sidecar()
wait_state(base, "ok")
open(os.path.join(data, "build-expired.latch"), "w").write("stale\n")
pid0 = get(base, "/soloist/health")[1]["child"]
code, r = post(base, "/soloist/updated")
assert r["result"] == "restarted", r
assert not os.path.exists(os.path.join(data, "build-expired.latch")), "a fresh build = a fresh 90 days"
wait_state(base, "ok")
assert get(base, "/soloist/health")[1]["child"] not in (None, pid0), "the child was restarted"
post(base, "/player/play", {"uri": CTX}); time.sleep(0.3)
code, r = post(base, "/soloist/updated")
assert r["result"] == "deferred" and r["pending_restart"] == "updated", r
run_dir = json.loads(open(os.path.join(data, "argv.json")).read().replace("'", '"'))  # noqa: just to touch data
p.terminate(); p.wait(5)
print("8. updated: latch cleared, restart when idle, deferred while playing OK")

# 8b. never on the way down
FAKE.status, FAKE.context = "idle", None
p, base, data = start_sidecar()
wait_state(base, "ok")
run_env_dir = None
for line in open("/proc/%d/environ" % p.pid, "rb").read().split(b"\0"):
    if line.startswith(b"VIBB_RUN="):
        run_env_dir = line.split(b"=", 1)[1].decode()
open(os.path.join(run_env_dir, "poweroff-imminent"), "w").write(str(time.time()))
pid0 = get(base, "/soloist/health")[1]["child"]
code, r = post(base, "/soloist/updated")
assert r["result"] == "next-boot", r
time.sleep(0.5)
assert get(base, "/soloist/health")[1]["child"] == pid0, "no restart on the poweroff path"
p.terminate(); p.wait(5)
print("8b. updated on the way down: next boot, child untouched OK")

# 9. /soloist/pair: the normal child is stopped, `soloist -p` runs and
#    stores the session, the child comes back; no key -> 409
FAKE.status, FAKE.context = "idle", None
p, base, data = start_sidecar()
wait_state(base, "ok")
pid0 = get(base, "/soloist/health")[1]["child"]
code, r = post(base, "/soloist/pair")
assert code == 202 and r["result"] == "pairing", (code, r)
for _ in range(60):
    h = get(base, "/soloist/health")[1]
    if h["state"] == "ok" and h["child"] not in (None, pid0) and not h["pairing"]:
        break
    time.sleep(0.1)
else:
    raise AssertionError(f"pairing never completed: {h}")
assert os.path.exists(os.path.join(data, "paired")), "soloist -p ran against the same data dir"
p.terminate(); p.wait(5)
p, base, data = start_sidecar(key="")
wait_state(base, "needs-key")
assert post(base, "/soloist/pair")[0] == 409
p.terminate(); p.wait(5)
print("9. pair: child stopped, --pair stored the session, child back; no key -> 409 OK")

# 10. the restore grace: a fresh child answers logged_in=false while it
#     restores the stored session (the Zero: ~1 s) — 'starting' inside the
#     grace (the daemon fast-fails a tap on needs-pair, AM-48); a child that
#     never logs in is needs-pair once the grace is over
FAKE.logged_in = False
try:
    p, base, data = start_sidecar()
    t0 = time.monotonic()
    seen = set()
    while time.monotonic() - t0 < 4.0:
        seen.add(get(base, "/soloist/health")[1]["state"])
        if "needs-pair" in seen:
            break
        time.sleep(0.05)
    dt = time.monotonic() - t0
    assert "needs-pair" in seen and dt >= 1.2, (seen, dt)          # not before the 1.5 s grace
    log = open(p.logpath).read()
    assert "state starting -> needs-pair" in log, log[-800:]        # straight from starting
    p.terminate(); p.wait(5)
finally:
    FAKE.logged_in = True
print(f"10. restore grace: 'starting' while the session restores, needs-pair after {dt:.1f}s OK")

print("\nall soloist_sidecar checks passed")
