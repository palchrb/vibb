#!/usr/bin/env python3
"""The vibbd <-> vibb-sonos contract, as an EXECUTABLE fixture.

Both test rigs import this file: the fake sidecar GENERATES its answers
from these shapes and 400s any request that fails check_play(); the
wire test validates the REAL sidecar's answers with the same
check_state(). One source of truth — neither fake can drift into
agreeing with itself (QA review 2026-08-09, the two-fakes trap).

Signatures and field sets are FROZEN; grow additively only.
Run standalone to self-check the canonical examples.
"""

KINDS = ("url", "nrk_program", "spotify_sharelink")

# closed set: vibbd maps anything NOT in this set to the conservative
# branch (freeze position, no local fallback, no retry)
TRANSPORTS = ("PLAYING", "PAUSED_PLAYBACK", "STOPPED", "TRANSITIONING",
              "UNREACHABLE")

STATE_REQUIRED = ("armed", "seq", "stale_s")
STATE_ARMED = ("uid", "kind", "transport", "reachable")

PLAY_REQUIRED = {"uid", "kind", "uri"}
PLAY_BY_KIND = {
    "url": set(),
    "nrk_program": {"program_id", "series"},
    "spotify_sharelink": set(),
}
PLAY_OPTIONAL = {"title", "artist", "album", "art", "duration_s",
                 "start_s", "track_index", "program_id", "series"}


def check_play(body):
    """Validate a /play request body. Returns None or an error string."""
    if not isinstance(body, dict):
        return "body must be an object"
    missing = PLAY_REQUIRED - set(body)
    if missing:
        return f"missing: {sorted(missing)}"
    if body["kind"] not in KINDS:
        return f"unknown kind: {body['kind']!r}"
    missing = PLAY_BY_KIND[body["kind"]] - set(body)
    if missing:
        return f"missing for {body['kind']}: {sorted(missing)}"
    unknown = set(body) - PLAY_REQUIRED - PLAY_OPTIONAL
    if unknown:
        return f"unknown fields: {sorted(unknown)}"
    if "start_s" in body and not isinstance(body["start_s"], (int, float)):
        return "start_s must be seconds (number)"
    return None


def check_state(snap):
    """Validate a /state snapshot. Returns None or an error string."""
    if not isinstance(snap, dict):
        return "state must be an object"
    for k in STATE_REQUIRED:
        if k not in snap:
            return f"missing: {k}"
    if not isinstance(snap["seq"], int):
        return "seq must be a monotonic int"
    if snap["stale_s"] is not None \
            and not isinstance(snap["stale_s"], (int, float)):
        return "stale_s must be an age in seconds or null"
    if snap["armed"]:
        for k in STATE_ARMED:
            if k not in snap:
                return f"armed state missing: {k}"
        if snap.get("reachable") and snap["transport"] not in TRANSPORTS:
            return f"unknown transport: {snap['transport']!r}"
    return None


# --- canonical examples (the fake sidecar serves these verbatim) -----------

PLAY_URL = {"uid": "RINCON_TEST01400", "kind": "url",
            "uri": "https://podkast.example/ep12.mp3",
            "title": "Sommerfuglen", "artist": "Barnas Supersommer",
            "duration_s": 1453, "start_s": 427.0}

PLAY_NRK = {"uid": "RINCON_TEST01400", "kind": "nrk_program",
            "uri": "https://radio.nrk.no/serie/x/MKTT81008181",
            "program_id": "MKTT81008181", "series": "x",
            "title": "Kap. 3"}

PLAY_SHARE = {"uid": "RINCON_TEST01400", "kind": "spotify_sharelink",
              "uri": "https://open.spotify.com/album/abc123",
              "track_index": 4, "start_s": 61.0}

