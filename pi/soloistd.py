#!/usr/bin/env python3
"""vibb-soloistd — the Spotify engine sidecar for Spotify's official
headless client "Soloist" (PLAN-soloistd.md P1, PLAN-pipewire-soloist.md
Phase 3). Runs as $RUN_USER, binds 127.0.0.1 only, stdlib only.

It SPEAKS THE GO-LIBRESPOT REST DIALECT (tests/soloist_contract.py):
same paths, same field names. The daemon, player.py and every other
caller run unmodified — the engine is an install-time toggle that
points VIBB_GO_API/VIBB_GO_UNIT here. Inside: one hand-rolled RFC6455
client on Soloist's local WebSocket (loopback JSON), an event-fed
status mirror (position interpolated from position_sync — push beats
polling), single-in-flight command correlation under one lock.

It SUPERVISES the soloist child (never a sibling unit): exit code 10 =
build expired LATCHES, persisted in the state dir, so neither this
process nor a systemd Restart= can brick-loop an expired binary; the
box shows a clear "Spotify trenger oppdatering" instead (the bedtime
rule). No API key -> the clear needs-key state, no child at all.

The states /soloist/health reports (AM-48): starting | ok | needs-key |
needs-pair | expired | offline | audio-unbound. /status carries them as
spotify_state for the daemon to fast-fail on.

The resume walk (kill criterion 1, bench-proven): /player/play with
skip_to_uri -> play context, pause, skip_next until item.uri matches,
seek, play — under the volume shroud (set_volume 0, restored after),
because the pause does not always win the race with the first frames.
"""

