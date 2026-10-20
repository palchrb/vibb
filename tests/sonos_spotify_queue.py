#!/usr/bin/env python3
"""v2: vibb owns the spotify queue — the Sonos merely holds it.
Pins the two 2026-08-09 field bugs: (a) transfer restarted the playlist
from the TOP (the sharelink /play never read the context bookmark);
(b) the card was fed from the speaker's DIDL instead of our list."""
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

sys.path.insert(0, os.path.join(REPO, "tests"))
import sonos_contract  # noqa: E402

FAKE = {"log": []}


class FakeSidecar(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        out = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        self._send(404, {})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        FAKE["log"].append((self.path, body))
        if self.path == "/play":
            err = sonos_contract.check_play(body)
            if err:
                self._send(400, {"error": err})
                return
            self._send(200, {"ok": True, "uid": body["uid"],
                             "uri": body["uri"], "sought": True,
                             "base": 1, "queue_len": 3,
                             "play_mode": "NORMAL"})
        elif self.path == "/queue_play":
            if not sonos_contract.QUEUE_PLAY_REQUIRED <= set(body):
                self._send(400, {"error": "missing index"})
                return
            self._send(200, {"ok": True, "sought": True})
        elif self.path in ("/pause", "/resume", "/stop", "/seek",
                           "/volume", "/adopt"):
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not-found"})  # typo'd verbs die loud

    def log_message(self, *a):
        pass


srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeSidecar)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["VIBB_SONOS_API"] = f"http://127.0.0.1:{srv.server_port}"
sys.path.insert(0, os.path.join(REPO, "pi"))
import daemon  # noqa: E402
from vibb import renderer  # noqa: E402

PL = "https://open.spotify.com/playlist/PPP"
CTX = "spotify:playlist:PPP"
LISTING = {"ready": True, "length": 3, "cached": 3, "tracks": [
    {"uri": "spotify:track:a", "track": {
        "name": "Blue Monday", "artist_names": ["New Order"],
        "album_cover_url": "https://i.scdn.co/a"}},
    {"uri": "spotify:track:b", "track": {
        "name": "Enola Gay", "artist_names": ["OMD"],
        "album_cover_url": "https://i.scdn.co/b"}},
    {"uri": "spotify:track:c", "track": {
        "name": "Shout", "artist_names": ["Tears for Fears"],
        "album_cover_url": "https://i.scdn.co/c"}}]}
asked = []
daemon._spotify.context_tracks = lambda uri, timeout=5, settle_s=None: (
    asked.append(uri), LISTING)[1]
daemon.go = lambda *a, **k: b"{}"
daemon.go_status = lambda **k: {}
daemon.ORCH._mpv_alive = lambda: False
daemon._spotify.save_bookmark({
    "context_uri": CTX, "uri": "spotify:track:c", "position": 61000,
    "duration": 200000, "name": "Shout", "artists": ["Tears for Fears"],
    "artwork": "https://i.scdn.co/c"})
orch = daemon.ORCH
renderer.write("sonos", uid="RINCON_T", name="Stua")

# 1. transfer resumes the BOOKMARKED track at the bookmarked second —
#    one sharelink of the whole context (field bug a)
r = orch.sonos_start_target(PL)
plays = [b for p, b in FAKE["log"] if p == "/play"]
assert len(plays) == 1 and plays[0]["uri"] == PL, FAKE["log"]
assert plays[0]["track_index"] == 2, f"bookmark=track c=idx 2: {plays[0]}"
assert abs(plays[0]["start_s"] - 61.0) < 0.1, plays[0]
assert asked == [CTX], asked
assert [e["id"] for e in orch.sonos_queue] == [
    "spotify:track:a", "spotify:track:b", "spotify:track:c"]
assert orch.sonos_idx == 2 and orch.sonos_ctx == CTX
print("1. transfer resumes bookmarked track + second, one sharelink OK")

# 2. the card reads OUR list — the DIDL is never the primary (field bug b)
orch.sonos_snap = dict(sonos_contract.STATE_SHARE,
                       uid="RINCON_T", track_no=3,
                       track_title="Sh0ut (mangled DIDL)",
                       track_art="http://192.168.1.9:1400/getaa?x")
