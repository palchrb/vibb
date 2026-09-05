#!/usr/bin/env python3
"""Ask Soloist DIRECTLY over its WebSocket — no sidecar, no daemon in between.

Run on the box while something plays (as the user vibb-soloistd runs as):

    python3 ~/vibb/bench/soloist_probe.py

It reuses the sidecar's own tiny WebSocket client (pulled out of the installed
vibb-soloistd by source, so nothing else of the sidecar runs), reads the
port Soloist wrote next to its state, and prints:

  1. get_state  -> status, track, position, context
  2. get_queue limit=0 -> how long Soloist took, how many previous/upcoming
     entries, their sources, and the first few names

Owner 2026-09-05: "kan vi ikke teste å spørre soloist direkte?"
"""
import ast
import json
import os
import sys
import time

CANDIDATES = ["/usr/local/bin/vibb-soloistd",
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pi", "soloistd.py")]
DATA_DIR = os.environ.get("VIBB_SOLOIST_DATA", "/var/lib/vibb-soloist")


def load_ws_class():
    for path in CANDIDATES:
        if os.path.exists(path):
            src = open(path, encoding="utf-8").read()
            tree = ast.parse(src)
            node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "WS")
            ns = {}
            exec("import base64, errno, hashlib, json, os, random, secrets, socket, ssl, struct, time\n"
                 + ast.get_source_segment(src, node), ns)
            return ns["WS"]
    sys.exit("no soloistd source found")


def name_of(entry):
    item = (entry or {}).get("item") or {}
    deco = item.get("decorations") or {}
    return (deco.get("identity") or {}).get("name") or item.get("uri") or "?"


def wait(ws, etype, timeout):
    end = time.monotonic() + timeout
    seen = []
    while time.monotonic() < end:
        try:
            m = ws.recv_json(timeout=max(0.1, end - time.monotonic()))
        except Exception as e:
            print(f"   (recv: {e.__class__.__name__}: {e})")
            break
        if not m:
            continue
        seen.append(m.get("type"))
        if m.get("type") == etype:
            return m, seen
        if m.get("type") == "error":
            print("   error frame:", json.dumps(m)[:300])
    return None, seen


def main():
    WS = load_ws_class()
    try:
        addr = open(os.path.join(DATA_DIR, "ws.addr")).read().strip()
        port = int(open(os.path.join(DATA_DIR, "ws.port")).read().strip())
    except OSError as e:
        sys.exit(f"cannot read {DATA_DIR}/ws.addr|ws.port ({e}) — is vibb-soloistd running, "
                 f"and are you the user it runs as?")
    print(f"soloist websocket at {addr}:{port}")
    ws = WS(addr, port, timeout=10)

    ws.send_json({"type": "command", "command": "get_state"})
    st, seen = wait(ws, "playback_state", 5)
    print("1. get_state ->", "events seen:", seen)
    if st:
        item = st.get("item") or {}
        deco = item.get("decorations") or {}
        pos = st.get("position") or {}
        print(f"   status={st.get('status')} track={(deco.get('identity') or {}).get('name')!r} "
              f"uri={item.get('uri')} position_ms={pos.get('position_ms')} "
              f"context={(st.get('context') or {}).get('uri')}")
    else:
        print("   no playback_state within 5 s")

    t0 = time.monotonic()
    ws.send_json({"type": "command", "command": "get_queue", "limit": 0})
    q, seen = wait(ws, "queue_changed", 10)
    dt = time.monotonic() - t0
    print(f"2. get_queue limit=0 -> {dt:.2f}s, events seen: {seen}")
    if not q:
        print("   NO queue_changed within 10 s")
        return
    prev, upc = q.get("previous") or [], q.get("upcoming") or []
    srcs = {}
    for e in prev + upc:
        srcs[e.get("source")] = srcs.get(e.get("source"), 0) + 1
    print(f"   previous={len(prev)} upcoming={len(upc)} total={len(prev) + len(upc)} sources={srcs}")
    print("   previous (newest first):", [name_of(e) for e in prev[:5]])
    print("   upcoming:", [name_of(e) for e in upc[:8]])
    # a second ask right away: is the second answer as fast as the first?
    t0 = time.monotonic()
    ws.send_json({"type": "command", "command": "get_queue", "limit": 0})
    q2, seen2 = wait(ws, "queue_changed", 10)
    print(f"3. get_queue again -> {time.monotonic() - t0:.2f}s, events: {seen2}, "
          f"total={len((q2 or {}).get('previous') or []) + len((q2 or {}).get('upcoming') or [])}")


if __name__ == "__main__":
    main()