STATE_PLAYING = {"armed": True, "uid": "RINCON_TEST01400", "kind": "url",
                 "seq": 7, "stale_s": 1.2, "reachable": True,
                 "transport": "PLAYING", "rel_s": 431.0, "dur_s": 1453.0,
                 "uri": "https://podkast.example/ep12.mp3", "ours": True,
                 "foreign_uri": None, "grouped_away": False,
                 "coordinator": None, "lost_session": False,
                 "volume": 31, "retried_at": None}

STATE_FOREIGN = dict(STATE_PLAYING, ours=False,
                     uri="x-sonos-spotify:someoneelse",
                     foreign_uri="x-sonos-spotify:someoneelse")

STATE_UNREACHABLE = {"armed": True, "uid": "RINCON_TEST01400",
                     "kind": "url", "seq": 9, "stale_s": 47.0,
                     "reachable": False, "transport": "UNREACHABLE",
                     "retried_at": None}

STATE_LOST = dict(STATE_PLAYING, transport="STOPPED", rel_s=0.0,
                  uri="", ours=False, foreign_uri=None, lost_session=True)

STATE_IDLE = {"armed": False, "uid": None, "kind": None,
              "seq": 1, "stale_s": None, "retried_at": None}

# Stage B1 (2026-08-24): our uid pulled into someone else's group as a
# MEMBER — its AVTransport names the coordinator (x-rincon:<uid>), ours
# is False, and BOTH aux fields are set on the same poll (instant
# detection). The daemon shows renderer_state grouped-away (ordered
# before taken-over, which the x-rincon foreign_uri would otherwise win).
STATE_GROUPED = dict(STATE_PLAYING, ours=False,
                     uri="x-rincon:RINCON_PARENT1400",
                     foreign_uri="x-rincon:RINCON_PARENT1400",
                     grouped_away=True, coordinator="RINCON_PARENT1400")

# v2 (vibb owns the spotify LOGIC; the speaker holds the queue):
# track_spotify_uri is the inbound authority (decoded from TrackURI);
# track_no is 1-based raw Track (cross-check only — 0 for an armed
# sharelink session is a contract violation); /queue_play {index} is
# 0-based. The off-by-one lives in exactly one place: the poller.
STATE_SHARE = dict(STATE_PLAYING, kind="spotify_sharelink",
                   uri="x-sonos-spotify:spotify%3atrack%3ac?sid=9",
                   track_spotify_uri="spotify:track:c",
                   track_no=3, queue_len=3, base=1,
                   track_title="Shout", track_artist=None,
                   track_art=None)

QUEUE_PLAY_REQUIRED = {"index"}


def check_state_share(snap):
    err = check_state(snap)
    if err:
        return err
    if snap.get("armed") and snap.get("kind") == "spotify_sharelink"             and snap.get("reachable") and snap.get("track_no") == 0:
        return "track_no is 1-based; 0 is a contract violation"
    return None


def main():
    for name in ("PLAY_URL", "PLAY_NRK", "PLAY_SHARE"):
        err = check_play(globals()[name])
        assert err is None, f"{name}: {err}"
        print(f"{name} validates OK")
    assert check_state_share(STATE_SHARE) is None
    assert check_state_share(dict(STATE_SHARE, track_no=0))
    for name in ("STATE_PLAYING", "STATE_FOREIGN", "STATE_UNREACHABLE",
                 "STATE_LOST", "STATE_IDLE", "STATE_GROUPED"):
        err = check_state(globals()[name])
        assert err is None, f"{name}: {err}"
        print(f"{name} validates OK")
    # and the validators actually reject garbage
    assert check_play({"uid": "x", "kind": "local_file", "uri": "/a.mp3"})
    assert check_play({"uid": "x", "kind": "nrk_program", "uri": "u"})
    assert check_play(dict(PLAY_URL, start_s="427"))
    assert check_state({"armed": True, "seq": 3, "stale_s": 0.1})
    assert check_state(dict(STATE_PLAYING, transport="SLEEPING"))
    print("malformed payloads rejected OK")
    print("\nSONOS CONTRACT OK — one source of truth for both fakes.")


if __name__ == "__main__":
    main()
