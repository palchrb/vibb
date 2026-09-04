#!/usr/bin/env python3
"""D2/AM-46..48: the Soloist API key enters through the PWA, and the
daemon fast-fails a Spotify tap on every engine state that means no
session can come.

  1. POST /soloist/configure is token-gated (default deny) and JSON-typed
  2. under the soloist engine a key lands in /etc/vibb/soloist.env as
     KEY=VALUE, mode 0600, and the engine UNIT is restarted (--no-block)
     through go_unit_cmd; the key never appears in the daemon's log
  3. an empty key removes the file (-> needs-key); a bad shape is 400;
     under go-librespot the route answers 409 and writes nothing
  4. /status.spotify_state passes the engine's verdict through, and is
     ok|offline for an engine that reports none (go-librespot)
  5. play() on a Spotify target fast-fails with spotify-<state> for
     needs-key / needs-pair / expired / bad-key / audio-unbound — no
     player spawned — and proceeds on ok
  6. the PWA has the form and the state line (real selectors)
"""
import json
import os
import stat
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["VIBB_STATE"] = TMP
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
os.environ["VIBB_SETTINGS"] = os.path.join(TMP, "se.json")
os.environ["VIBB_LIBRARY"] = os.path.join(TMP, "lib.json")
os.environ["VIBB_TOKEN_FILE"] = os.path.join(TMP, "api-token")
os.environ["VIBB_SOLOIST_ENV"] = os.path.join(TMP, "soloist.env")
os.environ["VIBB_GO_UNIT"] = "vibb-soloistd"
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402
from vibb import paths, token  # noqa: E402

TOKEN = token.ensure()
LOGS, RUNS = [], []
daemon.log = lambda m: LOGS.append(m)
daemon.subprocess.run = lambda args, **k: RUNS.append(list(args)) or None
ENGINE = {"st": {}}
daemon.go_status = lambda **_k: dict(ENGINE["st"])
orch = daemon.ORCH
orch._mpv_alive = lambda: False
orch.target, orch.source = None, None
daemon.current_output = lambda **_k: {"output": "local", "pcm": "vibb_local"}
daemon._kick_bt_connect = lambda: None
orch._ensure_spotify_backend = lambda: True
SPAWNS = []
orch._spawn = lambda *a, **k: SPAWNS.append((a, k))

srv = ThreadingHTTPServer(("127.0.0.1", 0), daemon.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"


def call(path, body=None, tok=TOKEN, ctype="application/json"):
    req = urllib.request.Request(BASE + path, data=json.dumps(body or {}).encode(), method="POST")
    if tok:
        req.add_header("X-Vibb-Token", tok)
    req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# 1. gates
assert call("/soloist/configure", {"api_key": "abc"}, tok=None)[0] == 401
assert call("/soloist/configure", {"api_key": "abc"}, ctype="text/plain")[0] in (400, 415)
assert not os.path.exists(os.environ["VIBB_SOLOIST_ENV"])
print("1. token + JSON content-type gates OK")

# 2. the key lands 0600 as KEY=VALUE, the unit restarts, nothing logged
RUNS.clear(); LOGS.clear()
code, r = call("/soloist/configure", {"api_key": "sk-SECRET-123"})
assert code == 202 and r["restarting"] is True, (code, r)
env = os.environ["VIBB_SOLOIST_ENV"]
assert open(env).read() == "SOLOIST_API_KEY=sk-SECRET-123\n"
assert stat.S_IMODE(os.stat(env).st_mode) == 0o600
assert RUNS == [["systemctl", "--no-block", "restart", "vibb-soloistd"]], RUNS
assert not any("SECRET" in m for m in LOGS), LOGS
print("2. key written 0600 as KEY=VALUE, unit restarted via go_unit_cmd, never logged OK")

# 3. removal, bad shapes, wrong engine
code, r = call("/soloist/configure", {"api_key": ""})
assert code == 202 and r["removed"] is True and not os.path.exists(env)
assert call("/soloist/configure", {"api_key": "has space"})[0] == 400
assert call("/soloist/configure", {"api_key": "x" * 600})[0] == 400
assert call("/soloist/configure", {})[0] == 400
paths.GO_UNIT = "go-librespot"
code, r = call("/soloist/configure", {"api_key": "abc"})
assert code == 409 and r["error"] == "engine-not-soloist" and not os.path.exists(env)
paths.GO_UNIT = "vibb-soloistd"
print("3. removal -> file gone; bad shapes 400; go-librespot engine 409 OK")

# 4. /status.spotify_state
ENGINE["st"] = {"spotify_state": "needs-key", "track": None}
assert orch.status()["spotify_state"] == "needs-key"
ENGINE["st"] = {"username": "kid"}            # go-librespot reports no state
daemon._SPOT_OFFLINE[0] = False
assert orch.status()["spotify_state"] == "ok"
daemon._SPOT_OFFLINE[0] = True
assert orch.status()["spotify_state"] == "offline"
daemon._SPOT_OFFLINE[0] = False
print("4. spotify_state passthrough, ok/offline for an engine without one OK")

# 5. play() fast-fails on the no-session states
target = "https://open.spotify.com/playlist/x"
for state in daemon.ENGINE_NO_SESSION:
    ENGINE["st"] = {"spotify_state": state}
    SPAWNS.clear()
    r = orch.play(target)
    assert r.get("error") == f"spotify-{state}" and r.get("spotify_state") == state, (state, r)
    assert SPAWNS == [], f"{state}: must not spawn a player"
ENGINE["st"] = {"spotify_state": "ok", "username": "kid"}
SPAWNS.clear()
r = orch.play(target)
assert not r.get("error") and SPAWNS, r
print("5. play(): spotify-<state> fast-fail, no spawn; ok proceeds OK")

# 6. the PWA
html = open(os.path.join(REPO, "pi/web/index.html"), encoding="utf-8").read()
js = open(os.path.join(REPO, "pi/web/app.js"), encoding="utf-8").read()
for sel in ('id="spotify-state"', 'id="soloist-key-form"', 'id="soloist-key"', 'id="btn-soloist-key-forget"'):
    assert sel in html, sel
assert '"/soloist/configure"' in js and "renderSpotifyState(st.spotify_state)" in js
assert 'confirm("Save this key in your password manager now' in js, "the local confirm before the key leaves the phone"
for state in daemon.ENGINE_NO_SESSION:
    assert f'"{state}":' in js, f"the PWA names the {state} state"
print("6. PWA form, state line, local confirm, every state named OK")

print("\nall soloist_configure checks passed")
