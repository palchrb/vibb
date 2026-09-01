#!/usr/bin/env python3
"""Soloist bench spike — the kill-criteria protocol from docs/PLAN-soloistd.md.

Runs STANDALONE on a spare Pi (never the box) against a paired Soloist
daemon. Pure stdlib — no venv, no vibb. Operator-guided: some verdicts
need human ears ("was track 1 audible?"), the script asks and records.

This is also the CONTRACT CANARY: Soloist builds expire every 90 days,
so the binary WILL be replaced for the life of the box. Re-run this
after every update — a silent behavior change in their closed binary
(new caps, changed pause semantics) should be caught here, not by a
kid at bedtime.

Setup (once, on the bench Pi):
  1. Install soloist per docs/PLAN-soloistd.md / the official docs.
  2. Pair the target account:  soloist ... --pair --data-dir DIR
  3. Start it with a FIXED ws port for the spike:
       soloist -n "SpikeBench" -k "$KEY" --ws 127.0.0.1:3690 \\
               --data-dir DIR
  4. Have ready: a 500+ track playlist URI, a 100+ episode show URI,
     a short playlist (~10 tracks), an artist URI, and (if testing
     Liked Songs) spotify:user:<id>:collection.

Run:  python3 soloist_spike.py --ws 127.0.0.1:3690 \\
        --big-playlist spotify:playlist:... --show spotify:show:... \\
        --small-playlist spotify:playlist:...
Report lands in soloist-spike-report.json + a human summary on stdout.
"""
import argparse
import base64
import hashlib
import json
import os
import secrets
import socket
import struct
import sys
import time

# --- minimal RFC6455 client (text frames, client-masked, loopback) ----------

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WS:
    def __init__(self, host, port, timeout=10.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        req = (f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("handshake: connection closed")
            resp += chunk
        head, _, rest = resp.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"handshake refused: {head[:200]!r}")
        want = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        if want.encode() not in head:
            raise ConnectionError("handshake: bad Sec-WebSocket-Accept")
        self.buf = rest

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
        """Next TEXT message as parsed JSON (reassembles fragments,
        answers pings, skips binary). Raises socket.timeout."""
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
            payload = self._read_exact(ln)  # server frames are unmasked
            if opcode == 0x9:               # ping -> pong
                mask = secrets.token_bytes(4)
                pong = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
                self.sock.sendall(
                    struct.pack("!BB", 0x8A, 0x80 | len(payload))
                    + mask + pong)
                continue
            if opcode == 0x8:
                raise ConnectionError("server closed")
            if opcode in (0x1, 0x0):
                message += payload
                if fin:
                    return json.loads(message.decode())
                continue
            # binary/pong: skip

    def drain(self, seconds=1.0):
        """Collect every event that arrives within the window."""
        out, end = [], time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return out
            try:
                out.append(self.recv_json(timeout=left))
            except socket.timeout:
                return out


# --- spike harness ----------------------------------------------------------

REPORT = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "tests": {}}


def record(name, verdict, **detail):
    REPORT["tests"][name] = {"verdict": verdict, **detail}
    print(f"  -> {name}: {verdict}")
    for k, v in detail.items():
        vs = json.dumps(v, ensure_ascii=False)
        if len(vs) > 100:
            vs = vs[:100] + "…"
        print(f"       {k}: {vs}")


def ask(question):
    return input(f"  [operator] {question} [y/n] ").strip().lower() == "y"


def cmd(ws, command, wait_event=None, wait_s=8.0, **fields):
    """Send a command; return (command_result_or_error, events_seen)."""
    ws.send_json({"type": "command", "command": command, **fields})
    result, events, end = None, [], time.monotonic() + wait_s
    while time.monotonic() < end:
        try:
            msg = ws.recv_json(timeout=max(0.1, end - time.monotonic()))
        except socket.timeout:
            break
        t = msg.get("type")
        if t in ("command_result", "error") and result is None:
            result = msg
            if wait_event is None:
                break
        else:
            events.append(msg)
            if wait_event and t == wait_event:
                break
    return result, events


