#!/usr/bin/env python3
"""Pressing A on the tile that is ALREADY playing over Sonos must not
re-transfer the queue (owner 2026-08-18: an audible hiccup on every
press — sonos_start_target re-expands, re-reads the bookmark and
re-pushes the episode; for Storytel that re-mints a signed URL).

The guard in play() mirrors the local "already loaded -> unpause"
shortcuts, and is deliberately NARROW. What must hold:

1. same tile, OUR live PLAYING session, steady -> NO-OP: the speaker is
   not re-commanded at all (the UI just opens now-playing).
2. same tile PAUSED on the speaker -> ONE /resume verb, no re-transfer,
   optimistic transport flip (the playpause branch's resume side).
3. the heal is never swallowed: a sharelink press with
   sonos_map_trusted=False re-transfers.
4. a press inside the poller's 8s settle window (a jump just issued)
   falls through — the snap may still describe the OLD track.
5. a STALE snapshot falls through — "already playing" may be old truth.
6. fresh=True (play from the start) and an explicit episode pick always
   respawn — the same rule the local shortcuts enforce.
"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
for k in ("VIBB_RUN", "VIBB_STATE", "VIBB_CACHE"):
    os.environ[k] = TMP
os.environ["VIBB_SETTINGS"] = os.path.join(TMP, "settings.json")
os.environ["VIBB_BT_FILE"] = os.path.join(TMP, "bt-headset")
os.environ["VIBB_BT_LOCKFILE"] = os.path.join(TMP, "bt.lock")
os.environ["VIBB_BT_KICK"] = os.path.join(TMP, "kick")
os.environ["VIBB_BT_QUIET"] = os.path.join(TMP, "quiet")

FAKE = {"log": []}


class FakeSidecar(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        out = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        if self.path.startswith("/state"):
            self._send(200, {"armed": True, "seq": 1, "stale_s": 1.0,
                             "uid": "RINCON_T", "kind": "url",
                             "transport": "PLAYING", "reachable": True})
        elif self.path.startswith("/players"):
            self._send(200, {"players": [{"uid": "RINCON_T",
                                          "name": "Stua"}]})
        else:
            self._send(404, {})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        FAKE["log"].append((self.path, body))
        if self.path == "/play":
            self._send(200, {"ok": True, "uid": body.get("uid"),
                             "uri": body.get("uri"), "sought": True})
            return
        self._send(200, {"ok": True})

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeSidecar)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["VIBB_SONOS_API"] = f"http://127.0.0.1:{srv.server_port}"
sys.path.insert(0, os.path.join(REPO, "pi"))
import daemon  # noqa: E402

daemon.go_status = lambda **k: {}
SPAWNED = []
daemon.Orchestrator._spawn = lambda self, *a, **k: SPAWNED.append(a)
orch = daemon.ORCH
orch._mpv_alive = lambda: False

TARGET = "https://podkast.example/feed.xml"
orch.set_output("sonos", uid="RINCON_T", name="Stua")
r = orch.play(TARGET)
assert [p for p, _ in FAKE["log"] if p == "/play"], f"setup play: {r}"
assert orch.source == "sonos" and orch.target == TARGET


def steady(transport="PLAYING"):
    """A settled live session of ours: fresh snap, no press in flight."""
    orch.sonos_pending = None
    orch.sonos_opt_tr = None
    orch.sonos_map_trusted = True
    orch.sonos_kind = "url"
    if orch.sonos_idx is None:
        orch.sonos_idx = 0
    orch.sonos_snap = {"armed": True, "uid": "RINCON_T", "kind": "url",
                       "seq": 5, "stale_s": 1.0, "reachable": True,
                       "transport": transport, "rel_s": 431.0,
                       "dur_s": 1453.0, "uri": "u", "ours": True,
                       "foreign_uri": None, "volume": 30}
    orch.sonos_snap_at = time.monotonic()
    FAKE["log"].clear()


# 1. already PLAYING -> the speaker is not commanded at all
steady("PLAYING")
r = orch.play(TARGET)
assert r == {"source": "sonos", "target": TARGET, "resumed": True}, r
assert FAKE["log"] == [], \
    f"a press on the playing tile must not re-command the speaker: " \
    f"{FAKE['log']}"
print("1. same tile playing -> no-op, zero sidecar posts OK")

# 2. PAUSED on the speaker -> one /resume, no re-transfer
steady("PAUSED_PLAYBACK")
r = orch.play(TARGET)
assert r.get("resumed") is True, r
posts = [p for p, _ in FAKE["log"]]
assert posts == ["/resume"], \
    f"paused must resume with ONE verb, never re-push: {posts}"
assert orch.sonos_snap["transport"] == "PLAYING", \
    "the optimistic flip must patch the snapshot (QA §1A rule)"
assert orch.sonos_opt_tr and orch.sonos_opt_tr[0] == "PLAYING", \
    "the flip must be held against the next stale poll"
print("2. same tile paused -> one /resume, optimistic flip held OK")

# 3. the heal path is never swallowed: sharelink with a drifted map
steady("PLAYING")
orch.sonos_kind = "spotify_sharelink"
orch.sonos_map_trusted = False
orch.play(TARGET)
assert [p for p, _ in FAKE["log"] if p == "/play"], \
    "an untrusted map means the press IS the re-sync — it must transfer"
print("3. drifted sharelink map -> the press still re-transfers OK")

# 3b. ...and the url kind with no queue mapping heals the same way
steady("PLAYING")
orch.sonos_idx = None
orch.play(TARGET)
assert [p for p, _ in FAKE["log"] if p == "/play"], \
    "no queue mapping -> the full start is the heal"
print("3b. url kind without its queue mapping -> re-transfers OK")

# 4. a press inside the settle window falls through (stale-snap trap)
steady("PLAYING")
orch.sonos_pending = ("some-id", time.monotonic())
orch.play(TARGET)
assert [p for p, _ in FAKE["log"] if p == "/play"], \
    "mid-jump the snap may describe the OLD track — must not no-op"
steady("PLAYING")
orch.sonos_opt_tr = ("PLAYING", time.monotonic())
orch.play(TARGET)
assert [p for p, _ in FAKE["log"] if p == "/play"], \
    "an optimistic transport hold is our own guess, not the speaker's truth"
print("4. press in flight (pending / opt hold) -> falls through OK")

# 5. a stale snapshot falls through
steady("PLAYING")
orch.sonos_snap_at = time.monotonic() - 999
orch.play(TARGET)
assert [p for p, _ in FAKE["log"] if p == "/play"], \
    "stale 'playing' may be long over — the full start must run"
print("5. stale snapshot -> falls through OK")

# 6. fresh=True and an explicit episode pick always respawn
steady("PLAYING")
orch.play(TARGET, fresh=True)
assert [p for p, _ in FAKE["log"] if p == "/play"], \
    "'from the start' must restart, exactly like the local shortcuts"
steady("PLAYING")
orch.play(TARGET, episode="does-not-matter")
assert [p for p, _ in FAKE["log"] if p == "/play"], \
    "an explicit episode pick must seek, never no-op"
print("6. fresh / explicit episode -> always respawns OK")

# 7. the grouped-away RECLAIM (stage B pin, QA 2026-08-23): when the
#    speaker was pulled into someone else's group, ours is False — the
#    guard must fall through to the FULL transfer at OUR OWN uid, which
#    detaches the member and resumes the kid's session in the kid's
#    room only. This is the zero-blast-radius behavior the rejected
#    "follow the coordinator" sketch would have destroyed; pin it so no
#    future change can regress it silently.
steady("PLAYING")
orch.sonos_snap = dict(orch.sonos_snap, ours=False,
                       uri="x-rincon:RINCON_PARENT",
                       foreign_uri="x-rincon:RINCON_PARENT",
                       grouped_away=True, coordinator="RINCON_PARENT")
orch.play(TARGET)
plays = [b for pth, b in FAKE["log"] if pth == "/play"]
assert plays, "grouped-away + A-press must re-transfer (the reclaim)"
assert all(b.get("uid") == "RINCON_T" for b in plays), \
    f"the reclaim goes to OUR speaker, never the coordinator: {plays}"
print("7. grouped-away A-press reclaims our own speaker OK")

assert SPAWNED == [], "no sonos path may ever spawn a local player"

print("\nSONOS SAME TILE OK — A on the playing tile commands nothing, "
      "paused resumes with one verb, and every unhealthy state still "
      "heals through the full transfer.")