import base64
import hashlib
import json
import os
import secrets
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_here = os.path.dirname(os.path.abspath(__file__))
for _p in (_here, "/usr/local/lib/vibb-py"):
    if os.path.isdir(os.path.join(_p, "vibb")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
        break

from vibb.paths import STATE_DIR, read_settings  # noqa: E402

PORT = int(os.environ.get("VIBB_SOLOISTD_PORT", "3688"))
SOLOIST = os.environ.get("VIBB_SOLOIST_BIN", "/usr/local/bin/soloist")
DATA_DIR = os.environ.get("STATE_DIRECTORY") or os.path.join(STATE_DIR, "soloist")
CACHE_DIR = os.environ.get("CACHE_DIRECTORY") or os.path.join(STATE_DIR, "soloist-cache")
DEVICE_NAME = os.environ.get("VIBB_DEVICE_NAME") or f"Vibb ({socket.gethostname()})"
LATCH_FILE = os.path.join(DATA_DIR, "build-expired.latch")
OUT_FILE = os.path.join(STATE_DIR, "output.json")
MAC_FILE = os.environ.get("VIBB_BT_FILE", "/etc/vibb/bt-headset")
BOX_ORIGIN = "go-librespot"          # the dialect's "box started this" value
RUN_DIR = os.environ.get("VIBB_RUN", "/run/vibb" if os.access("/run", os.W_OK) else "/tmp")
IDLE_RESTART_S = float(os.environ.get("VIBB_SOLOIST_IDLE_RESTART_S", "600"))  # paused this long = idle
PAIR_MAX_S = float(os.environ.get("VIBB_SOLOIST_PAIR_MAX_S", "180"))
WALK_MAX_SKIPS = 300                 # a 500-item context is a Web-API job (P2)
WALK_MAX_S = 25.0
CMD_TIMEOUT_S = 8.0
RESTART_BACKOFF_S = tuple(float(x) for x in os.environ.get(
    "VIBB_SOLOIST_BACKOFF_S", "5,10,20,40,60").split(","))
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def log(msg):
    print(f"soloistd: {msg}", file=sys.stderr, flush=True)


# --- minimal RFC6455 client (text frames, client-masked, loopback) ----------

class WS:
    def __init__(self, host, port, timeout=10.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.sock.sendall((f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\n"
                           "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("handshake: connection closed")
            resp += chunk
        head, _, rest = resp.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"handshake refused: {head[:120]!r}")
        want = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest())
        if want not in head:
            raise ConnectionError("handshake: bad Sec-WebSocket-Accept")
        self.buf = rest
        self.wlock = threading.Lock()

    def send_json(self, obj):
        data = json.dumps(obj).encode()
        mask = secrets.token_bytes(4)
        n = len(data)
        if n < 126:
            hdr = struct.pack("!BB", 0x81, 0x80 | n)
        elif n < 65536:
            hdr = struct.pack("!BBH", 0x81, 0x80 | 126, n)
        else:
            hdr = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        with self.wlock:
            self.sock.sendall(hdr + mask + masked)

    def _read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv_json(self, timeout=10.0):
        """Next TEXT message as JSON (fragments reassembled, pings
        answered, binary skipped). Raises socket.timeout / ConnectionError."""
        self.sock.settimeout(timeout)
        message = b""
        while True:
            b1, b2 = self._read_exact(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            ln = b2 & 0x7F
            if ln == 126:
                (ln,) = struct.unpack("!H", self._read_exact(2))
            elif ln == 127:
                (ln,) = struct.unpack("!Q", self._read_exact(8))
            payload = self._read_exact(ln)
            if opcode == 0x9:
                mask = secrets.token_bytes(4)
                pong = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
                with self.wlock:
                    self.sock.sendall(struct.pack("!BB", 0x8A, 0x80 | len(payload)) + mask + pong)
                continue
            if opcode == 0x8:
                raise ConnectionError("server closed")
            if opcode in (0x1, 0x0):
                message += payload
                if fin:
                    try:
                        out = json.loads(message.decode())
                    except ValueError:
                        message = b""
                        continue
                    return out
            # binary / pong: skip

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# --- entity helpers (the docs' decorations shape) -------------------------------

def _deco(ent):
    return (ent or {}).get("decorations") or {}


_COVER_RANK = {"xlarge": 4, "large": 3, "medium": 2, "small": 1, "xsmall": 0}


def _largest_cover(cover):
    """Soloist's cover[] lists several sizes; cover[0] gave the PWA a thumbnail
    where go-librespot's art was full size (owner 2026-09-05). Prefer the
    entry with the largest pixel width, then the best 'size' label, then
    the last entry (the list reads small -> large in the samples seen)."""
    best, best_key = None, None
    for i, c in enumerate(cover):
        url = c.get("url")
        if not url:
            continue
        w = c.get("width") or c.get("height") or 0
        try:
            w = int(w)
        except (TypeError, ValueError):
            w = 0
        key = (w, _COVER_RANK.get(str(c.get("size") or "").lower(), -1), i)
        if best_key is None or key > best_key:
            best, best_key = url, key
    return best


def entity_to_track(ent, position_ms=0):
    """A Soloist Entity -> the dialect's track dict (contract TRACK_FIELDS)."""
    if not ent or not ent.get("uri"):
        return None
    d = _deco(ent)
    cover = [c for c in ((d.get("visual_identity") or {}).get("cover") or []) if isinstance(c, dict)]
    cover_url = _largest_cover(cover)
    parent = ((d.get("parent") or {}).get("entity") or {})
    return {"uri": ent["uri"],
            "name": (d.get("identity") or {}).get("name"),
            "artist_names": [((c.get("entity") or {}).get("decorations") or {})
                             .get("identity", {}).get("name")
                             for c in (d.get("creators") or [])],
            "album_cover_url": cover_url,
            "album_name": (_deco(parent).get("identity") or {}).get("name"),
            "position": int(position_ms),
            "duration": int((d.get("playback") or {}).get("duration_ms") or 0)}


# --- the engine: child supervision + WS mirror + the dialect --------------------

class Engine:
    def __init__(self):
        self.lock = threading.Lock()          # one command in flight
        self.mirror_lock = threading.Lock()
        self.child = None
        self.ws = None
        self.state = "starting"
        self.auth = {"logged_in": False, "is_active": False, "device_name": None}
        self.pb = {"status": "idle", "item": None, "context": None,
                   "position": {"position_ms": 0, "timestamp_ms": 0.0, "speed": 1.0},
                   "volume": None, "options": {"shuffle": False}}
        self.pending_uri = None
        self.box_context = None                # last context the BOX started
        self.days_left = None
        self.build = None
        self.node = None
        self.last_lines = []
        self.restarts = 0
        self.bad_key = False                   # the child said the key is no good
        self.pending_restart = None            # why a restart waits for idle (AM-52)
        self.paused_since = None
        self.pairing = False
        self.bound = None                      # AM-16: None unknown, True/False
        self._binding = False                  # one authoritative check in flight
        self.stop = threading.Event()
        # every WS event lands in a bounded log with a sequence number;
        # waiters scan entries NEWER than the point they started waiting.
        # (A single last-event slot lost the command_result whenever the
        # state-change event that follows it arrived first — always.)
        self.events = threading.Condition()
        self.evseq = 0
        self.evlog = []

    # ----- state -----
    def set_state(self, s):
        if s != self.state:
            log(f"state {self.state} -> {s}")
            self.state = s

    def health(self):
        return {"state": self.state, "days_left": self.days_left, "build": self.build,
                "child": self.child.pid if self.child and self.child.poll() is None else None,
                "ws": self.ws is not None, "node": self.node, "bound": self.bound,
                "pending_restart": self.pending_restart, "pairing": self.pairing,
                "device_name": DEVICE_NAME, "warming": None}

    # ----- the child -----
    def _node_for(self, pcm):
        from vibb import audio
        if pcm == "vibb_local":
            return audio.find_local_sink()
        if pcm == "vibb_bt":
            try:
                mac = open(MAC_FILE).read().strip()
            except OSError:
                return None
            return audio.find_bt_sink(mac) if mac else None
        return pcm or None

    def current_pcm(self):
        try:
            with open(OUT_FILE) as f:
                return json.load(f).get("pcm") or "vibb_bt"
        except (OSError, ValueError):
            return "vibb_bt"

    def start_child(self):
        key = os.environ.get("SOLOIST_API_KEY", "")
        if not key:
            self.set_state("needs-key")
            return False
        if os.path.exists(LATCH_FILE):
            self.set_state("expired")
            return False
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(CACHE_DIR, exist_ok=True)
        for f in ("ws.addr", "ws.port"):
            try:
                os.remove(os.path.join(DATA_DIR, f))
            except OSError:
                pass
        self.node = self._node_for(self.current_pcm())
        mb = int(read_settings().get("spotify_cache_gb", 20)) * 1024
        argv = [SOLOIST, "-n", DEVICE_NAME, "-k", key, "-D", DATA_DIR, "-C", CACHE_DIR,
                "-z", str(max(100, mb)), "-w", "127.0.0.1:0"]
        if self.node:
            argv += ["-d", self.node]
        else:
            log("no sink node for the current output yet — starting unbound (audio-unbound)")
        try:
            self.child = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, text=True,
                                          start_new_session=True)
        except OSError as e:
            log(f"cannot start {SOLOIST}: {e!r}")
            self.set_state("offline")
            return False
        threading.Thread(target=self._read_child, args=(self.child,), daemon=True).start()
        threading.Thread(target=self._wait_child, args=(self.child,), daemon=True).start()
        threading.Thread(target=self._ws_loop, args=(self.child,), daemon=True).start()
        threading.Thread(target=self._bind_check, args=(False,), daemon=True).start()
        self.set_state("starting")
        return True

    def _read_child(self, child):
        for line in child.stdout:
            line = line.rstrip("\n")
            self.last_lines = (self.last_lines + [line])[-20:]
            low = line.lower()
            if "expires in" in low:
                for tok in low.replace("(", " ").split():
                    if tok.isdigit():
                        self.days_left = int(tok)
                        break
            if "build" in low and self.build is None and "soloist" in low:
                self.build = line.strip()
            # Which line means 'bad key' is NOT documented (AM-47: bench it
            # once with a mangled key); until then the widest honest net.
            if "api key" in low or "api-key" in low or "apikey" in low:
                if any(w in low for w in ("invalid", "unauthori", "forbidden",
                                          "rejected", "denied")):
                    self.bad_key = True

    def _wait_child(self, child):
        rc = child.wait()
        if child is not self.child or self.stop.is_set():
            return
        if rc == 10:
            # PLAN-soloistd: the build expired. LATCH, persisted — a restart
            # loop here or in systemd would brick the box; the screen says
            # "Spotify trenger oppdatering" and the updater (D1) clears it.
            try:
                with open(LATCH_FILE, "w") as f:
                    f.write(f"exit 10 at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            except OSError:
                pass
            self.set_state("expired")
            log("soloist exit 10: build expired — latched, not restarting")
            return
        if self.bad_key:
            # not a restart case: a new key arrives as a UNIT restart from
            # /soloist/configure, which starts a fresh sidecar
            self.set_state("bad-key")
            log("soloist rejected the API key — waiting for a new one")
            return
        self.set_state("offline")
        delay = RESTART_BACKOFF_S[min(self.restarts, len(RESTART_BACKOFF_S) - 1)]
        self.restarts += 1
        log(f"soloist exited rc={rc} — restarting in {delay}s "
            f"(last: {self.last_lines[-1] if self.last_lines else '-'})")
        time.sleep(delay)
        if not self.stop.is_set():
            self.start_child()

    def stop_child(self):
        child, self.child = self.child, None
        if child and child.poll() is None:
            try:
                child.terminate()
                child.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    child.kill()
                except OSError:
                    pass

    def restart_child(self, why):
        log(f"restarting soloist ({why})")
        self.stop_child()
        self.bound = None
        if self.state == "audio-unbound":
            self.state = "starting"
        self.start_child()

    # ----- the WebSocket mirror -----
    def _ws_loop(self, child):
        addr = port = None
        for _ in range(300):                      # ws.addr/ws.port appear after start
            if child is not self.child or child.poll() is not None:
                return
            try:
                addr = open(os.path.join(DATA_DIR, "ws.addr")).read().strip()
                port = int(open(os.path.join(DATA_DIR, "ws.port")).read().strip())
                break
            except (OSError, ValueError):
                time.sleep(0.2)
        if not addr:
            return
        while child is self.child and child.poll() is None and not self.stop.is_set():
            try:
                ws = WS(addr, port, timeout=10)
            except OSError as e:
                log(f"ws connect failed: {e!r}")
                time.sleep(1)
                continue
            self.ws = ws
            try:
                ws.send_json({"type": "command", "command": "get_auth_state"})
                ws.send_json({"type": "command", "command": "get_state"})
                while child is self.child and not self.stop.is_set():
                    try:
                        msg = ws.recv_json(timeout=30)
                    except socket.timeout:
                        continue
                    self._on_event(msg)
            except (OSError, ConnectionError) as e:
                log(f"ws dropped: {e!r}")
            finally:
                self.ws = None
                ws.close()
            time.sleep(0.5)

    def _on_event(self, msg):
        t = msg.get("type")
        with self.mirror_lock:
            if t == "auth_state":
                self.auth = {"logged_in": bool(msg.get("logged_in")),
                             "is_active": bool(msg.get("is_active")),
                             "device_name": msg.get("device_name")}
                self._derive_state()
            elif t == "playback_state":
                for k in ("status", "item", "context", "position", "volume", "options"):
                    if k in msg:
                        self.pb[k] = msg[k]
                if msg.get("item") and self.pending_uri == (msg["item"] or {}).get("uri"):
                    self.pending_uri = None
                self._derive_state()
                self._maybe_bind_check()
            elif t == "track_changed":
                self.pb["item"] = msg.get("item")
                self.pb["position"] = {"position_ms": 0, "timestamp_ms": time.time() * 1000,
                                       "speed": self.pb["position"].get("speed", 1.0)}
                if self.pending_uri == (msg.get("item") or {}).get("uri"):
                    self.pending_uri = None
            elif t == "playback_changed":
                self.pb["status"] = msg.get("status") or self.pb["status"]
                self.paused_since = time.monotonic() if self.pb["status"] == "paused" \
                    else (None if self.pb["status"] == "playing" else self.paused_since)
                self._maybe_bind_check()
            elif t == "position_sync":
                self.pb["position"] = msg.get("position") or self.pb["position"]
            elif t == "volume_changed":
                self.pb["volume"] = msg.get("volume")
            elif t == "context_changed":
                self.pb["context"] = msg.get("context")
            elif t == "options_changed":
                self.pb["options"] = msg.get("options") or self.pb["options"]
        with self.events:
            self.evseq += 1
            self.evlog.append((self.evseq, msg))
            del self.evlog[:-200]
            self.events.notify_all()

    def mark(self):
        with self.events:
            return self.evseq

    # ----- AM-52: the post-update restart, only when idle -----
    def is_idle(self):
        """No track loaded, or paused for IDLE_RESTART_S: the bookmarker has
        flushed and play()'s resume falls through to the bookmark."""
        with self.mirror_lock:
            st, item, since = self.pb.get("status"), self.pb.get("item"), self.paused_since
        if st in (None, "idle") or not item:
            return True
        return st == "paused" and since is not None and time.monotonic() - since >= IDLE_RESTART_S

    def _poweroff_imminent(self):
        try:
            return 0 <= time.time() - os.path.getmtime(os.path.join(RUN_DIR, "poweroff-imminent")) < 600
        except OSError:
            return False

    def updated(self):
        """The updater swapped the binary. A fresh build is a fresh 90 days:
        drop the exit-10 latch. Restart the child now if idle, at the next
        idle moment otherwise — and never on the way down."""
        try:
            os.remove(LATCH_FILE)
        except OSError:
            pass
        if self._poweroff_imminent():
            self.pending_restart = "updated (poweroff imminent — next boot)"
            return "next-boot"
        if self.is_idle():
            self.pending_restart = None
            self.restart_child("updated")
            return "restarted"
        self.pending_restart = "updated"
        return "deferred"

    def _idle_restart_watch(self):
        while not self.stop.is_set():
            time.sleep(30)
            if self.pending_restart and self.pending_restart == "updated" and self.is_idle() \
                    and not self._poweroff_imminent():
                self.pending_restart = None
                self.restart_child("updated, box idle")

    # ----- 5b: pairing -----
    def pair(self):
        """`soloist --pair`: the phone's Spotify app picks the box under
        Devices; the child stores the session in the data dir and exits.
        One owner of the data dir at a time: the normal child is stopped
        for the duration, then started again."""
        key = os.environ.get("SOLOIST_API_KEY", "")
        if not key:
            return "needs-key"
        if self.pairing:
            return "already"
        self.pairing = True
        threading.Thread(target=self._pair_run, args=(key,), daemon=True).start()
        return "pairing"

    def _pair_run(self, key):
        try:
            self.stop_child()
            self.set_state("pairing")
            r = subprocess.run([SOLOIST, "-n", DEVICE_NAME, "-k", key, "-D", DATA_DIR, "-p"],
                               capture_output=True, text=True, timeout=PAIR_MAX_S)
            log(f"pair exited rc={r.returncode}: {(r.stdout or r.stderr).strip()[-120:]}")
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"pair failed: {e!r}")
        finally:
            self.pairing = False
            self.state = "starting"
            self.start_child()

    def _maybe_bind_check(self):
        """AM-16: Soloist may create its stream lazily on the first play, so
        the AUTHORITATIVE check runs whenever audio is playing and the
        binding is not yet proven — one at a time. (Under mirror_lock.)"""
        if self.pb.get("status") == "playing" and self.bound is not True and not self._binding:
            self._binding = True
            threading.Thread(target=self._bind_check, args=(True,), daemon=True).start()

    def _derive_state(self):
        if self.state in ("expired", "needs-key", "bad-key"):
            return
        if not self.auth["logged_in"]:
            self.set_state("needs-pair")
        elif self.node is None or self.bound is False:
            self.set_state("audio-unbound")
        else:
            self.set_state("ok")

    def _bind_check(self, authoritative):
        """AM-16 / bench B9: is the soloist stream linked to the sink we
        pinned, and nothing else? Informational at start (the stream may
        not exist yet); AUTHORITATIVE within 2 s of the first playing
        event — a mis-bound child is paused and killed, and the state is
        audio-unbound (fail closed, never 'some other sink')."""
        from vibb import audio
        try:
            self._bind_check_body(audio, authoritative)
        finally:
            if authoritative:
                self._binding = False

    def _bind_check_body(self, audio, authoritative):
        time.sleep(2.0 if authoritative else 0.5)
        dump = audio.pw_dump()
        if not dump:
            return                                  # no graph to ask: unknown
        streams = [obj for obj in dump
                   if obj.get("type") == "PipeWire:Interface:Node"
                   and ((obj.get("info") or {}).get("props") or {}).get("media.class") == "Stream/Output/Audio"
                   and "soloist" in json.dumps((obj.get("info") or {}).get("props") or {}).lower()]
        if not streams:
            self.bound = None if not authoritative else self.bound
            log("bind check: no soloist stream node yet" + ("" if authoritative else " (lazy?)"))
            return
        sinks = set()
        for s in streams:
            sinks |= audio._linked_sinks(dump, s["id"])
        ok = bool(self.node) and sinks == {self.node}
        self.bound = ok
        if ok:
            log(f"bind check: soloist stream on {self.node} OK")
            with self.mirror_lock:
                self._derive_state()
            return
        log(f"bind check FAILED: stream linked to {sorted(sinks) or 'nothing'}, wanted {self.node}")
        if authoritative:
            try:
                self.cmd("pause")
            except OSError:
                pass
            self.stop_child()
            self.set_state("audio-unbound")

    def wait_event(self, etype, timeout, since=None, pred=None):
        """The first event of type `etype` logged AFTER `since` (a mark()),
        or None at the timeout."""
        end = time.monotonic() + timeout
        with self.events:
            seen = self.evseq if since is None else since
            while True:
                for seq, m in self.evlog:
                    if seq > seen and m.get("type") == etype and (pred is None or pred(m)):
                        return m
                rem = end - time.monotonic()
                if rem <= 0:
                    return None
                self.events.wait(rem)

    # ----- commands -----
    def cmd(self, command, **fields):
        """Send one command; the reply is the command_result/error frame
        (async: state changes arrive as events). Raises OSError when there
        is no live WebSocket — the dialect's 'unreachable'."""
        ws = self.ws
        if ws is None:
            raise OSError("soloist websocket not connected")
        since = self.mark()
        ws.send_json({"type": "command", "command": command, **fields})
        end = time.monotonic() + CMD_TIMEOUT_S
        while True:
            m = self.wait_event("command_result", 0.2, since,
                                lambda x: x.get("command") in (command, None))
            if m:
                return True, m
            e = self.wait_event("error", 0.0, since)
            if e:
                return False, e
            if time.monotonic() >= end:
                return False, {"type": "error", "message": "timeout"}

    # ----- the dialect -----
    def status(self):
        with self.mirror_lock:
            pb = dict(self.pb)
            auth = dict(self.auth)
            pending = self.pending_uri
        pos = pb.get("position") or {}
        position_ms = pos.get("position_ms") or 0
        if pb.get("status") == "playing" and pos.get("timestamp_ms"):
            position_ms += (time.time() * 1000 - pos["timestamp_ms"]) * (pos.get("speed") or 1.0)
        track = entity_to_track(pb.get("item"), position_ms)
        ctx = (pb.get("context") or {}).get("uri")
        origin = BOX_ORIGIN if (ctx and ctx == self.box_context) or not track else "remote"
        st = {"username": auth["device_name"] or DEVICE_NAME if auth["logged_in"] else None,
              "paused": pb.get("status") == "paused",
              "stopped": pb.get("status") == "idle" or track is None,
              "volume": pb.get("volume") if pb.get("volume") is not None else 0,
              "volume_steps": 100,
              "play_origin": origin,
              "shuffle_context": bool((pb.get("options") or {}).get("shuffle")),
              "pending_track_uri": pending,
              "track": track,
              "spotify_state": self.state}
        if not auth["logged_in"]:
            st["username"] = None
        return st

    def play(self, body):
        uri = body.get("uri")
        target = body.get("skip_to_uri")
        position = int(body.get("position") or 0)
        if not uri:                                  # bare resume
            return self.cmd("play")
        self.box_context = uri
        with self.mirror_lock:            # optimistic: context_changed confirms it
            self.pb["context"] = {"uri": uri}
        shroud = None
        if target or position:
            with self.mirror_lock:
                shroud = self.pb.get("volume")
            self.cmd("set_volume", volume=0)
        try:
            self.pending_uri = target or None
            since = self.mark()
            ok, r = self.cmd("play", uri=uri)
            if not ok:
                return ok, r
            self.wait_event("track_changed", 15, since)
            if target:
                self.cmd("pause")
                t0 = time.monotonic()
                for _ in range(WALK_MAX_SKIPS):
                    with self.mirror_lock:
                        cur = ((self.pb.get("item") or {}).get("uri"))
                    if cur == target:
                        break
                    if time.monotonic() - t0 > WALK_MAX_S:
                        log(f"resume walk gave up after {WALK_MAX_S:.0f}s (at {cur})")
                        break
                    since = self.mark()
                    ok, r = self.cmd("skip_next")
                    if not ok:
                        break
                    self.wait_event("track_changed", 10, since)
            if position:
                self.cmd("seek", position_ms=position)
            if target:
                ok, r = self.cmd("play")
            return ok, r
        finally:
            if shroud is not None:
                self.cmd("set_volume", volume=int(shroud))

    def listing(self, uri):
        """/context/tracks for the ACTIVE context from get_queue (the 80
        window). Another context: ready but empty — the daemon's
        'spotify-listing-unavailable' path; the full picker is P2."""
        with self.mirror_lock:
            active = (self.pb.get("context") or {}).get("uri")
            cur = self.pb.get("item")
        if not active or uri != active:
            return {"ready": True, "cached": False, "length": 0, "tracks": []}
        since = self.mark()
        self.cmd("get_queue", limit=0)
        q = self.wait_event("queue_changed", 5, since) or {}
        # `previous` is a history stack, most recent first — reversed it is
        # chronological (PLAN-soloistd kill 2; confirm on the bench, B9)
        rows = [e.get("item") for e in reversed(q.get("previous") or [])
                if e.get("source") == "context"]
        if cur:
            rows.append(cur)
        rows += [e.get("item") for e in (q.get("upcoming") or []) if e.get("source") == "context"]
        tracks = []
        for ent in rows:
            tr = entity_to_track(ent)
            if tr:
                tracks.append({"uri": tr["uri"], "track": tr})
        return {"ready": True, "cached": True, "length": len(tracks), "tracks": tracks}


ENGINE = Engine()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        out = json.dumps(obj).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        except OSError:
            pass

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/status":
            self._send(200, ENGINE.status())
        elif path == "/soloist/health":
            self._send(200, ENGINE.health())
        elif path == "/context/tracks":
            import urllib.parse
            q = urllib.parse.parse_qs(self.path.partition("?")[2])
            uri = (q.get("uri") or [""])[0]
            try:
                self._send(200, ENGINE.listing(uri))
            except OSError as e:
                self._send(503, {"error": "engine-unreachable", "detail": str(e)})
        elif path == "/cache/snapshot":
            self._send(404, {"error": "not-supported"})   # library.py fails open
        else:
            self._send(404, {"error": "not-found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            self._send(400, {"error": "bad-json"})
            return
        path = self.path
        try:
            with ENGINE.lock:
                if path == "/player/play":
                    ok, r = ENGINE.play(body)
                elif path == "/player/pause":
                    ok, r = ENGINE.cmd("pause")
                elif path == "/player/resume":
                    ok, r = ENGINE.cmd("play")
                elif path == "/player/playpause":
                    with ENGINE.mirror_lock:
                        playing = ENGINE.pb.get("status") == "playing"
                    ok, r = ENGINE.cmd("pause" if playing else "play")
                elif path in ("/player/next", "/player/prev"):
                    with ENGINE.mirror_lock:
                        ENGINE.pending_uri = None
                    ok, r = ENGINE.cmd("skip_next" if path.endswith("next") else "skip_prev")
                elif path == "/player/seek":
                    ok, r = ENGINE.cmd("seek", position_ms=int(body.get("position") or 0))
                elif path == "/player/volume":
                    ok, r = ENGINE.cmd("set_volume",
                                       volume=max(0, min(100, int(body.get("volume") or 0))))
                elif path == "/player/shuffle_context":
                    ok, r = ENGINE.cmd("set_shuffle", enabled=bool(body.get("shuffle_context")))
                elif path == "/player/output":
                    # no live reopen in Soloist: rebind by restarting the child
                    # on the node the pcm name resolves to (plan §I)
                    ENGINE.node = ENGINE._node_for(body.get("device") or ENGINE.current_pcm())
                    ENGINE.restart_child(f"output -> {body.get('device')}")
                    self._send(200, {"ok": True, "node": ENGINE.node})
                    return
                elif path == "/soloist/updated":
                    self._send(200, {"result": ENGINE.updated(),
                                     "pending_restart": ENGINE.pending_restart})
                    return
                elif path == "/soloist/pair":
                    r = ENGINE.pair()
                    self._send(409 if r == "needs-key" else 202, {"result": r})
                    return
                elif path == "/cache/download":
                    self._send(404, {"error": "not-supported"})   # warming: D3, step 4d
                    return
                else:
                    self._send(404, {"error": "not-found"})
                    return
        except OSError as e:
            self._send(503, {"error": "engine-unreachable", "detail": str(e),
                             "spotify_state": ENGINE.state})
            return
        self._send(200 if ok else 500, {"ok": ok, "result": r})


def _on_term(*_a):
    ENGINE.stop.set()
    ENGINE.stop_child()
    os._exit(0)


def main():
    signal.signal(signal.SIGTERM, _on_term)
    ENGINE.start_child()
    threading.Thread(target=ENGINE._idle_restart_watch, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    assert srv.server_address[0] == "127.0.0.1"
    log(f"up on 127.0.0.1:{PORT} state={ENGINE.state} node={ENGINE.node}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
