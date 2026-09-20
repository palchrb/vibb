#!/usr/bin/env python3
"""Gate the spotify pre-cache politeness (snapshot gating): playlists
re-queue only when Spotify's snapshot_id changed (one light Web API
call — against go-librespot's own session since fork v0.0.4, so no
Web API credentials are needed), immutable content queues
exactly ONCE, artists always re-queue (they change invisibly), and
everything fails open — no credentials/offline behaves like before the
gate existed. Turning an entry's cache OFF prunes its state so turning
it back ON re-downloads once (the parent's 'download again' lever)."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = tempfile.mkdtemp()
os.environ["VIBB_STATE"] = STATE
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_LIBRARY"] = os.path.join(STATE, "lib.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

from vibb import library as lib  # noqa: E402

SNAP = ["snap-1"]


def fake_snapshot(uri):
    if isinstance(SNAP[0], Exception):
        raise SNAP[0]
    return SNAP[0]


lib.spotify.snapshot = fake_snapshot
PL = "spotify:playlist:AAA"
AL = "spotify:album:BBB"
AR = "spotify:artist:CCC"

# 1. fresh playlist: due; after done, the SAME snapshot is not due
assert lib._precache_due(PL) is True
lib._precache_done(PL)
assert lib._precache_due(PL) is False
print("1. unchanged playlist snapshot skips the re-queue OK")

# 2. the playlist changes (new snapshot) -> due again, then quiet again
SNAP[0] = "snap-2"
assert lib._precache_due(PL) is True
lib._precache_done(PL)
assert lib._precache_due(PL) is False
print("2. edited playlist (new snapshot) re-queues once OK")

# 3. album: due exactly once, never re-checked (no Web API call at all)
CALLS = []
lib.spotify.snapshot = lambda uri: CALLS.append(uri)
assert lib._precache_due(AL) is True
lib._precache_done(AL)
assert lib._precache_due(AL) is False
assert CALLS == [], "immutable content must never hit the snapshot API"
lib.spotify.snapshot = fake_snapshot
print("3. album queues once, zero API calls OK")

# 4. artist contexts always re-queue (their tracklist changes invisibly)
assert lib._precache_due(AR) is True
lib._precache_done(AR)
assert lib._precache_due(AR) is True
print("4. artist context always re-queues OK")

# 5. fail open: snapshot fetch raises (go-librespot down / offline) -> due,
# exactly the pre-gate behavior; go-librespot's cached-track skip makes
# the extra POST nearly free
SNAP[0] = OSError("go-librespot unreachable")
assert lib._precache_due(PL) is True
SNAP[0] = "snap-2"
print("5. go-librespot down/offline fails open (still queues) OK")

# 6. prune: cache toggled OFF forgets the state -> back ON re-queues
lib._precache_prune({PL})  # album no longer cache-enabled
assert lib._precache_due(AL) is True, "off->on must re-download once"
assert lib._precache_due(PL) is False, "still-enabled playlist unaffected"
print("6. prune on cache-off re-arms the entry OK")

# 7. corrupt state file fails open
with open(lib.PRECACHE_STATE, "w") as f:
    f.write("{broken")
assert lib._precache_due(PL) is True
print("7. corrupt state file fails open OK")

# 8. AM-99: a stamp is re-asked after PRECACHE_RECHECK_S — under go-librespot
#    'done' only ever meant 'queued', so a download cut by a restart never
#    came back for an album (once EVER) or a playlist (until edited)
import time
lib.spotify.snapshot = fake_snapshot
lib._precache_done(AL)
lib._precache_done(PL)
assert lib._precache_due(AL) is False and lib._precache_due(PL) is False, "fresh stamps"
lib.PRECACHE_RECHECK_S = 0.2
time.sleep(0.3)
assert lib._precache_due(AL) is True, "an album is re-asked once its stamp is old"
assert lib._precache_due(PL) is True, "an unchanged playlist too, once its stamp is old"
lib.PRECACHE_RECHECK_S = 24 * 3600
lib._precache_done(AL)
assert lib._precache_due(AL) is False
print("8. an old stamp is re-asked, a fresh one is not (album + playlist) OK")

# 9. a legacy stamp (bare "queued" / snapshot string, no age) reads as due
st = lib._precache_state(); st[AL] = "queued"; lib._precache_save(st)
assert lib._precache_due(AL) is True, "unknown age = due"
print("9. legacy 'queued' stamps are re-asked once OK")

# 10. a show gets new episodes: always due, like a playlist
SH = "spotify:show:s1"
lib._precache_done(SH)
assert lib._precache_due(SH) is True
print("10. shows are always due OK")

print("SPOT PRECACHE GATE OK — polite to Spotify, immutable queues once, "
      "and the gate can only ever fail open.")
