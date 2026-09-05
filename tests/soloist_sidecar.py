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
import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tests"))
sys.path.insert(0, os.path.join(REPO, "pi"))
import soloist_contract as C  # noqa: E402

TMP = tempfile.mkdtemp()
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CTX = "spotify:playlist:p"
TRACKS = [f"spotify:track:t{i}" for i in range(6)]


# --- the fake Soloist: an RFC6455 server scripted per command --------------
class FakeSoloist:
    def __init__(self):
        self.srv = socket.socket(); self.srv.bind(("127.0.0.1", 0)); self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.received = []
        self.idx = 0
        self.status = "idle"
        self.context = None
        self.volume = 40
        self.logged_in = True
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += c.recv(4096)
        head, _, buf = buf.partition(b"\r\n\r\n")
        key = [l.split(b":", 1)[1].strip() for l in head.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
        acc = base64.b64encode(hashlib.sha1(key + GUID.encode()).digest())
        c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  b"Sec-WebSocket-Accept: " + acc + b"\r\n\r\n")
        self.conn = c
        try:
            while True:
                while len(buf) < 2:
                    d = c.recv(65536)
                    if not d:
                        return
                    buf += d
                b1, b2 = buf[0], buf[1]; ln = b2 & 0x7F; off = 2
                if ln == 126:
                    while len(buf) < 4: buf += c.recv(65536)
                    (ln,) = struct.unpack("!H", buf[2:4]); off = 4
                need = off + 4 + ln
                while len(buf) < need:
                    buf += c.recv(65536)
                mask = buf[off:off + 4]; payload = bytes(b ^ mask[i % 4] for i, b in enumerate(buf[off + 4:need]))
                buf = buf[need:]
                if (b1 & 0x0F) == 0x8:
                    return
                if (b1 & 0x0F) != 0x1:
                    continue
                self._handle(json.loads(payload.decode()))
        except OSError:
            pass

    def send(self, obj):
        data = json.dumps(obj).encode()
        hdr = struct.pack("!BB", 0x81, len(data)) if len(data) < 126 else struct.pack("!BBH", 0x81, 126, len(data))
        self.conn.sendall(hdr + data)

    def item(self, i):
        return C.sample_entity(TRACKS[i], f"T{i}", ["A"], "L", 180000)

    def state(self):
        return C.sample_playback_state(status=self.status,
                                       item=self.item(self.idx) if self.context else None,
                                       context={"uri": self.context, "entity_type": "playlist",
                                                "decorations": {"identity": {"name": "P"}}} if self.context else None,
                                       position={"position_ms": 0, "timestamp_ms": time.time() * 1000, "speed": 1.0},
                                       volume=self.volume)

    def _handle(self, msg):
        cmd = msg.get("command"); self.received.append(msg)
        if cmd == "get_auth_state":
            self.send({"type": "auth_state", "logged_in": self.logged_in, "is_active": True, "device_name": "FakeBox"})
            return
        if cmd == "get_state":
            self.send(self.state()); return
        self.send({"type": "command_result", "command": cmd})
        if cmd == "play":
            if msg.get("uri"):
                self.context, self.idx = msg["uri"], 0
                self.send({"type": "context_changed", "context": {"uri": self.context, "entity_type": "playlist",
                                                                   "decorations": {"identity": {"name": "P"}}}})
                self.send({"type": "track_changed", "item": self.item(0)})
            self.status = "playing"; self.send({"type": "playback_changed", "status": "playing"})
        elif cmd == "pause":
            self.status = "paused"; self.send({"type": "playback_changed", "status": "paused"})
        elif cmd in ("skip_next", "skip_prev"):
            self.idx = min(len(TRACKS) - 1, self.idx + 1) if cmd == "skip_next" else max(0, self.idx - 1)
            self.send({"type": "track_changed", "item": self.item(self.idx)})
        elif cmd == "seek":
            self.send({"type": "position_sync", "position": {"position_ms": msg["position_ms"], "timestamp_ms": time.time() * 1000, "speed": 1.0}})
        elif cmd == "set_volume":
            self.volume = msg["volume"]; self.send({"type": "volume_changed", "volume": self.volume})
        elif cmd == "set_shuffle":
            self.send({"type": "options_changed", "options": {"shuffle": msg["enabled"], "repeat": "off", "playback_speed": 1.0}})
        elif cmd == "get_queue":
            # Soloist's `previous` is a history stack, most recent first
            # (PLAN-soloistd: "reversed previous + current + upcoming")
            prev = [{"uid": f"u{i}", "source": "context", "item": self.item(i)} for i in reversed(range(self.idx))]
            upc = [{"uid": f"u{i}", "source": "context", "item": self.item(i)} for i in range(self.idx + 1, len(TRACKS))]
            upc.append({"uid": "ux", "source": "autoplay", "item": C.sample_entity("spotify:track:radio", "R", ["X"], "Y", 1000)})
            self.send({"type": "queue_changed", "previous": prev, "upcoming": upc})