def wait_for(ws, etype, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            msg = ws.recv_json(timeout=max(0.1, end - time.monotonic()))
        except socket.timeout:
            return None
        if msg.get("type") == etype:
            return msg
    return None


def current_item(ws):
    ws.send_json({"type": "command", "command": "get_state"})
    st = wait_for(ws, "playback_state", 5)
    item = (st or {}).get("item") or {}
    return st or {}, item.get("uri")


# --- kill criterion 1: the resume walk --------------------------------------

def t1_resume_walk(ws, playlist, target_index=8):
    print("\n== KILL 1: resume walk (play -> pause -> skips -> seek -> play)")
    print("   Listen carefully NOW — is anything audible during the walk?")
    t0 = time.monotonic()
    r, _ = cmd(ws, "play", uri=playlist)
    wait_for(ws, "track_changed", 15)
    cmd(ws, "pause")
    t_pause = time.monotonic() - t0
    skip_times = []
    for i in range(target_index):
        ts = time.monotonic()
        r, _ = cmd(ws, "skip_next", wait_event="track_changed", wait_s=10)
        skip_times.append(round(time.monotonic() - ts, 2))
        if r and r.get("type") == "error":
            record("resume_walk", "KILLED",
                   reason=f"skip {i} errored: {r}", skips=skip_times)
            return
    cmd(ws, "seek", position_ms=45000)
    cmd(ws, "play")
    total = round(time.monotonic() - t0, 2)
    audible = ask("Was track 1 (or any walk audio) audible before the "
                  "final track started?")
    _st, uri = current_item(ws)
    record("resume_walk",
           "KILLED" if audible else
           ("SLOW" if total > 20 else "PASS"),
           total_s=total, time_to_pause_s=round(t_pause, 2),
           per_skip_s=skip_times, landed_uri=uri, audible=audible)
    cmd(ws, "pause")


def t1b_paused_skips(ws, playlist):
    print("\n== KILL 1b: do skips fire while PAUSED at all?")
    cmd(ws, "play", uri=playlist, wait_event="track_changed", wait_s=15)
    cmd(ws, "pause")
    _st, before = current_item(ws)
    r, ev = cmd(ws, "skip_next", wait_event="track_changed", wait_s=8)
    _st, after = current_item(ws)
    moved = before != after and after is not None
    record("paused_skip", "PASS" if moved else "KILLED",
           before=before, after=after,
           result=(r or {}).get("type"))
    cmd(ws, "pause")


# --- kill criterion 2: queue truth ------------------------------------------

def t2_queue_truth(ws, uri, label, expect_hint):
    print(f"\n== KILL 2: get_queue(limit=0) truth for {label}")
    cmd(ws, "play", uri=uri, wait_event="track_changed", wait_s=15)
    cmd(ws, "pause")
    ws.send_json({"type": "command", "command": "get_queue", "limit": 0})
    q = wait_for(ws, "queue_changed", 15) or {}
    prev = q.get("previous") or []
    upc = q.get("upcoming") or []
    sample = (upc[:1] or [{}])[0]
    has_meta = bool(str(sample))
    record(f"queue_{label}",
           "WINDOWED" if len(upc) < expect_hint else "PASS",
           previous=len(prev), upcoming=len(upc),
           expected_at_least=expect_hint,
           sample_entry_keys=sorted(sample.keys()) if sample else [],
           note="~80 cap per the announcement — verify against this",
           metadata_present=has_meta)
    cmd(ws, "pause")


# --- kill criterion 4: the mash ---------------------------------------------

def t4_mash(ws):
    print("\n== KILL 4: mash — 10 skip_next in ~3s, watch for throttling")
    errors, t0 = [], time.monotonic()
    for _ in range(10):
        ws.send_json({"type": "command", "command": "skip_next"})
        time.sleep(0.3)
    events = ws.drain(20.0)
    for e in events:
        if e.get("type") == "error":
            errors.append(e)
    _st, uri = current_item(ws)
    ok = uri is not None and not errors
    record("mash", "PASS" if ok else
           ("ERRORS" if errors else "STALLED"),
           duration_s=round(time.monotonic() - t0, 1),
           error_frames=errors[:5], event_count=len(events),
           landed_uri=uri)
    cmd(ws, "pause")


# --- kill criterion 5: URI acceptance ---------------------------------------

def t5_uris(ws, uris):
    print("\n== KILL 5: URI acceptance (docs say track/album/playlist/"
          "episode only — try the rest)")
    for label, uri in uris.items():
        if not uri:
            record(f"uri_{label}", "SKIPPED", reason="not provided")
            continue
        r, _ = cmd(ws, "play", uri=uri, wait_event="track_changed",
                   wait_s=12)
        landed = wait_for(ws, "playback_state", 3)
        ok = r is not None and r.get("type") == "command_result"
        record(f"uri_{label}", "PASS" if ok else "REFUSED",
               result=(r or {}).get("type"),
               detail=(r or {}).get("message"))
        cmd(ws, "pause")
        time.sleep(1)


# --- cache-fill semantics (the warming hinge) --------------------------------

def _du(path):
    total = 0
    for root, _d, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def t6_cache_fill_disk(ws, show_uri, cache_dir):
    """Objective cache-fill semantics: no ears, no network-pulling.
    Requires soloist started WITH a cache dir (-C ./cache -z 500) —
    without -C there may be NO cache at all, and the old
    disconnect-and-listen variant silently measured nothing.

    Method: play ~5s of a LONG episode (hours = hundreds of MB whole),
    pause, watch the cache dir grow. Bytes written >> 5s-of-audio
    (~0.2MB) means whole-file/aggressive prefetch -> warming is
    seconds per track. Bytes ~= the played window means chunked ->
    warming is real-time only."""
    print("\n== WARMING HINGE (disk-based): whole-file or chunked?")
    if not cache_dir or not os.path.isdir(cache_dir):
        record("cache_fill", "SKIPPED",
               reason="--cache-dir missing or not a directory; restart "
                      "soloist with -C ./cache -z 500 and pass "
                      "--cache-dir ./cache")
        return
    base = _du(cache_dir)
    uri = show_uri
    if not uri:
        record("cache_fill", "SKIPPED", reason="needs --show")
        return
    cmd(ws, "play", uri=uri, wait_event="track_changed", wait_s=15)
    time.sleep(5)
    cmd(ws, "pause")
    grown = []
    for _ in range(6):            # give a prefetcher 30s to show itself
        time.sleep(5)
        grown.append(_du(cache_dir) - base)
    delta = grown[-1]
    verdict = ("NO-CACHE-WRITTEN" if delta < 50_000 else
               "WHOLE-FILE" if delta > 5_000_000 else "CHUNKED")
    record("cache_fill", verdict,
           bytes_written=delta, growth_curve=grown,
           implication={"WHOLE-FILE": "warming = seconds per track",
                        "CHUNKED": "warming = real-time play-through",
                        "NO-CACHE-WRITTEN":
                        "no cache with these flags — check -C/-z"}[verdict])


# --- shuffle / autoplay / lifecycle probes ----------------------------------

def t7_probes(ws):
    print("\n== PROBES: idle set_shuffle, bare play on playing session")
    cmd(ws, "pause")
    r, _ = cmd(ws, "set_shuffle", enabled=True)
    record("idle_set_shuffle",
           "ACCEPTED" if r and r.get("type") == "command_result"
           else "REFUSED", result=(r or {}).get("type"))
    cmd(ws, "set_shuffle", enabled=False)
    cmd(ws, "play")
    time.sleep(2)
    _st, before = current_item(ws)
    cmd(ws, "play")   # bare play while playing — the sonos-hiccup class
    time.sleep(2)
    _st2, after = current_item(ws)
    hiccup = ask("Did the second bare 'play' cause any audible restart/"
                 "hiccup?")
    record("bare_play_on_playing",
           "HICCUP" if hiccup or before != after else "NOOP",
           before=before, after=after)
    cmd(ws, "pause")


def normalize_uri(link):
    """Accept a share link straight from the Spotify app —
    https://open.spotify.com/playlist/ABC?si=... — or a bare
    spotify:playlist:ABC uri, and return the uri form."""
    if not link:
        return link
    link = link.strip()
    if link.startswith("spotify:"):
        return link.split("?")[0]
    if "open.spotify.com" in link:
        path = link.split("open.spotify.com/", 1)[1].split("?")[0]
        parts = [p for p in path.split("/") if p]
        # intl-xx/ prefixes appear in some app links
        if parts and parts[0].startswith("intl-"):
            parts = parts[1:]
        if len(parts) >= 2:
            return f"spotify:{parts[0]}:{parts[1]}"
    sys.exit(f"unrecognized spotify link/uri: {link!r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", default="127.0.0.1:3690")
    ap.add_argument("--big-playlist", required=True)
    ap.add_argument("--small-playlist", required=True)
    ap.add_argument("--show")
    ap.add_argument("--artist")
    ap.add_argument("--collection")
    ap.add_argument("--walk-depth", type=int, default=8)
    ap.add_argument("--cache-dir", help="soloist's -C dir, enables the "
                    "disk-based cache-fill test")
    args = ap.parse_args()
    for name in ("big_playlist", "small_playlist", "show", "artist",
                 "collection"):
        setattr(args, name, normalize_uri(getattr(args, name)))
    host, port = args.ws.rsplit(":", 1)

    ws = WS(host, int(port))
    print(f"connected to {args.ws}; draining greeting events...")
    greeting = ws.drain(2.0)
    auth = next((e for e in greeting if e.get("type") == "auth_state"), {})
    print(f"  auth_state: {json.dumps(auth)[:200]}")
    if not auth.get("logged_in", True):
        sys.exit("not logged in — run --pair first")

    t1b_paused_skips(ws, args.small_playlist)
    t1_resume_walk(ws, args.big_playlist, args.walk_depth)
    t2_queue_truth(ws, args.big_playlist, "big_playlist", 100)
    if args.show:
        t2_queue_truth(ws, args.show, "show", 50)
    t4_mash(ws)
    t5_uris(ws, {"show": args.show, "artist": args.artist,
                 "collection": args.collection})
    t6_cache_fill_disk(ws, args.show, args.cache_dir)
    t7_probes(ws)

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "soloist-spike-report.json")
    with open(path, "w") as f:
        json.dump(REPORT, f, indent=1)
    print(f"\nreport: {path}")
    print("\n=== VERDICT MAP (against docs/PLAN-soloistd.md) ===")
    for name, t in REPORT["tests"].items():
        print(f"  {t['verdict']:>10}  {name}")
    print("\nKILLED anywhere above = the corresponding plan section "
          "decides the fallback; all PASS = soloistd is a go.")


if __name__ == "__main__":
    main()
