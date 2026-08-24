#!/usr/bin/env python3
"""Stage B2, daemon half — acting on the sidecar's migration hint.

The daemon OWNS identity: on a verified hint it re-attaches the sidecar
session via the EXISTING /adopt (zero transport commands — the music
never stopped) and only THEN rewrites renderer.json, so a failed adopt
changes nothing and the hint retries. What must hold:

1. a fresh hinted snapshot whose session uid matches our renderer uid
   -> one /adopt {uid,kind,uri-from-the-hint} + renderer.write(new uid,
   name) + the volume optimism cleared. Adopt BEFORE write.
2. the guards: a hint for the uid we already have is a no-op; a
   snapshot whose session uid does not match renderer.json (the user
   re-picked mid-flight) is a no-op; a stale snapshot is a no-op; a
   non-sonos renderer is a no-op.
3. a SidecarDown on /adopt changes NOTHING (no renderer.write) — the
   hint is still published and the next tick retries.
4. continuity: post-adopt, verbs read renderer.json at call time, so
   they carry the NEW uid.
"""
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
for k in ("VIBB_RUN", "VIBB_STATE", "VIBB_CACHE"):
    os.environ[k] = TMP
os.environ["VIBB_SETTINGS"] = os.path.join(TMP, "settings.json")
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

daemon.go_status = lambda **k: {}
orch = daemon.ORCH

RD = {"renderer": "sonos", "uid": "RINCON_KID", "name": "Barnerom"}
POSTS, WRITES = [], []
daemon._renderer.read = lambda: dict(RD)
daemon._renderer.write = lambda renderer, uid=None, name=None: \
    WRITES.append((renderer, uid, name)) or RD.update(
        renderer=renderer, uid=uid, name=name)


def post_ok(path, body=None, timeout=None):
    POSTS.append((path, body))
    return 200, {"ok": True}


daemon._renderer.post = post_ok
orch.sonos_kind = "url"
orch._sonos_vol_opt = (55, time.monotonic())

HINT = {"uid": "RINCON_KITCHEN", "name": "Kjøkken",
        "uri": "https://cdn.example/signed/ep12.mp3?tok=abc"}
SNAP = {"armed": True, "uid": "RINCON_KID", "kind": "url",
        "stale_s": 1.0, "transport": "STOPPED", "uri": "",
        "ours": False, "stream_moved": dict(HINT)}

# 1. the act: adopt with the hint's uri, then the identity write
r = orch._sonos_stream_moved(dict(SNAP))
assert r is True
assert POSTS == [("/adopt", {"uid": "RINCON_KITCHEN", "kind": "url",
                             "uri": HINT["uri"]})], POSTS
assert WRITES == [("sonos", "RINCON_KITCHEN", "Kjøkken")], WRITES
assert orch._sonos_vol_opt is None, "new speaker, new volume world"
print("1. hint -> one /adopt (hint uri) -> renderer.write, vol-opt reset OK")

# 4. continuity: a verb now carries the NEW uid (renderer read at call
#    time) — pause() with an ours snapshot goes to Kjøkken
orch.source = "sonos"
orch.sonos_idx = 0
orch.target = "https://podkast.example/feed.xml"
orch.sonos_snap = {"ours": True, "transport": "PLAYING", "rel_s": 10.0,
                   "uri": HINT["uri"]}
orch.sonos_snap_at = time.monotonic()
POSTS.clear()
orch.pause()
assert POSTS and POSTS[0][0] == "/pause" \
    and POSTS[0][1]["if_uid"] == "RINCON_KITCHEN", POSTS
print("4. verbs carry the promoted uid after the follow OK")

# 2. the guards, each a strict no-op
RD.update(renderer="sonos", uid="RINCON_KID", name="Barnerom")
POSTS.clear()
WRITES.clear()

assert orch._sonos_stream_moved(
    dict(SNAP, stream_moved=dict(HINT, uid="RINCON_KID"))) is False, \
    "a hint naming the uid we already have is a no-op"
assert orch._sonos_stream_moved(
    dict(SNAP, uid="RINCON_SOMEONE_ELSE")) is False, \
    "a snapshot from a session we no longer own (re-pick race) is a no-op"
assert orch._sonos_stream_moved(dict(SNAP, stale_s=99.0)) is False, \
    "a ghost snapshot must never retarget the box"
assert orch._sonos_stream_moved(dict(SNAP, stream_moved=None)) is False
RD["renderer"] = "box"
assert orch._sonos_stream_moved(dict(SNAP)) is False, \
    "a box renderer must never be yanked to sonos by a stale hint"
RD["renderer"] = "sonos"
assert POSTS == [] and WRITES == [], (POSTS, WRITES)
print("2. same-uid / re-pick / stale / absent / non-sonos all no-op OK")

# 3. SidecarDown on adopt changes nothing — identity untouched, retry free
def post_down(path, body=None, timeout=None):
    POSTS.append((path, body))
    raise daemon._renderer.SidecarDown("down")


daemon._renderer.post = post_down
assert orch._sonos_stream_moved(dict(SNAP)) is False
assert [p for p, _b in POSTS] == ["/adopt"] and WRITES == [], \
    "a failed adopt must leave renderer.json alone (the hint retries)"
print("3. failed adopt leaves identity untouched — retried next tick OK")

print("\nSONOS MIGRATE OK — the box follows its own stream, exactly "
      "once, and nothing else can move its identity.")
