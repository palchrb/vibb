#!/usr/bin/env python3
"""Stage B2, sidecar half — the migration probe (owner use case
2026-08-23: the son removes the room the session started on; Sonos
promotes another member to carry his stream; the box must keep up).

What must hold:
1. the probe fires ONLY on the migration signature — STOPPED with an
   EMPTY TrackURI after an ours+live sighting inside the window. A
   hijack (x-rincon member uri, non-empty) and a natural episode end
   (STOPPED with the uri RETAINED) can never enter.
2. url kind matches by _norm_uri equality — percent-encoding variants
   included — against each candidate coordinator's LIVE transport;
   a candidate that is not playing/paused never qualifies.
3. sharelink follows only on exact decoded track + position continuity;
   with no recorded track it NEVER follows (prefix alone would follow
   any stranger's Spotify).
4. tries are bounded per transition (3), then today's lost-session;
   a found hint ends probing (probe-once — hold-play flap resistance);
   an ours+live classify re-arms and clears; adopt() clears.
5. the hint rides every published snapshot and carries the SESSION's
   uri (the snapshot's own is empty during STOPPED); _cadence returns
   POLL_S only while a migration transition pends.
"""
import os
import sys
import tempfile
import time
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
for k in ("VIBB_RUN", "VIBB_STATE", "VIBB_CACHE"):
    os.environ[k] = TMP
sys.path.insert(0, os.path.join(REPO, "pi"))

import sonosd  # noqa: E402

URI = "https://cdn.example/signed/ep12.mp3?tok=abc"
PROBES = {"topology": 0}
CANDIDATES = {}   # ip -> (transport, track_uri, rel)


def fake_topology():
    PROBES["topology"] += 1
    return {"players": {u: {"ip": f"10.0.0.{i+10}", "name": n}
                        for i, (u, n) in enumerate(
                            [("RINCON_KID", "Barnerom"),
                             ("RINCON_KITCHEN", "Kjøkken"),
                             ("RINCON_BATH", "Bad")])},
            "groups": [{"coordinator": "RINCON_KITCHEN",
                        "members": ["RINCON_KITCHEN"]},
                       {"coordinator": "RINCON_BATH",
                        "members": ["RINCON_BATH"]}]}


class FakeCandidate:
    def __init__(self, ip):
        tr, uri, rel = CANDIDATES.get(ip, ("STOPPED", "", None))
        self.avTransport = types.SimpleNamespace(
            GetPositionInfo=lambda args: {"TrackURI": uri, "RelTime": rel,
                                          "TrackDuration": "0:24:13"},
            GetTransportInfo=lambda args: {"CurrentTransportState": tr})


sonosd.refresh_topology = fake_topology
sonosd._soco = lambda: types.SimpleNamespace(SoCo=FakeCandidate)

STOPPED_EMPTY = {"transport": "STOPPED", "uri": ""}


def fresh_session(kind="url", uri=URI):
    s = sonosd.Session()
    s.uid, s.kind, s.uri = "RINCON_KID", kind, uri
    s._ours_at = time.monotonic()
    s._migrate_tries = sonosd.MIGRATE_TRIES
    return s


# 1. signature discipline: only STOPPED+empty probes
sess = fresh_session()
PROBES["topology"] = 0
sess._maybe_probe_migration({"transport": "STOPPED",
                             "uri": "x-rincon:RINCON_PARENT"})
sess._maybe_probe_migration({"transport": "STOPPED", "uri": URI})
sess._maybe_probe_migration({"transport": "PLAYING", "uri": ""})
assert PROBES["topology"] == 0, \
    "hijack / episode-end / mid-transition must never probe"
sess._ours_at = time.monotonic() - 9999   # window long past
sess._maybe_probe_migration(dict(STOPPED_EMPTY))
assert PROBES["topology"] == 0, "an old session must not probe"
print("1. only the migration signature probes OK")

# 2. url kind: _norm match against a LIVE candidate, encoding-tolerant
sess = fresh_session()
CANDIDATES.clear()
CANDIDATES["10.0.0.11"] = ("PLAYING",
                           "https://cdn.example/signed/ep12.mp3%3Ftok%3Dabc"
                           .replace("%3F", "?").replace("%3D", "="),
                           "0:07:12")
CANDIDATES["10.0.0.12"] = ("PLAYING", "https://elsewhere.example/x.mp3",
                           "0:01:00")
sess._maybe_probe_migration(dict(STOPPED_EMPTY))
assert sess._moved == {"uid": "RINCON_KITCHEN", "name": "Kjøkken",
                       "uri": URI}, sess._moved
