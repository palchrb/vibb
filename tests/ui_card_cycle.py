#!/usr/bin/env python3
"""X cycles a strip of cards: volume, seek, shuffle.

Shuffle used to hang off a hold on play, and seek did not exist at all.
Both are now tabs on the card X already opened for volume, because the
box had run out of gestures: A is play/pause and half the settings
combo, B is previous and back, X is volume and the output picker, Y is
next and the episode picker. A cycle costs no new gesture — but a hidden
sequence of presses has to be LEARNED, so the tab strip is what makes it
honest. Three tabs, one lit: 'there are others' the first time you see
it.

Two rules carry the whole design, and both are pinned below:

  A LAPSED CARD REOPENS ON VOLUME (pin 3). X has meant volume for
  months and that reflex must never land somewhere else. It is also why
  CARD_TTL_S had to grow to five seconds — at three, with a press
  counting only on RELEASE and a hold threshold under it, a child had
  about two seconds of thinking time per press, which made the third tab
  unreachable rather than merely slow.

  THE CARD NEVER FOLLOWS YOU OUT (pin 6). In the browse views X is
  volume and nothing else; a seek card that leaked there would rebind
  B/Y away from flipping tiles, silently.

Pins 11-13 are inherited from the gesture this replaces and pin exactly
what SURVIVED it: the top-bar glyph, the /status fold every icon reads,
and the daemon's refusal to shuffle a Sonos room."""
import os
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))
os.environ["VIBB_RUN"] = tempfile.mkdtemp()
os.environ.setdefault("VIBB_UI_PNG", "/dev/null")
os.environ["VIBB_EMOJI"] = "0"

import ui  # noqa: E402

ui.SEEK_POST_MIN_S = 0.02   # test pacing — the pattern is under test,
#                             not the field constant
from PIL import Image  # noqa: E402

POSTS, GETS = [], []
VOL = [40]


def fake_post(p, b=None, timeout=None):
    POSTS.append((p, b))
    if p == "/volume":
        VOL[0] = max(0, min(100, VOL[0] + (b or {}).get("delta", 0)))
    return {"routed": "mpv", "volume": VOL[0],
            "position": (b or {}).get("position"),
            "shuffle": (b or {}).get("enabled")}


ui.api_post = fake_post
ui.api_get = lambda p, timeout=None: GETS.append(p) or {"volume": VOL[0]}


class FakeDisplay:
    on = True

    def show(self, img):
        pass

    def set_backlight(self, on):
        pass

    def set_brightness(self, b):
        pass


class FakeInputs:
    gesture_mode = False
    b_hold = False
    down = {}

    def poll(self, timeout):
        return []


def app_now(**status):
    a = ui.App(FakeDisplay(), FakeInputs())
    a.view = "now"
    a.system = {"battery": 50}
    a.status = {"title": "Noe", "playing": True, "duration": 1800.0,
                "position": 600.0, "target": "t1", "episode_id": "e1",
                **status}
    return a


def settle(n=1, key=None):
    for _ in range(60):
        if len(POSTS) >= n:
            break
        time.sleep(0.05)


# 1. a cold X opens the VOLUME card — tab 0, every time
app = app_now()
app.handle_now("x")
assert app._card() == "vol", app._card()
assert app.card_idx == 0
print("1. a cold X opens the volume card OK")

# 2. X again walks the strip, and WRAPS. Wrapping is the point: a child
#    who overshoots presses on rather than waiting out the timeout.
seen = []
for _ in range(4):
    app.handle_now("x")
    seen.append(app._card())
assert seen == ["seek", "shuf", "vol", "seek"], seen
print("2. X cycles volume -> seek -> shuffle -> volume OK")

# 3. THE REFLEX RULE: once the card has lapsed, X starts over at volume —
#    it does not resume where the cycle left off
app.vol_mode_until = 0.0
assert app._card() is None, "the card must lapse"
app.handle_now("x")
assert app._card() == "vol", f"a lapsed card must reopen on volume, got {app._card()}"
assert ui.CARD_TTL_S >= 5.0, "three seconds could not carry three tabs"
print("3. a lapsed card reopens on volume, never mid-cycle OK")

# 4. B and Y mean whatever the showing card says they mean
POSTS.clear()
app = app_now()
app.handle_now("x")                      # volume card
for _ in range(60):                      # the /volume read is off-thread
    if app.volume_shown is not None:
        break
    time.sleep(0.05)