orch.sonos_snap_at = time.monotonic()
st = orch.status()
assert st["title"] == "Shout — Tears for Fears", st["title"]
assert st["artwork"] == "https://i.scdn.co/c", st["artwork"]
assert st["playing"] is True
print("2. card metadata from context_tracks, not the speaker's DIDL OK")

# 2b. guest playback (ours=False) is NOT playing for sharelink either
orch.sonos_snap = dict(orch.sonos_snap, ours=False,
                       foreign_uri="x-sonos-spotify:guest")
st = orch.status()
assert st["playing"] is False and st["renderer_state"] == "taken-over"
orch.sonos_snap = dict(orch.sonos_snap, ours=True, foreign_uri=None)
print("2b. a guest's spotify never reads as ours OK")

# 3. next/prev = /queue_play by index with wrap — never a new link
FAKE["log"].clear()
asked.clear()
orch.sonos_snap = dict(orch.sonos_snap, rel_s=2.0)  # prev<5s -> previous
r = orch.sonos_step(1)  # 2 -> wraps to 0; async worker posts it
assert r["index"] == 0, r
for _ in range(40):  # the coalescing worker runs off-thread now
    if FAKE["log"]:
        break
    time.sleep(0.05)
posts = [p for p, _ in FAKE["log"]]
assert posts == ["/queue_play"], FAKE["log"]
assert FAKE["log"][0][1]["index"] == 0, FAKE["log"]
assert asked == [], "next/prev must never re-fetch the listing"
assert orch.sonos_idx == 0
print("3. step wraps via /queue_play, no new sharelink OK")

# 4. bookmark hold: a refused seek must not let the poller overwrite the
#    good position (writes suppressed until playback passes it)
orch.sonos_bm_hold = ("spotify:track:a", 61.0)
orch.sonos_snap = dict(orch.sonos_snap, rel_s=3.0,
                       track_spotify_uri="spotify:track:a")
orch.sonos_snap_at = time.monotonic()
orch._sonos_bookmark_now(force=True)
bm = daemon._spotify.read_bookmark(CTX)
assert bm["uri"] == "spotify:track:c" and bm["position"] == 61000, \
    f"held bookmark was overwritten: {bm}"
orch.sonos_snap = dict(orch.sonos_snap, rel_s=90.0)
orch._sonos_bookmark_now(force=True)
bm = daemon._spotify.read_bookmark(CTX)
assert bm["uri"] == "spotify:track:a" and bm["position"] == 90000, bm
print("4. refused-seek hold protects the bookmark, then releases OK")

# 5. drift: untrusted map -> step re-transfers instead of index-jumping
FAKE["log"].clear()
orch.sonos_map_trusted = False
orch.sonos_step(1)
for _ in range(40):
    if FAKE["log"]:
        break
    time.sleep(0.05)
posts = [p for p, _ in FAKE["log"]]
assert posts == ["/play"], f"untrusted map must re-transfer: {posts}"
print("5. queue drift heals via fresh transfer on the next press OK")

# 6. unsupported spotify kinds refuse loudly — never a silent box-fallback
r = orch.sonos_start_target("spotify:user:kid:collection")
assert r.get("error") == "unsupported-on-sonos", r
print("6. Liked Songs/shows refuse with a named error OK")

print("\nSONOS SPOTIFY QUEUE OK — vibb owns the logic, the speaker "
      "holds the queue, and the bookmark survives every path.")

# 7. the step coalescer survives a raise — a mash storm queued verbs
#    past the sidecar timeout, _sonos_play_entry raised SidecarDown,
#    the worker thread died with _sonos_stepping still True, and every
#    later next/prev saw "a worker is running" and never spawned one:
#    next/prev DEAD until a daemon restart (field 2026-09-01, the son
#    mashing when the music did not start). The worker must drop the
#    failed step, keep consuming, and exit clean.
CALLS = []


def _boom_then_ok(want, start):
    CALLS.append(want)
    if len(CALLS) == 1:
        raise daemon._renderer.SidecarDown("timed out")


orch._sonos_play_entry = _boom_then_ok
orch._sonos_step_want = 1
orch._sonos_stepping = True
orch._sonos_step_worker()          # first want raises...
assert orch._sonos_stepping is False, \
    "a raise must never strand the stepping flag — next/prev bricks"
orch._sonos_step_want = 2
orch._sonos_stepping = True
orch._sonos_step_worker()          # ...and the machinery still works
assert CALLS == [1, 2], f"the failed step is dropped, not fatal: {CALLS}"
print("7. step coalescer survives SidecarDown, buttons never brick OK")

