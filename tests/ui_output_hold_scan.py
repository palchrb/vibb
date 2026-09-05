#!/usr/bin/env python3
"""Hold X on the screen (owner 2026-09-05):

  1. empty Sonos cache -> today's two-way toggle, unchanged for the kid,
     and ONE /sonos?fresh=1 behind it (the sidecar's SSDP fall-through),
     so a box that has Sonos shows the menu from the NEXT hold
  2. cached rooms -> the menu at once, fresh=1 behind it; when that answer
     says scanning, the list is read once more after SONOS_SCAN_WAIT_S so
     the new rooms appear while the menu is still open
  3. cached rooms, fresh answer not scanning -> exactly one read, no wait
"""
import os
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("VIBB_UI_PNG", "/dev/null")
os.environ["VIBB_EMOJI"] = "0"
sys.path.insert(0, os.path.join(REPO, "pi"))

import ui  # noqa: E402

GETS, SLEPT, PUSHED, TOGGLED = [], [], [], []
ANSWERS = {}


def fake_get(path, timeout=10):
    GETS.append(path)
    return dict(ANSWERS.get(path, {"players": []}))


ui.api_get = fake_get
ui.time.sleep = lambda s: SLEPT.append(s)

app = object.__new__(ui.App)
app.sonos = None
app.dirty = False
app.push = lambda view: PUSHED.append(view)
app._toggle_output = lambda: TOGGLED.append(1)


def settle():
    for t in threading.enumerate():
        if t is not threading.current_thread() and t.daemon:
            t.join(2)


# 1. empty cache
ANSWERS.clear(); GETS.clear(); SLEPT.clear(); PUSHED.clear(); TOGGLED.clear()
ANSWERS["/sonos"] = {"players": []}
ANSWERS["/sonos?fresh=1"] = {"players": [], "stale": True, "scanning": True}
app._output_action(); settle()
assert TOGGLED == [1] and PUSHED == [], (TOGGLED, PUSHED)
assert GETS == ["/sonos", "/sonos?fresh=1"], GETS
assert SLEPT == [], "the toggle path never waits"
print("1. empty cache: toggle as today + one scan behind it OK")

# 2. cached rooms, nobody answers -> menu now, re-read after the round
ANSWERS.clear(); GETS.clear(); SLEPT.clear(); PUSHED.clear(); TOGGLED.clear()
ANSWERS["/sonos"] = {"players": [{"uid": "RINCON_A", "name": "Stua"}]}
ANSWERS["/sonos?fresh=1"] = {"players": [{"uid": "RINCON_A", "name": "Stua"}],
                             "stale": True, "scanning": True}
app._output_action()
assert PUSHED == ["output"] and TOGGLED == [], (PUSHED, TOGGLED)
settle()
assert GETS == ["/sonos", "/sonos?fresh=1", "/sonos"], GETS
assert SLEPT == [ui.SONOS_SCAN_WAIT_S], SLEPT
assert app.dirty is True
print("2. cached rooms, nobody answered: menu at once, list re-read after the round OK")

# 3. cached rooms, they answer -> one fresh read, no wait
ANSWERS.clear(); GETS.clear(); SLEPT.clear(); PUSHED.clear(); TOGGLED.clear()
ANSWERS["/sonos"] = {"players": [{"uid": "RINCON_A", "name": "Stua"}]}
ANSWERS["/sonos?fresh=1"] = {"players": [{"uid": "RINCON_A", "name": "Stua"}]}
app._output_action(); settle()
assert PUSHED == ["output"] and GETS == ["/sonos", "/sonos?fresh=1"] and SLEPT == [], (GETS, SLEPT)
print("3. cached rooms that answer: menu + one fresh read, nothing more OK")

print("\nall ui_output_hold_scan checks passed")
