#!/usr/bin/env python3
"""Gate the spotify song picker (fork v0.1.2 GET /context/tracks).

Hold-Y in now-playing lists a playing playlist's songs — same picker as
podcasts — and picking one starts it via /play {id, episode:<track uri>}
-> player.py --episode -> /player/play {uri, skip_to_uri}. Contract:

- expand_target(tracks=True) maps the fork's listing to episode rows
  (id/url = track uri, title = "name — artists", image = cover url);
  sweep-pending entries (track: null) are skipped, not rendered blank.
- tracks stays OPT-IN: without it a spotify entry expands to the same
  leaf card as before — a browse tap must PLAY, never open a list.
- albums list too (v0.1.2); a non-listable uri (HTTP 400) and a
  pre-v0.1.2 fork (404) degrade to the leaf card, no crash.
- v0.1.7: the endpoint answers WITHOUT waiting on the network, so the
  first call for an unknown context is ready=false + empty. The
  wrapper polls until the listing is renderable — rendered straight it
  opened the picker on nothing.
- play_spotify(start_uri=...) plays {uri, skip_to_uri=<pick>} from the
  top: no position, and the bookmark is neither read nor cleared."""
import io
import os
import sys
import tempfile
import time
import urllib.error

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_LIBRARY"] = os.path.join(os.environ["VIBB_STATE"],
                                            "lib.json")
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(REPO, "pi"))

from vibb import library, spotify  # noqa: E402

PL = "https://open.spotify.com/playlist/0hgSZmY9xhzx51hlLB2arI"
LISTING = {"uri": "spotify:playlist:0hgSZmY9xhzx51hlLB2arI",
           "ready": True, "length": 3, "cached": 2,
           "tracks": [
               {"uri": "spotify:track:a",
                "track": {"name": "Blue Monday '88",
                          "artist_names": ["New Order"],
                          "album_cover_url": "https://i.scdn.co/a"}},
               {"uri": "spotify:track:b", "track": None},  # sweep pending
               {"uri": "spotify:track:c",
                "track": {"name": "Shout", "artist_names": []}},
           ]}

REAL_CONTEXT_TRACKS = spotify.context_tracks  # restored for section 5

asked = []
spotify.context_tracks = lambda uri, timeout=5: (asked.append(uri),
                                                  LISTING)[1]
library.spotify.context_tracks = spotify.context_tracks

# 1. tracks=True: fork listing -> picker rows; null-track rows skipped
r = library.expand_target(PL, name="80s", tracks=True)
assert r["kind"] == "spotify"
assert asked == ["spotify:playlist:0hgSZmY9xhzx51hlLB2arI"], asked
eps = r["episodes"]
assert [e["id"] for e in eps] == ["spotify:track:a", "spotify:track:c"], eps
assert eps[0]["title"] == "Blue Monday '88 — New Order", eps[0]
assert eps[0]["image"] == "https://i.scdn.co/a"
assert eps[1]["title"] == "Shout", eps[1]  # no artists: bare name
assert r["pending"] is True, \
    "cached < length = metadata sweep still filling -> pending"
print("1. tracks=True: fork listing mapped to picker rows OK")

# 1b. albums (v0.1.2): same mapping
AL = "https://open.spotify.com/album/4rxfprnLYz3592ZGaeqcON"
ALBUM = {"uri": "spotify:album:4rxfprnLYz3592ZGaeqcON",
         "ready": True, "length": 1, "cached": 1,
         "tracks": [{"uri": "spotify:track:d",
                     "track": {"name": "Un poco loco",
                               "artist_names": ["Coco"],
                               "album_cover_url": "https://i.scdn.co/d"}}]}