# 8. AM-102: under soloist the hand-off listing is a REMEMBERED one; when
#    the sidecar says it is not stale (complete + verified) and the speaker's
#    queue_len matches, the map is trusted -> next/prev are /queue_play jumps
del orch._sonos_play_entry     # case 7's fake shadowed the real method
FAKE["log"].clear()
LISTING["stale"] = False
LISTING["complete"] = True
orch.sonos_start_target(PL)
assert orch.sonos_map_trusted is True, "a fresh remembered listing is trusted"
FAKE["log"].clear()
orch.sonos_snap = dict(orch.sonos_snap, rel_s=2.0)
orch.sonos_step(1)
for _ in range(40):
    if FAKE["log"]:
        break
    time.sleep(0.05)
assert [p for p, _ in FAKE["log"]] == ["/queue_play"], FAKE["log"]
print("8. remembered-but-fresh listing: trusted, step = /queue_play OK")

# 9. ...and a STALE remembered listing (a 10-row start window, an old ledger)
#    stays untrusted even when the lengths happen to match — the drift path
#    (case 5) is the steady state there, never a wrong positional jump
FAKE["log"].clear()
LISTING["stale"] = True
orch.sonos_start_target(PL)
assert orch.sonos_map_trusted is False, "a stale listing is never trusted"
LISTING["stale"] = False
print("9. stale remembered listing: untrusted, re-transfer per press OK")

# 10. the card paints from OUR row whenever the index is CONFIRMED, even on
#     an untrusted map: the poller matched the playing track's uri to it
orch.sonos_start_target(PL)
orch.sonos_map_trusted = False
orch.sonos_pending = None
orch.sonos_idx = 1
orch.sonos_snap = dict(sonos_contract.STATE_SHARE, uid="RINCON_T",
                       track_no=2, track_spotify_uri="spotify:track:b",
                       track_title="En0la (DIDL)", track_artist="0MD",
                       track_art="http://192.168.1.9:1400/getaa?b")
orch.sonos_snap_at = time.monotonic()
st = orch.status()
assert st["title"] == "Enola Gay — OMD" and st["artwork"] == "https://i.scdn.co/b", \
    (st["title"], st["artwork"])
assert st["spotify"]["next_artwork"] == "https://i.scdn.co/c", st.get("spotify")
print("10. untrusted map + uri-confirmed index: card from our row, next cover prewarmed OK")

# 10b. the press itself confirms: during the 8 s settle the speaker still
#      reports the OLD uri, and the card must show where the kid is GOING
orch.sonos_idx = 2
orch.sonos_pending = ("spotify:track:c", time.monotonic())
st = orch.status()
assert st["title"] == "Shout — Tears for Fears", st["title"]
orch.sonos_pending = None
print("10b. optimistic press: card flips to the pressed row at once OK")

# 11. a track BEYOND the rows we know (a prefix listing): the index cannot
#     be confirmed -> the speaker's DIDL, never a stale row
orch.sonos_idx = 2
orch.sonos_snap = dict(orch.sonos_snap, track_spotify_uri="spotify:track:zz",
                       track_title="Unknown Twelve", track_artist="Someone",
                       track_art="http://192.168.1.9:1400/getaa?zz")
st = orch.status()
assert st["title"] == "Unknown Twelve" and st["artists"] == ["Someone"], st
assert st["artwork"] == "http://192.168.1.9:1400/getaa?zz", st["artwork"]
print("11. uri outside the known rows: DIDL fallback, no stale row OK")

# 12. the speaker's queue_len rides every snapshot: a session ADOPTED at
#     boot (no hand-off drift check) is untrusted the moment they disagree
orch.sonos_map_trusted = True
orch.sonos_snap = dict(orch.sonos_snap, track_spotify_uri="spotify:track:b",
                       queue_len=7, stale_s=0.5, ours=True)
daemon._sonos_follow_once = getattr(daemon, "_sonos_follow_once", None)
src = open(os.path.join(REPO, "pi", "daemon.py")).read()
body = src[src.index("def _sonos_poller"):]
assert 'qlen = snap.get("queue_len")' in body and "queue drift" in body, \
    "the poller must apply the queue_len drift check (adopt-at-boot parity)"
print("12. poller applies the queue_len drift check for adopted sessions (source pin) OK")

