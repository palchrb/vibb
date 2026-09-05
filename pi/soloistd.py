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
# The daemon/idle markers (poweroff-imminent, PAGING) live under the ROOT
# run dir, /run. As $RUN_USER "/run is writable?" is false, so the old
# default fell to /tmp and _poweroff_imminent() never fired on a box
# (AM-69). Read-only use here; the unit sets VIBB_RUN=/run explicitly.
RUN_DIR = os.environ.get("VIBB_RUN", "/run")
IDLE_RESTART_S = float(os.environ.get("VIBB_SOLOIST_IDLE_RESTART_S", "600"))  # paused this long = idle
PAIR_MAX_S = float(os.environ.get("VIBB_SOLOIST_PAIR_MAX_S", "180"))
WALK_MAX_SKIPS = 300                 # a 500-item context is a Web-API job (P2)
WALK_MAX_S = 25.0
# Below the daemon's 5 s per request (vibb.spotify.go): a command that Soloist
# does not ack in time must come back as an error, never as a socket
# timeout the daemon reads as "engine down" (Zero 2026-09-05: the Sonos
# hand-off timed out waiting for the listing)
CMD_TIMEOUT_S = 4.0
LISTING_WAIT_S = 2.0   # for queue_changed after get_queue (Zero: 40 ms); below the daemon's 5 s
QUERY_COMMANDS = {"get_auth_state", "get_state", "get_queue"}   # answered by an event, never acked
RESTART_BACKOFF_S = tuple(float(x) for x in os.environ.get(
    "VIBB_SOLOIST_BACKOFF_S", "5,10,20,40,60").split(","))
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# ----- 4d: the silent pass (AM-62..81) -----
WARM_NODE = os.environ.get("VIBB_WARM_NODE", "vibb_null")   # the null sink from 10-vibb.conf
STORE_DIR = os.path.join(DATA_DIR, "vibb")                   # ours, never Soloist's own -D files
CACHE_ID_FILE = os.path.join(CACHE_DIR, "vibb-cache-id")
WARM_TICK_S = float(os.environ.get("VIBB_WARM_TICK_S", "0.5"))
WARM_QUIET_S = float(os.environ.get("VIBB_WARM_QUIET_S", "2.0"))     # no growth this long = whole
WARM_STALL_S = float(os.environ.get("VIBB_WARM_STALL_S", "5.0"))     # no growth, below size = stall
WARM_SETTLE_S = float(os.environ.get("VIBB_WARM_SETTLE_S", "3.0"))   # nothing new by then = cached
WARM_MIN_KBPS = 96                     # floor until the received bitrate is benched (Zero: 160)
WARM_FILL = 0.85                       # of duration_s * kbps: below this it is a partial
WARM_CAP_MIN_S = float(os.environ.get("VIBB_WARM_CAP_MIN_S", "30"))
WARM_CAP_RATE = 50_000                 # B/s: the cap grows for long tracks on slow links
WARM_PASS_MAX_S = float(os.environ.get("VIBB_WARM_PASS_MAX_S", str(20 * 60)))
WARM_PASS_MAX_ITEMS = int(os.environ.get("VIBB_WARM_PASS_MAX_ITEMS", "100"))
WARM_VERIFY_S = 24 * 3600              # a done context is re-verified at most this often
WARM_LEDGER_TTL_S = 30 * 24 * 3600     # Soloist's own eviction is invisible: re-walk after this
WARM_MAX_STALLS = 3
PREFETCH_BLOCK_B = 131168              # one 128 KiB block + 96 B header: the next item's prefetch


def _load_json(path, default):
    try:
        with open(path) as f:
            v = json.load(f)
        return v if isinstance(v, type(default)) else default
    except (OSError, ValueError):
        return default            # absent, truncated or torn: start clean


