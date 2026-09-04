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


# --- the OTHER side: Soloist's WebSocket dialect (docs 2026-09-04) ----------
# Envelope: {"type": "command", "command": <name>, **fields}; replies are
# {"type": "command_result", "command": ..} or {"type": "error",
# "message": ..}; everything else is an event. Control commands are
# accepted ASYNCHRONOUSLY — confirmation is the state-change event, never
# the command_result (the spike's cmd()/wait_for pattern).
WS_COMMANDS = {
    "get_auth_state": set(), "get_state": set(), "get_queue": {"limit"},
    "play": {"uri"},                # bare play = resume; uri = start a context
    "pause": set(), "skip_next": set(), "skip_prev": set(),
    "seek": {"position_ms"}, "set_volume": {"volume"},        # 0..100
    "set_shuffle": {"enabled"}, "set_repeat_context": {"enabled"},
    "set_repeat_track": {"enabled"}, "add_to_queue": {"uri"},
    "activate": set(), "deactivate": set(),
}
WS_EVENTS = {
    "auth_state": {"logged_in", "is_active", "device_name"},
    "playback_state": {"status", "item", "context", "position", "volume",
                       "is_active", "options", "available_actions"},
    "track_changed": {"item"}, "playback_changed": {"status"},
    "volume_changed": {"volume"}, "context_changed": {"context"},
    "options_changed": {"options"}, "position_sync": {"position"},
    "queue_changed": {"previous", "upcoming"},   # entries: uid, source, item
    "command_result": {"command"}, "error": {"message"},
}
WS_STATUS = ("idle", "playing", "paused", "buffering")
WS_QUEUE_SOURCE = ("context", "queue", "autoplay")   # autoplay rows: not ours
ENTITY_TYPES = ("track", "episode", "artist", "album", "playlist", "show",
                "ad", "unknown")
# Entity: uri, entity_type, decorations{identity{name}, visual_identity
# {cover[{url,size}]}, parent{entity}, creators[{entity}], playback
# {duration_ms, content_ratings}}. Broadcast queue_changed is capped at 10
# entries; get_queue limit=0 returned exactly 80 upcoming on the bench.

# --- the translation table: what soloistd does per REST endpoint -----------
# (values are the design, PLAN-soloistd.md; tests read the KEYS)
TRANSLATE = {
    "/status":                 "playback_state mirror (+position_sync interpolation, auth_state)",
    "/player/play":            "play uri; skip_to_uri -> the resume walk (pause, skip_next until item.uri, seek, play) under the volume shroud",
    "/player/pause":           "pause",
    "/player/resume":          "play (bare)",
    "/player/playpause":       "pause | play by status",
    "/player/next":            "skip_next",
    "/player/prev":            "skip_prev (the prev-restart dance stays client-side in spotify.command)",
    "/player/seek":            "seek position_ms",
    "/player/volume":          "set_volume (volume_steps=100 makes the scaling identity)",
    "/player/shuffle_context": "set_shuffle enabled",
    "/player/output":          "restart the child with the new --pipewire-device (no live reopen)",
    "/context/tracks":         "get_queue limit=0 for the ACTIVE context (80-window); Web API listing is P2",
    "/cache/snapshot":         "404 (no such thing) — library.py fails open",
    "/cache/download":         "404 — warming (D3) replaces it: play ~2s + skip_next per item on vibb_null",
}


def sample_playback_state(**over):
    """A playback_state the fake WS server emits and soloistd mirrors."""
    st = {"type": "playback_state", "status": "playing", "is_active": True,
          "item": sample_entity("spotify:track:x", "T", ["A"], "L", 180000),
          "context": {"uri": "spotify:playlist:p", "entity_type": "playlist",
                      "decorations": {"identity": {"name": "P"}}},
          "position": {"position_ms": 12000, "timestamp_ms": 0, "speed": 1.0},
          "volume": 50,
          "options": {"shuffle": False, "repeat": "off", "playback_speed": 1.0},
          "available_actions": {"pause": {}, "seek": {}, "skip_next": {}}}
    st.update(over)
    return st


def sample_entity(uri, name, artists, album, duration_ms, cover="https://i/x.jpg"):
    return {"uri": uri, "entity_type": uri.split(":")[1],
            "decorations": {
                "identity": {"name": name},
                "visual_identity": {"cover": [{"url": cover, "size": "large"}]},
                "parent": {"entity": {"uri": "spotify:album:a", "entity_type": "album",
                                      "decorations": {"identity": {"name": album}}}},
                "creators": [{"entity": {"uri": "spotify:artist:r", "entity_type": "artist",
                                         "decorations": {"identity": {"name": a}}}}
                             for a in artists],
                "playback": {"duration_ms": duration_ms, "content_ratings": []}}}


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
    # 5. the translation table covers every endpoint, and only those
    assert set(TRANSLATE) == set(ENDPOINTS), set(TRANSLATE) ^ set(ENDPOINTS)
    # 6. the samples carry every field the box reads
    st = sample_status()
    assert STATUS_FIELDS <= set(st) and TRACK_FIELDS <= set(st["track"])
    ps = sample_playback_state()
    assert WS_EVENTS["playback_state"] <= set(ps)
    print("contract self-check: endpoints, bodies, origin value, translation, samples OK")
    return True


if __name__ == "__main__":
    sys.exit(0 if selfcheck() else 1)
