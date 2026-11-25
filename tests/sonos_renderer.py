#!/usr/bin/env python3
"""Gate the Sonos renderer axis in vibbd (QA priorities 1-3,
2026-08-09) against a FAKE sidecar. What must hold:

1. output stays two-valued: OUTPUT_PCMS never grows a sonos key, and
   set_output("sonos") touches neither OUT_FILE nor the BT quiet
   marker nor the BT kick — the double-playback / btwatchd-poisoning
   traps.
2. status() during sonos: same keys as the mpv card, playing:true only
   from a FRESH snapshot; a stale one reads as NOT playing but keeps
   position (never zeroed) — stuck-true keeps the box awake all night,
   zeroing corrupts the card.
3. bookmark-before-stop: switching back to the box reads the position
   BEFORE the transport Stop (after a Stop the speaker reads 0:00).
4. play() while renderer is sonos never spawns a local player.
5. btwatchd's fallback announce is skipped while renderer is sonos.
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

FAKE = {"state": {"armed": False, "seq": 1, "stale_s": None},
        "log": []}


class FakeSidecar(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        out = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        if self.path.startswith("/state"):
            self._send(200, FAKE["state"])
        elif self.path.startswith("/players"):
            self._send(200, {"players": [{"uid": "RINCON_T", "name": "Stua"}]})
        else:
            self._send(404, {})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        FAKE["log"].append((self.path, body))
        # contract discipline: an unknown /play shape must 400 loudly
        if self.path == "/play":
            # what the screen's /status sees DURING the hand-off
            FAKE["at_play"] = (daemon.ORCH.sonos_idx,
                               len(daemon.ORCH.sonos_queue))
            sys.path.insert(0, os.path.join(REPO, "tests"))
            import sonos_contract
            err = sonos_contract.check_play(body)
            if err:
                self._send(400, {"error": err})
                return
            self._send(200, {"ok": True, "uid": body["uid"],
                             "uri": body["uri"], "sought": True})
            return
        self._send(200, {"ok": True})

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeSidecar)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["VIBB_SONOS_API"] = f"http://127.0.0.1:{srv.server_port}"
sys.path.insert(0, os.path.join(REPO, "pi"))
import daemon  # noqa: E402
from vibb import renderer  # noqa: E402
from vibb.output import OUTPUT_PCMS  # noqa: E402

daemon.go_status = lambda **k: {}
daemon._flush_spotify_bookmark = lambda: None
SPAWNED = []
daemon.Orchestrator._spawn = lambda self, *a, **k: SPAWNED.append(a)
orch = daemon.ORCH
orch._mpv_alive = lambda: False

# 1. the axis is a separate file — OUTPUT_PCMS is untouched
assert "sonos" not in OUTPUT_PCMS, "sonos must NEVER be an ALSA pcm key"
r = orch.set_output("sonos", uid="RINCON_T", name="Stua")
assert r and r.get("renderer") == "sonos", r
assert renderer.read()["uid"] == "RINCON_T"
assert not os.path.exists(os.path.join(TMP, "output.json")), \
    "sonos switch must not write OUT_FILE (player.py would read pcm null)"
assert not os.path.exists(os.environ["VIBB_BT_QUIET"]), \
    "sonos switch must not touch the BT quiet marker"
assert not os.path.exists(os.environ["VIBB_BT_KICK"]), \
    "sonos switch must not page the BT speaker"
print("1. renderer axis is orthogonal — no OUT_FILE/quiet/kick OK")

# 2. play() while sonos: contract-valid /play to the sidecar, no local
#    spawn ever
FAKE["log"].clear()
r = orch.play("https://podkast.example/feed.xml")
plays = [b for p, b in FAKE["log"] if p == "/play"]
assert SPAWNED == [], "renderer sonos must never spawn a local player"
assert plays and plays[0]["kind"] == "url", (r, FAKE["log"])
# the index is written WITH the queue, before the seconds-long hand-off:
# a previous list's index outliving its queue raised IndexError in every
# /status of that window (field 2026-11-21)
assert FAKE["at_play"] == (0, len(orch.sonos_queue)) and orch.sonos_queue, \
    FAKE["at_play"]
assert orch.source == "sonos"
print("2. play routes to the sidecar, never a local spawn OK")

# 3. status(): fresh PLAYING -> playing:true with the mpv card's keys;
#    stale -> playing:false with position PRESERVED
orch.sonos_snap = {"armed": True, "uid": "RINCON_T", "kind": "url",
                   "seq": 5, "stale_s": 1.0, "reachable": True,
                   "transport": "PLAYING", "rel_s": 431.0, "dur_s": 1453.0,
                   "uri": orch.sonos_queue[0]["url"] if orch.sonos_queue
                   else "u", "ours": True, "foreign_uri": None,
                   "grouped_away": False, "lost_session": False,
                   "volume": 30, "retried_at": None}
orch.sonos_snap_at = time.monotonic()
st = orch.status()
assert st["playing"] is True and st["source"] == "sonos"
assert st["position"] is not None and abs(st["position"] - 431) < 3
assert st["duration"] == 1453.0
orch.sonos_snap_at = time.monotonic() - 999  # snapshot goes stale
st = orch.status()
assert st["playing"] is False, "a stale snapshot must read as NOT playing"
assert st["position"] == 431.0, "staleness must never zero the position"
assert st.get("renderer_state") == "unreachable"
print("3. fresh=playing, stale=not-playing with position kept OK")

# 3b. foreign URI: never playing, flagged for the screen
orch.sonos_snap_at = time.monotonic()
orch.sonos_snap = dict(orch.sonos_snap, ours=False,
                       foreign_uri="x-sonos-spotify:guest")
st = orch.status()
assert st["playing"] is False and st["renderer_state"] == "taken-over"
print("3b. foreign uri reads as taken-over, not ours OK")

# 3c. an index beyond the queue (the poller rebuilt a shorter one under
#     it, or a play's hand-off is in flight) must never raise — the card
#     just has no row to paint from (field 2026-11-21 20:55)
_q, _i = orch.sonos_queue, orch.sonos_idx
orch.sonos_snap = dict(orch.sonos_snap, ours=True, foreign_uri=None)
orch.sonos_queue, orch.sonos_idx = [{"url": "u", "id": "e1", "title": "T"}], 7
st = orch.status()
assert st["source"] == "sonos" and st.get("episode_id") is None, st
orch.sonos_queue, orch.sonos_idx = _q, _i
print("3c. an index past the queue reads as no row, never a raise OK")

# 4. bookmark-before-stop: switch back to the box — the position is
#    persisted from the LAST MEASURED snapshot before /stop is posted
orch.sonos_snap = dict(orch.sonos_snap, ours=True, foreign_uri=None,
                       rel_s=712.0, transport="PLAYING")
orch.sonos_snap_at = time.monotonic()
orch.sonos_idx = 0
FAKE["log"].clear()
orch.play = lambda *a, **k: None  # the resume fire-and-forget is not under test
orch._renderer_to_box()
from vibb.bookmarks import load_state  # noqa: E402
from vibb.library import state_key  # noqa: E402
st_file = load_state(state_key("https://podkast.example/feed.xml"))
# pos = 712 + the snapshot's measurement age (stale_s) — the age
# correction is intended (architect G1-b)
assert st_file and abs(st_file["pos"] - 712.0) < 3.0, st_file
stops = [p for p, b in FAKE["log"] if p == "/stop"]
assert stops == ["/stop"], FAKE["log"]
assert renderer.read()["renderer"] == "box"
print("4. switch-back bookmarks the measured position, then stops OK")

# 5. btwatchd's fallback announce while sonos: skipped, normal reply
renderer.write("sonos", uid="RINCON_T", name="Stua")
r = daemon.ORCH.set_output("bt", fallback=True)
assert r.get("skipped") == "renderer is sonos", r
print("5. A2DP fallback announce cannot yank the output off sonos OK")

print("\nSONOS RENDERER OK — the axis is orthogonal, staleness is honest, "
      "and the bookmark lands before the stop.")