library.spotify.context_tracks = lambda uri, timeout=5: ALBUM
r = library.expand_target(AL, name="Coco", tracks=True)
assert [e["id"] for e in r["episodes"]] == ["spotify:track:d"], r["episodes"]
assert r["pending"] is False, "a complete listing is not pending"
# still enumerating (settle timed out): empty rows, but SAY so — the
# screen used to read this as 'no list exists' and the PWA pinned an
# empty queue card (architect review 2026-08-03)
library.spotify.context_tracks = lambda uri, timeout=5: {
    "uri": "spotify:album:cold", "ready": False, "length": 0,
    "cached": 0, "tracks": []}
r = library.expand_target(AL, name="Cold", tracks=True)
assert r["episodes"] == [] and r["pending"] is True, r
library.spotify.context_tracks = spotify.context_tracks
print("1b. album listing maps the same way; not-ready is pending OK")

# 2. default (browse) expansion: unchanged leaf card, fork NOT queried
asked.clear()
r = library.expand_target(PL, name="80s")
assert r["episodes"] == [] and asked == [], (r["episodes"], asked)
print("2. tracks omitted: leaf card, fork not queried OK")

# 3. albums (400) / old fork (404) / api down: leaf card, no crash


def _boom(uri, timeout=5):
    raise urllib.error.HTTPError("u", 400, "not listable", {},
                                 io.BytesIO(b""))


library.spotify.context_tracks = _boom
r = library.expand_target(PL, tracks=True)
assert r["episodes"] == [], r["episodes"]
library.spotify.context_tracks = \
    lambda uri, timeout=5: (_ for _ in ()).throw(OSError("down"))
r = library.expand_target(PL, tracks=True)
assert r["episodes"] == [], r["episodes"]
# a session drop mid-listing answers 204/empty body -> json.loads(b"")
# raises ValueError, which must degrade the same way (QA 2026-08-03:
# it escaped the OSError-only catch and 502'd the /expand route)
library.spotify.context_tracks = \
    lambda uri, timeout=5: (_ for _ in ()).throw(ValueError("empty body"))
r = library.expand_target(PL, tracks=True)
assert r["episodes"] == [], r["episodes"]
assert r["pending"] is False, \
    "a FAILURE must not be pending — retrying a 400 forever is wrong"
print("3. artist/old-fork/down/204-mid-poll: degrades to the leaf card OK")

# 4. play_spotify(start_uri): {uri, skip_to_uri}, from the top, bookmark
#    untouched
import player  # noqa: E402

BODY = {}
bookmark_calls = []
player.radio.touch_busy = lambda: None
player.radio.wait_paging_clear = lambda: None
# the fork answers /player/play once the context is resolved (v0.2.3);
# the pick is then the track that lands
PLAYED = {"track": {}}
player.spotify.status = lambda timeout=5: {"username": "u",
                                           "track": PLAYED["track"]}
player.spotify.read_bookmark = \
    lambda uri: bookmark_calls.append(("read", uri))
player.spotify.clear_bookmark = \
    lambda uri: bookmark_calls.append(("clear", uri))


def _go(path, timeout=15, body=None):
    if path == "/player/play":
        BODY.update(body)
        PLAYED["track"] = {"uri": body["skip_to_uri"], "position": 0}
    return b"{}"


player.spotify.go = _go
player.play_spotify(PL, start_uri="spotify:track:c")
assert BODY.get("skip_to_uri") == "spotify:track:c", BODY
assert "position" not in BODY, f"a pick starts from the top: {BODY}"
assert bookmark_calls == [], \
    f"a pick must neither read nor clear the bookmark: {bookmark_calls}"
print("4. picked track: /player/play {uri, skip_to_uri}, bookmark alone OK")

# 5. the v0.1.7 settle contract. The endpoint no longer waits on the
#    network: the first call for an unknown context is ready=false with
#    an EMPTY listing, so rendering the first answer opened the picker
#    on nothing. context_tracks polls until the listing is renderable.
import json as _json  # noqa: E402
import urllib.request as _ureq  # noqa: E402


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _serve(pages):
    """urlopen stub replaying one JSON body per call (last one repeats)."""
    calls = []

    def _open(url, timeout=None):
        calls.append(url)
        page = pages[min(len(calls) - 1, len(pages) - 1)]
        return _Resp(_json.dumps(page).encode())
    return _open, calls