app.handle_now("y")
assert app.volume_shown == 45, app.volume_shown   # optimistic +5 off 40
app.handle_now("x")                      # -> seek
app.handle_now("y")
settle(1)
assert POSTS and POSTS[-1][0] == "/seek", POSTS
assert POSTS[-1][1]["position"] == 615.0, POSTS[-1]   # 600 + one 15s tap
app.handle_now("x")                      # -> shuffle
POSTS.clear()
app.handle_now("y")
settle(1)
assert POSTS[-1] == ("/shuffle", {"enabled": True}), POSTS
POSTS.clear()
app.handle_now("b")                      # B is OFF, not a toggle
settle(1)
assert POSTS[-1] == ("/shuffle", {"enabled": False}), POSTS
print("4. B/Y route per card, and shuffle is off/on rather than a toggle OK")

# 5. A IS NEVER REBOUND. It is play/pause on every card — the one
#    control a child finds without looking, and a mode error there costs
#    "the pause button didn't pause".
POSTS.clear()
for _ in range(3):
    app.handle_now("a")
    settle(len(POSTS) + 0 or 1)
    app.handle_now("x")
assert {p for p, _ in POSTS} == {"/playpause"}, POSTS
print("5. A stays play/pause on every card OK")

# 6. THE CARD NEVER FOLLOWS YOU OUT: in a browse view it is always the
#    volume card, whatever tab was showing when the user left
app = app_now()
for _ in range(2):
    app.handle_now("x")
assert app._card() == "seek"
app.view = "carousel"
assert app._card() == "vol", "seek must not rebind B/Y in the carousel"
app.view = "now"
assert app._card() == "seek", "and it is still there on the way back"
print("6. outside now-playing the card is always volume OK")


# 7. the settings combo still wins, and no long-press fires en route.
#    This is the invariant the removed A-hold used to threaten; it
#    guards b_long/x_long/y_long just as much.
def mk(now_view=True):
    inp = object.__new__(ui.GpioInput)
    inp.queue, inp.down, inp.tainted = [], {}, set()
    inp._long_sent = {}
    inp._b_gesture = False
    inp.b_hold = False
    inp.gesture_mode = now_view
    inp.wake = threading.Event()
    return inp


T = [1000.0]
real_mono = time.monotonic
ui.time.monotonic = lambda: T[0]
try:
    inp = mk()
    inp._pressed("a")
    T[0] += 0.3
    inp._pressed("b")
    for _ in range(6):
        T[0] += 0.2
        assert inp._events() == [], "no long-press while the combo forms"
    T[0] += ui.GpioInput.HOLD_S
    assert inp._events() == ["settings"], "the combo must still work"
    print("7. A+B opens settings with no long-press en route OK")

    # 8. and A no longer holds AT ALL: it is a plain press everywhere,
    #    which is what frees it from ever racing the combo again
    inp = mk()
    inp._pressed("a")
    T[0] += ui.GpioInput.LONG_S + 1.0
    assert inp._events() == [], "A must not produce a hold any more"
    inp._released("a")
    assert inp._events() == ["a"], "and the press is still play/pause"
    print("8. A has no hold gesture left OK")
finally:
    ui.time.monotonic = real_mono

# 9. TAPS ARE UNIFORM (owner design 2026-09-01): every tap is 15s, no
#    matter how many or how fast — the old per-press ladder reached
#    120s in three quick taps, a surprise jump on every short song.
#    The single-flight poster coalesces: first target immediately,
#    then the latest — intermediate targets may never hit the wire,
#    so assert the COMPOUNDED END STATE, not every post.
POSTS.clear()
app = app_now()
app.handle_now("x")
app.handle_now("x")                      # seek card
for _ in range(3):
    app.handle_now("y")
for _ in range(80):
    if app._pos_expect == 645.0 and not app._seek_dirty \
            and not app._seek_posting:
        break
    time.sleep(0.02)
assert app._pos_expect == 645.0, \
    f"three taps must travel exactly 3x15s: {app._pos_expect}"
steps = [p[1]["position"] for p in POSTS if p[0] == "/seek"]
assert steps[0] == 615.0 and steps[-1] == 645.0, steps
assert all(a < b for a, b in zip(steps, steps[1:])), \
    f"the posted targets must never move backward: {steps}"
