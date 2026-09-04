#!/usr/bin/env python3
"""THE ENGINE CONTRACT — the go-librespot REST dialect as the box
actually speaks it (PLAN-soloistd.md: soloistd is a dialect-preserving
sidecar; PLAN-pipewire-soloist.md Phase 3).

Extracted 2026-09-04 from every call site in pi/. Two fakes will import
this file — the go-librespot fakes the suite already has, and the
stdlib fake WebSocket server that will drive the REAL soloistd — and
that is the "two-fakes trap" the plan named: if each fake carried its
own idea of the dialect they could both pass while disagreeing. So the
dialect is FROZEN here, and this file, run standalone, greps the tree
and fails when the code starts speaking something the contract does
not list. Grow it additively; never rename a field.

What the box sends (all JSON POST unless GET):
  GET  /status
  POST /player/play            {uri, skip_to_uri?, position?(ms)}
  POST /player/pause | /player/resume | /player/playpause
  POST /player/next | /player/prev
  POST /player/seek            {position (ms)}
  POST /player/volume          {volume (0..volume_steps)}
  POST /player/shuffle_context {shuffle_context (bool)}
  POST /player/output          {device (alsa pcm name)}   404 = too old
  GET  /context/tracks?uri=    -> {ready, cached, length, tracks[]}
                                  400 = not a listable uri
  GET  /cache/snapshot?uri=    -> {snapshot_id}
  POST /cache/download         {uri}

What the box reads from /status (nothing else is ever .get()'d):
  username, paused, stopped, volume, volume_steps, play_origin,
  shuffle_context, pending_track_uri,
  track{uri, name, artist_names, album_cover_url, album_name,
        position(ms), duration(ms)}
No /player/* RESPONSE body is parsed anywhere: only OSError (unreachable)
and HTTPError codes carry meaning. status() folds errors into {};
status_strict() raises. context_uri is NOT an engine field (it is the
bookmark file's key).
"""
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PI = os.path.join(REPO, "pi")

ENDPOINTS = {
    "/status": "GET",
    "/player/play": "POST", "/player/pause": "POST", "/player/resume": "POST",
    "/player/playpause": "POST", "/player/next": "POST", "/player/prev": "POST",
    "/player/seek": "POST", "/player/volume": "POST",
    "/player/shuffle_context": "POST", "/player/output": "POST",
    "/context/tracks": "GET", "/cache/snapshot": "GET", "/cache/download": "POST",
}
BODY = {
    "/player/play": {"uri", "skip_to_uri", "position"},
    "/player/seek": {"position"},
    "/player/volume": {"volume"},
    "/player/shuffle_context": {"shuffle_context"},
    "/player/output": {"device"},
    "/cache/download": {"uri"},
}
STATUS_FIELDS = {"username", "paused", "stopped", "volume", "volume_steps",
                 "play_origin", "shuffle_context", "pending_track_uri", "track"}
TRACK_FIELDS = {"uri", "name", "artist_names", "album_cover_url", "album_name",
                "position", "duration"}
LISTING_FIELDS = {"ready", "cached", "length", "tracks"}
LISTING_ITEM = {"uri", "track"}          # item.track carries TRACK_FIELDS
ERRORS = {"/context/tracks": {400}, "/player/output": {404}}
PLAY_ORIGIN_BOX = ("go-librespot", "", None)   # box-initiated playback


def sample_status(**over):
    """A /status the whole box accepts — the shape both fakes must emit."""
    st = {"username": "kid", "paused": False, "stopped": False,
          "volume": 32768, "volume_steps": 65535, "play_origin": "go-librespot",
          "shuffle_context": False, "pending_track_uri": None,
          "track": {"uri": "spotify:track:x", "name": "T", "artist_names": ["A"],
                    "album_cover_url": "https://i/x.jpg", "album_name": "L",
                    "position": 12000, "duration": 180000}}
    st.update(over)
    return st


def sample_listing(uris):
    return {"ready": True, "cached": True, "length": len(uris),
            "tracks": [{"uri": u, "track": {"name": f"T{i}", "artist_names": ["A"],
                                            "album_cover_url": None, "duration": 180000}}
                       for i, u in enumerate(uris)]}


def _sources():
    for root, _d, files in os.walk(PI):
        for f in files:
            if f.endswith(".py"):
                yield open(os.path.join(root, f), encoding="utf-8").read()


def selfcheck():
    src = "\n".join(_sources())
    # 1. every engine path the code names is in the contract
    # GET paths are built as API + "/context/tracks?uri=" — allow a query
    named = set(re.findall(r'"(/(?:player|context|cache)/[a-z_]+|/status)[?"]', src))
    unknown = named - set(ENDPOINTS)
    assert not unknown, f"the code speaks endpoints the contract does not list: {unknown}"
    # 2. every contract endpoint is actually used (no dead contract)
    unused = set(ENDPOINTS) - named
    assert not unused, f"contract lists endpoints nobody calls: {unused}"
    # 3. body keys: every literal body key sent is in the contract
    for path, keys in BODY.items():
        for m in re.finditer(re.escape(f'"{path}"') + r"[^\n]*?body=\{([^}]*)\}", src):
            sent = set(re.findall(r'"([a-z_]+)":', m.group(1)))
            assert sent <= keys, f"{path} sends {sent - keys} not in the contract"
    # 4. the play_origin box value survives (the phone-clobber guard)
    assert 'origin != "go-librespot"' in src
    print("contract self-check: endpoints, bodies, origin value OK")
    return True


if __name__ == "__main__":
    sys.exit(0 if selfcheck() else 1)