spotify.context_tracks = REAL_CONTEXT_TRACKS  # sections 1-3 stubbed it out
spotify.CONTEXT_SETTLE_S = 3.0
_ureq.urlopen, calls = _serve([
    {"uri": "spotify:album:x", "ready": False, "length": 0,
     "cached": 0, "tracks": []},                    # enumerating
    {"uri": "spotify:album:x", "ready": True, "length": 2,
     "cached": 0, "tracks": [{"uri": "t1", "track": None},
                             {"uri": "t2", "track": None}]},  # meta filling
    {"uri": "spotify:album:x", "ready": True, "length": 2, "cached": 2,
     "tracks": [{"uri": "t1", "track": {"name": "A"}},
                {"uri": "t2", "track": {"name": "B"}}]},      # done
])
d = spotify.context_tracks("spotify:album:x")
assert d["cached"] == 2 and len(calls) == 3, (d, calls)
print("5. not-ready and metadata-filling answers are polled through OK")

# 5b. a ready-but-EMPTY listing (a show: episodes are omitted) returns
#     at once — there is nothing to wait for
_ureq.urlopen, calls = _serve([{"uri": "spotify:show:s", "ready": True,
                                "length": 0, "cached": 0, "tracks": []}])
d = spotify.context_tracks("spotify:show:s")
assert d["length"] == 0 and len(calls) == 1, (d, calls)
print("5b. ready-but-empty (show) returns immediately OK")

# 5c. version skew: a pre-v0.1.7 binary has no 'ready' field, and must
#     behave exactly as before instead of polling out the whole budget
_ureq.urlopen, calls = _serve([{"uri": "spotify:playlist:p", "length": 1,
                                "cached": 1,
                                "tracks": [{"uri": "t", "track": {}}]}])
d = spotify.context_tracks("spotify:playlist:p")
assert len(calls) == 1, f"old fork must not be polled: {calls}"
print("5c. pre-v0.1.7 binary (no 'ready' field) is not polled OK")

# 5d. a context that never settles is bounded — the picker's 'Fetching
#     episodes ...' frame must not outlive the UI's own budget
_ureq.urlopen, calls = _serve([{"uri": "spotify:playlist:q", "ready": False,
                                "length": 0, "cached": 0, "tracks": []}])
spotify.CONTEXT_SETTLE_S = 0.5
t0 = time.monotonic()
d = spotify.context_tracks("spotify:playlist:q")
took = time.monotonic() - t0
assert 0.4 < took < 2.0, f"settle wait unbounded/absent: {took:.2f}s"
assert d["ready"] is False, "the last answer is still returned"
print("5d. a never-settling context is bounded by settle_s OK")

# 6. the Liked Songs collection (fork v0.1.9): the URI form must parse
#    — it is the gate for play, bookmarks, pre-cache and the listing.
#    No share link exists for it, so the URI is what a card carries.
assert spotify.to_uri("spotify:user:palchrb:collection") == \
    "spotify:user:palchrb:collection"
assert spotify.to_uri("spotify:user:11x.y-z_9:collection") == \
    "spotify:user:11x.y-z_9:collection", "user ids carry ./-/_"
assert spotify.is_spotify("spotify:user:palchrb:collection")
# ...and the shapes that must NOT sneak through the new alternative
assert spotify.to_uri("spotify:user:palchrb:playlist") is None
assert spotify.to_uri("spotify:user::collection") is None
# a collection bookmark file is hash-named — any URI shape is safe
assert spotify.bm_path("spotify:user:palchrb:collection").endswith(".json")
print("6. Liked Songs collection URI parses (and impostors do not) OK")

print("\nall spot_track_picker checks passed")
