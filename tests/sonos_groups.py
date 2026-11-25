#!/usr/bin/env python3
"""Sonos group-awareness (owner 2026-08-21): the box shows and selects
groups as the Sonos app made them — it does not manage them.

What must hold:
1. refresh_topology: ONE call against a cached ip merges every zone
   (fresh ip/name — DHCP moves and renames self-heal), stores the group
   map with the coordinator FIRST, filters bonded invisibles, and
   REPLACES groups wholesale (a group list is only a snapshot).
2. nobody answering raises — the endpoint then serves the cache marked
   stale, so a cabin trip shows the truth, not home's ghosts.
3. players_payload: uid+name only (no LAN ips over a token-free GET),
   groups only when multi-member — and only the rooms the LAST round
   found (AM-104): the cache remembers every house, the list does not.
4. ui._sonos_choices: a group is one row labelled coordinator-first,
   selecting it yields the COORDINATOR uid; absorbed zones don't repeat;
   solo zones unchanged; a group naming unknown uids is skipped whole.
"""
import json
import os
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
for k in ("VIBB_RUN", "VIBB_STATE", "VIBB_CACHE"):
    os.environ[k] = TMP
os.environ.setdefault("VIBB_UI_PNG", "/dev/null")
os.environ["VIBB_EMOJI"] = "0"
sys.path.insert(0, os.path.join(REPO, "pi"))

import sonosd  # noqa: E402


def zone(uid, ip, name, visible=True):
    return types.SimpleNamespace(uid=uid, ip_address=ip,
                                 player_name=name, is_visible=visible)


STUA = zone("RINCON_A", "10.0.0.11", "Stua")
KJOKKEN = zone("RINCON_B", "10.0.0.12", "Kjøkken")
BAD = zone("RINCON_C", "10.0.0.13", "Bad")
SUB = zone("RINCON_SUB", "10.0.0.14", "Sub", visible=False)


class FakeSoCo:
    groups = []
    fail = False
    asked = []

    def __init__(self, ip):
        FakeSoCo.asked.append(ip)
        if FakeSoCo.fail:
            raise OSError("unreachable")
        self.ip = ip

    @property
    def all_groups(self):
        return FakeSoCo.groups


sonosd._soco = lambda: types.SimpleNamespace(SoCo=FakeSoCo)
sonosd._reachable = lambda ip, timeout=1.0: True   # the SoCo fake decides

# seed the cache with ONE stale record: old ip, old name
with open(sonosd.CACHE_FILE, "w") as f:
    json.dump({"players": {"RINCON_A": {"ip": "10.0.0.99",
                                        "name": "Gamlestua",
                                        "seen_at": 1.0}},
               "groups": [{"coordinator": "RINCON_A",
                           "members": ["RINCON_A", "RINCON_C"]}]}, f)

# 1. one call: zones merged with fresh ip/name, groups replaced,
#    coordinator first, the bonded sub filtered out
FakeSoCo.groups = [
    types.SimpleNamespace(coordinator=KJOKKEN,
                          members=[STUA, SUB, KJOKKEN]),
    types.SimpleNamespace(coordinator=BAD, members=[BAD]),
]
cache = sonosd.refresh_topology()
assert cache["players"]["RINCON_A"]["ip"] == "10.0.0.11", \
    "topology must heal a DHCP-moved ip"
assert cache["players"]["RINCON_A"]["name"] == "Stua", \
    "topology must heal a rename"
assert "RINCON_SUB" not in cache["players"], \
    "bonded invisibles are not pickable rooms"
assert cache["groups"] == [
    {"coordinator": "RINCON_B", "members": ["RINCON_B", "RINCON_A"]},
    {"coordinator": "RINCON_C", "members": ["RINCON_C"]},
], f"groups must replace wholesale, coordinator first: {cache['groups']}"
print("1. topology merges zones, heals ip/name, coordinator first OK")

# 2. nobody answers -> raises (endpoint serves the cache marked stale)
FakeSoCo.fail = True
try:
    sonosd.refresh_topology()
    raise AssertionError("no speaker answering must raise")
except OSError:
    pass
FakeSoCo.fail = False
print("2. unreachable household raises — cache serves marked stale OK")

# 2b. the TCP knock: a cached ip nobody answers on :1400 is never asked
#     over SOAP (an ARP timeout per absent speaker made the cabin's
#     picker wait ~3 s each) — and most-recently-seen ips go first
FakeSoCo.asked.clear()
sonosd._reachable = lambda ip, timeout=1.0: False
try:
    sonosd.refresh_topology()
    raise AssertionError("no reachable ip must raise")
except OSError:
    pass
assert FakeSoCo.asked == [], f"unreachable ips must skip SoCo: {FakeSoCo.asked}"
sonosd._reachable = lambda ip, timeout=1.0: ip != "10.0.0.11"
with open(sonosd.CACHE_FILE) as f:
    _c = json.load(f)
_c["players"]["RINCON_A"]["seen_at"] = 5.0      # an older visit
_c["players"]["RINCON_B"]["seen_at"] = 7.0
_c["players"]["RINCON_C"]["seen_at"] = 9.0      # seen most recently
with open(sonosd.CACHE_FILE, "w") as f:
    json.dump(_c, f)
cache = sonosd.refresh_topology()
assert FakeSoCo.asked == ["10.0.0.13"], \
    f"most-recently-seen reachable ip first, one call: {FakeSoCo.asked}"
sonosd._reachable = lambda ip, timeout=1.0: True
print("2b. the knock skips dead ips; recent ips are asked first OK")

# 3. the wire shape: no ips, groups only when multi-member
out = sonosd.players_payload(cache)
assert all(set(p) == {"uid", "name"} for p in out["players"]), \
    f"uid+name only over a token-free GET: {out['players']}"
