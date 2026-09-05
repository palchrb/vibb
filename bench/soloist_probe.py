#!/usr/bin/env python3
"""Ask Soloist DIRECTLY over its WebSocket — no sidecar, no daemon in between.

Run on the box while something plays, as the user vibb-soloistd runs as:

    python3 ~/vibb/bench/soloist_probe.py

Self-contained: its own minimal RFC 6455 client (stdlib only), the port read
from the ws.addr/ws.port files Soloist writes next to its state. Prints:

  1. get_state          -> status, track, position, context
  2. get_queue limit=0  -> how long Soloist took, previous/upcoming counts,
                           their sources, the first names
  3. get_queue again    -> is the second answer as fast as the first?

  --raw   also dump, as JSON: the whole context entity from playback_state
          (does it carry a playlist revision / snapshot id anywhere?), the
          current item entity, and ONE queue entry — every key Soloist sends,
          so nothing is guessed about the entity shape.

  --play <uri> [--watch N] [--mute]
          send `play uri`, then watch the audio cache tree for N seconds
          (default 20): every new or growing file, per second, and at the end
          the largest new file, when it stopped growing, and the bitrate it
          implies against the track's duration_ms. Then `pause`. --mute sets
          volume 0 first and restores it after (use it against the LIVE child
          on a real sink; not needed when the child sits on vibb_null).
          Answers AM-79 (a) prefetch of the next track, (b) does Soloist keep
          fetching into vibb_null, (c) the bitrate actually received.

Owner 2026-09-05: "kan vi ikke teste å spørre soloist direkte?"
"""
import base64
import hashlib
import json
import os
import secrets
import socket
import struct
import sys
import time