app.handle_now("b")                      # reversal: minus ONE tap
for _ in range(80):
    if not app._seek_dirty and not app._seek_posting:
        break
    time.sleep(0.02)
back = [p[1]["position"] for p in POSTS if p[0] == "/seek"][-1]
assert back == 630.0, f"a reversal is one 15s tap back, got {back}"
print("9. taps are uniform 15s; poster coalesces, never backward OK")

# 9b. THE HOLD LADDER: acceleration keys off time-since-hold-start,
#     never press count. Driven directly through _seek_press(held=True)
#     under a frozen clock — the repeat pin's own cadence is pinned by
#     test 9c's machinery, the LADDER is what's under test here. Also:
#     the mid-hold confirmation trap (QA 2026-08-14) is gone by
#     construction — nothing between repeats touches _seek_hold_t0.
POSTS.clear()
app = app_now()
# an audiobook-length track: the ladder itself is under test here, so
# the dur/12 clamp (own test, 9e) must stay out of the way — with the
# harness's 1800s track it would rightly cap the 300 rung at 150
app._set("status", {**app.status, "duration": 28800.0})
app.handle_now("x")
app.handle_now("x")                      # seek card
T9 = [2000.0]
real_mono9 = ui.time.monotonic
ui.time.monotonic = lambda: T9[0]
try:
    app._seek_press(+1)                  # the fresh press: a 15s tap
    assert app.seek_shown == 15.0, app.seek_shown
    T9[0] += 1.0                         # held 1.0s: still tap-size
    app._seek_press(+1, held=True)
    assert app.seek_shown == 15.0, app.seek_shown
    T9[0] += 1.0                         # held 2.0s: 45s rung
    app._seek_press(+1, held=True)
    assert app.seek_shown == 45.0, app.seek_shown
    T9[0] += 2.5                         # held 4.5s: 120s rung
    app._seek_press(+1, held=True)
    assert app.seek_shown == 120.0, app.seek_shown
    T9[0] += 4.0                         # held 8.5s: 300s rung
    app._seek_press(+1, held=True)
    assert app.seek_shown == 300.0, app.seek_shown
    # a mid-hold confirmation must not reset the climb
    app._set("status", {**app.status, "position": app._pos_expect})
    T9[0] += 0.35
    app._seek_press(+1, held=True)
    assert app.seek_shown == 300.0, \
        "confirmation between repeats must not reset the hold clock"
    # reversal mid-hold: back to tap size (the "I overshot" rule)
    T9[0] += 0.35
    app._seek_press(-1, held=True)
    assert app.seek_shown == -15.0, app.seek_shown
finally:
    ui.time.monotonic = real_mono9
for _ in range(100):                     # drain THIS app's poster —
    if not app._seek_posting:            # later tests count in-flight
        break                            # posts globally
    time.sleep(0.01)
print("9b. hold time climbs the ladder; confirmation never resets it OK")

# 9e. THE DURATION CLAMP: a step never exceeds ~8% of the track — a
#     3-minute song stays at tap size even deep into a hold (the
#     step-only clamp still teleported it, QA 2026-09-01).
app = app_now()
app._set("status", {**app.status, "duration": 180.0, "position": 60.0})
app.handle_now("x")
app.handle_now("x")
T9 = [3000.0]
ui.time.monotonic = lambda: T9[0]
try:
    app._seek_press(+1)
    T9[0] += 9.0                         # deep hold: ladder says 300
    app._seek_press(+1, held=True)
    assert app.seek_shown == 15.0, \
        f"a 180s track must cap the step at tap size: {app.seek_shown}"
finally:
    ui.time.monotonic = real_mono9
for _ in range(100):                     # drain THIS app's poster —
    if not app._seek_posting:            # later tests count in-flight
        break                            # posts globally
    time.sleep(0.01)
print("9e. dur/12 clamp keeps short songs at tap size OK")

# 9f. THE ADOPTION SEAM (QA's rig, review 2026-09-01): the daemon's
#     response is DELAYED past further compounding — the echoed clamp
#     for target N arrives after targets N+1.. exist. The old per-press
#     go() adopted it unconditionally and REWOUND the bar mid-hold; the
#     single-flight poster may adopt only when nothing newer is dirty.
#     Also pinned: never two posts in flight, and the FINAL compounded
#     target always reaches the wire.
import threading as _th