FAKE = FakeSoloist()

# --- the fake child binary ---
BIN = os.path.join(TMP, "soloist")
open(BIN, "w").write('''#!/usr/bin/env python3
import os, sys, time
args = sys.argv[1:]
d = args[args.index("-D") + 1]
if "-p" in args:                       # --pair: store the session, exit 0
    open(os.path.join(d, "paired"), "w").write("ok")
    print("paired", flush=True); sys.exit(0)
open(os.path.join(d, "argv.json"), "w").write(repr(args))
open(os.path.join(d, "ws.addr"), "w").write("127.0.0.1")
open(os.path.join(d, "ws.port"), "w").write(os.environ["FAKE_WS_PORT"])
print("soloist 1.3.7.518 build 1788264113 (20260901)", flush=True)
print("client expires in 42 days", flush=True)
mode = os.environ.get("FAKE_MODE", "run")
if mode == "exit10":
    time.sleep(0.3); sys.exit(10)
if mode == "crash":
    time.sleep(0.3); sys.exit(1)
while True:
    time.sleep(1)
''')
os.chmod(BIN, 0o755)


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def start_sidecar(key="k", mode="run", data=None, pcm="vibb_bench_node"):
    data = data or tempfile.mkdtemp()
    state = tempfile.mkdtemp()
    with open(os.path.join(state, "output.json"), "w") as f:
        json.dump({"output": "local", "pcm": pcm}, f)
    port = free_port()
    env = dict(os.environ, VIBB_SOLOISTD_PORT=str(port), VIBB_SOLOIST_BIN=BIN,
               STATE_DIRECTORY=data, CACHE_DIRECTORY=tempfile.mkdtemp(), VIBB_STATE=state,
               VIBB_RUN=tempfile.mkdtemp(), VIBB_SETTINGS=os.path.join(state, "se.json"),
               VIBB_BT_FILE=os.path.join(state, "bt"), VIBB_DEVICE_NAME="Vibb (test)",
               FAKE_WS_PORT=str(FAKE.port), FAKE_MODE=mode, VIBB_SOLOIST_BACKOFF_S="0.5,0.5")
    if key:
        env["SOLOIST_API_KEY"] = key
    else:
        env.pop("SOLOIST_API_KEY", None)
    p = subprocess.Popen([sys.executable, os.path.join(REPO, "pi", "soloistd.py")], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/soloist/health", timeout=1); break
        except OSError:
            time.sleep(0.1)
    return p, base, data


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as r:
        return r.status, json.loads(r.read())


def post(base, path, body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait_state(base, want, timeout=8):
    for _ in range(int(timeout * 10)):
        st = get(base, "/soloist/health")[1]
        if st["state"] == want:
            return st
        time.sleep(0.1)
    raise AssertionError(f"state never became {want}: {get(base, '/soloist/health')[1]}")


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
assert h["days_left"] == 42 and "build" in (h["build"] or "") and h["ws"] is True
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

# 4. controls, pending, listing, cache 404s
FAKE.received.clear()
post(base, "/player/pause"); post(base, "/player/resume"); post(base, "/player/seek", {"position": 1000})
post(base, "/player/volume", {"volume": 55}); post(base, "/player/shuffle_context", {"shuffle_context": True})
names = [m["command"] for m in FAKE.received]
assert names == ["pause", "play", "seek", "set_volume", "set_shuffle"], names
code, r = post(base, "/player/next"); time.sleep(0.2)
assert get(base, "/status")[1]["track"]["uri"] == TRACKS[4] and get(base, "/status")[1]["pending_track_uri"] is None
code, listing = get(base, "/context/tracks?uri=" + CTX)
assert C.LISTING_FIELDS <= set(listing) and listing["ready"] and listing["cached"]
uris = [t["uri"] for t in listing["tracks"]]
assert uris == TRACKS, uris                       # previous + current + upcoming, autoplay dropped
assert C.LISTING_ITEM <= set(listing["tracks"][0]) and listing["tracks"][0]["track"]["name"] == "T0"
code, other = get(base, "/context/tracks?uri=spotify:playlist:other")
assert other["ready"] and other["tracks"] == [] and other["length"] == 0
assert get(base, "/status")[1]["play_origin"] == "go-librespot"
FAKE.context = "spotify:album:phone"; FAKE.send(FAKE.state()); time.sleep(0.2)
assert get(base, "/status")[1]["play_origin"] == "remote", "a context the box did not start = the phone"
try:
    urllib.request.urlopen(base + "/cache/snapshot?uri=x", timeout=5); raise AssertionError("must 404")
except urllib.error.HTTPError as e:
    assert e.code == 404
assert post(base, "/cache/download", {"uri": CTX})[0] == 404
p.terminate(); p.wait(5)
print("4. controls 1:1, pending cleared, listing shape, remote origin, cache 404 OK")

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
p.terminate(); out = p.communicate(timeout=5)[0]
assert "restarting in 0.5s" in out, out[-500:]
print("6. a crash -> offline -> bounded-backoff restart OK")

# 7. AM-16: the bind check. A fake pw-dump on PATH shows the soloist
#    stream linked (a) to the pinned node -> bound, state ok; (b) to
#    another sink -> on the first playing event the child is paused,
#    killed, and the state is audio-unbound (fail closed)
PWD_FILE = os.path.join(TMP, "pw-dump.json")
fake_bin = os.path.join(TMP, "fakebin"); os.makedirs(fake_bin, exist_ok=True)
open(os.path.join(fake_bin, "pw-dump"), "w").write(f"#!/bin/sh\ncat {PWD_FILE}\n")
os.chmod(os.path.join(fake_bin, "pw-dump"), 0o755)


def graph(linked_to):
    return [{"id": 1, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "vibb_bench_node", "media.class": "Audio/Sink"}}},
            {"id": 2, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "alsa_output.hdmi", "media.class": "Audio/Sink"}}},
            {"id": 9, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "spotify", "application.name": "Spotify", "media.class": "Stream/Output/Audio"}}},  # the real shape (field 2026-09-05): nothing says "soloist"
            {"id": 20, "type": "PipeWire:Interface:Link", "info": {"output-node-id": 9, "input-node-id": linked_to}}]


os.environ["PATH"] = fake_bin + ":" + os.environ["PATH"]
FAKE.status, FAKE.context = "idle", None          # the shared fake: a fresh session
open(PWD_FILE, "w").write(json.dumps(graph(1)))
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
open(PWD_FILE, "w").write(json.dumps(graph(2)))            # linked to the HDMI sink instead
FAKE.status, FAKE.context = "idle", None
p, base, data = start_sidecar()
wait_state(base, "ok")
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

print("\nall soloist_sidecar checks passed")
