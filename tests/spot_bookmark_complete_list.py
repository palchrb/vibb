#!/usr/bin/env python3
"""AM-95 (6): under soloist the bookmark rule (AM-75, 'a track no longer in
the list -> clean start') may only judge against a list proven COMPLETE.
After one play the sidecar's store holds a single ~20-row window; a
bookmark at row 40 must survive it.

  1. partial list (complete False), bookmark uri outside it -> accepted
  2. complete list, uri outside it -> rejected (clean start)
  3. complete list, uri inside it -> accepted
  4. empty list -> accepted (nothing known yet)
"""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(REPO, "pi"))
import player  # noqa: E402

player.paths.GO_UNIT = "vibb-soloistd"
CTX = "spotify:playlist:p"
BM = {"context_uri": CTX, "uri": "spotify:track:t40", "position": 60000}
LISTING = [{}]
player.spotify.context_tracks = lambda uri, **k: LISTING[0]

LISTING[0] = {"tracks": [{"uri": f"spotify:track:t{i}"} for i in range(20)], "complete": False, "stale": True}
assert player.accept_spot_bookmark(dict(BM), CTX) is not None, "a partial window must not reject"
print("1. partial list: the deep bookmark survives OK")

LISTING[0] = {"tracks": [{"uri": f"spotify:track:t{i}"} for i in range(20)], "complete": True}
assert player.accept_spot_bookmark(dict(BM), CTX) is None, "complete list without the track -> clean start"
print("2. complete list, track gone: clean start OK")

LISTING[0] = {"tracks": [{"uri": f"spotify:track:t{i}"} for i in range(60)], "complete": True}
assert player.accept_spot_bookmark(dict(BM), CTX) is not None
print("3. complete list, track present: accepted OK")

LISTING[0] = {"tracks": [], "complete": False}
assert player.accept_spot_bookmark(dict(BM), CTX) is not None
print("4. nothing known yet: accepted OK")
print("\nall spot_bookmark_complete_list checks passed")