assert out["groups"] == [{"coordinator": "RINCON_B",
                          "members": ["RINCON_B", "RINCON_A"]}], \
    f"solo 'groups' are noise — multi-member only: {out['groups']}"
assert "stale" not in out
assert sonosd.players_payload(cache, stale=True)["stale"] is True
assert out["known"] is True
print("3. payload: uid+name only, multi-member groups, stale flag OK")

# 3b. the present set (AM-104): a room from ANOTHER house stays in the
#     cache (uid -> ip is the next visit's fast path) but is not listed,
#     and a group naming it loses that member; a cache no round has
#     closed yet lists everything it has (first run after the upgrade)
cache["players"]["RINCON_Z"] = {"ip": "10.9.0.5", "name": "Hytta",
                                "seen_at": 1.0}
cache["groups"].append({"coordinator": "RINCON_C",
                        "members": ["RINCON_C", "RINCON_Z"]})
out = sonosd.players_payload(cache)
assert [p["uid"] for p in out["players"]] == ["RINCON_C", "RINCON_B", "RINCON_A"], \
    f"another house's room must not be listed: {out['players']}"
assert out["groups"] == [{"coordinator": "RINCON_B",
                          "members": ["RINCON_B", "RINCON_A"]}], \
    f"a group with an absent member drops it (and solo groups go): {out['groups']}"
legacy = dict(cache); legacy.pop("present_at")
assert len(sonosd.players_payload(legacy)["players"]) == 4, \
    "no round closed yet -> the whole cache is listed"
empty = {"players": {"RINCON_Z": cache["players"]["RINCON_Z"]},
         "groups": [], "present_at": 100.0}
out = sonosd.players_payload(empty)
assert out["players"] == [] and out["known"] is True, out
assert sonosd.players_payload({"players": {}})["known"] is False
print("3b. the list is the last round's finds; known says rooms exist OK")

# 4. the ui rows: one source for display AND selection
import ui  # noqa: E402

app = object.__new__(ui.App)
app.sonos = {"players": [{"uid": "RINCON_C", "name": "Bad"},
                         {"uid": "RINCON_B", "name": "Kjøkken"},
                         {"uid": "RINCON_A", "name": "Stua"}],
             "groups": [{"coordinator": "RINCON_B",
                         "members": ["RINCON_B", "RINCON_A"]}]}
rows = app._sonos_choices()
assert rows[0] == ("Kjøkken + Stua", "RINCON_B", ["Kjøkken", "Stua"]), \
    f"a group is one row, labelled coordinator first: {rows[0]}"
assert ("Bad", "RINCON_C", ["Bad"]) in rows, "solo zones unchanged"
assert len(rows) == 2, \
    f"absorbed members must not repeat as solo rows: {rows}"
# selecting the group row targets the coordinator — the property that
# makes everything downstream (transport verbs, ours-check) just work
assert rows[0][1] == "RINCON_B"

# a group naming a uid the player list lacks is skipped WHOLE — better
# a solo row than a group row that would mislabel what a press does
app.sonos["groups"] = [{"coordinator": "RINCON_B",
                        "members": ["RINCON_B", "RINCON_GONE"]}]
rows = app._sonos_choices()
assert len(rows) == 3 and all(len(r[2]) == 1 for r in rows), \
    f"unknown member -> no group row: {rows}"
print("4. ui rows: group=one row -> coordinator uid, no drift OK")

# 5. the cabin case: rows persist (mid-menu row removal moves targets
#    under a kid's finger, and the cache never deletes), but the hint
#    stops promising "play here" when the fresh probe found nobody
_src = open(ui.__file__, encoding="utf-8").read()
_i = _src.index('draw_list(d, "Sonos"')
assert 'sonos.get("stale")' in _src[_i - 400:_i + 400], \
    "the sonos submenu must surface the stale marker in its hint"
print("5. stale household -> the hint says so, rows stay put OK")

# 6. the ghost grey (owner picked grey-only, mockup 2026-08-21): with
#    the stale marker set, the output menu's Sonos row and every
#    speaker row carry the ghost flag — but "Look again" never does,
#    because it is the action that fixes the situation
app.sonos = {"players": [{"uid": "RINCON_A", "name": "Stua"}],
             "groups": [], "stale": True}
app.status = {"renderer": None, "output": "local"}
app.view = "output"
rows = app.current_items()
assert rows[2][0] == "Sonos" and rows[2][2] is True, \
    f"a stale household must ghost the Sonos row: {rows[2]}"
assert len(rows[0]) == 2 and len(rows[1]) == 2, \
    "box/bt rows are never ghosted"
app.view = "sonos"
rows = app.current_items()
assert rows[0] == ("Look again", ""), "the fix action is never ghosted"
assert rows[1][2] is True, f"speaker rows ghost when stale: {rows[1]}"
app.sonos.pop("stale")
app.view = "output"
assert app.current_items()[2][2] is False, "no stale -> no ghost"
# and draw_list actually honours the flag: the ghosted SELECTED label
# renders in GHOST, never FG
from PIL import Image
img = Image.new("RGB", (ui.W, ui.H), ui.BG)
ui.draw_list(ui._draw(img), "T", [("Row", "", True)], 0, {})
cols = {c for _n, c in img.getcolors(65536)}
assert ui.GHOST in cols and ui.FG not in cols, \
    "a ghosted selected row must draw GHOST, not FG"
print("6. stale ghosts the rows, spares Look again, draws GHOST OK")

print("\nSONOS GROUPS OK — the box is group-aware; the Sonos app "
      "stays the group manager.")