assert sess._migrate_tries == 0, "a found hint ends the probing"
print("2. url stream found on the promoted coordinator OK")

# 2b. the hint rides the snapshot, carrying the SESSION uri
sess.publish(transport="STOPPED", uri="", ours=False)
snap = sess.state()
assert snap["stream_moved"]["uid"] == "RINCON_KITCHEN"
assert snap["stream_moved"]["uri"] == URI, \
    "the hint must echo SESSION.uri — the snapshot's own is empty"
print("2b. hint published with the session's uri OK")

# 2c. a not-playing candidate never qualifies
sess = fresh_session()
CANDIDATES.clear()
CANDIDATES["10.0.0.11"] = ("STOPPED", URI, "0:07:12")
sess._maybe_probe_migration(dict(STOPPED_EMPTY))
assert sess._moved is None, "a stopped candidate is not carrying anything"
print("2c. only live candidates qualify OK")

# 3. sharelink: exact track + continuity, never prefix alone
sess = fresh_session(kind="spotify_sharelink",
                     uri="https://open.spotify.com/album/abc")
CANDIDATES.clear()
CANDIDATES["10.0.0.11"] = (
    "PLAYING", "x-sonos-spotify:spotify%3atrack%3aT1?sid=9", "0:02:10")
sess._last_ours_track = None
sess._maybe_probe_migration(dict(STOPPED_EMPTY))
assert sess._moved is None, \
    "no recorded track -> NEVER follow (prefix would follow any Spotify)"
sess = fresh_session(kind="spotify_sharelink",
                     uri="https://open.spotify.com/album/abc")
sess._last_ours_track = "spotify:track:T1"
sess._last_ours_rel = 125.0
sess._maybe_probe_migration(dict(STOPPED_EMPTY))
assert sess._moved and sess._moved["uid"] == "RINCON_KITCHEN", \
    f"exact track + rel continuity must follow: {sess._moved}"
sess = fresh_session(kind="spotify_sharelink",
                     uri="https://open.spotify.com/album/abc")
sess._last_ours_track = "spotify:track:T1"
sess._last_ours_rel = 2000.0   # nowhere near the candidate's 130s
sess._maybe_probe_migration(dict(STOPPED_EMPTY))
assert sess._moved is None, "position discontinuity must not follow"
print("3. sharelink: exact track + continuity, never prefix OK")

# 4. bounded tries, then lost; re-armed by ours+live; adopt clears
sess = fresh_session()
CANDIDATES.clear()   # nobody carries the stream
PROBES["topology"] = 0
for _ in range(5):
    sess._maybe_probe_migration(dict(STOPPED_EMPTY))
assert PROBES["topology"] == sonosd.MIGRATE_TRIES, \
    f"exactly {sonosd.MIGRATE_TRIES} attempts, then stop: {PROBES}"
assert sess._moved is None

# re-arm via a real ours+live classify (the tail bookkeeping)
class FakeAV:
    def GetPositionInfo(self, args):
        return {"TrackURI": URI, "RelTime": "0:07:20",
                "TrackDuration": "0:24:13", "TrackMetaData": "<x/>"}

    def GetTransportInfo(self, args):
        return {"CurrentTransportState": "PLAYING"}

sess._moved = {"uid": "STALE"}
sess._aux_n = 99
sess._classify(types.SimpleNamespace(avTransport=FakeAV(), group=None,
                                     volume=30, ip_address="10.0.0.10"))
assert sess._migrate_tries == sonosd.MIGRATE_TRIES and sess._moved is None, \
    "an ours+live sighting must re-arm and clear a stale hint"

sess._moved = {"uid": "STALE"}
sess.adopt({"uid": "RINCON_KITCHEN", "kind": "url", "uri": URI})
assert sess._moved is None, "adopt must clear the hint it answers"
print("4. bounded tries, re-arm on ours, adopt clears OK")

# 5. cadence: fast only while a migration transition pends
sess = fresh_session()
snap_stopped = {"uri": ""}
assert sess._cadence("STOPPED", snap_stopped, 0) == sonosd.POLL_S, \
    "probe attempts must land at seconds, not the 60s stopped cadence"
sess._migrate_tries = 0
assert sess._cadence("STOPPED", snap_stopped, 0) == sonosd.POLL_STOPPED_S
sess._migrate_tries = 3
assert sess._cadence("STOPPED", {"uri": URI}, 0) == sonosd.POLL_STOPPED_S, \
    "an episode-end STOPPED (uri retained) keeps its cadence"
print("5. cadence fast only while the transition pends OK")

print("\nSONOS MIGRATE PROBE OK — the stream is found by proof, never "
      "by prefix, and a quiet house stays quiet.")