DATA_DIR = os.environ.get("VIBB_SOLOIST_DATA", "/var/lib/vibb-soloist")
CACHE_DIR = os.environ.get("VIBB_SOLOIST_CACHE", "/var/cache/vibb-soloist")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WSClient:
    """Text frames only, client-masked as RFC 6455 requires, ping answered."""

    def __init__(self, host, port, timeout=10.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.sock.sendall((f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\n"
                           "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("handshake: connection closed")
            head += chunk
        head, _, rest = head.partition(b"\r\n\r\n")
        self.buf = rest
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status:
            raise ConnectionError(f"handshake refused: {status}")
        want = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        accept = next((l.split(b":", 1)[1].strip().decode() for l in head.split(b"\r\n")
                       if l.lower().startswith(b"sec-websocket-accept:")), "")
        if accept != want:
            raise ConnectionError("handshake: bad Sec-WebSocket-Accept")

    def _send_frame(self, opcode, payload):
        mask = secrets.token_bytes(4)
        n = len(payload)
        hdr = bytes([0x80 | opcode])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack("!Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(hdr + mask + masked)

    def send_json(self, obj):
        self._send_frame(0x1, json.dumps(obj).encode())

    def _read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv_json(self, timeout=10.0):
        """The next text frame as JSON (fragments joined); None on a control
        frame with nothing to say; raises socket.timeout when silent."""
        self.sock.settimeout(timeout)
        message = b""
        while True:
            b1, b2 = self._read_exact(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if b2 & 0x80 else b""
            payload = self._read_exact(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:                       # ping -> pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0x8:
                raise ConnectionError("server closed the websocket")
            if opcode in (0x1, 0x0):
                message += payload
                if fin:
                    return json.loads(message.decode()) if message else None
                continue
            return None                              # pong / binary: ignore


def name_of(entry):
    item = (entry or {}).get("item") or {}
    deco = item.get("decorations") or {}
    return (deco.get("identity") or {}).get("name") or item.get("uri") or "?"


def wait(ws, etype, timeout):
    """The first event of `etype` within timeout, plus every type seen."""
    end = time.monotonic() + timeout
    seen = []
    while time.monotonic() < end:
        try:
            m = ws.recv_json(timeout=max(0.1, end - time.monotonic()))
        except socket.timeout:
            break
        except ConnectionError as e:
            print(f"   (connection: {e})")
            break
        if not m:
            continue
        seen.append(m.get("type"))
        if m.get("type") == etype:
            return m, seen
        if m.get("type") == "error":
            print("   error frame:", json.dumps(m)[:300])
    return None, seen


def walk_keys(obj, prefix="", out=None, depth=0):
    """Every key path in a nested dict/list, with a short value preview."""
    out = [] if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            walk_keys(v, f"{prefix}.{k}" if prefix else k, out, depth + 1)
    elif isinstance(obj, list):
        if obj:
            walk_keys(obj[0], prefix + "[0]", out, depth + 1)
        else:
            out.append(f"{prefix}: []")
    else:
        v = json.dumps(obj)
        out.append(f"{prefix}: {v if len(v) <= 60 else v[:57] + '...'}")
    return out


def dump(label, obj):
    print(f"--- {label} ---")
    for line in walk_keys(obj):
        print("   ", line)
    hits = [l for l in walk_keys(obj) if any(w in l.lower() for w in ("snapshot", "revision", "version", "etag", "hash", "updated", "modified"))]
    print("    revision-like keys:", hits or "NONE")


def cache_files():
    """{path: size} for every file under CACHE_DIR/cache (content-addressed
    buckets; one walk is ~256 scandirs, milliseconds on a Zero)."""
    root = os.path.join(CACHE_DIR, "cache")
    out = {}
    try:
        buckets = os.scandir(root)
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


def play_and_watch(ws, uri, seconds, mute):
    duration_ms = None
    vol = None
    if mute:
        ws.send_json({"type": "command", "command": "get_state"})
        st, _ = wait(ws, "playback_state", 5)
        vol = (st or {}).get("volume")
        ws.send_json({"type": "command", "command": "set_volume", "volume": 0})
        wait(ws, "volume_changed", 3)
    before = cache_files()
    print(f"cache before: {len(before)} files, {sum(before.values()) / 1e6:.1f} MB")
    t0 = time.monotonic()
    ws.send_json({"type": "command", "command": "play", "uri": uri})
    tc, seen = wait(ws, "track_changed", 10)
    if tc:
        item = tc.get("item") or {}
        duration_ms = ((item.get("decorations") or {}).get("playback") or {}).get("duration_ms")
        print(f"track_changed after {time.monotonic() - t0:.2f}s: {name_of({'item': item})!r} "
              f"duration_ms={duration_ms} (events: {seen})")
    else:
        print(f"no track_changed within 10 s (events: {seen}) — watching the cache anyway")
    last_total = None
    last_growth_at = None
    grown = {}
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(1.0)
        now = cache_files()
        t = time.monotonic() - t0
        line = []
        for path, size in now.items():
            was = before.get(path)
            if was is None or size != was:
                delta = size - (was or 0)
                grown[path] = size
                line.append(f"{os.path.basename(path)[:12]}…={size:,}B(+{delta:,})")
        total = sum(now.values())
        if last_total is not None and total != last_total:
            last_growth_at = t
        last_total = total
        print(f"  t={t:5.1f}s files={len(now)} total={total / 1e6:.1f}MB " + (" ".join(line) if line else "(no change)"))
        # sink the drained events so the socket buffer never fills
        try:
            ws.sock.settimeout(0.01)
            while ws.recv_json(timeout=0.01):
                pass
        except Exception:
            pass
    ws.send_json({"type": "command", "command": "pause"})
    wait(ws, "playback_changed", 3)
    if mute and vol is not None:
        ws.send_json({"type": "command", "command": "set_volume", "volume": int(vol)})
        wait(ws, "volume_changed", 3)
    new_files = {p: sz for p, sz in grown.items() if p not in before}
    if not new_files and not grown:
        print("RESULT: the cache did not change at all — Soloist fetched nothing for this play")
        return
    biggest = max(grown.items(), key=lambda kv: kv[1])
    print(f"RESULT: {len(new_files)} new file(s), {len(grown) - len(new_files)} grown; "
          f"largest {os.path.basename(biggest[0])} = {biggest[1]:,} B; "
          f"growth last seen at t={last_growth_at}s")
    if duration_ms and biggest[1] > 200000:
        kbps = biggest[1] * 8 / (duration_ms / 1000.0) / 1000.0
        print(f"        implied bitrate for the largest file: {kbps:.0f} kbps "
              f"({'whole track' if kbps > 80 else 'PARTIAL — a stub or a cut fetch'})")
    if len(new_files) >= 2:
        print("        two or more new files: a second fetch ran alongside (prefetch of the next track?)")


def main():
    raw = "--raw" in sys.argv[1:]
    args = sys.argv[1:]
    play_uri = args[args.index("--play") + 1] if "--play" in args and args.index("--play") + 1 < len(args) else None
    watch_s = int(args[args.index("--watch") + 1]) if "--watch" in args else 20
    mute = "--mute" in args
    try:
        addr = open(os.path.join(DATA_DIR, "ws.addr")).read().strip()
        port = int(open(os.path.join(DATA_DIR, "ws.port")).read().strip())
    except OSError as e:
        sys.exit(f"cannot read {DATA_DIR}/ws.addr|ws.port ({e}) — is vibb-soloistd running, "
                 f"and are you the user it runs as?")
    print(f"soloist websocket at {addr}:{port}")
    ws = WSClient(addr, port, timeout=10)
    print("connected (Soloist accepts a second client)")
    if play_uri:
        play_and_watch(ws, play_uri, watch_s, mute)
        return

    ws.send_json({"type": "command", "command": "get_state"})
    st, seen = wait(ws, "playback_state", 5)
    print("1. get_state -> events seen:", seen)
    if st:
        item = st.get("item") or {}
        deco = item.get("decorations") or {}
        pos = st.get("position") or {}
        print(f"   status={st.get('status')} track={(deco.get('identity') or {}).get('name')!r} "
              f"uri={item.get('uri')} position_ms={pos.get('position_ms')} "
              f"context={(st.get('context') or {}).get('uri')}")
    else:
        print("   no playback_state within 5 s")
    if raw and st:
        dump("context entity (playback_state.context)", st.get("context") or {})
        dump("current item entity (playback_state.item)", st.get("item") or {})
        other = {k: v for k, v in st.items() if k not in ("context", "item")}
        dump("the rest of playback_state", other)

    for label in ("2. get_queue limit=0", "3. get_queue again"):
        t0 = time.monotonic()
        ws.send_json({"type": "command", "command": "get_queue", "limit": 0})
        q, seen = wait(ws, "queue_changed", 10)
        dt = time.monotonic() - t0
        print(f"{label} -> {dt:.2f}s, events seen: {seen}")
        if not q:
            print("   NO queue_changed within 10 s")
            continue
        prev, upc = q.get("previous") or [], q.get("upcoming") or []
        srcs = {}
        for e in prev + upc:
            srcs[e.get("source")] = srcs.get(e.get("source"), 0) + 1
        print(f"   previous={len(prev)} upcoming={len(upc)} total={len(prev) + len(upc)} sources={srcs}")
        print("   previous (newest first):", [name_of(e) for e in prev[:5]])
        print("   upcoming:", [name_of(e) for e in upc[:8]])
        if raw and label.startswith("2.") and (upc or prev):
            dump("one queue entry (upcoming[0])", (upc or prev)[0])
            other = {k: v for k, v in q.items() if k not in ("previous", "upcoming")}
            dump("the rest of queue_changed", other)


if __name__ == "__main__":
    main()