SLOW = {"on": False, "inflight": 0, "max_inflight": 0}
_real_fake_post = fake_post


def slow_post(p, b=None, timeout=None):
    if p != "/seek" or not SLOW["on"]:
        return _real_fake_post(p, b, timeout)
    SLOW["inflight"] += 1
    SLOW["max_inflight"] = max(SLOW["max_inflight"], SLOW["inflight"])
    time.sleep(0.15)               # answer AFTER more repeats compounded
    SLOW["inflight"] -= 1
    return _real_fake_post(p, b, timeout)


ui.api_post = slow_post
SLOW["on"] = True
POSTS.clear()
app = app_now()
app._set("status", {**app.status, "duration": 28800.0})
app.handle_now("x")
app.handle_now("x")                      # seek card
bars = []
app._seek_press(+1)                      # tap -> first post (slow)
bars.append(app._pos_expect)
for _ in range(6):                       # repeats land DURING the post
    time.sleep(0.05)
    app._seek_press(+1, held=True)
    bars.append(app._pos_expect)
for _ in range(100):
    if not app._seek_dirty and not app._seek_posting:
        break
    time.sleep(0.02)
SLOW["on"] = False
ui.api_post = _real_fake_post
assert all(a < b for a, b in zip(bars, bars[1:])), \
    f"the bar must NEVER move backward mid-hold: {bars}"
assert SLOW["max_inflight"] == 1, \
    f"single-flight means ONE post in the air, saw {SLOW['max_inflight']}"
final_posted = [b["position"] for pth, b in POSTS if pth == "/seek"][-1]
assert final_posted == bars[-1], \
    f"the final compounded target must land: {final_posted} != {bars[-1]}"
print("9f. delayed responses never rewind the bar; final target lands OK")

# 9g. a refusal clears the dirty flag — no doomed re-post after
#     "Can't seek here", even when targets compounded behind it
def refuse_post(p, b=None, timeout=None):
    POSTS.append((p, b))
    return {"routed": None}


ui.api_post = refuse_post
POSTS.clear()
app = app_now()
app.handle_now("x")
app.handle_now("x")
app._seek_press(+1)
app._seek_press(+1, held=True)           # compounds behind the refusal
for _ in range(100):
    if not app._seek_posting:
        break
    time.sleep(0.02)
seeks = [pth for pth, _b in POSTS if pth == "/seek"]
# a press that lands BEFORE the refusal is known may legitimately post
# (and be refused too) — the contract is no posting AFTER the refusal
# settles, not exactly-one
assert len(seeks) <= 2, \
    f"a refusal must kill the queue, not drain it: {len(seeks)} posts"
assert app.seek_refused and app._pos_expect is None
assert not app._seek_dirty and not app._seek_posting
n = len(seeks)
time.sleep(0.2)
assert len([pth for pth, _b in POSTS if pth == "/seek"]) == n, \
    "no zombie post may fire after the refusal settled"
ui.api_post = _real_fake_post
print("9g. a refusal clears the queue — no zombies after it settles OK")

# 9c. A HELD B/Y BELONGS TO THE CARD, and must never ALSO fire the
#     navigation it means when no card is up. Holding is exactly the
#     instinct a seek card invites, and it used to throw the child out
#     to the carousel (B) or open the episode picker (Y) instead. The
#     volume card had the same trap and is fixed with it: once a child
#     learns "hold to keep going" on one card, she will try it on the
#     other.
NAV = []
for cards, want_nav in ((0, True), (1, False), (2, False), (3, False)):
    app = app_now()
    app._back_to_episodes = lambda: NAV.append("carousel")
    app._open_episodes = lambda: NAV.append("picker")
    NAV.clear()
    POSTS.clear()
    for _ in range(cards):
        app.handle_now("x")
    app.handle_now("b_long")
    app.handle_now("y_long")
    if want_nav:
        assert NAV == ["carousel", "picker"], \
            f"with no card up the holds must still navigate, got {NAV}"
    else:
        assert NAV == [], \
            f"a card was up ({app._card()}) but the hold navigated: {NAV}"
print("9c. a held B/Y is owned by the card and never navigates too OK")