def _save_json(path, obj):
    """tmp + fsync + os.replace — the content.py pattern; a poweroff between
    the rename and the flush left an EMPTY file in the field once."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _order_path(uri):
    import hashlib
    return os.path.join(STORE_DIR, "order-" + hashlib.sha1(uri.encode()).hexdigest()[:12] + ".json")


def _fingerprint(uris):
    import hashlib
    return hashlib.sha1("\n".join(sorted(uris)).encode()).hexdigest()


class WarmAborted(Exception):
    pass


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


# Spotify's image CDN encodes the size in the id's prefix; the hash after it
# is the same image (Zero 2026-09-05: the 64 px url with the prefix swapped to
# b273 answered 200, 85 KB). Soloist hands out ONE cover, size "small" = 64 px.
_COVER_SIZE_PREFIX = {"ab67616d00004851": "ab67616d0000b273",   # 64  -> 640
                      "ab67616d00001e02": "ab67616d0000b273"}   # 300 -> 640


def _full_size(url):
    for small, big in _COVER_SIZE_PREFIX.items():
        if small in url:
            return url.replace(small, big, 1)
    return url


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
    return _full_size(best) if best else best


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
            # strings only: a creator without an identity name gave the screen
            # UI a None in the list and ", ".join() crashed vibb-ui in a loop
            # (Zero 2026-09-05)
            "artist_names": [n for n in (((c.get("entity") or {}).get("decorations") or {})
                                         .get("identity", {}).get("name")
                                         for c in (d.get("creators") or []))
                             if isinstance(n, str) and n],
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
        # the remembered lists (AM-60/73): order per context + metadata per
        # track, persisted under STORE_DIR; loaded once, mirrored in memory
        self.meta = _load_json(os.path.join(STORE_DIR, "meta.json"), {})
        self.orders = {}
        try:
            for name in os.listdir(STORE_DIR):
                if name.startswith("order-") and name.endswith(".json"):
                    rec = _load_json(os.path.join(STORE_DIR, name), {})
                    if rec.get("uri") and isinstance(rec.get("tracks"), list):
                        self.orders[rec["uri"]] = rec
        except OSError:
            pass
        self.ledger = _load_json(os.path.join(STORE_DIR, "ledger.json"), {})
        # the pass (4d): one thread, one queue, one abort flag, one freeze
        self.gen = 0                           # child generation: every start bumps it
        self.ws_gen = -1                       # the generation the live socket belongs to
        self.node_override = None              # WARM_NODE while a pass owns the child
        self.warm = None                       # the running pass, or None
        self.warm_last = None                  # the last pass's summary
        self._warm_queue = []
        self.lock_warm = threading.Lock()
        self.z_mb = None
        self._warm_thread = None
        self._warm_abort = threading.Event()
        self._frozen = None                    # /status snapshot while the pass owns the child
        self._warm_owned = False               # the pass holds the child (restore in finally)
        self.cache_id = None
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
                "device_name": DEVICE_NAME, "warming": dict(self.warm) if self.warm else None,
                "warm_last": self.warm_last, "gen": self.gen}

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

    def start_child(self, node_override=None, pin=None):
        """node_override = the pass's WARM_NODE (marks the child as the
        pass's); pin = a node the daemon asked for explicitly (/player/output);
        else the node output.json resolves to."""
        key = os.environ.get("SOLOIST_API_KEY", "")
        if not key:
            self.set_state("needs-key")
            return False
        if os.path.exists(LATCH_FILE):
            self.set_state("expired")
            return False
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(CACHE_DIR, exist_ok=True)
        self.build = None   # re-read from THIS child's banner (a D1 swap changes it, AM-72)
        for f in ("ws.addr", "ws.port"):
            try:
                os.remove(os.path.join(DATA_DIR, f))
            except OSError:
                pass
        self.node_override = node_override
        # AM-62: the pass pins the child to WARM_NODE explicitly — output.json
        # says where the KID's sound goes, and every other start follows it
        self.node = node_override or pin or self._node_for(self.current_pcm())
        self.gen += 1
        self.cache_id = self._ensure_cache_id()
        mb = int(read_settings().get("spotify_cache_gb", 20)) * 1024
        self.z_mb = max(100, mb)
        argv = [SOLOIST, "-n", DEVICE_NAME, "-k", key, "-D", DATA_DIR, "-C", CACHE_DIR,
                "-z", str(self.z_mb), "-w", "127.0.0.1:0"]
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
            # a pass that owned the dead child must not send its next play to
            # this one, which starts on the KID's node (AM-62 generation); the
            # pass's own finally may already have restored a child — never
            # start a second one beside it
            self.warm_abort_join("child died")
            if self.child is not None and self.child.poll() is None:
                return
            self.start_child()

    def stop_child(self, grace_s=5.0):
        child, self.child = self.child, None
        self.ws = None                        # never let a caller talk to the dead child's socket
        if child and child.poll() is None:
            try:
                child.terminate()
                child.wait(timeout=grace_s)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    child.kill()
                except OSError:
                    pass

    def restart_child(self, why):
        self._restart_for(why)

    def _restart_for(self, why, node_override=None, from_pass=False, pin=None):
        """The ONE way to restart the child (AM-62). Any restart that is not
        the pass's own first aborts the pass and waits for it to yield, so a
        pass can never send its next play to a child on the kid's node."""
        if not from_pass:
            self.warm_abort_join(why)
        log(f"restarting soloist ({why})" + (f" on {node_override}" if node_override else ""))
        self.stop_child(grace_s=1.0 if (from_pass or self.node_override) else 5.0)
        self.bound = None
        if self.state == "audio-unbound":
            self.state = "starting"
        self.start_child(node_override, pin=pin)

    # ----- the WebSocket mirror -----
    def _ws_loop(self, child):
        gen = self.gen                      # the child this socket belongs to
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
            self.ws_gen = gen
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
            self.warm_abort_join("pair")
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

    def _is_our_stream(self, props):
        """The child's output stream. Field 2026-09-05: it is node.name
        "spotify" / application.name "Spotify" — nothing says "soloist", so
        the old substring match never found it. The child's pid is the
        authoritative key; the names are the fallback for a dump without
        application.process.id."""
        if props.get("media.class") != "Stream/Output/Audio":
            return False
        pid = props.get("application.process.id")
        child = self.child.pid if self.child and self.child.poll() is None else None
        if pid is not None and child is not None:
            try:
                return int(pid) == child
            except (TypeError, ValueError):
                pass
        names = " ".join(str(props.get(k) or "") for k in ("node.name", "application.name", "media.name")).lower()
        return "spotify" in names or "soloist" in names

    def _bind_check_body(self, audio, authoritative, delay=None):
        time.sleep((2.0 if authoritative else 0.5) if delay is None else delay)
        dump = audio.pw_dump()
        if not dump:
            return                                  # no graph to ask: unknown
        streams = [obj for obj in dump
                   if obj.get("type") == "PipeWire:Interface:Node"
                   and self._is_our_stream((obj.get("info") or {}).get("props") or {})]
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
    def cmd(self, command, timeout=None, **fields):
        """Send one command; the reply is the command_result/error frame
        (async: state changes arrive as events). Raises OSError when there
        is no live WebSocket — the dialect's 'unreachable'.

        QUERY commands (get_auth_state, get_state, get_queue) get NO
        command_result — their answer IS the event (Soloist docs, and the
        Zero 2026-09-05: queue_changed in 40 ms while this waited the full
        timeout for an ack that never comes — the whole reason the Sonos
        hand-off's listing overran the daemon's 5 s)."""
        ws = self.ws
        if ws is None:
            raise OSError("soloist websocket not connected")
        since = self.mark()
        ws.send_json({"type": "command", "command": command, **fields})
        if command in QUERY_COMMANDS:
            return True, {"type": "command_result", "command": command, "query": True}
        end = time.monotonic() + (CMD_TIMEOUT_S if timeout is None else timeout)
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
    # ----- 4d: the silent pass (AM-62..81) -----
    def _ensure_cache_id(self):
        """A random id written into CACHE_DIR at first child start: a restore,
        a cache wipe or a -z shrink gives a different one, and the ledger's
        `warmed` for the old id is worthless (AM-72)."""
        try:
            with open(CACHE_ID_FILE) as f:
                v = f.read().strip()
            if v:
                return v
        except OSError:
            pass
        v = secrets.token_hex(8)
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(CACHE_ID_FILE, "w") as f:
                f.write(v)
        except OSError:
            pass
        return v

    def _ledger_save(self):
        try:
            _save_json(os.path.join(STORE_DIR, "ledger.json"), self.ledger)
        except OSError as e:
            log(f"ledger: {e!r}")

    def _ledger_entry(self, uri):
        """The ledger row for uri, with `warmed` emptied when the cache it was
        warmed into is not this one (cache id, a smaller -z, a new build)."""
        e = self.ledger.get(uri) or {}
        stamp = {"cache_id": self.cache_id, "z_mb": self.z_mb, "build": self.build}
        reset = (e.get("cache_id") != stamp["cache_id"]
                 or (e.get("z_mb") or 0) > (stamp["z_mb"] or 0)
                 or (self.build and e.get("build") and e.get("build") != self.build)
                 or (e.get("warmed_at") and time.time() - e["warmed_at"] > WARM_LEDGER_TTL_S))
        if reset and e.get("warmed"):
            log(f"ledger: {uri}: forgetting {len(e['warmed'])} warmed rows "
                f"(cache/size/build changed or {WARM_LEDGER_TTL_S // 86400} days old)")
            e["warmed"] = []
            e["verified_at"] = None
        e.update(stamp)
        e.setdefault("warmed", [])
        e.setdefault("unavailable", [])
        self.ledger[uri] = e
        return e

    def warm_start(self, uri, budget=None):
        """POST /cache/download: queue a context for the pass (duplicates
        coalesced) and start the pass thread if none runs. Answers at once:
        {done: true} when the ledger says so and it was verified within
        WARM_VERIFY_S (no restart at all, AM-72), else {queued: true}."""
        if not uri or not uri.startswith("spotify:"):
            return 400, {"error": "bad-uri"}
        e = self._ledger_entry(uri)
        rec = self.orders.get(uri) or {}
        if (e.get("result") == "done" and e.get("complete") and rec.get("tracks")
                and set(rec["tracks"]) <= set(e["warmed"]) | set(e["unavailable"])
                and e.get("verified_at") and time.time() - e["verified_at"] < WARM_VERIFY_S):
            return 200, {"done": True, "uri": uri, "warmed": len(e["warmed"])}
        with self.lock_warm:
            if uri not in self._warm_queue and not (self.warm and self.warm.get("uri") == uri):
                self._warm_queue.append({"uri": uri, "budget": budget or {}})
            if self._warm_thread is None or not self._warm_thread.is_alive():
                self._warm_abort.clear()
                self._warm_thread = threading.Thread(target=self._warm_run, daemon=True)
                self._warm_thread.start()
        return 202, {"queued": True, "uri": uri, "queue": len(self._warm_queue)}

    def warm_abort_join(self, why, timeout=20.0):
        """Abort a running pass and wait for it to yield the child (restored
        onto the kid's node by the pass's own finally). No-op without a pass,
        and never from the pass thread itself."""
        t = self._warm_thread
        if not t or not t.is_alive() or t is threading.current_thread():
            return False
        log(f"warm: abort ({why})")
        self._warm_abort.set()
        with self.lock_warm:
            self._warm_queue.clear()
        t.join(timeout)
        return True

    def _step(self, fn, *a, **kw):
        """One command under ENGINE.lock — taken with a bounded wait so an
        abort (set by a Handler that HOLDS the lock while it joins us) is
        seen instead of deadlocking (AM-66)."""
        while True:
            if self._warm_abort.is_set():
                raise WarmAborted("abort")
            if self.lock.acquire(timeout=0.2):
                try:
                    return fn(*a, **kw)
                finally:
                    self.lock.release()

    def _check(self, gen):
        if self._warm_abort.is_set():
            raise WarmAborted("abort")
        if self.gen != gen or self.child is None or self.child.poll() is not None:
            raise WarmAborted("child changed")
        if self._poweroff_imminent():
            raise WarmAborted("poweroff imminent")
        if self.warm and time.monotonic() > self.warm["_until_mono"]:
            raise WarmAborted("budget")

    def _wait_ws(self, timeout, gen=None, abortable=False):
        """A live socket to the CURRENT child (gen None = whatever is current).
        Only the pass itself treats the abort flag as a reason to stop
        waiting — a Handler that just aborted the pass must keep waiting for
        the restored child."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.ws is not None and self.ws_gen == (self.gen if gen is None else gen):
                return True
            if abortable and self._warm_abort.is_set():
                raise WarmAborted("abort")
            time.sleep(0.1)
        return False

    def _cache_files(self):
        root = os.path.join(CACHE_DIR, "cache")
        out = {}
        try:
            buckets = list(os.scandir(root))
        except OSError:
            return out
        for b in buckets:
            if not b.is_dir():
                continue
            try:
                for f in os.scandir(b.path):
                    if f.is_file():
                        out[f.path] = f.stat().st_size
            except OSError:
                pass
        return out

    def _dwell(self, gen, duration_ms, before):
        """Wait until the track Soloist is playing (silently) is WHOLE in the
        cache (AM-71): per-file deltas over the whole tree; the largest new
        file must reach WARM_FILL of duration x WARM_MIN_KBPS and then stop
        growing for WARM_QUIET_S. The next item's one-block prefetch
        (PREFETCH_BLOCK_B) and temp files never count. Returns 'fetched',
        'cached' (nothing new needed) or 'stalled'."""
        dur_s = max(1.0, (duration_ms or 0) / 1000.0)
        need = int(WARM_FILL * dur_s * WARM_MIN_KBPS * 125)
        cap = max(WARM_CAP_MIN_S, dur_s * 160 * 125 / WARM_CAP_RATE)
        # `before` was taken BEFORE the play/skip that started this fetch: the
        # Zero fetches a whole track in ~2 s, so a baseline taken here would
        # already contain it and read as 'cached'
        t0 = time.monotonic()
        last_size, last_growth, seen_any = 0, t0, False
        while True:
            time.sleep(WARM_TICK_S)
            self._check(gen)
            now = self._cache_files()
            best = 0
            for path, size in now.items():
                was = before.get(path)
                if size == PREFETCH_BLOCK_B and was is None:
                    continue                      # the next item's first block
                if was is None or size > was:
                    best = max(best, size - (was or 0))
            t = time.monotonic()
            if best > last_size:
                last_size, last_growth, seen_any = best, t, True
            if seen_any and best >= need and t - last_growth >= WARM_QUIET_S:
                return "fetched"
            if not seen_any and t - t0 >= WARM_SETTLE_S:
                return "cached"                   # nothing new: it was already here
            if seen_any and t - last_growth >= WARM_STALL_S:
                return "stalled"
            if t - t0 > cap:
                return "stalled"

    def _current_uri(self):
        with self.mirror_lock:
            return ((self.pb.get("item") or {}).get("uri"))

    def _current_duration(self):
        with self.mirror_lock:
            item = self.pb.get("item") or {}
        return ((item.get("decorations") or {}).get("playback") or {}).get("duration_ms")

    def _queue_ends(self):
        """AM-41: after a track_changed, is the context done? True when the
        next entry is autoplay or nothing is upcoming."""
        since = self.mark()
        self.cmd("get_queue", limit=1)
        q = self.wait_event("queue_changed", LISTING_WAIT_S, since) or {}
        upc = q.get("upcoming") or []
        return not upc or upc[0].get("source") != "context"

    def _warm_run(self):
        """The pass thread: every queued context in turn on ONE child pinned
        to WARM_NODE, then the child restored onto the kid's node. Whatever
        happens, the finally restores and lifts the freeze."""
        self._warm_owned = False
        try:
            while not self.stop.is_set():
                with self.lock_warm:
                    if not self._warm_queue:
                        break
                    job = self._warm_queue.pop(0)
                uri = job["uri"]
                started = time.time()
                self.warm = {"uri": uri, "idx": 0, "n": None, "started": started,
                             "until": started + WARM_PASS_MAX_S, "result": None,
                             "_until_mono": time.monotonic() + WARM_PASS_MAX_S}
                result = "error"
                try:
                    result = self._warm_one(uri)
                except WarmAborted as e:
                    result = f"aborted: {e}"
                except Exception as e:
                    log(f"warm: {uri}: {e!r}")
                    result = f"error: {e.__class__.__name__}"
                e = self._ledger_entry(uri)
                e["result"] = result
                e["warmed_at"] = time.time()
                self._ledger_save()
                self.warm["result"] = result
                self.warm_last = {k: v for k, v in self.warm.items() if not k.startswith("_")}
                log(f"warm: {uri}: {result} ({len(e['warmed'])} warmed)")
                if result.startswith(("aborted", "error", "engine")):
                    break
        finally:
            self.warm = None
            # ownership lives on self, not in a return value: an abort raised
            # mid-track must still hand the child back (the W3 hole)
            if self._warm_owned:
                self._warm_restore()
            self._frozen = None
            self._warm_abort.clear()

    def _warm_own(self):
        """Take the child: freeze /status, restart on WARM_NODE, shroud."""
        from vibb import audio
        dump = audio.pw_dump() or []
        if audio._node_named(dump, WARM_NODE) is None:
            raise WarmAborted(f"no {WARM_NODE} sink")
        self._frozen = self.status()
        self._frozen.update({"stopped": True, "paused": False})
        self._warm_owned = True                # from here on the finally restores
        self._restart_for("warm", node_override=WARM_NODE, from_pass=True)
        gen = self.gen
        if not self._wait_ws(15, gen, abortable=True):
            raise WarmAborted("engine offline")
        self._step(self.cmd, "set_volume", volume=0)
        return gen

    def _warm_restore(self):
        """Give the child back on the kid's node; lift the freeze only once
        the new child is up (AM-64's edge) — a still-frozen status meanwhile."""
        try:
            self._restart_for("warm end", from_pass=True)
            self._wait_ws(15)
        except WarmAborted:
            pass
        except Exception as e:
            log(f"warm: restore failed ({e!r}) — audio-unbound until the next restart")
        self._warm_owned = False
        if self.node == WARM_NODE:
            self.set_state("audio-unbound")

    def _warm_one(self, uri):
        """One context: decide from the ledger, own the child if needed, play
        it on the null sink and dwell per track (AM-71). Returns the result."""
        if self.state in ("needs-key", "bad-key", "expired"):
            return f"engine-{self.state}"
        if self._poweroff_imminent():
            return "poweroff"
        e = self._ledger_entry(uri)
        if not self._warm_owned:
            self._warm_own()
        gen = self.gen
        self._check(gen)
        try:
            from vibb import radio as _radio
            _radio.wait_paging_clear()
        except Exception:
            pass
        since = self.mark()
        before = self._cache_files()          # baseline for the first track's dwell
        ok, r = self._step(self.cmd, "play", uri=uri)
        if not ok:
            return f"play failed: {r.get('message')}"
        if not self.wait_event("track_changed", 15, since):
            return "no track"
        self._check(gen)
        # the whole list at its start (AM-60) — order + meta + fingerprint
        rows = self._snapshot_listing(uri)
        rec = self.orders.get(uri) or {}
        order = list(rec.get("tracks") or [])
        if rows is not None:
            # 'complete' when the window held everything: previous was empty
            # at a fresh start, so upcoming < the 80-window means the end is in sight
            complete = len(rows) < 80
            rec["complete"] = complete
            e["complete"] = complete
            fp = _fingerprint(order)
            if e.get("fingerprint") and e["fingerprint"] != fp:
                gone = [u for u in e["warmed"] if u not in order]
                log(f"warm: {uri}: list changed ({len(gone)} removed rows dropped)")
                e["warmed"] = [u for u in e["warmed"] if u in order]
            e["fingerprint"] = fp
            self._ledger_save()
        # the AM-63 belt: the stream must sit on WARM_NODE, proven early
        try:
            from vibb import audio
            self._bind_check_body(audio, True, delay=0.5)
        except Exception:
            pass
        self._check(gen)
        if self.bound is False:
            raise WarmAborted("mis-bound")
        targets = [u for u in order if u not in e["warmed"] and u not in e["unavailable"]]
        self.warm["n"] = len(targets)
        if not targets:
            e["verified_at"] = time.time()
            self._step(self.cmd, "pause")
            return "done"
        items = 0
        stalls = 0
        while targets and items < WARM_PASS_MAX_ITEMS:
            self._check(gen)
            cur = self._current_uri()
            if cur not in targets:
                # walk to the next wanted item; a whole file is skipped through
                # in a beat, and a skip lands the prefetch block only
                if self._queue_ends():
                    break
                since = self.mark()
                before = self._cache_files()
                ok, _r = self._step(self.cmd, "skip_next")
                if not ok:
                    break
                self.wait_event("track_changed", 10, since)
                continue
            dur = self._current_duration()
            verdict = self._dwell(gen, dur, before)
            items += 1
            self.warm["idx"] = items
            log(f"warm: {cur}: {verdict}")
            if verdict in ("fetched", "cached"):
                if cur not in e["warmed"]:
                    e["warmed"].append(cur)
                stalls = 0
            else:
                stalls += 1
                log(f"warm: {cur} stalled ({stalls}/{WARM_MAX_STALLS})")
                if stalls >= WARM_MAX_STALLS:
                    self._ledger_save()
                    return "stalled"
            targets = [u for u in targets if u != cur]
            self._ledger_save()
            if self._queue_ends():
                break
            since = self.mark()
            before = self._cache_files()
            ok, _r = self._step(self.cmd, "skip_next")
            if not ok:
                break
            self.wait_event("track_changed", 10, since)
        self._step(self.cmd, "pause")
        left = [u for u in order if u not in e["warmed"] and u not in e["unavailable"]]
        if not left:
            e["verified_at"] = time.time()
            return "done"
        return "budget" if items >= WARM_PASS_MAX_ITEMS else "partial"

    def status(self):
        frozen = self._frozen
        if frozen is not None:
            # AM-64: the pass owns the child; the kid's session is gone with it.
            # Honest: stopped, last track kept, origin unchanged, state LIVE.
            st = dict(frozen)
            st["spotify_state"] = self.state
            return st
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
            # the whole list is visible NOW (previous empty, upcoming = the
            # rest): remember it before the walk's skips push the first
            # tracks out of Soloist's 10-deep history (AM-60)
            self._snapshot_listing(uri)
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

    def _queue_rows(self):
        """One get_queue: (tracks in play order, fresh_start) or (None, False)
        when queue_changed did not come within LISTING_WAIT_S. `previous` is
        a history stack, most recent first — reversed it is chronological;
        it is capped at 10 on the box (AM-59), which is why the caller
        REMEMBERS lists instead of trusting one window."""
        with self.mirror_lock:
            cur = self.pb.get("item")
        since = self.mark()
        self.cmd("get_queue", limit=0)
        q = self.wait_event("queue_changed", LISTING_WAIT_S, since)
        if not q:
            return None, False
        prev = [e.get("item") for e in reversed(q.get("previous") or [])
                if e.get("source") == "context"]
        rows = list(prev)
        if cur:
            rows.append(cur)
        rows += [e.get("item") for e in (q.get("upcoming") or []) if e.get("source") == "context"]
        tracks = []
        for ent in rows:
            tr = entity_to_track(ent)
            if tr:
                tracks.append({"uri": tr["uri"], "track": tr})
        return tracks, not prev

    def _remember(self, uri, tracks, fresh_start, complete=None):
        """AM-60/73: the list seen at a context's START is the whole list (up
        to Soloist's window); later windows lose the first tracks to the
        10-deep history. A fresh start (no previous) SEEDS the order for that
        uri; any later window only appends tracks not seen yet. Every entity
        seen lands in the metadata store. Both persist (tmp+fsync+replace)."""
        for t in tracks:
            self.meta[t["uri"]] = t["track"]
        rec = self.orders.get(uri)
        if fresh_start or not rec:
            rec = {"uri": uri, "tracks": [t["uri"] for t in tracks],
                   "remembered_at": time.time(), "complete": bool(complete)}
        else:
            known = set(rec["tracks"])
            rec["tracks"] = rec["tracks"] + [t["uri"] for t in tracks if t["uri"] not in known]
            rec["remembered_at"] = time.time()
            if complete is not None:
                rec["complete"] = bool(complete)
        self.orders[uri] = rec
        try:
            _save_json(_order_path(uri), rec)
            _save_json(os.path.join(STORE_DIR, "meta.json"), self.meta)
        except OSError as e:
            log(f"store: {e!r}")

    def _remembered(self, uri):
        """The remembered list for uri as dialect rows, or []."""
        rec = self.orders.get(uri)
        if not rec:
            return []
        return [{"uri": u, "track": self.meta.get(u) or {"uri": u, "name": None, "artist_names": [],
                                                          "album_cover_url": None, "album_name": None,
                                                          "position": 0, "duration": None}}
                for u in rec["tracks"]]

    def _snapshot_listing(self, uri, complete=None):
        try:
            tracks, fresh = self._queue_rows()
        except OSError:
            return None
        if tracks is not None:
            self._remember(uri, tracks, fresh, complete)
            log(f"listing: remembered {len(tracks)} rows for {uri} at start")
        return tracks

    def listing(self, uri):
        """/context/tracks for the ACTIVE context: the list REMEMBERED from
        its start (AM-60), refreshed by one bounded get_queue (AM-59) so a
        list longer than the window keeps growing. Another context: ready
        but empty — the daemon's 'spotify-listing-unavailable' path."""
        with self.mirror_lock:
            active = (self.pb.get("context") or {}).get("uri")
        if self._frozen is not None or not active or uri != active:
            # not the live context (or the pass owns the child): from disk,
            # marked stale (AM-74) — the picker may use it, the Sonos hand-off
            # must not trust its indices
            rows = self._remembered(uri)
            rec = self.orders.get(uri) or {}
            return {"ready": True, "cached": len(rows), "length": len(rows), "tracks": rows,
                    "stale": True, "remembered_at": rec.get("remembered_at")}
        tracks, fresh = self._queue_rows()
        if tracks is None:
            cached = self._remembered(uri)
            if cached:
                log(f"listing: soloist slow — serving the remembered list ({len(cached)} rows)")
                return {"ready": True, "cached": len(cached), "length": len(cached), "tracks": cached}
            log("listing: soloist slow and nothing remembered — not ready yet")
            return {"ready": False, "cached": 0, "length": 0, "tracks": []}
        self._remember(uri, tracks, fresh)
        out = self._remembered(uri) or tracks
        # cached = a COUNT (the fork dialect): the picker computes pending =
        # cached < length, and a bool made every list "still filling" (AM-74)
        return {"ready": True, "cached": len(out), "length": len(out), "tracks": out}

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
        if path == "/cache/download":
            code, r = ENGINE.warm_start(body.get("uri"), body)
            self._send(code, r)
            return
        if path == "/cache/abort":
            self._send(200, {"aborted": ENGINE.warm_abort_join("api")})
            return
        try:
            with ENGINE.lock:
                warming = ENGINE.warm is not None or ENGINE._frozen is not None
                if warming and path in ("/player/play", "/player/resume", "/player/playpause",
                                        "/player/next", "/player/prev", "/player/seek"):
                    # AM-65: the kid comes first — abort, wait for the pass to
                    # hand the child back on the kid's node, then do the thing
                    ENGINE.warm_abort_join(path)
                    if not ENGINE._wait_ws(10):
                        self._send(503, {"error": "engine-unreachable", "detail": "restoring",
                                         "spotify_state": ENGINE.state})
                        return
                elif warming and path in ("/player/pause", "/player/volume", "/player/shuffle_context"):
                    self._send(200, {"ok": True, "result": {"type": "command_result", "warming": True}})
                    return
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
                    node = ENGINE._node_for(body.get("device") or ENGINE.current_pcm())
                    ENGINE._restart_for(f"output -> {body.get('device')}", pin=node)
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
