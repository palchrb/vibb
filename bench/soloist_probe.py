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


def main():
    raw = "--raw" in sys.argv[1:]
    try:
        addr = open(os.path.join(DATA_DIR, "ws.addr")).read().strip()
        port = int(open(os.path.join(DATA_DIR, "ws.port")).read().strip())
    except OSError as e:
        sys.exit(f"cannot read {DATA_DIR}/ws.addr|ws.port ({e}) — is vibb-soloistd running, "
                 f"and are you the user it runs as?")
    print(f"soloist websocket at {addr}:{port}")
    ws = WSClient(addr, port, timeout=10)
    print("connected (Soloist accepts a second client)")

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
