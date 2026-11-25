#!/usr/bin/env python3
"""Hold X fills the Sonos list itself — never ssh (owner 2026-09-05).

The sidecar's /players?fresh=1 is ONE topology call against a cached
speaker. When nobody can be asked — an empty cache (a fresh box) or a new
LAN (the cabin) — it now falls through to ONE SSDP round in the background
and says so ("scanning": true); the request stays instant and serves the
old cache marked stale. A second ask while the round runs starts nothing.
The list is the LAST ROUND's finds, never the merged history of every
house the box has visited (AM-104, owner 2026-11-21); the cache still
remembers every uid -> ip. Driven through a real HTTPServer on the sidecar's Handler, soco faked."""
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import types
from http.server import HTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
for k in ("VIBB_RUN", "VIBB_STATE", "VIBB_CACHE"):
    os.environ[k] = TMP
sys.path.insert(0, os.path.join(REPO, "pi"))

import sonosd  # noqa: E402


def zone(uid, ip, name, visible=True):
    return types.SimpleNamespace(uid=uid, ip_address=ip,
                                 player_name=name, is_visible=visible)


DISCOVERS = []
GATE = threading.Event()   # the fake SSDP round blocks until released


FOUND = [zone("RINCON_E", "192.168.1.17", "Edith"),
         zone("RINCON_K", "192.168.1.21", "Kjøkken")]


def fake_discover(timeout=3):
    DISCOVERS.append(timeout)
    GATE.wait(5)
    return list(FOUND)


class FakeSoCo:
    def __init__(self, ip):
        raise OSError("unreachable")   # nobody answers a topology call


sonosd._soco = lambda: types.SimpleNamespace(SoCo=FakeSoCo, discover=fake_discover)
sonosd._reachable = lambda ip, timeout=1.0: True   # the SoCo fake decides
sonosd.log = lambda m: None

srv = HTTPServer(("127.0.0.1", 0), sonosd.Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def get(path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path)
    r = c.getresponse()
    body = json.loads(r.read() or b"{}")
    c.close()
    return r.status, body


# 1. empty cache: fresh=1 answers at once — stale, scanning, no players —
#    and ONE SSDP round is running behind it
t0 = time.monotonic()
st, body = get("/players?fresh=1")
assert st == 200 and body["players"] == [] and body.get("stale") is True, body
assert body.get("scanning") is True, body
assert time.monotonic() - t0 < 2.0, "the request must not wait for SSDP"
assert DISCOVERS == [3], DISCOVERS
print("1. empty cache: instant stale answer, one SSDP round behind it OK")

# 2. a second hold while it runs starts nothing more
st, body = get("/players?fresh=1")
assert body.get("scanning") is True and DISCOVERS == [3], (body, DISCOVERS)
print("2. a second ask while scanning starts no second round OK")

# 3. the round finishes: the list is filled, scanning is off
GATE.set()
for _ in range(50):
    st, body = get("/players")
    if body["players"]:
        break
    time.sleep(0.1)
names = sorted(p["name"] for p in body["players"])
assert names == ["Edith", "Kjøkken"], names
assert not body.get("scanning"), body
print("3. the round fills the cache; the plain read serves it OK")

# 4. the cabin: cached rooms, nobody answers -> stale rows kept + a new round
DISCOVERS.clear(); GATE.clear()
st, body = get("/players?fresh=1")
assert body.get("stale") is True and body.get("scanning") is True, body
assert sorted(p["name"] for p in body["players"]) == ["Edith", "Kjøkken"], "old rows stay"
assert DISCOVERS == [3]
GATE.set()
print("4. cached rooms that stopped answering: kept grey, one round started OK")
for _ in range(50):
    if not sonosd.scanning():
        break
    time.sleep(0.1)

# 5. a NEW house (AM-104): the round finds one room the cache never saw
#    — the list is THAT room alone; home's two rooms stay in the cache
#    (next visit's fast path) but leave the picker
FOUND[:] = [zone("RINCON_H", "10.9.0.5", "Hytta")]
st, body = get("/players?rescan=1")
assert [p["name"] for p in body["players"]] == ["Hytta"], body
assert body.get("known") is True and not body.get("stale"), body
assert sorted(sonosd.players()["players"]) == ["RINCON_E", "RINCON_H",
                                                "RINCON_K"], "cache remembers"
print("5. a new house lists only the rooms found there OK")

# 6. nowhere: SSDP finds nothing and no cached ip answers — the round
#    still closes, on an EMPTY present set; `known` keeps the Sonos row
#    so the hold opens the menu (and its hint) instead of the toggle
FOUND[:] = []
st, body = get("/players?rescan=1")
assert body["players"] == [] and body["groups"] == [], body
assert body.get("known") is True, body
print("6. nobody found -> empty list, cache kept, known stays OK")

# 7. the plain read serves the same truth — not the merged history
st, body = get("/players")
assert body["players"] == [] and body.get("known") is True, body
print("7. the cached read is the last round's finds OK")
srv.shutdown()
print("\nall sonos_hold_scan checks passed")
