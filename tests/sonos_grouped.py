#!/usr/bin/env python3
"""Stage B1 — grouped-away hygiene (architect+QA rounds 2026-08-23).

The scenario: someone pulls the box's speaker into ANOTHER group as a
member; the speaker's own AVTransport then names the coordinator
(x-rincon:<uid>) and carries someone else's audio. What must hold:

1. detection is INSTANT: the x-rincon signature sets grouped_away +
   coordinator on the SAME poll (the aux topology fetch lags minutes);
   x-rincon-mp3radio:// must not false-positive; a real track uri
   clears; an EMPTY uri decides nothing.
2. status() can actually SAY it: grouped-away is ordered before
   taken-over (the x-rincon foreign_uri used to shadow it completely),
   and the card's position is ours-gated (a member's RelTime is the
   parent's, painting it under the kid's book title was nonsense).
3. the three verb leaks are closed: pause() on card removal, unpause()
   on re-insert and _sonos_on_term's stop must NOT command a speaker
   carrying someone else's stream — a forwarding firmware would
   pause/stop the parent's whole group.
"""
import json
import os
import sys
import tempfile
import time
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
for k in ("VIBB_RUN", "VIBB_STATE", "VIBB_CACHE"):
    os.environ[k] = TMP
os.environ["VIBB_SETTINGS"] = os.path.join(TMP, "settings.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import sonosd  # noqa: E402


class FakeAV:
    def __init__(self, uri, tr, rel="0:07:11", dur="0:24:13"):
        self.uri, self.tr, self.rel, self.dur = uri, tr, rel, dur

    def GetPositionInfo(self, args):
        return {"TrackURI": self.uri, "RelTime": self.rel,
                "TrackDuration": self.dur, "TrackMetaData": "<x/>"}

    def GetTransportInfo(self, args):
        return {"CurrentTransportState": self.tr}


def classify(sess, uri, tr="PLAYING"):
    spk = types.SimpleNamespace(avTransport=FakeAV(uri, tr),
                                group=None, volume=30,
                                ip_address="10.0.0.5")
    sess._aux_n = 99   # keep the aux sub-cadence out of the way
    return sess._classify(spk)


# --- 1. instant grouped-away detection in the sidecar -----------------------
sess = sonosd.Session()
sess.uid, sess.kind = "RINCON_KID", "url"
sess.uri = "https://podkast.example/ep12.mp3"

f = classify(sess, "x-rincon:RINCON_PARENT")
assert f["ours"] is False
assert f["grouped_away"] is True, "the x-rincon uri IS the signal — same poll"
assert f["coordinator"] == "RINCON_PARENT", f
print("1. x-rincon member uri -> grouped_away + coordinator, instantly OK")

# 1b. the radio prefix must not false-positive
f = classify(sess, "x-rincon-mp3radio://stream.example/radio")
assert f["grouped_away"] is False and f["coordinator"] is None, \
    "x-rincon-mp3radio:// is a STREAM, not a membership"
print("1b. x-rincon-mp3radio does not read as grouped OK")

# 1c. a real track uri clears; an empty uri decides nothing
classify(sess, "x-rincon:RINCON_PARENT")
f = classify(sess, "", tr="STOPPED")
assert f["grouped_away"] is True, "an empty uri must not clear the state"
f = classify(sess, sess.uri)
assert f["grouped_away"] is False and f["coordinator"] is None, \
    "our own track back on the transport clears the membership"
print("1c. empty uri holds, a real uri clears OK")

# --- 2. the daemon can say it, and stops painting the parent's position -----
import daemon  # noqa: E402

daemon.go_status = lambda **k: {}
orch = daemon.ORCH

POSTS = []
daemon._renderer.read = lambda: {"renderer": "sonos", "uid": "RINCON_KID",
                                 "name": "Barnerom"}
daemon._renderer.post = lambda path, body=None, timeout=None: \
    POSTS.append((path, body)) or (200, {"ok": True})
daemon._renderer.is_sonos = lambda: True

orch.source, orch.target = "sonos", "https://podkast.example/feed.xml"
orch.sonos_idx = 0
GROUPED = {"armed": True, "uid": "RINCON_KID", "kind": "url", "seq": 5,
           "stale_s": 1.0, "reachable": True, "transport": "PLAYING",
           "rel_s": 1234.0, "dur_s": 4000.0,
           "uri": "x-rincon:RINCON_PARENT", "ours": False,
           "foreign_uri": "x-rincon:RINCON_PARENT",
           "grouped_away": True, "coordinator": "RINCON_PARENT",
           "lost_session": False, "volume": 30}
orch.sonos_snap = dict(GROUPED)
orch.sonos_snap_at = time.monotonic()

st = orch.status()
assert st["renderer_state"] == "grouped-away", \
    f"grouped-away must not be shadowed by taken-over: {st['renderer_state']}"
assert st["playing"] is False
assert st["position"] is None, \
    "the member's RelTime is the PARENT's — never paint it"

# ...and a genuine takeover still reads as taken-over
orch.sonos_snap = dict(GROUPED, grouped_away=False, coordinator=None,
                       uri="x-sonos-spotify:guest",
                       foreign_uri="x-sonos-spotify:guest")
orch.sonos_snap_at = time.monotonic()
assert orch.status()["renderer_state"] == "taken-over"
print("2. grouped-away unshadowed; position ours-gated OK")

# --- 3. the verb leaks are closed -------------------------------------------
orch.sonos_snap = dict(GROUPED)   # ours False
orch.sonos_snap_at = time.monotonic()
POSTS.clear()
r = orch.pause()
assert r == {"paused": []} and POSTS == [], \
    f"card removal must not pause the parent's group: {r}, {POSTS}"
r = orch.unpause()
assert r == {"resumed": []} and POSTS == [], \
    f"card re-insert must not resume the parent's group: {r}, {POSTS}"

orch._sonos_refresh_live = lambda: None
daemon.load_settings = lambda: {}
daemon._sonos_on_term()
assert POSTS == [], \
    f"an install-restart must not stop the parent's group: {POSTS}"
print("3. pause/unpause/term never command someone else's stream OK")

# 3b. ...and a session that IS ours still gets every verb
orch.sonos_snap = dict(GROUPED, ours=True, foreign_uri=None,
                       grouped_away=False, coordinator=None,
                       uri="https://podkast.example/ep12.mp3")
orch.sonos_snap_at = time.monotonic()
POSTS.clear()
orch.pause()
orch.unpause()
daemon._sonos_on_term()
assert [p for p, _b in POSTS] == ["/pause", "/resume", "/stop"], \
    f"ours-True must keep today's behavior verbatim: {POSTS}"
print("3b. an ours session still pauses/resumes/stops OK")

print("\nSONOS GROUPED OK — the box sees the membership instantly, says "
      "it honestly, and never drives a stream that is not its own.")