# 9d. and the repeat STOPS when the button comes up. The input layer
#     fires a hold once by design, so the repeat is timed in the render
#     loop against the pin state — a repeat that outlived the finger
#     would seek away on its own.
app = app_now()
app.handle_now("x")
app.handle_now("x")                      # seek card
app.inputs.down = {"b": 1.0}
app.handle_now("b_long")
assert app._card_repeat is not None, "holding must arm the repeat"
app.inputs.down = {}                     # finger up
assert ("b" not in app.inputs.down)
print("9d. holding arms a repeat that the release can clear OK")

# 10. the optimistic position holds until a poll CONFIRMS it — and it is
#     track-scoped, or skipping to the next episode would show 0:00
#     masked behind the old spot. Confirmation is a window, not equality:
#     a correct report is the target plus whatever played since.
app = app_now()
app._pos_expect, app._pos_at = 900.0, time.monotonic()
app._pos_until = app._pos_at + 10
app._pos_key = ("t1", "e1")
app._set("status", {**app.status, "position": 12.0})
assert app.status["position"] == 900.0, "a pre-seek report must not win"
app._set("status", {**app.status, "position": 901.0})
assert app._pos_expect is None, "a report inside the window confirms it"
app._pos_expect, app._pos_key = 900.0, ("t1", "e1")
app._pos_until = time.monotonic() + 10
app._set("status", {**app.status, "position": 3.0, "episode_id": "e2"})
assert app._pos_expect is None, "a new track drops the expectation"
assert app.status["position"] == 3.0
print("10. the optimistic position holds, confirms on a window, and is "
      "track-scoped OK")

# 10b. a modal popup DISMISSES the card. Its box is bigger than the
#      card's, so the card would keep owning B/Y while invisible —
#      merely odd for volume, but an invisible seek loses your place.
app = app_now()
app.handle_now("x")
app.handle_now("x")
assert app._card() == "seek"
app._set("status", {**app.status, "bt_lost": True})
assert app._card() is None, "a speaker popup must close the card"
print("10b. a modal popup dismisses the card underneath it OK")

# 11. the glyph is drawn only when shuffle is on, and the top bar is
#     otherwise byte-identical — absence IS the off state
base = {"battery": 60, "plugged": False, "wifi": {"ip": "1.2.3.4"},
        "bt_ready": True}


def bar(shuffle):
    img = Image.new("RGB", (ui.W, 26), ui.BG)
    ui.battery_corner(ui._draw(img), {**base, "shuffle": shuffle})
    return img


off, on = bar(False), bar(True)
assert off.tobytes() != on.tobytes(), "shuffle-on must look different"
assert bar(False).tobytes() == off.tobytes(), "off must be stable"
box = [i for i in range(ui.W)
       if off.crop((i, 0, i + 1, 26)).tobytes()
       != on.crop((i, 0, i + 1, 26)).tobytes()]
assert box and max(box) < ui.W - 60, \
    f"the glyph must sit left of the existing icons, changed at {box}"
print("11. the glyph appears only when on, left of the other icons OK")

# 12. the daemon's shuffle state reaches self.system through the same
#     fold bt_connected uses — the icon row reads self.system only
app = app_now()
app.system = {"battery": 50}
app._set("status", {"bt_connected": True, "shuffle": True, "playing": True})
assert app.system.get("shuffle") is True
assert app.system.get("bt_ready") is True, "must not break the bt fold"
app._set("status", {"bt_connected": True, "shuffle": False, "playing": True})
assert app.system.get("shuffle") is False
print("12. /status shuffle folds into system beside bt_ready OK")

# 13. the daemon refuses on a sonos renderer instead of quietly aiming
#     the command at go-librespot in another room
import daemon  # noqa: E402

sent = []
daemon.go = lambda *a, **k: sent.append(a) or b"{}"
real_sonos = daemon._renderer.is_sonos
daemon._renderer.is_sonos = lambda: True
try:
    orch = object.__new__(daemon.Orchestrator)
    r = orch.shuffle(True)
finally:
    daemon._renderer.is_sonos = real_sonos
assert r == {"routed": None, "shuffle": None}, r
assert sent == [], "nothing may reach go-librespot from a sonos box"
print("13. sonos: refused, and go-librespot is left alone OK")

print("\nCARD CYCLE OK — one gesture, three cards, and a strip that says "
      "the other two are there.")
