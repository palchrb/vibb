#!/usr/bin/env python3
"""The fake Soloist + fake child + helpers shared by tests/soloist_sidecar.py
and tests/soloist_warm.py (listed in run_all's FIXTURES: not a test itself).

FakeSoloist is an RFC 6455 server scripted per command from the contract. For
4d it also plays the audio CACHE: on `play`/`skip_next` a writer thread grows
`<cache_dir>/cache/<2 hex>/<40 hex>.file` for the current track in 128 KiB
blocks at the scenario's rate (`fetch_mode`), leaves the next item's one-block
prefetch beside it, and stops on pause/skip — the shapes measured on the Zero
(AM-71/81). `cached_uris` are tracks the cache already holds (no writes).
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

__all__ = ["C", "TMP", "GUID", "CTX", "TRACKS", "PREV_CAP", "FakeSoloist", "FAKE", "BIN",
           "free_port", "start_sidecar", "get", "post", "wait_state", "graph", "install_pw_dump",
           "track_hash", "os", "sys", "json", "time", "subprocess", "urllib"]

TMP = tempfile.mkdtemp()
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CTX = "spotify:playlist:p"
TRACKS = [f"spotify:track:t{i}" for i in range(6)]
PREV_CAP = 2   # Soloist keeps only the last N played in get_queue's `previous` (10 on the box)


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
        self.is_active = True
        self.login_delay_s = 0.6         # the Zero: 'restoring session' -> 'logged in' ~1 s
        self._conn_at = 0.0
        # 4d: the audio cache the sidecar watches
        self.cache_dir = None            # set by start_sidecar (CACHE_DIRECTORY)
        self.fetch_mode = "fast"         # fast | slow | stall | none
        self.window = None               # upcoming rows per get_queue (the Zero: 10; None = all)
        self.tail_b = 0                  # the Zero: a ~17 KB tail lands ~3 s after the whole file
        self.tail_delay_s = 1.0          # ... whatever the player does meanwhile (skip included)
        self.cached_uris = set()         # already in the cache: nothing is written
        self.fetch_log = []              # (uri, bytes) per completed fetch
        self._writer = None
        self._writer_stop = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    # ----- the cache writer (AM-71/81 shapes) -----
    def _cache_path(self, uri, tag=""):
        h = hashlib.sha1((uri + tag).encode()).hexdigest()
        d = os.path.join(self.cache_dir, "cache", h[:2])
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, h + ".file")

    def _start_fetch(self, uri, duration_ms):
        self._stop_fetch()
        if not self.cache_dir or self.fetch_mode == "none" or uri in self.cached_uris:
            return
        stop = threading.Event()
        self._writer_stop = stop
        total = int(duration_ms / 1000 * 160 * 125)      # 160 kbps, the Zero's rate
        block = 131072

        def run():
            # the next item's prefetch: ONE block + 96 B header, never grows —
            # and when that item plays later, its file GROWS from there (the
            # Zero: same content-addressed file, first block then the rest)
            try:
                nxt = TRACKS[(TRACKS.index(uri) + 1) % len(TRACKS)] if uri in TRACKS else uri + "#next"
                np = self._cache_path(nxt)
                if not os.path.exists(np):
                    with open(np, "wb") as f:
                        f.write(b"p" * 131168)
            except (OSError, ValueError):
                pass
            path = self._cache_path(uri)
            written = os.path.getsize(path) if os.path.exists(path) else 0
            appended = 0
            with open(path, "ab") as f:
                while written < total and not stop.is_set():
                    if self.fetch_mode == "stall" and appended >= block:
                        stop.wait(60); break
                    n = min(block, total - written)
                    f.write(b"a" * n); f.flush(); written += n; appended += n
                    stop.wait(0.05 if self.fetch_mode == "fast" else 0.7)
            if written >= total:
                self.cached_uris.add(uri)
                self.fetch_log.append((uri, written))
                if self.tail_b:
                    def tail():
                        time.sleep(self.tail_delay_s)
                        try:
                            with open(path, "ab") as tf:
                                tf.write(b"t" * self.tail_b)
                        except OSError:
                            pass
                    threading.Thread(target=tail, daemon=True).start()
        self._writer = threading.Thread(target=run, daemon=True)
        self._writer.start()

    def _stop_fetch(self):
        self._writer_stop.set()

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
        self._conn_at = time.monotonic()
        # a new socket is a NEW child: its playback starts idle (the session is
        # restored, the playback is not) and the old child's fetch died with it
        self._stop_fetch()
        self.status, self.context, self.idx = "idle", None, 0
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
            if self.logged_in and time.monotonic() - self._conn_at < self.login_delay_s:
                # not yet: the session is still being restored; the real
                # child then pushes auth_state on its own when it is
                self.send({"type": "auth_state", "logged_in": False, "is_active": False, "device_name": None})
                def later():
                    time.sleep(self.login_delay_s)
                    try:
                        self.send({"type": "auth_state", "logged_in": self.logged_in, "is_active": self.is_active, "device_name": "FakeBox"})
                    except OSError:
                        pass
                threading.Thread(target=later, daemon=True).start()
                return
            self.send({"type": "auth_state", "logged_in": self.logged_in, "is_active": self.is_active, "device_name": "FakeBox"})
            return
        if cmd == "get_state":
            self.send(self.state()); return
        if cmd == "get_queue":
            # a QUERY: no command_result, the answer is the queue_changed event
            # (Soloist docs; confirmed on the Zero 2026-09-05)
            if getattr(self, "mute_queue", False):
                return                               # a Soloist that does not answer
            self._answer_queue(limit=msg.get("limit"))
            return
        self.send({"type": "command_result", "command": cmd})
        if cmd == "play":
            if msg.get("uri"):
                self.context, self.idx = msg["uri"], 0
                self.send({"type": "context_changed", "context": {"uri": self.context, "entity_type": "playlist",
                                                                   "decorations": {"identity": {"name": "P"}}}})
                self.send({"type": "track_changed", "item": self.item(0)})
            self.status = "playing"; self.send({"type": "playback_changed", "status": "playing"})
            self._start_fetch(TRACKS[self.idx], 180000)
        elif cmd == "pause":
            self.status = "paused"; self.send({"type": "playback_changed", "status": "paused"})
            self._stop_fetch()
        elif cmd in ("skip_next", "skip_prev"):
            self.idx = min(len(TRACKS) - 1, self.idx + 1) if cmd == "skip_next" else max(0, self.idx - 1)
            self.send({"type": "track_changed", "item": self.item(self.idx)})
            if self.status == "playing":
                self._start_fetch(TRACKS[self.idx], 180000)
        elif cmd == "seek":
            self.send({"type": "position_sync", "position": {"position_ms": msg["position_ms"], "timestamp_ms": time.time() * 1000, "speed": 1.0}})
        elif cmd == "set_volume":
            self.volume = msg["volume"]; self.send({"type": "volume_changed", "volume": self.volume})
        elif cmd == "set_shuffle":
            self.send({"type": "options_changed", "options": {"shuffle": msg["enabled"], "repeat": "off", "playback_speed": 1.0}})

    def _answer_queue(self, limit=None):
        # Soloist's `previous` is a history stack, most recent first
        # (PLAN-soloistd: "reversed previous + current + upcoming")
        prev = [{"uid": f"u{i}", "source": "context", "item": self.item(i)} for i in reversed(range(self.idx))]
        prev = prev[:PREV_CAP]   # the box caps the history at 10 (AM-59); 2 here, same shape
        upc = [{"uid": f"u{i}", "source": "context", "item": self.item(i)} for i in range(self.idx + 1, len(TRACKS))]
        upc.append({"uid": "ux", "source": "autoplay", "item": C.sample_entity("spotify:track:radio", "R", ["X"], "Y", 1000)})
        if limit:                # get_queue limit=N: that many upcoming (0/None = all)
            upc = upc[:int(limit)]
        elif self.window:        # the Zero (2026-09-06): a start shows 10 upcoming, no more
            upc = upc[:self.window]
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
n = len([x for x in os.listdir(d) if x.startswith("argv-")]) + 1
open(os.path.join(d, f"argv-{n:03d}.json"), "w").write(repr(args))   # one per start (4d: node per start)
open(os.path.join(d, "ws.addr"), "w").write("127.0.0.1")
open(os.path.join(d, "ws.port"), "w").write(os.environ["FAKE_WS_PORT"])
# the Zero's banner shape (2026-09-06): the child's own log stamp first
print(time.strftime("%Y-%m-%d %H:%M:%S") + ".123: soloist 1.3.8.13 build 1788609705 (20260905) (g5c3a2053ac) (linux/aarch64)", flush=True)
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


def start_sidecar(key="k", mode="run", data=None, pcm="vibb_bench_node", cache=None):
    data = data or tempfile.mkdtemp()
    try:                                   # a "follow" graph tracks THIS sidecar's children
        doc = json.load(open(PWD_FILE))
        if isinstance(doc, dict):
            doc["follow"] = data
            open(PWD_FILE, "w").write(json.dumps(doc))
    except (OSError, ValueError):
        pass
    state = tempfile.mkdtemp()
    with open(os.path.join(state, "output.json"), "w") as f:
        json.dump({"output": "local", "pcm": pcm}, f)
    port = free_port()
    cache = cache or tempfile.mkdtemp()
    FAKE.cache_dir = cache
    FAKE.cached_uris = set()
    FAKE.fetch_log = []
    env = dict(os.environ, VIBB_SOLOISTD_PORT=str(port), VIBB_SOLOIST_BIN=BIN,
               STATE_DIRECTORY=data, CACHE_DIRECTORY=cache, VIBB_STATE=state,
               VIBB_RUN=tempfile.mkdtemp(), VIBB_SETTINGS=os.path.join(state, "se.json"),
               VIBB_BT_FILE=os.path.join(state, "bt"), VIBB_DEVICE_NAME="Vibb (test)",
               FAKE_WS_PORT=str(FAKE.port), FAKE_MODE=mode, VIBB_SOLOIST_BACKOFF_S="0.5,0.5",
               # 4d timing, scaled for a test: quiet 0.5 s, stall 1.5 s, settle 1 s
               VIBB_WARM_TICK_S="0.1", VIBB_WARM_QUIET_S="0.5", VIBB_WARM_STALL_S="1.5",
               VIBB_WARM_SETTLE_S="1.0", VIBB_WARM_CAP_MIN_S="4",
               VIBB_SOLOIST_RESTORE_GRACE_S="1.5")   # the fake logs in after 0.6 s
    if key:
        env["SOLOIST_API_KEY"] = key
    else:
        env.pop("SOLOIST_API_KEY", None)
    # the sidecar's log goes to a FILE: a PIPE nobody drains fills at 64 KB and
    # blocks the sidecar mid-pass (the warm logs per track)
    logf = open(os.path.join(state, "soloistd.log"), "w")
    p = subprocess.Popen([sys.executable, os.path.join(REPO, "pi", "soloistd.py")], env=env,
                         stdout=logf, stderr=subprocess.STDOUT, text=True)
    p.logpath = logf.name
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/soloist/health", timeout=1); break
        except OSError:
            time.sleep(0.1)
    return p, base, data


def track_hash(uri):
    return hashlib.sha1(uri.encode()).hexdigest()


# --- a fake pw-dump on PATH: the graph the bind check and the pass read ---
PWD_FILE = os.path.join(TMP, "pw-dump.json")
_fake_bin = os.path.join(TMP, "fakebin"); os.makedirs(_fake_bin, exist_ok=True)
# a static graph (a JSON list), or {"follow": <data dir>, "template": [...]}: the
# stream then sits on whichever node the NEWEST child was started with (-d), the
# way it does on a box — a pass's restart on vibb_null moves it, the restore moves it back
open(os.path.join(_fake_bin, "pw-dump"), "w").write(f'''#!/usr/bin/env python3
import json, os
doc = json.load(open({PWD_FILE!r}))
if isinstance(doc, dict):
    d = doc["follow"]
    names = sorted(x for x in os.listdir(d) if x.startswith("argv-")) if os.path.isdir(d) else []
    node = None
    if names:
        a = eval(open(os.path.join(d, names[-1])).read())
        node = a[a.index("-d") + 1] if "-d" in a else None
    ids = {{o["info"]["props"].get("node.name"): o["id"] for o in doc["template"] if o["type"].endswith("Node")}}
    out = []
    for o in doc["template"]:
        if o["type"].endswith("Link"):
            if node not in ids:
                continue                                   # no child yet: no stream, no link
            o = dict(o, info=dict(o["info"], **{{"input-node-id": ids[node]}}))
        elif o["id"] == 9 and node not in ids:
            continue
        out.append(o)
    doc = out
print(json.dumps(doc))
''')
os.chmod(os.path.join(_fake_bin, "pw-dump"), 0o755)


def graph(linked_to, null=False):
    """Sinks 1 (the bench node) and 2 (HDMI); 3 = vibb_null when `null`; the
    soloist stream (9) linked to `linked_to`."""
    nodes = [{"id": 1, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "vibb_bench_node", "media.class": "Audio/Sink"}}},
             {"id": 2, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "alsa_output.hdmi", "media.class": "Audio/Sink"}}},
             {"id": 9, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "spotify", "application.name": "Spotify", "media.class": "Stream/Output/Audio"}}},  # the real shape (field 2026-09-05): nothing says "soloist"
             {"id": 20, "type": "PipeWire:Interface:Link", "info": {"output-node-id": 9, "input-node-id": linked_to}}]
    if null:
        nodes.insert(2, {"id": 3, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "vibb_null", "media.class": "Audio/Sink"}}})
    return nodes


def install_pw_dump(linked_to, null=False):
    """`linked_to`: a sink id for a static graph, or "follow" — the stream on
    the newest child's node (start_sidecar fills in the data dir)."""
    os.environ["PATH"] = _fake_bin + ":" + os.environ["PATH"]
    if linked_to == "follow":
        open(PWD_FILE, "w").write(json.dumps({"follow": "", "template": graph(0, null)}))
    else:
        open(PWD_FILE, "w").write(json.dumps(graph(linked_to, null)))


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


