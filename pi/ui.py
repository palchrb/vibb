#!/usr/bin/env python3
"""vibb-ui — the screen daemon for the Pirate Audio HAT (240x240 ST7789,
four buttons). A pure consumer of the vibbd API (:3679).

Views:  Home (sections) -> Entries -> Episodes -> Now Playing
        Browse mode (settings.simple_nav): 0 = the text hierarchy above.
        1 = ONE flat cover carousel over every entry (B/Y flip, X volume,
        A plays into the normal Now Playing). 2 = a category carousel
        first, then that category's cover carousel — hold-B steps back
        up a level (entry carousel -> categories). No reading needed.
        Settings: hold A+B ~2s (parental lock — a kid must not be able to
        shut the box down or wipe caches)

Buttons (BCM 5=A, 6=B, 16=X, 24=Y):
  menus:        A=select  B=back   X=up      Y=down
  now playing:  A=play/pause (the same physical button as select — picking
                             something and pausing it feel like one action)
                B: press=previous, hold=back to the episode list (so back
                   is B everywhere: short in menus, hold here)
                X: press=volume mode (then B=down, Y=up; closes after 3s)
                   hold=switch output (bt speaker <-> built-in)
                Y: press=next, hold=episode picker (the same list the
                   full menus use — also kid mode's hidden episode way)

The battery indicator is drawn in the top-right corner of every view.
The screen blanks after settings.screen_timeout_s (0 = never); the
waking button press is swallowed. Brightness is settings.screen_
brightness (% backlight via PWM on BCM13).

Dev mode (no HAT needed):
  VIBB_UI_PNG=/tmp/frame.png   render frames to a PNG instead of SPI
  VIBB_UI_INPUT=/tmp/ui-fifo   read button events from a fifo: one char
                                 per event: a/b/x/y = press, l = long-B,
                                 s = settings
"""

import hashlib
import math
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time

# Time from HERE — the very first thing after the stdlib bits — so the
# heavy imports below are inside the measurement. On a cold boot those
# come off an SD card with nothing in page cache, and the ACT LED
# blinking right up until the screen lights says that is where the
# seconds are. systemd stamps when it FORKED us, not when main() runs,
# so nothing outside this file can see that gap (field 2026-08-13).
_T_START = time.monotonic()


def _uptime():
    """Seconds since the KERNEL started. The one clock in this box that
    cannot jump: the wall clock moves ~30s when the PiSugar RTC lands
    mid-boot, systemd measures from its own start, and time.monotonic()
    from ours — three timebases that cannot be compared, which is how an
    evening of boot analysis produced two confident wrong answers
    (2026-08-13). Every timing below is anchored here instead."""
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError):
        return 0.0


from PIL import Image, ImageDraw, ImageFont, ImageOps

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, "/usr/local/lib/vibb-py"):
    if os.path.isdir(os.path.join(_p, "vibb")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
        break
from vibb import boxapi  # noqa: E402
from vibb import paths as _paths  # noqa: E402 — idle activity marker
from vibb import radio as _radio  # noqa: E402 — artwork yields to audio
from vibb import netmgmt as _netmgmt  # noqa: E402 — the box's .local name

# --- screen-safe text ------------------------------------------------------
# DejaVuSans (the only font install.sh ships) draws .notdef tofu for the
# modern pictograph blocks, so a feed title like "🎃 Grøsserspesial"
# rendered as a black box on the screen (field 2026-08-11). Fix it HERE,
# at the UI's own API edge, so the screen is the only thing affected —
# bookmarks, the PWA and the car's AVRCP display all keep the original
# text, and they have fonts that can draw it.
#
# Font coverage was counted from the shipped DejaVu 2.37 cmap:
#   HAS  music ♪♫♬, stars ★☆✦, hearts ♥♡❤, weather ☀☁☃❄⚡, ticks ✓✔,
#        shapes ▶◀■●▲, all arrows/math/box-drawing, and 64 of the 80
#        Emoticons faces (😀😂😍😴 ...)
#   LACKS Misc Symbols & Pictographs (12 of 768: 🎃🎵🔥✨🎉 ...) and the
#        whole Transport block (🚀🚂 ...)
# So: translate what has a good twin, drop what does not, and never
# touch text the font can already draw.
_EMOJI_MAP = {
    "\U0001F3B5": "♪", "\U0001F3B6": "♪",   # 🎵🎶 -> ♪
    "\U0001F3BC": "♪", "\U0001F3A7": "♪",   # 🎼🎧
    "\U0001F3A4": "♪", "\U0001F399": "♪",   # 🎤🎙
    "\U0001F31F": "★", "⭐": "★",       # 🌟⭐ -> ★
    "✨": "★", "\U0001F4AB": "★",       # ✨💫
    "\U0001F3C6": "★", "\U0001F389": "★",   # 🏆🎉
    "\U0001F9E1": "♥", "\U0001F49B": "♥",   # 🧡💛 -> ♥
    "\U0001F49A": "♥", "\U0001F499": "♥",   # 💚💙
    "\U0001F49C": "♥", "\U0001F5A4": "♥",   # 💜🖤
    "\U0001F90D": "♥", "\U0001F496": "♥",   # 🤍💖
    "\U0001F495": "♥", "\U0001F498": "♥",   # 💕💘
    "\U0001F31E": "☀", "\U0001F319": "☾",   # 🌞 -> ☀, 🌙 -> ☾
    "\U0001F31B": "☾", "\U0001F31C": "☾",   # 🌛🌜
    "\U0001F327": "☂", "\U0001F328": "☃",   # 🌧 -> ☂, 🌨 -> ☃
    "\U0001F525": "▲",                           # 🔥 -> ▲
    "\U0001F451": "♔",                           # 👑 -> ♔ (chess king)
    "✅": "✓", "❌": "✗",           # ✅ -> ✓, ❌ -> ✗
    # the Emoticons faces DejaVu is missing -> its nearest neighbour
    "\U0001F642": "\U0001F60A", "\U0001F641": "\U0001F61E",   # 🙂🙁
    "\U0001F624": "\U0001F620", "\U0001F62C": "\U0001F610",   # 😤😬
    "\U0001F644": "\U0001F612",                               # 🙄
    "\U0001F923": "\U0001F602", "\U0001F970": "\U0001F60D",   # 🤣🥰
}
# Everything else in the pictograph/transport/flag planes is dropped,
# with the invisible modifiers (variation selectors, ZWJ, keycap,
# skin tones) that would otherwise leave stray boxes behind. The
# Emoticons block is NOT dropped wholesale — DejaVu draws 64 of its 80
# faces — so only the codepoints measured as missing are listed here
# (verified against DejaVu 2.37 by tests/ui_emoji.py).
_EMOJI_DROP = re.compile(
    "[\U0001F300-\U0001F5FF"          # misc symbols & pictographs
    "\U0001F624\U0001F62C"            # the two missing faces mid-block
    "\U0001F641\U0001F642\U0001F644"  # ...and these
    "\U0001F645-\U0001F64F"           # the gesture people (🙏🙌 ...)
    "\U0001F650-\U0001FAFF"           # ornaments, transport, supplemental
    "\U0001F1E6-\U0001F1FF"           # regional indicators (flags)
    "︎️‍⃣]")      # the invisibles
_SPACES = re.compile(r"  +")


def screen_text(s):
    """One string, minus anything the screen's font cannot draw."""
    for src, dst in _EMOJI_MAP.items():
        if src in s:
            s = s.replace(src, dst)
    if _EMOJI_DROP.search(s):
        s = _SPACES.sub(" ", _EMOJI_DROP.sub("", s)).strip()
    return s


def _screen_safe(obj):
    """Same, over a decoded API payload. Non-strings pass through as
    themselves — ids, urls and numbers must survive untouched.
    With the emoji sprite path healthy the payload flows RAW — the
    drawer renders the emoji, and scrubbing here would kill them
    before it ever sees them. With it off (no font, failed self-test,
    VIBB_EMOJI=0) this is byte-for-byte the shipped 1a2b642 path."""
    if emoji_active():
        return obj
    if isinstance(obj, str):
        return screen_text(obj)
    if isinstance(obj, dict):
        return {k: _screen_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_screen_safe(v) for v in obj]
    return obj


def api_get(*a, **kw):
    return _screen_safe(boxapi.get(*a, **kw))


def api_post(*a, **kw):
    return _screen_safe(boxapi.post(*a, **kw))


def api_put(*a, **kw):
    return _screen_safe(boxapi.put(*a, **kw))


# --- real emoji sprites ----------------------------------------------------
# The scrub above is the FALLBACK. With Noto Color Emoji present and
# healthy (apt: fonts-noto-color-emoji), RichDraw below renders actual
# color emoji instead. The font is a CBDT bitmap with one ~109px strike
# (asking Pillow for 17px raises OSError), so each cluster is rendered
# once at the strike, LANCZOS-scaled to the text line height and cached
# as a small PNG — after that a frame pays one RGBA paste (~0.004ms,
# ~200x cheaper than the text beside it). Design review 2026-08-12
# (architect + QA + implementer, all measured on this Pillow):
# - GUARD-FIRST: a string without emoji machinery never leaves Pillow's
#   own code path. Anchor math cannot be made pixel-identical (1px
#   drift on 90/160 centered draws), so it is only performed where
#   there is no yesterday to regress against.
# - NO SHAPING without raqm: ZWJ families, flag pairs and keycaps
#   decompose into parts. Those clusters decline to screen_text() (the
#   shipped scrub) rather than faking three heads for a family; skin
#   tones and variation selectors are stripped and the base is sprited.
# - the self-test must catch TOFU, not just blank: a .notdef box has a
#   perfectly good bbox (the 2026-08-11 field bug would re-enter
#   through the cache as black-box PNGs). Render a guaranteed-absent
#   codepoint and byte-compare; require non-empty alpha separately
#   (that half catches a COLR-only font, which renders silently blank).
# VIBB_EMOJI=0 kills the whole path: the box then runs the scrub
# pipeline byte-for-byte as shipped in 1a2b642.

_EMOJI_FONT_PATHS = (
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/truetype/noto-color-emoji/NotoColorEmoji.ttf",
)
_EMOJI_STRIKES = (109, 128, 136, 160)  # CBDT builds seen in the wild
UI_EMOJI_DIR = os.path.join(
    os.environ.get("VIBB_CACHE", "/var/lib/vibb/cache"), "ui-emoji")
_emoji = {"state": None, "font": None, "asc": 0, "em_h": 0}
_emoji_lock = threading.Lock()  # probe may be reached from the poller
_sprites = {}       # (cluster, lh) -> Image | None (failures remembered)
_SPRITES_MAX = 64   # a 113px tile sprite is ~68KB — cap the RAM side


def _probe_emoji():
    """Open the color font and prove it renders REAL glyphs. Any
    failure leaves state 'off' and the scrub pipeline in charge."""
    if os.environ.get("VIBB_EMOJI", "1") == "0":
        _emoji["state"] = "off"
        return
    f = None
    for path in _EMOJI_FONT_PATHS:
        for size in _EMOJI_STRIKES:
            try:
                f = ImageFont.truetype(path, size)
                break
            except OSError:
                continue
        if f:
            break
    if f is None:
        _emoji["state"] = "off"
        return
    try:
        asc, desc = f.getmetrics()
        em_h = asc + desc

        def _render(ch):
            img = Image.new("RGBA", (em_h * 2, em_h), (0, 0, 0, 0))
            ImageDraw.ImageDraw(img).text((0, 0), ch, font=f,
                                          embedded_color=True)
            return img

        probe = _render("\U0001F383")    # 🎃 — the field bug itself
        absent = _render("\U000FFF00")   # guaranteed .notdef
        if probe.getbbox() is None:
            raise OSError("renders blank (COLR-only build?)")
        if probe.tobytes() == absent.tobytes():
            raise OSError("renders tofu (wrong font resolved)")
        _emoji.update(state="on", font=f, asc=asc, em_h=em_h)
        log(f"emoji sprites on (strike {getattr(f, 'size', '?')}px)")
    except Exception as e:
        _emoji["state"] = "off"
        log(f"emoji sprites off ({e}) — scrub fallback active")


def emoji_active():
    if _emoji["state"] is None:
        with _emoji_lock:
            if _emoji["state"] is None:
                _probe_emoji()
    return _emoji["state"] == "on"


# Sprite-able: the SMP pictograph planes plus the two BMP emoji that
# actually appear in kids' feeds (⭐ ✨). Other BMP symbols keep their
# DejaVu twin from the scrub map — ☀️ stays the text sun. Flags and
# keycaps match as clusters so they decline WHOLE, never half.
_CLUSTER = re.compile(
    "(?P<flag>[\U0001F1E6-\U0001F1FF]{2})"
    "|(?P<keycap>[0-9#*]\uFE0F?\u20E3)"
    "|(?P<emoji>[\U0001F000-\U0001F1E5\U0001F200-\U0001FAFF"
    "\u2728\u2B50]"  # RI range excluded: a lone flag half (a
    #                    sliced 🇳🇴) must fall to the scrub, not
    #                    become a tofu sprite (QA pin 14)
    "(?:[\uFE0E\uFE0F\U0001F3FB-\U0001F3FF]"
    "|\u200D[\U0001F000-\U0001FAFF\u2640\u2642\u2764\uFE0F]?)*)")
_MODS = re.compile("[\uFE0E\uFE0F\U0001F3FB-\U0001F3FF]")
# The fast guard: none of these in the string -> Pillow verbatim. Must
# be a SUPERSET of everything screen_text() would touch, or a missed
# character class regresses to tofu (QA Q2). 2600-27BF natives (♪ ★ ☀)
# hit the slow path but come back unchanged from screen_text, so they
# still render through Pillow's own anchor code.
_MAYBE = re.compile(
    "[\U0001F000-\U0001FAFF\U0001F1E6-\U0001F1FF"
    "\u2600-\u27BF\u2B50\u2B55\u200D\u20E3\uFE0E\uFE0F]")


def _sprite_path(cluster, lh):
    key = hashlib.sha1(("-".join(f"{ord(c):x}" for c in cluster)
                        + f"|{lh}").encode()).hexdigest()[:16]
    return os.path.join(UI_EMOJI_DIR, key + ".png")


def _build_sprite(cluster, lh, path):
    f, em_h = _emoji["font"], _emoji["em_h"]
    try:
        w = max(1, int(math.ceil(f.getlength(cluster))))
        canvas = Image.new("RGBA", (w, em_h), (0, 0, 0, 0))
        ImageDraw.ImageDraw(canvas).text((0, 0), cluster, font=f,
                                         embedded_color=True)
        if canvas.getbbox() is None:
            return None  # nothing rendered — decline this cluster
        # resize the whole em box, never crop-to-bbox: cropping gives a
        # per-emoji origin that must be carried through the resize for
        # zero saved pixels (implementer review)
        spr = canvas.resize((max(1, round(w * lh / em_h)), lh),
                            Image.Resampling.LANCZOS)
        try:  # disk cache is an optimisation, not a need
            os.makedirs(UI_EMOJI_DIR, exist_ok=True)
            tmp = path + ".part"
            spr.save(tmp, "PNG")
            os.replace(tmp, path)  # atomic, like _art_disk_save
        except OSError:
            pass
        return spr
    except Exception:
        return None


def emoji_sprite(cluster, lh):
    """The cluster as an RGBA sprite scaled to line height lh, or None.
    RAM dict -> disk PNG -> render at the strike. Failures are also
    remembered, so a bad cluster is not re-attempted every frame.
    Assumes emoji_active() was checked by the caller."""
    key = (cluster, lh)
    if key in _sprites:
        return _sprites[key]
    spr = None
    path = _sprite_path(cluster, lh)
    try:
        spr = Image.open(path)
        spr.load()
        if spr.mode != "RGBA" or spr.size[1] != lh:
            raise OSError("wrong shape")
    except FileNotFoundError:
        spr = None
    except OSError:
        spr = None
        try:  # corrupt cache file: self-heal like the art cache does
            os.remove(path)
        except OSError:
            pass
    if spr is None:
        spr = _build_sprite(cluster, lh, path)
    if len(_sprites) >= _SPRITES_MAX:
        _sprites.clear()
    _sprites[key] = spr
    return spr


def _runs(text, font):
    """Split into ('t', str) / ('s', sprite) runs. Declined clusters
    (ZWJ/flag/keycap) and failed sprites dissolve back into the
    neighbouring text via screen_text; None when nothing is pasteable
    (the caller then takes the legacy whole-string path)."""
    asc, desc = font.getmetrics()
    lh = asc + desc
    out = []

    def _text(piece):
        piece = screen_text(piece)
        if not piece:
            return
        if out and out[-1][0] == "t":
            out[-1] = ("t", out[-1][1] + piece)
        else:
            out.append(("t", piece))

    pos = 0
    any_sprite = False
    for m in _CLUSTER.finditer(text):
        if m.start() > pos:
            _text(text[pos:m.start()])
        cluster = m.group()
        spr = None
        if m.lastgroup == "emoji" and "\u200D" not in cluster:
            base = _MODS.sub("", cluster)
            if base:
                spr = emoji_sprite(base, lh)
        if spr is not None:
            out.append(("s", spr))
            any_sprite = True
        else:
            _text(cluster)
        pos = m.end()
    if pos < len(text):
        _text(text[pos:])
    return out if any_sprite else None


class RichDraw(ImageDraw.ImageDraw):
    """ImageDraw that renders emoji clusters as color sprites.

    Guard-first: text with none of the emoji machinery characters is
    delegated to Pillow VERBATIM — same bytes as a plain ImageDraw (the
    pixel-identity pin in tests/ui_emoji.py holds this). Strings that
    contain scrub-relevant characters but no drawable sprite take the
    legacy path: screen_text() then Pillow with the ORIGINAL anchor —
    which is exactly what the api-edge scrub produced before. Only a
    string with a live sprite gets the split/anchor math, where there
    is no previous rendering to stay identical to.

    Anchor conversion (measured, 360/360 exact): base x uses Pillow's
    own rounding ceil(v - 0.5); vertical anchors handled: a/m (the only
    ones ui.py uses) — 'b'-anchors would fall back to 'a'."""

    def text(self, xy, text, fill=None, font=None, anchor=None, **kw):
        if (not isinstance(text, str) or "\n" in text
                or not _MAYBE.search(text)):
            return super().text(xy, text, fill=fill, font=font,
                                anchor=anchor, **kw)
        runs = (_runs(text, font)
                if font is not None and emoji_active() else None)
        if not runs:
            return super().text(xy, screen_text(text), fill=fill,
                                font=font, anchor=anchor, **kw)
        asc, desc = font.getmetrics()
        lh = asc + desc
        x, y = xy
        a = anchor or "la"
        w = 0.0
        for kind, val in runs:
            w += (super().textlength(val, font=font) if kind == "t"
                  else val.width)
        if a[0] == "m":
            x = math.ceil(x - w / 2 - 0.5)
        elif a[0] == "r":
            x = math.ceil(x - w - 0.5)
        if a[1] == "m":
            baseline = y + (asc - desc + 1) // 2  # half-up, like Pillow
            va = "lm"
        else:
            baseline = y + asc
            va = "la"
        spr_base = round(_emoji["asc"] * lh / _emoji["em_h"])
        pos = float(x)
        for kind, val in runs:
            if kind == "t":
                super().text((pos, y), val, fill=fill, font=font,
                             anchor=va, **kw)
                pos += super().textlength(val, font=font)
            else:
                self._image.paste(val, (int(pos), baseline - spr_base),
                                  val)
                pos += val.width

    def textlength(self, text, font=None, *a, **kw):
        if not isinstance(text, str) or not _MAYBE.search(text):
            return super().textlength(text, font=font, *a, **kw)
        runs = (_runs(text, font)
                if font is not None and emoji_active() else None)
        if not runs:
            return super().textlength(screen_text(text), font=font,
                                      *a, **kw)
        total = 0.0
        for kind, val in runs:
            total += (super().textlength(val, font=font) if kind == "t"
                      else val.width)
        return total


def _draw(img):
    """Every UI text call flows through RichDraw — one seam, so no call
    site can regress to tofu (QA review 2026-08-12)."""
    return RichDraw(img)


W = H = 240
PNG_PATH = os.environ.get("VIBB_UI_PNG")

# playful-ui: the carousel shelf slide. Force-off under the PNG dev
# backend so every existing test stays byte- AND call-count-stable (QA
# review 2026-08-12 — the gate must key on the env, not the display
# type: most tests pair VIBB_UI_PNG with a hand-rolled FakeDisplay).
# VIBB_UI_ANIM=0 kills it on the box too.
UI_ANIM = (not PNG_PATH) and os.environ.get("VIBB_UI_ANIM", "1") != "0"
SLIDE_MS = 0.150          # wall-clock cap for one glide
# (scheduled time, ease-out offset) — the two END frames are cut on
# purpose: p=0 is just "label pops out" and the landing is painted by
# the loop's render() WITH label/progress, so it arrives with the stop
SLIDE_SCHED = ((0.030, 0.35), (0.060, 0.65), (0.090, 0.85),
               (0.120, 0.95))
SLIDE_TRAVEL = 208        # 176px cover + 32px shelf gap = full clearance

# Album-art disk cache: remote covers are fetched ONCE ever, then served
# from disk across track changes, UI restarts and reboots. Capped by
# _art_disk_save's pruning; content.py's prune_cache leaves the dir alone.
UI_ART_DIR = os.path.join(
    os.environ.get("VIBB_CACHE", "/var/lib/vibb/cache"), "ui-art")
# Sized to CONVERGE for the real library: a 212-episode show with
# per-episode art touches 2-3 sizes each (56 rows / 128 / 176), so 400
# thrashed — every save evicted a thumb that the next browse re-decoded
# and re-saved, forever (field 2026-08-12: Snipp Snapp Snute fast-skip
# felt slow while Hallo Bablo, whose episodes share one cover, flew).
# Thumbs are 5-15KB; 1600 is <=25MB against a 20GB cache partition.
UI_ART_MAX_FILES = int(os.environ.get("VIBB_UI_ART_MAX", "1600"))
_art_saves = [0]  # cap check amortized: a listdir per save was a tax


def _art_disk(ref, size, square=False, mtime=None):
    """Disk path for one thumbnail. Local sources fold their mtime into
    the key — a re-synced podcast cover (same path, new bytes) must get
    a fresh thumb, exactly like the RAM key in _art_key; the stale file
    is left for the UI_ART_MAX_FILES cap to age out."""
    tag = f"{ref}|{size}|{'sq' if square else 'fit'}"
    if mtime is not None:
        tag += f"|{int(mtime)}"
    h = hashlib.sha1(tag.encode()).hexdigest()[:16]
    return os.path.join(UI_ART_DIR, f"{h}.jpg")


def _art_disk_load(path):
    """A cached thumb, or None. Corrupt files are deleted so the next
    call rebuilds them — same self-heal the remote path always had."""
    img = None
    try:
        img = Image.open(path)
        img.load()
        return img
    except OSError:
        if img is not None or os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        return None


def _art_disk_save(img, path):
    """Persist a fetched thumbnail; cap the dir by dropping the oldest.
    Best-effort — a full/broken SD must never break cover display."""
    try:
        os.makedirs(UI_ART_DIR, exist_ok=True)
        img.save(path + ".part", "JPEG", quality=85)
        os.replace(path + ".part", path)
        _art_saves[0] += 1
        if _art_saves[0] % 25 == 0:  # scan the dir 1-in-25 saves, not per
            names = [os.path.join(UI_ART_DIR, n)
                     for n in os.listdir(UI_ART_DIR) if n.endswith(".jpg")]
            if len(names) > UI_ART_MAX_FILES:
                names.sort(key=os.path.getmtime)
                for p in names[:len(names) - UI_ART_MAX_FILES]:
                    os.remove(p)
    except OSError:
        pass
FIFO_PATH = os.environ.get("VIBB_UI_INPUT")
TICK_S = 0.2
STATUS_POLL_S = 1.0
SESSION_WAIT_TICKS = 6   # x0.5s the splash waits for the daemon's
#                          session verdict. It is normally instant (the
#                          daemon settles it in its own boot thread), so
#                          this is only a guard against a slow clock —
#                          and an unresolved one lands on now-playing,
#                          which is what it did before any of this.
BURST_POLL_S = 0.3   # /status cadence while a command is in flight
#                      (poll_burst_until); measured from fetch
#                      completion, so a slow daemon self-paces
SYSTEM_POLL_S = 30.0
DARK_POLL_TICK_S = 5.0  # P1 poller: while the screen is dark it only waits for
                        # a wake kick (no HTTP all night); TICK_S when the
                        # screen is on.
CONTROL_TIMEOUT = 5   # play/pause/next/prev hit the LOCAL daemon — if it
                      # can't answer in 5s the backend is wedged; fail fast
                      # so buttons keep working instead of freezing the UI
PP_RECONCILE_S = 2.0  # after an optimistic play/pause flip, hold the local
                      # icon state until a poll confirms it (or this expires)
# The X cycle. One press opens the volume card; further presses walk the
# tabs. Index 0 is a contract, not a detail: a card that has LAPSED
# always reopens on volume, because that is the reflex the box has
# taught for months.
CARDS = ("vol", "seek", "shuf")
CARD_TTL_S = 5.0   # the card lives this long past the last press. Three
#                    seconds could not carry a three-tab cycle: a press
#                    only counts on RELEASE, must stay under LONG_S, and
#                    the sampler adds a tick on top — leaving ~2s of
#                    thinking time per press for a child reading a tab
#                    strip she has never seen. Combined with 'a lapsed
#                    card reopens on volume' that made the third tab
#                    unreachable rather than merely slow (QA 2026-08-14).
#                    The price is real and deliberate: B/Y are not
#                    prev/next for those five seconds.
SEEK_TAP_S = 15.0       # a TAP, every tap, on every content type
# Holding is the declaration of intent to travel: the step grows with
# TIME SINCE THE HOLD STARTED, never with press count — rapid taps stay
# 15s forever (owner design 2026-09-01; the old per-press ladder hit
# 120s within three quick taps, a surprise jump on every short song).
# (held longer than s, step becomes s-of-content). Travel rates at the
# 0.35s repeat: ~43/129/343/857 s per held second.
SEEK_HOLD_LADDER = ((1.5, 45.0), (4.0, 120.0), (8.0, 300.0))
SEEK_POST_MIN_S = 0.7   # single-flight poster pacing: the bar compounds
#                         per repeat, the NETWORK sees at most ~1.4/s
SEEK_TAIL_S = 5.0       # mirror of the daemon's end clamp — without it
#                         the echoed clamp pulls back 5s per adoption
#                         and the next repeat pushes past it again
CARD_REPEAT_S = 0.35   # hold B/Y on a card: the repeat cadence
SEEK_RECONCILE_S = 4.0  # hold the optimistic position until a poll lands
SEEK_TOL_S = 2.0        # a report this close to the target = it landed
# The render loop is single-threaded: any daemon poll it makes blocks
# every button until it returns. /system + /settings + /library used the
# 10s default, so a daemon slowed by go-librespot's blocking API (a track
# load stalls the whole HTTP layer — field 2026-07-20 12:54: control
# TimeoutError while spamming prev/playpause) froze the screen ~30s per
# refresh. Cap them at 2s like /status: a slow daemon shows one stale
# frame, never a dead screen.
RENDER_HTTP_TIMEOUT = 2.0
# Belt-and-suspenders: if the render loop ever wedges for real (a stuck
# SPI push, a runaway decode), nothing restarts vibb-ui until the
# 60-min idle shutdown — every OTHER Vibb component self-heals. A
# heartbeat the loop stamps each pass; if it goes stale past this, exit
# so systemd (Restart=always) brings the screen back in seconds. Set
# above the longest legit inline block (a 130s BT pair) so it never
# false-fires; 0 disables.
UI_WATCHDOG_S = float(os.environ.get("VIBB_UI_WATCHDOG", "180"))
NOW_RETURN_S = 10     # idle this long in a browse menu while music plays ->
                      # snap back to now-playing (once you left it, the only
                      # way back was re-tapping the same episode)

BG = (12, 12, 20)
FG = (235, 235, 235)
DIM = (140, 140, 150)
GHOST = (80, 80, 92)   # a row that IS there but currently can't deliver
#                        (sonos speakers cached from another network);
#                        darker than DIM so it reads as off even when
#                        the selection rect is under it (owner picked
#                        grey-only over an ✗ marker, mockup 2026-08-21)
HILITE = (255, 170, 30)
GOOD = (80, 200, 120)
WARN = (230, 80, 80)


def log(msg):
    print(f"vibb-ui: {msg}", flush=True)


def font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    try:
        return ImageFont.load_default(size)
    except TypeError:  # old pillow
        return ImageFont.load_default()


F_BIG, F_MED, F_SMALL = font(22), font(17), font(13)
# The 'Link phone' screen must stay lit while someone aims a camera at
# it — a QR that blanks after 30s is useless exactly when it is needed.
# Bounded, though: it holds a SECRET, and leaving it lit forever would
# both drain the battery and park the token on a screen in the living
# room. After this it auto-returns to settings and normal sleep resumes.
LINK_AWAKE_S = float(os.environ.get("VIBB_LINK_AWAKE_S", "180"))


# --- display backends -----------------------------------------------------------

BACKLIGHT_PIN = 13  # Pirate Audio backlight (BCM13, PWM1-capable)
# Owner-dropped launch scripts (docs/extras.md) — SSH is the only way in.
# Deliberately OUTSIDE every upload/media root; the API has no route.
EXTRAS_DIR = os.environ.get("VIBB_EXTRAS", "/etc/vibb/extras")
EXTRA_WRAPPER = "/usr/local/bin/vibb-extra"
# The extras message contract: a script (or the wrapper's restore, for
# generic failures) writes ONE human line here; the UI shows it on its
# next startup and deletes it. Without this, a failing extra just
# bounced back to the home screen with the reason buried in journalctl
# (owner 2026-07-29: 'no TV on HDMI' deserved to be ON the screen).
EXTRA_MSG_FILE = os.path.join(_paths.RUN_DIR, "vibb-extra.msg")
EXTRA_MSG_FRESH_S = 300  # older = a stale leftover, delete unshown


def consume_extra_msg():
    """The pending extras message, or None. Consuming DELETES the file
    either way — a stale message must never greet tomorrow's boot."""
    try:
        st = os.stat(EXTRA_MSG_FILE)
        with open(EXTRA_MSG_FILE, errors="ignore") as f:
            msg = f.read().strip()
    except OSError:
        return None
    try:
        os.unlink(EXTRA_MSG_FILE)
    except OSError:
        pass
    if not msg or time.time() - st.st_mtime > EXTRA_MSG_FRESH_S:
        return None
    return msg[:200]


class PngDisplay:
    def __init__(self):
        self.path = PNG_PATH
        self.on = True
        self.brightness = 100

    def show(self, img):
        img.save(self.path + ".tmp", "PNG")
        os.replace(self.path + ".tmp", self.path)

    def set_backlight(self, on):
        self.on = on

    def set_brightness(self, pct):
        self.brightness = pct


def _rgb565(img, rotation=90):
    """PIL RGB -> the ST7789's wire format (big-endian RGB565), same
    layout as the Pimoroni library's image_to_data — but returned as
    bytes. The library ends its conversion with .tolist(), turning
    every frame into a 115200-entry Python list; on the 600MHz
    powersave core that is ~60ms of the measured 75ms/frame push
    (rig 2026-08-12). numpy-to-bytes does the same job in a few ms."""
    import numpy as np
    a = np.rot90(np.asarray(img.convert("RGB"), dtype=np.uint16),
                 rotation // 90)
    c = ((a[..., 0] & 0xF8) << 8) | ((a[..., 1] & 0xFC) << 3) \
        | (a[..., 2] >> 3)
    return c.astype(">u2").tobytes()


class St7789Display:
    # Class-level defaults so a display built without __init__ (test
    # rigs use object.__new__) still answers these.
    _lit = False
    _t_import = 0.0
    _bl = None
    on = True
    brightness = 100

    def __init__(self):
        # Split-timed: bringing the panel up is the biggest single item
        # inside this process (2.2s measured on the box 2026-08-13) and
        # it has two very different halves — the SPI panel's own reset
        # sequence, and gpiozero, which probes its GPIO backends in turn
        # unless told which to use.
        t = time.monotonic()
        import st7789  # Pimoroni library
        # Split again: the reset sequence is NOT the cost here (we pass
        # rst=None, so st7789.reset()'s three 0.5s sleeps never run —
        # verified against the installed 1.0.1 on the box 2026-08-18).
        # What is left is the import itself (which drags in gpiodevice
        # and spidev) versus the constructor's GPIO lookup and register
        # writes, and they need very different fixes.
        imp = time.monotonic() - t
        # backlight=None: we drive BCM13 ourselves so we can DIM it (the
        # library only does on/off). PWMLED via the lgpio pin factory
        # gives real brightness control; only runs while the screen is on.
        self.disp = st7789.ST7789(
            height=240, width=240, rotation=90, port=0, cs=1, dc=9,
            backlight=None, spi_speed_hz=80 * 1000 * 1000)
        panel = time.monotonic() - t
        self._t_import = imp
        self.on = True
        self.brightness = 100
        self._fast = os.environ.get("VIBB_FAST_PUSH", "1") != "0"
        self._lit = False   # backlight stays down until the first frame
        self._bl = None
        t = time.monotonic()
        try:
            from gpiozero import PWMLED
            self._bl = PWMLED(BACKLIGHT_PIN)
            # DARK until there is something to show. vibb-backlight-off
            # kills the panel's uninitialised RAM — which reads as snow
            # on a lit screen — for the whole boot; raising the light
            # here, a second before the first frame exists, would put
            # that snow back for exactly that second. The first show()
            # lights it (owner 2026-08-18: "snøfilm og så kommer ui").
            self._bl.value = 0.0
        except Exception as e:
            log(f"backlight PWM unavailable ({e.__class__.__name__}) — "
                f"on/off only")
        log(f"panel {panel:.1f}s (import {self._t_import:.1f}s + init "
            f"{panel - self._t_import:.1f}s) + backlight "
            f"{time.monotonic() - t:.1f}s")

    def show(self, img):
        # Fast path: convert ourselves, push bytes through the library's
        # own set_window()/data() (they drive the DC pin correctly).
        # ANY surprise — older lib without these methods, spidev refusing
        # bytes — falls back to the stock display() forever after.
        if self._fast:
            try:
                buf = _rgb565(img, 90)
                self.disp.set_window()
                # data(b"") sends nothing but flips the DC pin to data
                # mode through the library's own GPIO handling; then
                # writebytes2 pushes the whole buffer with C-side
                # chunking. The library's data(buf) would xfer() in 29
                # chunks, each building a 4096-int rx list we throw
                # away — that was ~35ms of the frame.
                self.disp.data(b"")
                self.disp._spi.writebytes2(buf)
                self._first_frame()
                return
            except Exception as e:
                self._fast = False
                log(f"st7789 fast push off ({e.__class__.__name__}: {e})"
                    f" — using library display()")
        self.disp.display(img)
        self._first_frame()

    def _first_frame(self):
        """Light the backlight the moment a real frame is on the panel —
        once. Before this the screen is deliberately dark (see __init__).

        Never raises: the light is cosmetic, the frame is not, and this
        runs INSIDE show()'s fast path, where any exception would demote
        the box to the slow library push for the rest of the session."""
        if self._lit:
            return
        self._lit = True
        try:
            self._apply()
        except Exception as e:
            log(f"backlight raise failed ({e.__class__.__name__})")

    def _pinctrl(self, on):
        """Drive BCM13 without gpiozero — the same tool and pin
        vibb-backlight-off.service uses to pull it LOW at boot.

        This is the only way a box whose PWMLED never constructed gets
        its screen back. vibb-ui.service pins GPIOZERO_PIN_FACTORY=lgpio,
        so on a box where the lgpio build failed (install.sh warns and
        continues) gpiozero cannot fall back and _bl stays None — and
        with the pin already low, nothing else would ever raise it. The
        screen stayed dark forever; before the boot-shave it was merely
        unregulated-bright. Best-effort: pinctrl ships with raspi-utils
        and may be absent, exactly as the unit's '-' prefix allows."""
        try:
            subprocess.run(["pinctrl", "set", str(BACKLIGHT_PIN), "op",
                            "dh" if on else "dl"],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=2)
        except Exception as e:
            log(f"pinctrl backlight failed ({e.__class__.__name__})")

    def _apply(self):
        if self._bl is not None:
            self._bl.value = (self.brightness / 100.0) if self.on else 0.0
        else:
            self._pinctrl(self.on)   # no PWM: on/off only, but ON

    def set_backlight(self, on):
        self.on = on
        self._apply()

    def set_brightness(self, pct):
        self.brightness = max(10, min(100, int(pct)))
        self._apply()


def make_display():
    if PNG_PATH:
        log(f"dev display -> {PNG_PATH}")
        return PngDisplay()
    return St7789Display()


# --- input backends ---------------------------------------------------------------

class FifoInput:
    """Dev input: one char per event on a fifo (a/b/x/y press, l=long-B,
    e=long-Y, o=long-X, s=settings)."""

    def __init__(self, path):
        if not os.path.exists(path):
            os.mkfifo(path)
        self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        self.gesture_mode = False  # resolved tokens come pre-cooked here
        self.b_hold = False        # (dev fifo: tokens are pre-cooked)
        log(f"dev input <- {path}")

    def poll(self, timeout):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return []
        events = []
        for ch in os.read(self.fd, 64).decode(errors="ignore"):
            if ch in "abxy":
                events.append(ch)
            elif ch == "l":
                events.append("b_long")
            elif ch == "o":
                events.append("x_long")
            elif ch == "e":
                events.append("y_long")
            elif ch == "s":
                events.append("settings")
            elif ch == "g":
                events.append("extras")
        return events


class GpioInput:
    """Pirate Audio buttons — event/state logic. Hold A+B ~2s -> settings,
    hold X+Y ~2s -> extras (the owner-script menu, docs/extras.md).

    A and B fire on RELEASE — a press can be the start of the A+B
    combo, and firing them on press made the combo navigate the menu
    while you were holding it (select! back!). Overlapping A+B that
    never reaches HOLD_S is swallowed as a failed combo attempt, not
    delivered as two commands. X and Y got the same release-fired
    treatment when the X+Y combo landed (an instant press-fire made the
    first chord finger navigate the menu); the cost is one human
    release time of menu latency, same as A/B always had.

    In gesture_mode (the now-playing view) B, X and Y resolve
    short-vs-hold: release before LONG_S -> the plain press, held LONG_S
    -> '<name>_long' (fires while still held — but never while the A+B
    combo is forming). A does NOT hold: it is play/pause, the one
    control a child finds without looking, and it stays a plain press
    everywhere. Shuffle used to live on a hold here and moved to the
    card cycle, which shows its state instead of hiding it."""

    PINS = {"a": 5, "b": 6, "x": 16, "y": 24}
    HOLD_S = 2.0      # A+B settings combo
    LONG_S = 0.8      # B/X/Y held this long = the hold action

    def __init__(self):
        from gpiozero import Button
        self.buttons = {name: Button(pin, pull_up=True, bounce_time=0.05)
                        for name, pin in self.PINS.items()}
        self.queue = []
        self.gesture_mode = False
        self.b_hold = False   # detect a B hold even outside now-playing
                              # (the category carousel's 'up a level')
        self.down = {}        # name -> press timestamp while held
        self.tainted = set()  # a/b releases to swallow (combo attempt)
        self._long_sent = {}  # name -> the hold already fired
        self._b_gesture = False   # hold-B armed when B was pressed
        self.wake = threading.Event()  # any button activity ends poll()
        for name, btn in self.buttons.items():
            btn.when_pressed = lambda n=name: self._pressed(n)
            btn.when_released = lambda n=name: self._released(n)
        log("gpio buttons ready (BCM 5/6/16/24)")

    def _pressed(self, name):
        self.wake.set()  # end the current poll() immediately
        self.down[name] = time.monotonic()
        if name in ("x", "y"):
            # now-playing: short X = volume, held X = output;
            #              short Y = next, held Y = episode picker.
            # In menus X/Y are plain presses, fired on RELEASE so the
            # X+Y extras combo can form without navigating the menu.
            self._long_sent[name] = False
            return
        if name == "b":
            self._long_sent["b"] = False
            # judge the RELEASE by the mode the press STARTED in: b_long
            # navigates away from now-playing while still held, flipping
            # gesture_mode off — the release must not then be re-read as
            # a menu press (field bug: it re-selected the episode).
            # b_hold arms the same detection in the category carousel so
            # a held B steps up a level (short B still flips on release).
            self._b_gesture = self.gesture_mode or self.b_hold

    def _released(self, name):
        self.wake.set()
        held_since = self.down.pop(name, None)
        if name in self.tainted:
            self.tainted.discard(name)
            return
        if held_since is None:
            return
        if name in ("x", "y"):
            partner = "y" if name == "x" else "x"
            if partner in self.down:
                # overlapping X+Y released before HOLD_S: a failed
                # extras-combo attempt — swallow both, like A+B
                self.tainted.add(partner)
                return
            if not self._long_sent.get(name):
                self.queue.append(name)
            return
        other = "b" if name == "a" else "a"
        if other in self.down:
            # overlapping A+B released before HOLD_S: a failed combo
            # attempt, not two commands — swallow the other one too
            self.tainted.add(other)
            return
        if name == "b" and self._b_gesture:
            if not self._long_sent.get("b"):
                self.queue.append("b")
            return
        self.queue.append(name)

    def poll(self, timeout):
        # a button callback sets the event -> instant reaction; otherwise
        # this is the tick for hold-timing (combo, the _long gestures)
        self.wake.wait(timeout)
        self.wake.clear()
        return self._events()

    def _events(self):
        now = time.monotonic()
        if "a" in self.down and "b" in self.down:
            if now - max(self.down["a"], self.down["b"]) >= self.HOLD_S:
                # swallow both releases; drop anything queued meanwhile
                self.tainted.update(self.down)
                self.down.clear()
                self.queue.clear()
                return ["settings"]
        elif "x" in self.down and "y" in self.down:
            # the extras combo (owner gesture, mirror of A+B). While it
            # forms, the individual X/Y holds below must not fire.
            if now - max(self.down["x"], self.down["y"]) >= self.HOLD_S:
                self.tainted.update(self.down)
                self.down.clear()
                self.queue.clear()
                return ["extras"]
        elif (self._b_gesture and "b" in self.down
                and not self._long_sent.get("b")
                and now - self.down["b"] >= self.LONG_S):
            # long press fires while still held — no waiting for release
            self._long_sent["b"] = True
            self.queue.append("b_long")
        combo_xy = "x" in self.down and "y" in self.down
        for name in ("x", "y"):
            # the holds only mean something in now-playing, and never
            # while an X+Y combo is forming
            if (self.gesture_mode and not combo_xy
                    and name in self.down and not self._long_sent.get(name)
                    and now - self.down[name] >= self.LONG_S):
                self._long_sent[name] = True
                self.queue.append(f"{name}_long")
        ev, self.queue = self.queue[:], []
        return ev


class LgpioInput(GpioInput):
    """Same button logic, but the pins are SAMPLED (20Hz) over raw lgpio
    instead of watched via gpiozero callbacks: the lg alert machinery
    runs a ~1ms-tick thread that burned 13-15% CPU on the Zero around
    the clock (field measurement — one hot thread, screen dark). Four
    gpio_read ioctls every 50ms are unmeasurable, worst-case latency is
    one sample, and 50ms sampling inherently debounces."""

    def __init__(self):
        import lgpio
        self._lg = lgpio
        self._h = None
        for chip in (0, 4):  # main header: chip 0 (chip 4 on a Pi 5)
            try:
                h = lgpio.gpiochip_open(chip)
            except lgpio.error:
                continue
            try:
                for pin in self.PINS.values():
                    lgpio.gpio_claim_input(h, pin, lgpio.SET_PULL_UP)
                self._h = h
                break
            except lgpio.error:
                lgpio.gpiochip_close(h)
        if self._h is None:
            raise RuntimeError("no gpiochip exposes the button pins")
        self.queue = []
        self.gesture_mode = False
        self.b_hold = False
        self.down = {}
        self.tainted = set()
        self._long_sent = {}
        self._b_gesture = False
        self.wake = threading.Event()  # set by inherited handlers; unused
        self._level = {n: 1 for n in self.PINS}   # pull-up: 1 = released
        self._edge_at = {n: 0.0 for n in self.PINS}
        log("lgpio buttons ready (BCM 5/6/16/24, 20Hz sampled)")

    def _sample(self):
        now = time.monotonic()
        for name, pin in self.PINS.items():
            lvl = self._lg.gpio_read(self._h, pin)
            if lvl == self._level[name]:
                continue
            self._level[name] = lvl
            if now - self._edge_at[name] < 0.05:
                continue  # contact bounce — swallow the phantom edge
            self._edge_at[name] = now
            if lvl == 0:  # active low
                self._pressed(name)
            else:
                self._released(name)

    def poll(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            self._sample()
            if self.queue:
                break  # respond now — don't sit out the tick
            rest = deadline - time.monotonic()
            if rest <= 0:
                break
            time.sleep(min(0.05, rest))
        return self._events()


def make_input():
    if FIFO_PATH:
        return FifoInput(FIFO_PATH)
    try:
        return LgpioInput()
    except Exception as e:
        log(f"lgpio input unavailable ({e.__class__.__name__}: {e}) — "
            f"falling back to gpiozero")
        return GpioInput()


# --- drawing helpers ----------------------------------------------------------------

_BATT_COLOR = [None]  # hysteresis: the PiSugar percent jitters a few
                      # points, and a hard threshold made the gauge flap


def _batt_color(pct, plugged):
    if pct is None:
        return DIM
    if plugged:
        _BATT_COLOR[0] = GOOD
        return GOOD
    lo, mid = 10, 20
    prev = _BATT_COLOR[0]
    if prev == WARN:
        lo += 3    # once red, climb back to orange only at 13
    elif prev == HILITE:
        mid += 3   # once orange, back to green only at 23
    color = WARN if pct <= lo else (HILITE if pct <= mid else GOOD)
    _BATT_COLOR[0] = color
    return color


def _wifi_glyph(draw, cx, cy, color):
    """A wifi fan — dot + two arcs rising up-left/up-right — anchored at
    its base dot (cx, cy). ~13px wide, ~9px tall."""
    draw.ellipse([cx - 1, cy - 1, cx + 1, cy + 1], fill=color)
    draw.arc([cx - 3, cy - 4, cx + 3, cy + 2], 200, 340, fill=color)
    draw.arc([cx - 6, cy - 7, cx + 6, cy + 5], 205, 335, fill=color)


def _bt_glyph(draw, cx, top, bot, color):
    """The Bluetooth rune around the spine x=cx. dx sets the width."""
    dx = 4
    q, tq = top + (bot - top) // 4, bot - (bot - top) // 4
    tp, bp, mid = (cx, top), (cx, bot), (cx, (top + bot) // 2)
    ur, lr = (cx + dx, q), (cx + dx, tq)
    ul, ll = (cx - dx, q), (cx - dx, tq)
    # spine + right zigzag (the two triangles) + the left crossing stubs
    for a, b in ((tp, bp), (tp, ur), (ur, mid), (mid, lr), (lr, bp),
                 (ul, mid), (mid, ll)):
        draw.line([a, b], fill=color)


def _shuffle_glyph(draw, cx, top, bot, color):
    """Two paths swapping places, around the spine x=cx. Only ever drawn
    when shuffle is ON — its absence is the 'off' state, so the top bar
    stays quiet for the ordinary case (owner 2026-08-14).

    The heads are four pixels each, and WHERE they sit is the whole
    trick: a head on a horizontal exit stub reads as a plus sign, so
    the stub is gone and the diagonal runs all the way to the tip. Four
    pixels then form an L-corner pointing the way the path travels,
    which is enough to read as an arrow at eleven pixels tall. Bigger
    heads were tried first — filled triangles, line barbs, flat wedges —
    and every one of them landed as a blob."""
    dx = 9
    for y0, y1 in ((top, bot), (bot, top)):
        ty = 1 if y1 > y0 else -1
        draw.line([(cx - dx, y0), (cx - dx + 3, y0), (cx + dx, y1)],
                  fill=color, joint="curve")
        draw.point([(cx + dx - 1, y1), (cx + dx - 2, y1),
                    (cx + dx, y1 - ty), (cx + dx, y1 - 2 * ty)], fill=color)


def _seek_arrows(draw, x, cy, color, back=False):
    """The double triangle every player uses for rewind/fast-forward,
    ten pixels wide from x. Drawn, not typed — DejaVu has no media
    glyphs, which is why the button markers are shapes too."""
    s = 5
    for k in (0, 7):
        if back:
            draw.polygon([(x + k + s, cy - s), (x + k + s, cy + s),
                          (x + k, cy)], fill=color)
        else:
            draw.polygon([(x + k, cy - s), (x + k, cy + s),
                          (x + k + s, cy)], fill=color)


def _conn_icons(draw, system, right_x, y, h):
    """Wi-Fi + Bluetooth status, just left of the battery. Always shown:
    GOOD when connected, DIM + a slash when not — so 'why won't it play'
    (speaker off, or wifi down) is legible at a glance instead of only
    surfacing in a popup. State comes from the /system snapshot the UI
    already polls; no extra probes. Returns the x it consumed leftward."""
    wifi = (system or {}).get("wifi") or {}
    wifi_on = bool(wifi.get("ip")) and not wifi.get("hotspot")
    # bt_ready is the configured speaker's live A2DP transport (daemon
    # computes it cheaply); absent key (older daemon) -> treat as no icon
    bt_key = "bt_ready" in (system or {})
    bt_on = bool((system or {}).get("bt_ready"))

    cy = y + h - 2
    x = right_x
    if bt_key:
        x -= 9
        _bt_glyph(draw, x, y + 1, y + h - 1, GOOD if bt_on else DIM)
        if not bt_on:
            draw.line([x - 5, y + h + 1, x + 5, y - 1], fill=DIM)
        x -= 12
    x -= 4
    _wifi_glyph(draw, x, cy, GOOD if wifi_on else DIM)
    if not wifi_on:
        draw.line([x - 7, y + h + 1, x + 7, y - 1], fill=DIM)
    x -= 8
    # Shuffle rides in on the /status fold (see App._set) so the icon
    # row still reads nothing but self.system. Present = on; there is
    # no 'off' glyph, by design.
    if (system or {}).get("shuffle"):
        x -= 11
        _shuffle_glyph(draw, x, y + 2, y + h - 2, HILITE)
        x -= 11
    return x


def battery_corner(draw, system):
    """Battery gauge top-right — on every view. Just the bar (color
    carries the message: green ok/charging, orange <=20, red <=10);
    the exact percent lives in the PWA. Wi-Fi + BT status sit to its
    left."""
    pct = (system or {}).get("battery")
    plugged = (system or {}).get("plugged")
    x, y, w, h = W - 32, 8, 24, 11
    color = _batt_color(pct, plugged)
    draw.rounded_rectangle([x, y, x + w, y + h], radius=2, outline=color)
    draw.rectangle([x + w + 1, y + 3, x + w + 2, y + h - 3], fill=color)
    if pct is not None:
        fill = max(2, int((w - 4) * min(pct, 100) / 100))
        draw.rectangle([x + 2, y + 2, x + 2 + fill, y + h - 2], fill=color)
    if plugged:
        # a small bolt over the gauge: charging must be readable at a
        # glance — color alone only said it when the pack was LOW
        # (plugged forces green), a charging 50% looked identical to an
        # idle 50% (owner 2026-07-29). Orange with a dark halo reads on
        # both the filled and the empty half, and can't be mistaken for
        # the low-battery orange (the gauge itself is green while
        # plugged).
        cx, cy = x + w // 2, y + h // 2
        strokes = [((cx + 2, y + 1), (cx - 1, cy)),
                   ((cx - 1, cy), (cx + 2, cy)),
                   ((cx + 2, cy), (cx - 1, y + h - 1))]
        for a, b in strokes:
            draw.line([a, b], fill=BG, width=3)
        for a, b in strokes:
            draw.line([a, b], fill=HILITE, width=1)
    _conn_icons(draw, system, x - 6, y, h)


def wrap_text(d, text, font, max_w):
    """Word-wrap for message screens: explicit \\n is a hard break;
    within a paragraph, words fill lines up to max_w pixels (measured,
    not guessed). A single word wider than the panel stays on its own
    line — clipped beats an infinite loop."""
    lines = []
    for para in text.split("\n"):
        cur = ""
        for word in para.split():
            cand = (cur + " " + word).strip()
            if not cur or d.textlength(cand, font=font) <= max_w:
                cur = cand
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    return lines or [""]


MARQUEE_STEP_S = 0.35  # how fast a too-long selected label slides


def marquee(text, maxlen, t0=0.0):
    """(visible_window, scrolling?) for a list label. A too-long SELECTED
    row slides through its text — pause at each end — so the whole name
    can be read, instead of forever showing just the start.

    t0 is the PHASE ANCHOR: the moment this label became the selected
    one (App._marquee_t0). Without it the window position came from the
    global wall clock, so landing on a tile showed a random MIDDLE of
    the title instead of its start (field 2026-08-12). With the anchor,
    a fresh selection rests at the start for the lead-in steps, then
    slides — the start of the name is always what you see first.

    An emoji cluster counts as ONE character here but paints ~TWO chars
    wide (sprite advance ≈ line height), so each live cluster is
    charged 2 in the length math — otherwise a sprite title overruns
    its row (design review 2026-08-12; the residual ~9px overrun of a
    sliding window is accepted v1 cosmetics). With sprites off the
    scrub reduces clusters to single chars and the charge is zero."""
    n = (sum(1 for _ in _CLUSTER.finditer(text))
         if emoji_active() and _MAYBE.search(text) else 0)
    if len(text) + n <= maxlen:
        return text, False
    span = len(text) + n - maxlen
    period = span + 8  # 4 resting steps at each end
    step = int((time.monotonic() - t0) / MARQUEE_STEP_S) % period
    off = max(0, min(span, step - 4))
    return text[off:off + maxlen], True


def draw_list(draw, title, items, sel, system, hint=None, maxlen=24,
              t0=0.0):
    draw.text((10, 4), title, font=F_MED, fill=DIM)
    battery_corner(draw, system)
    top, row_h, visible = 30, 30, 6
    first = max(0, min(sel - 2, len(items) - visible))
    scrolling = False
    for i, item in enumerate(items[first:first + visible]):
        idx = first + i
        y = top + i * row_h
        if idx == sel:
            draw.rounded_rectangle([4, y - 2, W - 4, y + row_h - 6],
                                   radius=6, fill=(40, 40, 60))
        if isinstance(item, tuple):
            # (label, right[, ghost]) — ghost greys the label in BOTH
            # states: a row that exists but can't deliver right now
            label, right = item[0], item[1]
            ghost = len(item) > 2 and bool(item[2])
        else:
            label, right, ghost = item, None, False
        if idx == sel:
            label, rolls = marquee(label, maxlen, t0=t0)
            scrolling = scrolling or rolls
        else:
            label = label[:maxlen]
        draw.text((14, y), label, font=F_MED,
                  fill=GHOST if ghost else FG if idx == sel else DIM)
        if right:
            draw.text((W - 14, y), right, font=F_MED,
                      fill=HILITE if idx == sel else DIM, anchor="ra")
    if len(items) > visible:
        frac_top = first / len(items)
        frac_bot = (first + visible) / len(items)
        draw.rectangle([W - 5, top + frac_top * 180,
                        W - 3, top + frac_bot * 180], fill=DIM)
    if hint:
        draw.text((10, H - 18), hint, font=F_SMALL, fill=DIM)
    return scrolling


def wrap_two(d, text, fnt, maxw):
    """Split text onto up to two lines (word-wrapped). The second line is
    returned IN FULL — the caller marquees it when it overflows, so the
    part that tells kids' episodes apart is never simply cut off."""
    if d.textlength(text, font=fnt) <= maxw:
        return [text]
    words = text.split()
    first = ""
    while words:
        cand = (first + " " + words[0]).strip()
        if d.textlength(cand, font=fnt) > maxw:
            break
        first = cand
        words.pop(0)
    if not first:  # one monster word — hard-split it
        first = text
        while d.textlength(first, font=fnt) > maxw and len(first) > 1:
            first = first[:-1]
        words = [text[len(first):]]
    return [first, " ".join(words)]


def fmt_time(s):
    if s is None:
        return "--:--"
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 \
        else f"{s // 60}:{s % 60:02d}"


def fmt_bytes(n):
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# --- the app ---------------------------------------------------------------------

class App:
    def __init__(self, display, inputs):
        self.display = display
        self.inputs = inputs
        self.view = "home"          # home|entries|episodes|now|settings|storage
        self.stack = []             # (view, sel) breadcrumbs for back
        self.sel = 0
        self.library = {"sections": []}
        self.section = None
        self.expanded = None        # /expand result for current entry
        self.entry = None
        self.status = {}
        self.system = {}
        self.bt = {"devices": []}
        self._pending = []          # events caught mid-slide (mash rule)
        self.sonos = {"players": []}  # cached speaker list (menu gating)
        self.bt_found = []
        self.settings = {"screen_timeout_s": 30, "idle_shutdown_min": 30,
                         "volume_cap": 100}
        self.volume_flash = 0.0     # show volume overlay until this time
        self.volume_shown = None
        self.vol_mode_until = 0.0   # while set: a card is up and owns B/Y
        self.card_idx = 0           # which tab of CARDS that card shows
        self._seek_hold_t0 = 0.0    # when the current HOLD began
        self._seek_post_lock = threading.Lock()
        self._seek_dirty = False    # a compounded target awaits posting
        self._seek_posting = False  # the single-flight poster is alive
        self._seek_last_post = -1e9
        self.seek_dir = 0           # -1/+1 of the last seek press
        self.seek_shown = None      # the step just applied, for the card
        self.seek_refused = False   # a seek the box could not do
        self._card_repeat = None    # (dir, next_at) while B/Y is held down
        self._pos_expect = None     # optimistic position: where we told
        self._pos_until = 0.0       #   the user we jumped, held until a
        self._pos_at = 0.0          #   /status confirms it or this passes
        self._pos_key = None        #   the track it was true for
        self.output_flash = 0.0     # show the output-switch popup until this
        self.output_shown = ""      # the device name to name in that popup
        self.output_warning = False  # switched to a device with no sound card
        self.bt_connecting_until = 0.0  # popup X pressed: full connect running
        self.wifi_connecting_until = 0.0  # X pressed: wifi reconnect running
        self.catch_up_until = 0.0   # repaint every tick until this time
        self.play_offline = False   # _play_async parked a no-internet verdict
        self.shuffle_refused = False  # a shuffle the box could not do
        self._pp_expect = None      # optimistic play/pause: expected playing
        self._pp_until = 0.0        #   state, held until a poll confirms it
        self._expect_target = None  # optimistic new-tile card: only /status
        self._expect_until = 0.0    #   for THIS target may replace it
        self.poll_burst_until = 0.0  # /status at BURST_POLL_S until then
        self.last_status = 0.0
        self.last_system = 0.0
        self.last_input = time.monotonic()
        self.user_touched = False
        self.dirty = True
        self.last_render = 0.0
        self.marquee_active = False  # keep repainting while a label slides
        self.car_sel = 0            # carousel: index into the shown entries
        self.cat_sel = 0            # nav mode 2: index into the categories
        self.car_section = None     # nav mode 2: selected category's id, or
                                    # None while at the category level
        self.artwork_cache = {}
        self._art_pending = set()   # remote covers being fetched off-thread
        self._art_fails = {}        # per-cover failure count -> retry backoff
        self._now_art_prev = (None, None)  # (target, last shown now-cover)
        self._lib_at = 0.0          # last /library fetch (TTL'd)
        self._poll_wake = threading.Event()  # P1: kick the poller to refetch

    # -- data ---------------------------------------------------------------

    def _set(self, attr, value):
        """Repaint only when the data actually changed — every repaint is
        a full PIL compose + a 115KB SPI push, and a paused now-view
        otherwise redraws an identical frame every STATUS_POLL_S."""
        # Optimistic play/pause: the icon flips locally on press so it tracks
        # the music (which responds at once). Until a poll CONFIRMS the new
        # state (or the window expires) keep our value, so a stale go-librespot
        # report on the next /status can't flicker the icon back and forth.
        if attr == "status" and isinstance(value, dict) \
                and self._pp_expect is not None:
            if time.monotonic() >= self._pp_until \
                    or value.get("playing") == self._pp_expect:
                self._pp_expect = None  # confirmed or timed out -> accept
            else:
                value = {**value, "playing": self._pp_expect}
        # Optimistic position: a seek moves the bar on the press, or the
        # user over-seeks while /status catches up. Unlike play/pause
        # this cannot confirm on EQUALITY — position is continuous, and
        # a correct report is the target PLUS whatever played since. So
        # the window grows with elapsed time, while the lower bound
        # stays tight: a pre-seek report (the whole failure mode) sits
        # far below on a forward seek and far above on a backward one,
        # and gets overridden either way.
        if attr == "status" and isinstance(value, dict) \
                and self._pos_expect is not None:
            now = time.monotonic()
            pos = value.get("position")
            landed = (pos is not None
                      and self._pos_expect - SEEK_TOL_S <= pos
                      <= self._pos_expect + SEEK_TOL_S + (now - self._pos_at))
            if self._track_key(value) != self._pos_key:
                # a different book/episode: drop the expectation AND the
                # ladder, since the next press is a fresh piece of audio
                self._pos_expect = None
                self.seek_dir, self._seek_hold_t0 = 0, 0.0
            elif now >= self._pos_until or landed:
                # Confirmed or expired: accept the truth, but LEAVE THE
                # LADDER ALONE. Confirmation is the normal case and it
                # arrives fast — the press opens a 0.3s burst window, and
                # a held seek repeats at 0.35s, so resetting here meant
                # every repeat started over at 30s and the acceleration
                # could never build (QA 2026-08-14). Reversal and a cold
                # card open are what reset the ladder; both already do.
                self._pos_expect = None
            else:
                value = {**value, "position": self._pos_expect}
        # A modal popup COVERS the card completely (its box is bigger),
        # and without this the card would keep owning B/Y underneath it.
        # Invisible volume is merely odd; an invisible seek loses your
        # place in a way one press cannot undo.
        if attr == "status" and isinstance(value, dict) \
                and self.vol_mode_until and (
                    value.get("bt_waiting") or value.get("bt_lost")
                    or (value.get("spotify_offline")
                        and value.get("source") == "spotify")):
            self.vol_mode_until = self.volume_flash = 0.0
        # Optimistic new-tile card: after a tap the screen already shows
        # the tapped entry (_play_async). The daemon commits its source
        # switch LATE — until then /status truthfully describes the
        # PREVIOUS playback (field 2026-08-12: old track shown seconds
        # after a tap) — so inside the window only a status that talks
        # about the tapped target may replace the card. Expiry lets the
        # truth win no matter what (a failed play falls back honestly).
        if attr == "status" and isinstance(value, dict) \
                and self._expect_target is not None:
            if time.monotonic() >= self._expect_until \
                    or value.get("target") == self._expect_target:
                self._expect_target = None  # confirmed or expired
            else:
                return
        if getattr(self, attr) != value:
            setattr(self, attr, value)
            self.dirty = True
            # Fast-path the BT status icon: /status (1-2s) carries the
            # live connected state, so fold it into self.system (which
            # the icon reads, but the /system poll refreshes only every
            # 30s) — the icon then tracks connect/drop as fast as the
            # popup instead of lagging (field 2026-07-20).
            if attr == "status" and isinstance(value, dict) \
                    and "bt_connected" in value:
                # whole-dict swap, NOT in-place mutation: with P1 the poller
                # and the input thread both publish self.system, and an
                # in-place write could be read half-updated by the render
                # thread. An atomic reference swap can't be.
                base = self.system if isinstance(self.system, dict) else {}
                self.system = {**base, "bt_ready": value["bt_connected"],
                               # same trick for the shuffle glyph: the
                               # icon row reads self.system only, so the
                               # daemon's shuffle state rides along
                               "shuffle": bool(value.get("shuffle"))}

    def _poller(self):
        """P1: owns ALL daemon HTTP so the render/input loop never blocks on
        the network (a slow /status behind a go-librespot track-load used to
        stall the button->repaint path up to ~2s). Parks while the screen is
        dark — no HTTP all night — and a _force_poll() kick un-parks it and
        forces an immediate refetch."""
        while True:
            # Clear BEFORE fetching: a kick arriving DURING _poll_once leaves
            # the event set, so the next wait() returns at once (one redundant
            # poll) instead of swallowing the requested refetch.
            self._poll_wake.clear()
            if self.display.on:
                try:
                    self._poll_once()
                except Exception as e:
                    log(f"poller error: {e!r}")
            self._poll_wake.wait(
                TICK_S if self.display.on else DARK_POLL_TICK_S)

    def _poll_once(self):
        """Fetch /system, /settings, /status, /library into cached state via
        _set (which repaints on change). Pure data — NO view mutation; the
        home->now snap and nav reconcile run on the main loop
        (_reconcile_view), so this is safe off the render thread."""
        now = time.monotonic()
        # /status FIRST — it carries the now-view album art + progress bar and
        # the playing state. On a screen WAKE the run loop forces BOTH
        # last_status and last_system to 0; if /system (battery nc forks, wifi
        # read, bt check — the slow one) went first, the user-visible data
        # would land seconds behind it (field 2026-07-22: wake -> art/progress
        # only corrected after several seconds). Poll what the screen shows
        # before the housekeeping polls.
        if (self.view == "home" and not self.user_touched
                and now - self.last_status > 2.0):
            self.last_status = now
            try:
                self._set("status", api_get("/status", timeout=2))
            except (OSError, ValueError):
                self._set("status", {})
        elif self.view in ("now", "episodes", "carousel", "cats") \
                and now - self.last_status > (
                    BURST_POLL_S if now < self.poll_burst_until
                    else STATUS_POLL_S):
            self.last_status = now
            try:
                self._set("status", api_get("/status", timeout=2))
            except OSError:
                self._set("status", {})
            if time.monotonic() < self.poll_burst_until:
                # burst cadence counts from COMPLETION: a slow status()
                # (50-400ms, 2s timeout worst) must widen the gap, never
                # produce back-to-back fetches (QA 2026-08-13)
                self.last_status = time.monotonic()
        # v0.1.0: the fork's metadata cache names the UPCOMING track's
        # cover — fetch it into the art disk cache now, while nothing is
        # being skipped, so the next press paints its art instantly (the
        # now-card renders 128px square). artwork_async dedupes: one
        # background fetch per cover ever, backoff respected.
        nxt = ((self.status or {}).get("spotify") or {}).get("next_artwork")
        if nxt:
            self.artwork_async(nxt, 128, square=True)
        if now - self.last_system > SYSTEM_POLL_S:
            self.last_system = now
            try:
                self._set("system", api_get("/system",
                                            timeout=RENDER_HTTP_TIMEOUT))
                self._set("settings", api_get("/settings",
                                              timeout=RENDER_HTTP_TIMEOUT))
                # brightness may have changed from the PWA — apply live
                self.display.set_brightness(
                    self.settings.get("screen_brightness", 100))
            except OSError:
                pass
        self.load_library()

    def _force_poll(self):
        """Make the poller refetch /status now (after a play/enter/wake)."""
        self.last_status = 0.0
        self._poll_wake.set()

    def _reconcile_view(self):
        """Main-thread view mutations that used to live in refresh(): the
        nav-mode reconcile and the home->now snap. Reads cached self.status
        (the poller keeps it fresh); never touches the network."""
        self._apply_nav_mode()
        if (self.view == "home" and not self.user_touched
                and (self.status or {}).get("playing")):
            self.stack = [("home", 0)]
            self.view = "now"
            self.dirty = True

    def _nav_mode(self):
        """0 = text menus, 1 = flat cover carousel, 2 = category carousel."""
        try:
            return int(self.settings.get("simple_nav") or 0)
        except (TypeError, ValueError):
            return 0

    def carousel_cats(self):
        """Nav mode 2: the categories shown as big tiles — only those with
        content (entries, or a followed profile the sweeper fills in)."""
        return [s for s in (self.library or {}).get("sections", [])
                if s.get("entries") or s.get("spotify_user")]

    def carousel_entries(self):
        """The entries the cover carousel shows: a single category's in
        nav mode 2 (once one is opened), else every entry flat."""
        if self._nav_mode() == 2 and self.car_section is not None:
            for s in (self.library or {}).get("sections", []):
                if s.get("id") == self.car_section:
                    return s.get("entries") or []
            return []  # the category was deleted under us
        return self.flat_entries()

    def _root_view(self):
        """Where 'back to browsing' lands for the active nav mode."""
        return {0: "home", 1: "carousel", 2: "cats"}[self._nav_mode()]

    def _apply_nav_mode(self):
        """Follow the simple_nav setting live — flipped in the PWA or the
        box's settings menu. Only browse-side views are swapped; an open
        settings/bt view is left alone and reconciles when it is left."""
        nav = self._nav_mode()
        # Which browse views are legal for each mode. episodes / now are
        # shared sub-views and never reconciled. Mode 2 has TWO carousel
        # levels — but a 'carousel' with NO category selected is a leftover
        # flat carousel from mode 1 (switching flat -> categories while
        # browsing it), which must drop back to the category root, not
        # strand the kid on a flat carousel with no way up.
        if nav == 0:
            legal = ("home", "entries")
        elif nav == 1:
            legal = ("carousel",)
        else:  # nav 2
            legal = ("cats",) if self.car_section is None \
                else ("cats", "carousel")
        reconcilable = ("home", "entries", "carousel", "cats")
        if self.view in reconcilable and self.view not in legal:
            self.stack, self.view, self.sel = [], self._root_view(), 0
            self.car_section = None
            self.dirty = True

    def load_library(self, ttl=2.0):
        """/library with a small TTL. P1: the background poller owns this now
        (the render path no longer calls it), and it publishes through _set
        so a changed library triggers a repaint. Always stores a dict with a
        'sections' key so readers can subscript it safely."""
        now = time.monotonic()
        if now - self._lib_at < ttl and (self.library or {}).get("sections"):
            return
        self._lib_at = now
        try:
            lib = api_get("/library", timeout=RENDER_HTTP_TIMEOUT)
            self._set("library", lib if isinstance(lib, dict)
                      and "sections" in lib else {"sections": []})
        except OSError:
            self._set("library", {"sections": []})

    def flat_entries(self):
        """Every library entry in order — the kid-mode carousel is flat:
        one big picture per entry, no categories to understand."""
        return [e for s in (self.library or {}).get("sections", [])
                for e in s.get("entries", [])]

    def _art_key(self, ref, size, square=False):
        """Cache key for one artwork. Local files carry their mtime, so a
        re-uploaded category logo (same path, new content) refreshes on
        the next render instead of showing the old picture forever. The
        square flag namespaces cover crops from letterbox-fit logos."""
        if not ref.startswith("http"):
            try:
                return (ref, size, square, int(os.path.getmtime(ref)))
            except OSError:
                pass
        return (ref, size, square)

    def artwork(self, ref, size=110, square=False):
        if not ref:
            return None
        key = self._art_key(ref, size, square)
        cached = self.artwork_cache.get(key)
        if isinstance(cached, float):  # failed earlier — when to retry
            if time.monotonic() < cached:
                return None
        elif key in self.artwork_cache:
            return cached
        fetched = False
        try:
            if ref.startswith("http"):
                # Disk cache first: kids replay the same playlists, and
                # re-fetching every cover on every track change (and after
                # every reboot) both delayed the cover seconds and fought
                # the audio stream for the radio (field 2026-07-18). One
                # fetch per cover EVER; thumbnails persist under
                # CACHE_DIR/ui-art (prune_cache leaves it alone).
                disk = _art_disk(ref, size, square)
                img = _art_disk_load(disk)
                if img is None:
                    # 4s, not 10: a stalled fetch showed the placeholder
                    # for 10s before the (fast, escalating) retry could
                    # even start — field 2026-07-19 "art tar 5+ sek".
                    # And ask the CDN for the 300px variant of Spotify's
                    # 640px album art first (standard id-prefix swap):
                    # a third of the bytes over the shared radio and half
                    # the decode at 600MHz; the screen renders <=176px.
                    import io
                    import urllib.request      # lazy: 0.32s warm / ~0.8s
                    #   cold off the SD, and this box streams to Sonos or
                    #   a BT speaker for whole sessions without ever
                    #   fetching a cover. -X importtime, 2026-08-18.
                    small = ref.replace("ab67616d0000b273",
                                        "ab67616d00001e02")
                    try:
                        with urllib.request.urlopen(small, timeout=4) as r:
                            raw = r.read()
                    except OSError:
                        if small == ref:
                            raise
                        with urllib.request.urlopen(ref, timeout=4) as r:
                            raw = r.read()
                    img = Image.open(io.BytesIO(raw))
                    fetched = True
            else:
                # Local sources (podcast covers/episode images the sync
                # downloaded) get the SAME thumb cache as remote art:
                # the originals are often 1400-3000px, and re-decoding
                # them after every UI restart (= every deploy) cost a
                # 100-500ms placeholder flash per cover at 600MHz
                # (energy audit follow-up 2026-08-12). mtime keys the
                # thumb so a re-synced cover refreshes, like _art_key.
                try:
                    mt = os.path.getmtime(ref)
                except OSError:
                    mt = None
                disk = _art_disk(ref, size, square, mtime=mt)
                img = _art_disk_load(disk)
                if img is None:
                    img = Image.open(ref)
                    fetched = True  # decoded from the original -> save
            img = img.convert("RGB")
            if square:
                # Fill the tile: scale to cover, then centre-crop to a
                # square. Non-square source art (NRK series/episodes with
                # no squareImage fall back to a 16:9 banner) otherwise
                # thumbnailed to ~half height and floated in the slot —
                # field 2026-07-20 'album art halvparten så høy'.
                img = ImageOps.fit(img, (size, size), Image.LANCZOS)
            else:
                img.thumbnail((size, size))
            if fetched:
                _art_disk_save(img, disk)  # the branch-correct path —
                #                            local keys carry mtime
            # drop stale versions of the same file (older mtime keys) —
            # keyed on (ref, size, square) so a cover crop and a logo fit
            # of the same source never evict each other
            for k in [k for k in self.artwork_cache
                      if k[:3] == (ref, size, square) and k != key]:
                del self.artwork_cache[k]
            self.artwork_cache[key] = img
            self._art_fails.pop(key, None)
            return img
        except Exception as e:
            # Never cache a failure for good — and don't sit on the FIRST
            # failure either: boot is now fast enough that the resume's
            # cover fetch races wifi and loses (URLError seconds before
            # DHCP; field 2026-07-18), and a flat 60s backoff left the
            # mosaic up for a minute+ after the net was fine. Escalate
            # instead: retry in 5s, then 10, 20, 40, capped at 60 — the
            # boot race costs one short beat, a truly dead network still
            # backs off to the old cadence.
            # A corrupt LOCAL cache file (hard power cut mid-write, field
            # 2026-07-23: episode jpg failing every decode) never heals by
            # retrying — and the sync skips it forever because it exists.
            # Delete it so the next sweep refetches; scoped to CACHE_DIR
            # only (PWA-uploaded logos etc. must never be auto-deleted).
            cache_root = os.path.dirname(UI_ART_DIR)  # VIBB_CACHE root
            if (not ref.startswith("http") and os.path.exists(ref)
                    and os.path.abspath(ref).startswith(cache_root + os.sep)):
                try:
                    os.remove(ref)
                    log(f"artwork corrupt — deleted for refetch: {ref[:80]}")
                except OSError:
                    pass
            fails = self._art_fails.get(key, 0) + 1
            self._art_fails[key] = fails
            backoff = min(60.0, 5.0 * (2 ** (fails - 1)))
            log(f"artwork failed ({e.__class__.__name__}), retry in "
                f"{backoff:.0f}s: {ref[:80]}")
            self.artwork_cache[key] = time.monotonic() + backoff
            return None

    def artwork_async(self, ref, size=110, square=False):
        """artwork() that never touches the network on the render thread:
        a remote cover is fetched in the background and the view repaints
        when it lands. Local files still decode inline."""
        if not ref:
            return None
        if not ref.startswith("http"):
            return self.artwork(ref, size, square)
        key = self._art_key(ref, size, square)
        cached = self.artwork_cache.get(key)
        if isinstance(cached, float):  # failed recently — retry when due
            if time.monotonic() < cached:
                return None
        elif key in self.artwork_cache:
            return cached
        # A disk-warm cover needs NO network: decode it inline (~10-30ms
        # for the now-card's 128px) instead of the thread + next-tick
        # detour, which made every already-cached cover trail its title
        # by one repaint beat (field 2026-08-12). The deferral below is
        # for FETCHES only.
        if os.path.exists(_art_disk(ref, size, square)):
            return self.artwork(ref, size, square)
        if key not in self._art_pending:
            self._art_pending.add(key)

            def fetch():
                try:
                    # REVERTED (field 2026-07-18 23:xx): a busy-marker
                    # wait here deferred every new cover ~20s during
                    # skip sessions — each next/prev refreshes BUSY, so
                    # the wait always ran its full course and the screen
                    # sat on the previous cover / the mosaic. The cover
                    # is ONE ~50KB fetch ever (disk-cached after), which
                    # the radio absorbs fine even mid-load.
                    self.artwork(ref, size, square)
                finally:
                    self._art_pending.discard(key)
                    self.dirty = True
            threading.Thread(target=fetch, daemon=True).start()
        return None

    def _prewarm_art(self):
        """Decode every carousel/menu cover once, right after boot. Lazy
        decoding made the first pass through the carousel stutter tile by
        tile (a full-size JPEG takes ~0.5s at 600 MHz powersave)."""
        time.sleep(2.0)  # let the first paint and status fetch win the CPU
        for e in self.flat_entries():
            ref = e.get("image")
            if ref and not ref.startswith("http"):
                # match EXACTLY what each view reads, or the cache key's
                # square flag makes the warm a miss (it warmed 176 fit while
                # the views read square, so the warm decoded nothing useful):
                #   now-playing reads 128 SQUARE (render_now)
                #   carousel + category carousel read 176 SQUARE (render_
                #     carousel / render_cats)
                #   list rows read 56 fit (_row_art)
                self.artwork(ref, 128, square=True)
                self.artwork(ref, 176, square=True)
                self.artwork(ref, 56)
                time.sleep(0.05)
        self.dirty = True

    def _row_art(self, rows):
        """Cover of the highlighted list row (56px). Loading can hit the
        network for a non-synced show, so wait until scrolling settles
        — self.dirty retries next tick (the loop clears it pre-render)."""
        if not rows:
            return None
        ref = rows[min(self.sel, len(rows) - 1)].get("image")
        if not ref:
            return None
        if (self._art_key(ref, 56) not in self.artwork_cache
                and time.monotonic() - self.last_input < 0.4):
            self.dirty = True
            return None
        return self.artwork(ref, 56)

    def entry_art(self):
        return self._row_art(self.section.get("entries") or [])

    def episode_art(self):
        """Cover of the highlighted episode/track row (56px, top-right) —
        the same affordance the entries menus have. Row 0 ("Play all")
        and rows without their own art show the collection cover.
        Spotify picker rows carry REMOTE cover urls, so the fetch goes
        through artwork_async — a list scroll must never block the
        render thread on the network (the carousel's A3 rule); the view
        repaints when the cover lands."""
        exp = self.expanded or {}
        eps = exp.get("episodes") or []
        if 0 < self.sel <= len(eps):
            ref = eps[self.sel - 1].get("image") or exp.get("image")
        else:
            ref = exp.get("image")
        if not ref:
            return None
        if (self._art_key(ref, 56) not in self.artwork_cache
                and time.monotonic() - self.last_input < 0.4):
            self.dirty = True  # let scrolling settle; retry next tick
            return None
        return self.artwork_async(ref, 56)

    def section_art(self):
        """The highlighted category's uploaded logo on the home screen."""
        return self._row_art((self.library or {}).get("sections") or [])

    # -- input ----------------------------------------------------------------

    def push(self, view):
        self.stack.append((self.view, self.sel))
        self.view, self.sel = view, 0
        self.dirty = True

    def _enter_now(self):
        """Open now-playing right after issuing a play. Force an immediate
        status refetch and repaint every tick for a few seconds: the
        steady-state repaint is change-driven (CPU), but go-librespot
        takes a moment to load the new track and can briefly report an
        unchanged/blank status mid-switch — without this the panel keeps
        showing the previous playlist's cover until the next change or a
        keypress."""
        self.push("now")
        self._force_poll()                         # poll now, not in ~1s
        self.catch_up_until = time.monotonic() + 6
        self.poll_burst_until = time.monotonic() + 6

    def _no_internet(self):
        """Instant offline check from the last /system poll — no network
        probe. Only reports offline on positive evidence (hotspot mode, wifi
        off, or a link with no IP); an empty/unpolled status never blocks a
        play. Used to fail Spotify fast instead of hanging."""
        w = self.system.get("wifi")
        if not w:
            return False
        return bool(w.get("hotspot")) or not w.get("enabled") or not w.get("ip")

    def back(self):
        if self.stack:
            self.view, self.sel = self.stack.pop()
        self.dirty = True

    def handle(self, ev):
        self.dirty = True
        self.user_touched = True
        if ev == "settings":
            if self.view != "settings":
                self.push("settings")
            return
        if ev == "extras":
            # the X+Y owner chord — a no-op on stock boxes (empty dir)
            if self.view != "extras" and self.extras():
                self.push("extras")
            return
        if self.view == "now":
            self.handle_now(ev)
            return
        if self.view == "carousel":
            self.handle_carousel(ev)
            return
        if self.view == "cats":
            self.handle_cats(ev)
            return
        if ev == "b_long":
            ev = "b"  # the hold gesture only means something while playing
        items = self.current_items()
        if ev == "x":
            self.sel = (self.sel - 1) % max(1, len(items))
        elif ev == "y":
            self.sel = (self.sel + 1) % max(1, len(items))
        elif ev == "a":
            self.select()  # A acts everywhere: select here, play/pause in now
        elif ev == "b":
            self.back()    # B backs out everywhere — matching hold-B in now
        if self.view == "link":
            # keep the screen lit while someone is aiming a camera at it
            _paths.touch_activity()

    def handle_now(self, ev):
        # A = play/pause: the same physical button that selects in the
        # menus — pick something / pause it feel like one action. B is
        # previous (hold = back to the menu, mirroring short-B in menus).
        st = self.status or {}
        if ev == "x" and (st.get("bt_waiting") or st.get("bt_lost")):
            # the popup is modal for X: volume without sound is pointless
            self._bt_connect_last()
            return
        if ev == "a" and (st.get("bt_lost") or st.get("bt_waiting")) \
                and st.get("bt_local_ok"):
            self._play_on_local()  # the popup's "play on box speaker"
            return
        if ev == "x" and st.get("spotify_offline") \
                and st.get("source") == "spotify":
            self._wifi_reconnect()  # X = get the net back now
            return
        card = self._card()
        try:
            if ev == "a":
                # optimistic: flip the icon NOW (see PP_RECONCILE_S) so it
                # matches the music instead of waiting for the poller's next
                # /status, which can be a second+ behind on a busy daemon.
                # Set BEFORE the control kicks the poller — a pre-toggle
                # /status racing the gap could flicker the icon back.
                cur = bool((self.status or {}).get("playing"))
                self._pp_expect = not cur
                self._pp_until = time.monotonic() + PP_RECONCILE_S
                self.status = {**(self.status or {}), "playing": self._pp_expect}
                self.dirty = True
                self._control_async("/playpause")
            elif ev == "b":
                if card:        # a card owns B/Y while it shows
                    self._card_step(-1)
                else:
                    self._control_async("/prev")
            elif ev == "b_long":
                if card:
                    self._card_hold(-1)
                else:
                    self._back_to_episodes()
            elif ev == "x":
                if card:
                    self._card_next()   # cycle while it is up
                else:
                    self._volume_mode(delta=None)  # cold open: volume
            elif ev == "x_long":
                if card:
                    # X means "the next tab" for as long as a card shows;
                    # a hold that opened the output picker here would
                    # bury the card mid-cycle.
                    self._card_next()
                else:
                    self._output_action()
            elif ev == "y":
                if card:
                    self._card_step(1)
                else:
                    self._control_async("/next")
            elif ev == "y_long":
                if card:
                    self._card_hold(1)
                else:
                    self._open_episodes()
        except OSError as e:
            log(f"control failed: {e}")

    def _card(self):
        """Which card is showing, or None.

        Outside now-playing it is ALWAYS the volume card. X has never
        meant anything else while browsing, and seek/shuffle have no
        meaning there — this is also what stops a seek card opened in
        now-playing from following the user into the carousel and
        quietly rebinding B/Y away from flipping tiles."""
        if time.monotonic() >= self.vol_mode_until:
            return None
        return CARDS[self.card_idx] if self.view == "now" else "vol"

    def _card_touch(self):
        """Re-arm the showing card. BOTH timestamps, every time:
        vol_mode_until is what the handlers read, volume_flash is what
        the render loop's repaint exception reads. Touch one alone and
        you get a card that answers buttons while frozen on screen, or
        one that paints after it stopped listening."""
        self.vol_mode_until = time.monotonic() + CARD_TTL_S
        self.volume_flash = self.vol_mode_until
        self.dirty = True

    def _card_next(self):
        """X again while a card is up: the next tab, wrapping.

        Wrapping is deliberate — a child who overshoots fixes it by
        pressing on, not by waiting out five seconds of timeout."""
        self.card_idx = (self.card_idx + 1) % len(CARDS)
        self._card_touch()
        if CARDS[self.card_idx] == "vol":
            self._volume_mode(delta=None)   # re-read the number
        else:
            self.seek_dir, self._seek_hold_t0 = 0, 0.0  # a fresh visit
            self.seek_shown = None           #   to seek starts slow

    def _card_step(self, sign, held=False):
        """B and Y, meaning whatever the showing card says they mean.
        held=True comes from ONE caller — the main loop's repeat pin —
        and only the seek press consumes it. A parameter on purpose:
        reading self._card_repeat here would race a quick release-retap
        into "held" (the pin-clear runs before event dispatch in the
        same loop pass) — the surprise-jump class this exists to kill."""
        card = self._card()
        if card == "seek":
            self._seek_press(sign, held=held)
        elif card == "shuf":
            # OFF on B, ON on Y — not a toggle. Spatially the same as
            # the -/+ above it, and idempotent: a child mashing Y cannot
            # flip-flop a state whose only feedback is one small glyph.
            self._set_shuffle(sign > 0)
            self._card_touch()
        else:
            self._volume_mode(delta=5 * sign)

    def _card_hold(self, dirn):
        """A held B or Y belongs to the CARD, never to navigation.

        Holding is the instinct a card invites — keep spooling, keep
        turning it down — and until now it threw the user out to the
        carousel (B) or opened the episode picker (Y) instead. Seek and
        volume repeat; shuffle is binary, so it only swallows the hold,
        which is still the point: a gesture that means nothing here must
        not mean something drastic. One step fires now, and the render
        loop repeats it at CARD_REPEAT_S while the button stays down."""
        if self._card() == "shuf":
            self._card_touch()
            return
        self._card_step(dirn)
        self._card_repeat = (dirn, time.monotonic() + CARD_REPEAT_S)

    @staticmethod
    def _track_key(st):
        """What the optimistic position was true FOR. Position is
        track-scoped in a way play/pause is not: skip to the next
        episode right after a seek and, without this, the new track's
        0:00 would sit masked behind a stale 12:34."""
        return (st.get("target"), st.get("episode_id") or st.get("title"))

    def _seek_press(self, dirn, held=False):
        """One seek step: a TAP is SEEK_TAP_S, always; only a HOLD
        accelerates, by time since the hold began (SEEK_HOLD_LADDER).
        A reversal restarts the hold clock — "I overshot" lands 15s
        away, not another five minutes away.

        The base is our OWN last commanded target, never the reported
        position: /status is a second behind (fifteen on a sonos
        renderer), so basing step N on the poll gives 'every press does
        nothing, then one works' — the lesson the sonos volume optimism
        already records from 2026-08-09. Absolute targets are what let
        the presses compound at all."""
        st = self.status or {}
        now = time.monotonic()
        if not held or dirn != self.seek_dir:
            self._seek_hold_t0 = now   # fresh press or reversal: tap-size
        self.seek_dir = dirn
        step = SEEK_TAP_S
        if held:
            for since, grown in SEEK_HOLD_LADDER:
                if now - self._seek_hold_t0 > since:
                    step = grown
        dur = st.get("duration")
        if dur is None:            # live stream: no duration, nowhere to go
            self.seek_shown = dirn * step
            self._card_touch()
            self.seek_refused = True
            return
        # a step never exceeds ~8% of the track (QA 2026-09-01: a
        # step-only clamp still teleported a 3-min song — this bounds
        # the TRAVEL RATE): a short song stays at tap size for life,
        # a 10-min podcast tops near 50s, audiobooks are untouched
        step = min(step, max(SEEK_TAP_S, float(dur) / 12.0))
        self.seek_shown = dirn * step
        self._card_touch()
        with self._seek_post_lock:
            base = self._pos_expect if self._pos_expect is not None \
                else (st.get("position") or 0.0)
            # SEEK_TAIL_S mirrors the daemon's end clamp: without it the
            # echoed clamp and the next repeat fight over the last 5s
            target = max(0.0, min(float(base) + dirn * step,
                                  float(dur) - SEEK_TAIL_S))
            self._pos_expect = target
            self._seek_dirty = True
            spawn = not self._seek_posting
            if spawn:
                self._seek_posting = True
        self._pos_at = time.monotonic()
        self._pos_until = self._pos_at + SEEK_RECONCILE_S
        self._pos_key = self._track_key(st)
        self.status = {**st, "position": target}
        self.poll_burst_until = self._pos_at + 3
        if spawn:
            threading.Thread(target=self._seek_poster,
                             daemon=True).start()

    def _seek_poster(self):
        """The single-flight seek sender (QA round 2026-09-01) — the
        _sonos_seek_worker pattern one layer up. ONE post in flight,
        latest compounded target wins, paced at SEEK_POST_MIN_S, and it
        protects every upstream at once: the sonos sidecar, go-librespot
        (a slow spotify round can no longer be overtaken by a newer one
        — ordering by construction), and mpv. The daemon's echoed clamp
        is adopted ONLY when no newer target is dirty (adopting between
        repeats rewound the bar mid-hold), and a refusal clears the
        dirty flag so no doomed re-post follows 'Can't seek here'.
        Exits when the queue is drained — the final target of a hold
        always lands, whatever ended the hold."""
        while True:
            wait = self._seek_last_post + SEEK_POST_MIN_S \
                - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            with self._seek_post_lock:
                if not self._seek_dirty or self._pos_expect is None:
                    self._seek_dirty = False
                    self._seek_posting = False
                    return
                self._seek_dirty = False
                target = float(self._pos_expect)
            self._seek_last_post = time.monotonic()
            try:
                r = api_post("/seek", {"position": target},
                             timeout=CONTROL_TIMEOUT)
            except OSError as e:
                log(f"seek failed: {e}")
                r = None
            if not isinstance(r, dict) or r.get("routed") is None:
                with self._seek_post_lock:
                    self._pos_expect = None  # let the truth back in
                    self._seek_dirty = False
                self.seek_refused = True
            elif r.get("position") is not None:
                adopted = False
                with self._seek_post_lock:
                    if not self._seek_dirty \
                            and self._pos_expect is not None:
                        # the daemon's number carries the end clamp —
                        # but a newer compounded target outranks it
                        self._pos_expect = float(r["position"])
                        adopted = True
                if adopted:
                    self._pos_at = time.monotonic()
                    self.status = {**(self.status or {}),
                                   "position": self._pos_expect}
                    self.dirty = True
            self._force_poll()

    def _set_shuffle(self, want):
        """The shuffle card's B/Y: shuffle off or on.

        Nothing is announced on success — neither mpv nor go-librespot
        interrupts the current track to reorder what comes after it, so
        there is no gap to explain. The top-bar glyph carries the state,
        and it flips HERE rather than on the next poll so the press has
        an answer immediately (same contract as the play/pause icon).
        A message appears only when the box could not shuffle at all."""
        base = self.system if isinstance(self.system, dict) else {}
        self.system = {**base, "shuffle": want}   # atomic swap: the
        #   render thread may be reading this (see _set)
        self.status = {**(self.status or {}), "shuffle": want}
        self.dirty = True

        def go():
            try:
                r = api_post("/shuffle", {"enabled": want},
                             timeout=CONTROL_TIMEOUT)
            except OSError as e:
                log(f"shuffle failed: {e}")
                r = None
            if not isinstance(r, dict) or r.get("routed") is None:
                # nothing to shuffle (sonos, or no session): put the
                # glyph back and say so — a silent no-op reads as a
                # broken button
                self.shuffle_refused = True
            self._force_poll()
        threading.Thread(target=go, daemon=True).start()

    def _control_async(self, path):
        """POST a transport control off the UI thread. A slow control
        (wedged go-librespot api) must never freeze rendering or eat
        the next button press — hold-B to the carousel always works."""
        # Kick at SEND, not only at return: the metadata fetch overlaps
        # the command instead of queuing behind it (the daemon serves
        # /status and /command on separate threads), and open the burst
        # window so follow-up polls catch the truth when it lands (QA
        # 2026-08-13: a dedicated deadline, NOT catch_up_until — that
        # one also forces identical-frame repaints at 5fps).
        self.poll_burst_until = time.monotonic() + 3
        self._force_poll()

        def go():
            try:
                api_post(path, timeout=CONTROL_TIMEOUT)
            except OSError as e:
                log(f"control failed: {e}")
            self._force_poll()  # kick the poller so the icon confirms fast
        threading.Thread(target=go, daemon=True).start()

    def _play_async(self, body, entry=None):
        """Start playback off the UI thread, optimistically.

        /play is the SLOWEST endpoint the screen calls: for a Spotify
        target it runs `systemctl is-active` (10s budget) plus two
        go_status() round-trips (5s each) before it answers, so its
        worst case is far past CONTROL_TIMEOUT. Called inline it froze
        the panel mid-press and then gave up without even entering
        now-playing, so the box looked both stuck and idle while
        playback was in fact starting (field 2026-08-02: 6s freeze on
        an album tile, the retry hit the daemon's resume shortcut and
        felt instant).

        So: enter now-playing IMMEDIATELY (the screen shows intent),
        POST in the background, and let the poller confirm — the same
        contract as _control_async. The one answer the UI still needs
        is 'no-internet': a background thread must not draw, so it
        parks the verdict and the main loop runs the reconnect flow."""
        if entry is not None:
            # The tapped identity, painted NOW. The daemon serves the
            # old source's fully valid card until its switch commits
            # (0.5-3s; worse when the old child is slow to die), and the
            # screen knows better — it is holding the tapped entry. The
            # tile cover is local and 128sq-prewarmed, so the card is
            # complete on the very first paint; the real track title
            # arrives with the first on-target /status.
            self.status = {"target": entry.get("target"),
                           "title": entry.get("name"),
                           "artwork": entry.get("image"),
                           "playing": True}
            self._expect_target = entry.get("target")
            self._expect_until = time.monotonic() + 6
        self._enter_now()

        def go():
            try:
                r = api_post("/play", body, timeout=CONTROL_TIMEOUT)
            except OSError as e:
                log(f"play failed: {e}")
                return
            if isinstance(r, dict) and r.get("error") == "no-internet":
                # wifi is up but the WAN is down — the daemon's probe is
                # the authority (the screen's local check can't tell)
                self.play_offline = True
            self._force_poll()
        threading.Thread(target=go, daemon=True).start()

    def _open_episodes(self):
        """Hold-Y in now-playing: the episode picker for whatever is
        playing — the same list view the full menus use. In kid mode this
        is the (deliberately hidden) way to jump between episodes; back
        from the list returns to now-playing."""
        target = (self.status or {}).get("target")
        if not target:
            return
        self.load_library(ttl=0)  # fresh — we might not have browsed yet
        for sec in (self.library or {}).get("sections", []):
            for e in sec.get("entries", []):
                if e.get("target") != target:
                    continue
                self.draw_message("Fetching episodes ...")
                try:
                    # tracks=1: spotify playlists list their songs too
                    # (fork v0.1.1 metadata cache) — same picker as
                    # podcasts. Browse taps never pass it, so tapping a
                    # playlist card still just plays.
                    self.expanded = api_get(f"/expand?id={e['id']}&tracks=1")
                except (OSError, ValueError):
                    self.draw_message("Network error — try again")
                    time.sleep(1)
                    return
                if not self.expanded.get("episodes"):
                    if not self.expanded.get("pending"):
                        return  # no list exists (pre-v0.1.1 fork)
                    # Still enumerating/sweeping (the daemon's bounded
                    # settle timed out — a cold 800-track context): one
                    # more round usually completes it, with "Fetching
                    # episodes ..." still on screen. If it is STILL
                    # empty, say so — the silent bail read as "hold-Y
                    # does nothing" (architect review 2026-08-03).
                    try:
                        self.expanded = api_get(
                            f"/expand?id={e['id']}&tracks=1")
                    except (OSError, ValueError):
                        self.expanded = {}
                    if not self.expanded.get("episodes"):
                        self.draw_message("Episodes are still loading —"
                                          "\ntry again in a moment")
                        time.sleep(1.2)
                        return
                self.section, self.entry = sec, e
                self.push("episodes")
                now_id = (self.status or {}).get("episode_id") \
                    or ((self.status or {}).get("spotify")
                        or {}).get("track_uri")
                if now_id:  # land on the playing episode
                    for i, ep in enumerate(self.expanded["episodes"]):
                        if ep.get("id") == now_id:
                            self.sel = i + 1  # row 0 = "Play all"
                            break
                return

    def _back_to_episodes(self):
        """Leave now-playing for the episode list of whatever is playing.
        The stack usually has it — but the auto-jump to now-playing
        resets the stack to [home], which made hold-A land on the home
        screen instead of the episodes (field: 'jumps back several
        pages')."""
        nav = self._nav_mode()
        if nav in (1, 2):
            # carousel modes: the cover carousel IS the browse level.
            tgt = (self.status or {}).get("target")
            if nav == 2:
                # land inside the PLAYING entry's category carousel
                # (hold-B again there steps out to the categories)
                for s in (self.library or {}).get("sections", []):
                    for i, e in enumerate(s.get("entries") or []):
                        if e.get("target") == tgt:
                            self.car_section, self.car_sel = s.get("id"), i
                            self.stack, self.view = [], "carousel"
                            self.dirty = True
                            return
                self.stack, self.view, self.car_section = [], "cats", None
                self.dirty = True
                return
            for i, e in enumerate(self.flat_entries()):  # nav 1: flat
                if e["target"] == tgt:
                    self.car_sel = i
                    break
            self.stack, self.view = [], "carousel"
            self.dirty = True
            return
        if self.stack and self.stack[-1][0] == "episodes":
            self.back()
            return
        target = (self.status or {}).get("target")
        for sec in (self.library or {}).get("sections", []):
            for e in sec.get("entries", []):
                if e.get("target") == target:
                    try:
                        self.expanded = api_get(f"/expand?id={e['id']}")
                    except (OSError, ValueError):
                        break
                    if not self.expanded.get("episodes"):
                        break  # spotify etc: no episode view exists
                    self.section, self.entry = sec, e
                    self.stack = [("home", 0), ("entries", 0)]
                    self.view, self.sel = "episodes", 0
                    self.dirty = True
                    return
        self.back()

    def handle_carousel(self, ev):
        """Kid mode's browse level: B/Y flip through big covers, X is the
        volume card, and A = "play this tile" — one meaning: it resumes
        the entry at its own bookmark and opens the NORMAL now-playing
        view. The daemon makes a replay of what's already loaded a plain
        unpause/no-op, so A never restarts anything. Hold-B in
        now-playing comes back here; settings stay behind the parental
        A+B hold. In nav mode 2 the tiles are one category's, and hold-B
        steps out to the category carousel (short B still flips)."""
        ents = self.carousel_entries()
        if not ents:
            # a since-emptied category: hold-B still escapes to the cats
            if ev == "b_long" and self._nav_mode() == 2:
                self.stack, self.view, self.car_section = [], "cats", None
                self.dirty = True
            return
        st = self.status or {}
        if ev == "x" and (st.get("bt_waiting") or st.get("bt_lost")):
            # the popup is modal for X: volume without sound is pointless
            self._bt_connect_last()
            return
        if ev == "a" and (st.get("bt_lost") or st.get("bt_waiting")) \
                and st.get("bt_local_ok"):
            self._play_on_local()  # the popup's "play on box speaker"
            return
        in_vol = time.monotonic() < self.vol_mode_until
        # nav mode 2: hold-B inside a category steps back up to the
        # category carousel (short B keeps flipping tiles)
        if ev == "b_long" and not in_vol and self._nav_mode() == 2 \
                and self.car_section is not None:
            self.stack, self.view, self.car_section = [], "cats", None
            self.dirty = True
            return
        try:
            if ev == "y":
                if in_vol:
                    self._volume_mode(delta=5)
                else:
                    self._flip(ents, +1)
            elif ev in ("b", "b_long"):
                if in_vol:
                    self._volume_mode(delta=-5)
                else:
                    self._flip(ents, -1)
            elif ev == "a":
                e = ents[self.car_sel % len(ents)]
                if "spotify" in e["target"] and self._no_internet():
                    self._reconnect_for_spotify()  # try to GET the net
                    return
                self._play_async({"id": e["id"]}, entry=e)
            elif ev == "x":
                self._volume_mode(delta=None)  # open/extend the volume card
        except OSError as e:
            log(f"carousel action failed: {e}")

    def handle_cats(self, ev):
        """Nav mode 2 root: B/Y flip categories, X is the volume card, A
        opens the selected category's cover carousel (car_section)."""
        cats = self.carousel_cats()
        if not cats:
            return
        in_vol = time.monotonic() < self.vol_mode_until
        if ev == "y":
            if in_vol:
                self._volume_mode(delta=5)
            else:
                self.cat_sel = (self.cat_sel + 1) % len(cats)
        elif ev in ("b", "b_long"):
            if in_vol:
                self._volume_mode(delta=-5)
            else:
                self.cat_sel = (self.cat_sel - 1) % len(cats)
        elif ev == "a":
            s = cats[self.cat_sel % len(cats)]
            self.car_section, self.car_sel = s.get("id"), 0
            self.stack, self.view = [], "carousel"
            self.dirty = True
        elif ev == "x":
            self._volume_mode(delta=None)

    def _output_action(self):
        """Hold X. No Sonos known -> today's two-way toggle, unchanged.
        Sonos rooms known -> a three-way menu instead (owner 2026-08-09:
        the row only exists when speakers actually do). The list read is
        the sidecar's CACHE via vibbd — instant, never a scan. A fresh
        topology fetch (one ~200ms call, groups included) rides behind
        it so the speaker submenu is current by the time a finger gets
        there (owner 2026-08-21) — change-only repaint updates the rows
        if they are already on screen."""
        try:
            self.sonos = api_get("/sonos")
        except OSError:
            self.sonos = {"players": []}  # sidecar down = no sonos row
        if self.sonos.get("players"):
            def freshen():
                try:
                    self.sonos = api_get("/sonos?fresh=1", timeout=8)
                    self.dirty = True
                except OSError:
                    pass  # cache stays on screen; Look again exists
            threading.Thread(target=freshen, daemon=True).start()
            self.push("output")
            self.dirty = True
        else:
            self._toggle_output()

    def select_output(self):
        label = self.current_items()[self.sel][0]
        if label == "Sonos":
            # background refresh while the cached list is already on
            # screen (change-only repaint updates rows). Topology first
            # (~200ms, brings the group map); only when NOBODY answered
            # it — the stale marker — is the 3s+ SSDP scan worth its
            # cost, and only as this thread's own fallback.
            def scan():
                try:
                    self.sonos = api_get("/sonos?fresh=1", timeout=8)
                    self.dirty = True
                    if self.sonos.get("stale"):
                        self.sonos = api_get("/sonos?rescan=1", timeout=25)
                        self.dirty = True
                except OSError:
                    pass
            threading.Thread(target=scan, daemon=True).start()
            self.push("sonos")
            return
        dev = "local" if label == "Box speaker" else "bt"
        try:
            r = api_post("/output", {"device": dev})
        except OSError as e:
            log(f"output switch failed: {e}")
            return
        self.output_shown = ("Built-in speaker" if dev == "local"
                             else "Bluetooth speaker")
        self.output_warning = bool(r.get("warning"))
        self.output_flash = time.monotonic() + 1.5
        self.stack, self.view = [], "now"
        self.dirty = True

    def _sonos_choices(self):
        """The speaker rows — ONE source for display (current_items)
        and selection (select_sonos), so the indexes can never drift:
        [(label, uid, member_names)]. A multi-member group as the
        Sonos app made it is one row — "Stua + Kjøkken", coordinator
        first — and selecting it targets the COORDINATOR: transport
        verbs on a coordinator drive the whole group, so everything
        downstream is unchanged. Zones absorbed into a shown group
        don't repeat as solo rows; solo zones look exactly as before.
        Group CONTROL stays in the Sonos app on purpose (owner
        2026-08-21): the box is group-aware, not a group manager."""
        players = self.sonos.get("players") or []
        names = {p["uid"]: p.get("name") or "?" for p in players}
        rows, absorbed = [], set()
        for g in self.sonos.get("groups") or []:
            members = [u for u in g.get("members") or [] if u in names]
            if len(members) < 2 or g.get("coordinator") not in names:
                continue  # a group we cannot label or address in full
            rows.append((" + ".join(names[u] for u in members),
                         g["coordinator"],
                         [names[u] for u in members]))
            absorbed.update(members)
        for p in players:
            if p["uid"] not in absorbed:
                rows.append((p.get("name") or "?", p["uid"],
                             [p.get("name") or "?"]))
        return rows

    def select_sonos(self):
        rows = self.current_items()
        label = rows[self.sel][0] if self.sel < len(rows) else ""
        if label == "Look again":
            self.draw_message("Looking for speakers ...")
            try:
                self.sonos = api_get("/sonos?rescan=1", timeout=25)
            except OSError as e:
                self.draw_message(f"Failed: {e}")
                time.sleep(2)
            self.dirty = True
            return
        choices = self._sonos_choices()
        idx = self.sel - 1  # one action row on top
        if not (0 <= idx < len(choices)):
            return
        label, uid, _names = choices[idx]
        self.draw_message(f"Sending sound to {label} ...")
        try:
            r = api_post("/output", {"device": "sonos", "uid": uid,
                                     "name": label}, timeout=30)
        except OSError as e:
            self.draw_message(f"Failed: {e}")
            time.sleep(2)
            return
        # land on now-playing: the confirmation popup only paints there
        self.output_shown = f"Sonos: {label}"
        self.output_warning = bool(r.get("warning"))
        self.output_flash = time.monotonic() + 1.5
        self.stack, self.view = [], "now"
        self._force_poll()

    def _toggle_output(self):
        """Hold X: flip between the bluetooth speaker and the built-in
        one — the same set_output the PWA buttons use."""
        try:
            cur = api_get("/output").get("output")
            dev = "local" if cur == "bt" else "bt"
            r = api_post("/output", {"device": dev})
        except OSError as e:
            log(f"output toggle failed: {e}")
            return
        # Show the same message, but as the transient rounded-box popup the
        # speaker/net overlays use (cosmetic parity) instead of a blocking
        # full-screen repaint — the render loop self-clears it (field ask
        # 2026-07-21).
        self.output_shown = ("Built-in speaker" if dev == "local"
                             else "Bluetooth speaker")
        self.output_warning = bool(r.get("warning"))
        self.output_flash = time.monotonic() + 1.5
        self.dirty = True

    def _volume_mode(self, delta):
        """The volume card — tab 0 of the cycle, and the ONLY card the
        browse views can reach.

        The HTTP runs off this thread now. Inline was survivable while
        one press meant one round trip, but the cycle COUNTS presses,
        and with LgpioInput the pins are sampled inside poll() on this
        very thread: a press that begins and ends while a slow /volume
        blocks it changes no level at sample time and is never seen at
        all. A cycle cannot sit on a press path that drops presses (QA
        2026-08-14). /volume also takes the daemon's lock, which a slow
        control can hold for seconds."""
        self.card_idx = 0
        self._card_touch()
        if delta is not None and self.volume_shown is not None:
            # move the number at press speed; the answer confirms it
            self.volume_shown = max(0, min(100, self.volume_shown + delta))

        def go():
            try:
                r = api_get("/volume") if delta is None \
                    else api_post("/volume", {"delta": delta})
            except OSError as e:
                log(f"volume failed: {e}")
                return
            self.volume_shown = r.get("volume")
            self.dirty = True
        threading.Thread(target=go, daemon=True).start()

    def current_items(self):
        if self.view == "output":
            st = self.status or {}
            renderer = st.get("renderer")
            out = st.get("output")
            stale = bool((self.sonos or {}).get("stale"))
            return [("Box speaker",
                     "●" if renderer != "sonos" and out == "local" else ""),
                    ("Bluetooth",
                     "●" if renderer != "sonos" and out == "bt" else ""),
                    # ghost-grey when the fresh probe found nobody (the
                    # cabin: home's speakers still cached) — the row
                    # stays selectable, the submenu's hint explains
                    ("Sonos", "●" if renderer == "sonos" else "", stale)]
        if self.view == "sonos":
            cur = (self.status or {}).get("renderer_name")
            stale = bool((self.sonos or {}).get("stale"))
            rows = [("Look again", "")]   # never ghosted — it IS the fix
            for label, _uid, names in self._sonos_choices():
                rows.append((label, "●" if cur == label or cur in names
                             else "", stale))
            return rows
        if self.view == "home":
            return [s["name"] for s in (self.library or {}).get("sections", [])] \
                or ["(empty library)"]
        if self.view == "entries":
            return [e["name"] for e in self.section["entries"]]
        if self.view == "episodes":
            eps = self.expanded["episodes"]
            now_id = (self.status or {}).get("episode_id") \
                or ((self.status or {}).get("spotify") or {}).get("track_uri")
            rows = []
            for e in eps:
                playing = now_id is not None and e.get("id") == now_id
                title = e.get("title") or e.get("id") or "?"
                rows.append((("▶ " if playing else "") + title,
                             "✓" if e.get("cached") else ""))
            return ["▶ Play all"] + rows
        if self.view == "settings":
            s = self.settings
            w = self.system.get("wifi") or {}
            wifi = "on" if w.get("enabled") else "off"
            return [("Screen off after", self.fmt_timeout(s["screen_timeout_s"])),
                    ("Brightness", f"{s.get('screen_brightness', 100)}%"),
                    ("Volume cap", f"{s['volume_cap']}%"),
                    ("Auto-off (idle)", self.fmt_idle(s["idle_shutdown_min"])),
                    ("Browse", {0: "menus", 1: "carousel",
                                2: "categories"}.get(
                                    self._nav_mode(), "menus")),
                    ("Wi-Fi", wifi),
                    ("Setup hotspot", "on" if w.get("hotspot") else ""),
                    ("Bluetooth", ""),
                    ("Storage", ""),
                    ("Link phone", ""),
                    ("Shut down", ""),
                    ("Restart", "")]
        if self.view == "bt":
            rows = [("Pair nearest", ""), ("Scan for new", ""),
                    ("Pair from car", "")]
            for d in self.bt.get("devices", []):
                mark = "●" if d.get("connected") else (
                    "✓" if d["mac"] == self.bt.get("configured") else "")
                rows.append((d["name"], mark))
            return rows
        if self.view == "btscan":
            return [(d["name"] + (" ♪" if d.get("audio") else ""), "")
                    for d in self.bt_found] or ["(nothing found)"]
        if self.view == "extras":
            return [(e["name"], "") for e in self.extras()] \
                or ["(no extras installed)"]
        if self.view == "storage":
            return []
        if self.view == "link":
            return []
        return []

    @staticmethod
    def fmt_timeout(v):
        return "never" if v == 0 else f"{v}s"

    @staticmethod
    def fmt_idle(v):
        # "~": idle.py samples every 60s and gives button presses one
        # extra cycle of grace, so the real fire time runs up to ~2 min
        # past the setting — a plain "5 min" read as a bug in the QA
        # power audit (2026-08-10) when the box sat visibly on at 5:30
        return "off" if v == 0 else f"~{v} min"

    def select(self):
        try:
            if self.view == "link":
                # A = new token. Deliberately confirmed: it unlinks every
                # phone in the house, and the button is one press away
                # from a kid's hands.
                self.draw_message("New token? Every linked phone must be\n"
                                  "linked again.  (A confirms, B cancels)")
                if self.confirm():
                    from vibb import token as _tok
                    _tok.rotate()
                    log("api token rotated from the box screen")
                self.dirty = True
                return
            if self.view == "home":
                secs = (self.library or {}).get("sections", [])
                if not secs:
                    return
                self.section = secs[self.sel]
                self.push("entries")
            elif self.view == "entries":
                self.entry = self.section["entries"][self.sel]
                self.draw_message("Fetching episodes ...")
                # /expand is instant for Spotify (no network) — resolve first,
                # then guard: Spotify needs the net, so say so instantly
                # instead of spawning a play that just fails in the background.
                self.expanded = api_get(f"/expand?id={self.entry['id']}")
                if self.expanded["kind"] == "spotify" or not self.expanded["episodes"]:
                    if self.expanded["kind"] == "spotify" and self._no_internet():
                        self._reconnect_for_spotify()  # get the net now
                        return
                    self._play_async({"id": self.entry["id"]},
                                     entry=self.entry)
                else:
                    self.push("episodes")
            elif self.view == "episodes":
                body = {"id": self.entry["id"]}
                if self.sel > 0:
                    ep = self.expanded["episodes"][self.sel - 1]
                    if ep.get("id"):
                        body["episode"] = ep["id"]
                self._play_async(body, entry=self.entry)
            elif self.view == "settings":
                self.select_setting()
            elif self.view == "extras":
                self.select_extra()
            elif self.view == "bt":
                self.select_bt()
            elif self.view == "output":
                self.select_output()
            elif self.view == "sonos":
                self.select_sonos()
            elif self.view == "btscan":
                if self.bt_found:
                    d = self.bt_found[self.sel]
                    self.bt_connect(d["mac"], d["name"])
        except OSError as e:
            log(f"action failed: {e}")
            self.draw_message("Network error — try again")
            time.sleep(1)

    def select_setting(self):
        # Dispatch on the row's LABEL, never its index. The index form
        # broke every time a row was inserted — one inverted flag once
        # made "Restart" power the box off instead (field-reported) — and
        # this menu grows. The labels are the same literals as
        # current_items() above; a typo shows up as a dead row, not as
        # the wrong action firing.
        rows = self.current_items()
        label = rows[self.sel][0] if self.sel < len(rows) else ""
        cycles = {"Screen off after": ("screen_timeout_s", [15, 30, 60, 0]),
                  "Brightness": ("screen_brightness", [25, 50, 75, 100]),
                  "Volume cap": ("volume_cap", [60, 70, 80, 90, 100]),
                  # 5 first: it is the shipped default, and its absence
                  # meant ONE trip through this cycle lost the 5-min
                  # setting for good (QA power audit 2026-08-10)
                  "Auto-off (idle)": ("idle_shutdown_min", [5, 15, 30, 60, 0]),
                  "Browse": ("simple_nav", [0, 1, 2])}  # menus/flat/cats
        if label in cycles:
            key, opts = cycles[label]
            cur = self.settings.get(key)
            nxt = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0]
            self.settings = api_put("/settings", {key: nxt})
            if key == "screen_brightness":
                self.display.set_brightness(nxt)  # live preview
        elif label == "Wi-Fi":
            enabled = (self.system.get("wifi") or {}).get("enabled")
            self.draw_message("Please wait ...")
            r = api_post("/system/wifi", {"enabled": not enabled})
            self.system.setdefault("wifi", {}).update(r)
        elif label == "Setup hotspot":
            # Setup hotspot from the BOX: the only way in at a new place
            # when saved networks aren't around — the PWA needs a shared
            # network, which is exactly what's missing (chicken-and-egg).
            # Joining the AP pops the phone's captive portal into the PWA.
            hs = bool((self.system.get("wifi") or {}).get("hotspot"))
            if hs:
                self.draw_message("Stopping hotspot ...")
                api_post("/wifi/hotspot", {"enabled": False}, timeout=45)
                time.sleep(1.2)
            else:
                # start_hotspot scans FIRST (the radio can't scan in AP
                # mode) — that is most of the wait
                self.draw_message("Starting hotspot ...\n(scanning, ~30 s)")
                r = api_post("/wifi/hotspot", {"enabled": True}, timeout=90)
                if r.get("ok"):
                    self.draw_message(f"On your phone, join\n"
                                      f"“{r.get('ssid')}”\n"
                                      f"password: {r.get('password')}")
                    time.sleep(8)  # long enough to actually read it
                else:
                    self.draw_message("Hotspot failed — try again")
                    time.sleep(1.5)
            self.last_system = 0.0  # refresh the hotspot state row now
        elif label == "Bluetooth":
            self.draw_message("Loading speakers ...")
            self.bt = api_get("/bt")
            self.push("bt")
        elif label == "Storage":
            self.push("storage")
        elif label == "Link phone":
            self._link_since = time.monotonic()
            self.push("link")
        elif label in ("Shut down", "Restart"):
            restart = label == "Restart"
            action = "Restarting" if restart else "Shutting down"
            self.draw_message(f"{action} ... (A confirms, B cancels)")
            if self.confirm():
                self.draw_message(f"{action} ...")
                api_post("/system/shutdown", {"restart": restart})

    def extras(self):
        """Owner-dropped launch scripts (docs/extras.md). SSH is the
        only road in, and the scan enforces it: a file must be a
        regular executable owned by OUR uid (root on the box) and not
        writable by group/other — anything else is skipped, because a
        kid-writable file in this dir would be one A-press from root.
        Display name from a '# vibb-name:' header, else the
        filename."""
        out = []
        try:
            names = sorted(os.listdir(EXTRAS_DIR))
        except OSError:
            return out  # no dir = stock box = the chord is a no-op
        for fn in names:
            path = os.path.join(EXTRAS_DIR, fn)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if not os.path.isfile(path) or not os.access(path, os.X_OK):
                continue
            if st.st_uid != os.geteuid() or st.st_mode & 0o022:
                continue  # wrong owner / group- or world-writable
            name = ""
            try:
                with open(path, errors="ignore") as f:
                    for i, line in enumerate(f):
                        if i >= 15:
                            break
                        if line.lower().startswith("# vibb-name:"):
                            name = line.split(":", 1)[1].strip()
                            break
            except OSError:
                pass
            if not name:
                name = (os.path.splitext(fn)[0]
                        .replace("-", " ").replace("_", " ").strip()
                        .title() or fn)
            out.append({"name": name, "path": path})
        return out

    def select_extra(self):
        """Hand the whole box to an owner script. The wrapper runs as a
        TRANSIENT systemd unit (survives vibb-ui exiting — we hold
        the SPI display and the buttons, so we must die for the extra
        to live) whose ExecStopPost restores vibb no matter how the
        extra or the wrapper ends — clean exit, crash, even SIGKILL."""
        items = self.extras()
        if not items or self.sel >= len(items):
            return
        ex = items[self.sel]
        self.draw_message(f"Start {ex['name']}?\nVibb stops while it "
                          "runs.\n(A confirms, B cancels)")
        if not self.confirm():
            self.dirty = True
            return
        log(f"extras: handing the box to {ex['path']}")
        self.draw_message(f"Starting {ex['name']} ...\n"
                          "(first run can take a minute)")
        p = subprocess.Popen([
            "systemd-run", "--unit=vibb-extra", "--collect",
            "--property=Restart=no",
            f"--property=ExecStopPost={EXTRA_WRAPPER} --restore",
            EXTRA_WRAPPER, "--run", ex["path"]])
        try:
            rc = p.wait(timeout=10)  # systemd-run exits once the unit is up
        except Exception:
            rc = 0
        if rc:
            self.draw_message("Could not start — see journalctl "
                              "-u vibb-extra")
            time.sleep(4)
            self.dirty = True
            return
        # HOLD this frame until the wrapper kills us: returning to the
        # render loop repainted the MENU over it, and the panel then
        # froze on the menu for the whole handoff — read in the field
        # as 'it jumped back to extras and ignores buttons'
        # (2026-07-29). The ceiling is only the escape hatch for a
        # launch that silently never stops us.
        end = time.monotonic() + float(
            os.environ.get("VIBB_EXTRA_HOLD_S", "90"))
        while time.monotonic() < end:
            time.sleep(0.5)
        self.dirty = True

    def select_bt(self):
        # Label-based like select_setting: the action rows above the
        # device list have grown once already, and an index-keyed
        # dispatch silently misfires the moment they grow again.
        rows = self.current_items()
        label = rows[self.sel][0] if self.sel < len(rows) else ""
        n_actions = 3  # rows before the device list
        if label == "Pair nearest":  # the one-button flow
            self.draw_message("Pairing the nearest speaker ... (up to 60 s)")
            try:
                r = api_post("/bt/pair", {}, timeout=130)
                self.bt = {k: r[k] for k in ("configured", "devices", "pairing")
                           if k in r} or api_get("/bt")
                self.draw_message("Paired!" if r.get("ok")
                                  else (r.get("output") or "Failed").splitlines()[-1])
            except OSError as e:
                self.draw_message(f"Failed: {e}")
            time.sleep(2)
        elif label == "Scan for new":
            self.draw_message("Scanning ... (~25 s)")
            try:
                r = api_post("/bt/scan", {}, timeout=70)
                self.bt_found = r.get("found", [])
                self.push("btscan")
            except OSError as e:
                self.draw_message(f"Failed: {e}")
                time.sleep(2)
        elif label == "Pair from car":
            # The INBOUND direction: a car stereo (or any device that
            # insists on starting the pairing itself) can't be paired by
            # us reaching out — the box has to become discoverable and
            # accept. Same firmware-crash quiesce as the outbound flow,
            # server-side.
            secs = 120
            self.draw_message(f"Box is visible as a speaker for "
                              f"{secs // 60} min.\nStart pairing from the "
                              f"car's\nBluetooth menu.")
            try:
                r = api_post("/bt/visible", {"secs": secs},
                             timeout=secs + 160)
                self.bt = api_get("/bt")
                self.draw_message("Paired!" if r.get("ok")
                                  else "No pairing came in — try again")
            except OSError as e:
                self.draw_message(f"Failed: {e}")
            time.sleep(2)
        else:
            d = self.bt["devices"][self.sel - n_actions]
            self.bt_connect(d["mac"], d["name"])

    def bt_connect(self, mac, name):
        self.draw_message(f"Connecting to {name} ...")
        try:
            r = api_post("/bt/connect", {"mac": mac}, timeout=120)
            for k in ("configured", "devices", "pairing"):
                if k in r:
                    self.bt[k] = r[k]
            self.draw_message("Connected!" if r.get("ok")
                              else (r.get("output") or "Failed").splitlines()[-1])
        except OSError as e:
            self.draw_message(f"Failed: {e}")
        time.sleep(2)
        if self.view == "btscan":
            self.back()

    def confirm(self, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for ev in self.inputs.poll(0.1):
                if ev == "a":   # A acts, B backs out — same as everywhere
                    return True
                if ev == "b":
                    return False
        return False

    # -- rendering ----------------------------------------------------------------

    def splash(self, sub="starting"):
        """Boot screen: drawn the moment the process starts, long before
        vibbd (and the rest of the boot) is ready."""
        img = Image.new("RGB", (W, H), BG)
        d = _draw(img)
        d.text((W // 2, H // 2 - 16), "Vibb", font=F_BIG, fill=HILITE,
               anchor="mm")
        d.text((W // 2, H // 2 + 18), sub, font=F_SMALL, fill=DIM, anchor="mm")
        self.display.show(img)

    def draw_message(self, text):
        img = Image.new("RGB", (W, H), BG)
        d = _draw(img)
        # word-wrap to the panel: long one-liners (the extras 'no TV
        # found' note, verbose bt errors) ran off the 240px edge
        # (field 2026-07-30). Explicit \n stays a hard break.
        lines = wrap_text(d, text, F_MED, W - 16)
        line_h = 24
        y = H // 2 - (len(lines) - 1) * line_h // 2
        for ln in lines:
            d.text((W // 2, y), ln, font=F_MED, fill=FG, anchor="mm")
            y += line_h
        battery_corner(d, self.system)
        self.display.show(img)

    def render(self):
        img = Image.new("RGB", (W, H), BG)
        d = _draw(img)
        rolls = False  # a too-long selected label is sliding -> keep painting
        if self.view == "home":
            art = self.section_art()  # uploaded category logo (PWA)
            rolls = draw_list(d, "Vibb", self.current_items(), self.sel,
                              self.system,
                              hint="A: select   hold A+B: settings",
                              maxlen=17 if art else 24,
                              t0=self._marquee_t0("home", self.sel))
            if art:
                img.paste(art, (W - art.width - 6, 26))
        elif self.view == "entries":
            art = self.entry_art()
            rolls = draw_list(d, self.section["name"], self.current_items(),
                              self.sel, self.system, maxlen=17 if art else 24,
                              t0=self._marquee_t0("entries", self.sel))
            if art:
                img.paste(art, (W - art.width - 6, 26))
        elif self.view == "episodes":
            art = self.episode_art()
            rolls = draw_list(d, self.expanded.get("name") or "Episoder",
                              self.current_items(), self.sel, self.system,
                              hint="✓ = downloaded (plays offline)",
                              maxlen=17 if art else 24,
                              t0=self._marquee_t0("episodes", self.sel))
            if art:
                img.paste(art, (W - art.width - 6, 26))
        elif self.view == "settings":
            rolls = draw_list(d, "Settings", self.current_items(), self.sel,
                              self.system, hint="A: change   B: back",
                              t0=self._marquee_t0("settings", self.sel))
        elif self.view == "bt":
            rolls = draw_list(d, "Bluetooth speaker", self.current_items(),
                              self.sel, self.system,
                              hint="● connected   ✓ selected",
                              t0=self._marquee_t0("bt", self.sel))
        elif self.view == "btscan":
            rolls = draw_list(d, "Nearby devices", self.current_items(),
                              self.sel, self.system,
                              hint="A: pair and connect   B: back",
                              t0=self._marquee_t0("btscan", self.sel))
        elif self.view == "output":
            rolls = draw_list(d, "Sound out of", self.current_items(),
                              self.sel, self.system,
                              hint="A: choose   B: back",
                              t0=self._marquee_t0("output", self.sel))
        elif self.view == "sonos":
            # stale = the fresh probe found NOBODY (cabin: home's
            # speakers still cached). The rows stay — removing rows
            # under a finger mid-menu is the trap, and the cache's
            # merge semantics never delete — but the hint says the
            # truth instead of promising "play here".
            rolls = draw_list(d, "Sonos", self.current_items(),
                              self.sel, self.system,
                              hint=("No speakers answered here"
                                    if self.sonos.get("stale")
                                    else "A: play here   B: back"),
                              t0=self._marquee_t0("sonos", self.sel))
        elif self.view == "extras":
            # same list chrome as every other menu — the field bug that
            # forced this comment: the view existed (chord opened it, A
            # confirmed a launch) but had no render branch, so the
            # "menu" was a black screen (owner 2026-07-29)
            rolls = draw_list(d, "Extras", self.current_items(), self.sel,
                              self.system, hint="A: start   B: back",
                              t0=self._marquee_t0("extras", self.sel))
        elif self.view == "storage":
            self.render_storage(d)
        elif self.view == "link":
            self.render_link(d, img)
        elif self.view == "carousel":
            rolls = self.render_carousel(d, img)
        elif self.view == "cats":
            rolls = self.render_cats(d, img)
        elif self.view == "now":
            rolls = self.render_now(d, img)
        self.marquee_active = bool(rolls)
        self.display.show(img)

    def render_link(self, d, img):
        """'Link phone': the box's screen IS the credential handoff.
        Showing the token here proves physical possession — the same
        anchor the hold-X output parking uses — so no password, no
        account, nothing to remember or reuse from another service.

        The QR encodes http://<ip>:3679/#t=<TOKEN>. In the FRAGMENT, so
        the secret is never sent to a server (no logs, no Referer), and
        against the box's STABLE <name>.local rather than its IP: the
        browser stores the token per ORIGIN, so an IP-based link is lost
        the moment DHCP moves the box or it comes up as its own hotspot,
        and the parent has to re-scan. The name resolves in both modes
        (mDNS on the LAN; in hotspot the captive resolver answers every
        name with the box). The IP is printed underneath for the rare
        client that can't resolve .local — it can browse there and paste
        the token shown below.

        The same token is printed underneath in Crockford groups: that
        is the fallback when a camera won't focus on a 240px LCD, and
        the recovery path when a phone was never linked. If the qrcode
        lib is missing (an install where pip failed), the text alone
        still provisions a phone — so the import is lazy and optional."""
        from vibb import token as _tok
        d.rectangle([0, 0, W, H], fill=(255, 255, 255))  # scan contrast
        value = ""
        try:
            value = _tok.read()
        except OSError:
            pass
        if not value:
            d.text((10, 100), "No token on the box.", font=F_MED,
                   fill=(0, 0, 0))
            d.text((10, 124), "Restart vibbd to make one.", font=F_SMALL,
                   fill=(90, 90, 90))
            return
        host = _netmgmt.mdns_host()
        ip = (self.system.get("wifi") or {}).get("ip") or ""
        url = f"http://{host}:3679/#t={value}"
        qr_img = None
        try:
            import qrcode  # lazy: a box without it still shows the text
            q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                              border=3)
            q.add_data(url)
            q.make(fit=True)
            # get_matrix() already includes the quiet border, so size
            # against it directly. Deriving the module size (rather than
            # fixing it) means a longer host — a bumped QR version —
            # shrinks to fit instead of overflowing the screen.
            modules = len(q.get_matrix())
            box = max(3, (H - 60) // modules)  # 60px reserved for the 3 text lines
            qr_img = q.make_image(fill_color="black",
                                  back_color="white").convert("RGB")
            qr_img = qr_img.resize((box * modules,) * 2, Image.NEAREST)
        except Exception as e:  # noqa: BLE001 — never break the screen
            log(f"link view: no QR ({e.__class__.__name__}) — showing text")
        if qr_img is not None:
            img.paste(qr_img, ((W - qr_img.width) // 2, 2))
            ty = min(qr_img.height + 6, H - 40)
        else:
            d.text((10, 8), "Link phone", font=F_MED, fill=(0, 0, 0))
            ty = 60
        d.text((W // 2, ty), _tok.grouped(value), font=F_MED,
               fill=(0, 0, 0), anchor="ma")
        sub = f"{host}  ·  {ip}" if ip else host
        d.text((W // 2, ty + 19), sub, font=F_SMALL,
               fill=(110, 110, 110), anchor="ma")
        d.text((W // 2, ty + 33), "A: new token   B: back", font=F_SMALL,
               fill=(110, 110, 110), anchor="ma")

    def render_storage(self, d):
        d.text((10, 4), "Storage", font=F_MED, fill=DIM)
        battery_corner(d, self.system)
        y = 40
        disk = self.system.get("disk") or {}
        rows = []
        if disk:
            used = disk["total"] - disk["free"]
            rows.append(("SD card", f"{fmt_bytes(used)} / {fmt_bytes(disk['total'])}"))
            rows.append(("Free", fmt_bytes(disk["free"])))
        for name, size in (self.system.get("caches") or {}).items():
            label = "Podcast cache" if name == "podcasts" else "Spotify cache"
            rows.append((label, fmt_bytes(size)))
        wifi = self.system.get("wifi") or {}
        rows.append(("Wi-Fi", wifi.get("ssid") or "—"))
        rows.append(("IP", wifi.get("ip") or "—"))
        if self.system.get("cpu_temp") is not None:
            rows.append(("CPU temp", f"{self.system['cpu_temp']}°C"))
        for label, val in rows:
            d.text((12, y), label, font=F_MED, fill=DIM)
            d.text((W - 12, y), val, font=F_MED, fill=FG, anchor="ra")
            y += 26

    def render_now(self, d, img):
        st = self.status or {}
        battery_corner(d, self.system)
        # (no-internet is a POPUP now — _net_overlay at the end — matching
        # the BT-disconnect popup instead of a thin banner; field ask
        # 2026-07-18)
        # Cover priority: a per-item image already on disk (podcast
        # episode art) is instant; a remote cover (Spotify album art,
        # gfx.nrk.no episode art) is fetched OFF the render thread so it
        # never blocks or stalls the UI, and lands on a later repaint;
        # meanwhile the cached collection cover (show cover / playlist
        # mosaic) fills in immediately so the card is never blank —
        # offline or while the remote loads.
        ep_art = st.get("artwork")
        local = st.get("artwork_local")
        art = None
        if ep_art and not str(ep_art).startswith("http"):
            art = self.artwork(ep_art, 128, square=True)
        if art is None and ep_art and str(ep_art).startswith("http"):
            art = self.artwork_async(ep_art, 128, square=True)  # bg; may be None
        if art is None:
            # New remote cover still loading (track change): keep showing
            # the PREVIOUS cover for this target instead of flashing the
            # mosaic for a second on every skip (field 2026-07-18). The
            # mosaic is only the first-paint fallback; a target switch
            # never reuses another album's art.
            prev_target, prev_art = self._now_art_prev
            if prev_art is not None and prev_target == st.get("target"):
                art = prev_art
        if art is None and local:
            art = self.artwork(local, 128, square=True)  # offline fallback
        if art is not None:
            self._now_art_prev = (st.get("target"), art)
        if art:
            img.paste(art, ((W - art.width) // 2, 24))
            ty = 156
        else:
            ty = 70
        title = st.get("title") or "(nothing playing)"
        # width capped so the text never runs under the side markers
        # (they sit at the physical button heights, x < 22)
        rolls = False
        if st.get("source") == "spotify":
            # Spotify: ONE line — sliding when too long — so the artist
            # is ALWAYS visible beneath (field pick). Only when spotify
            # is the ACTIVE source: /status keeps the paused-spotify
            # block around during mpv playback, and its last artist has
            # nothing to do with the podcast episode showing.
            if d.textlength(title, font=F_MED) > W - 44:
                title, rolls = marquee(title, 20,
                                       t0=self._marquee_t0("now", title))
            d.text((W // 2, ty), title, font=F_MED, fill=FG, anchor="ma")
            sub = ", ".join((st.get("spotify") or {}).get("artists") or [])
            if sub:
                d.text((W // 2, ty + 22), sub[:30], font=F_SMALL,
                       fill=DIM, anchor="ma")
        else:
            # podcasts: up to two lines (long episode names matter most);
            # a tail that doesn't fit even then slides, never cut off
            lines = wrap_two(d, title, F_MED, W - 44)
            d.text((W // 2, ty), lines[0], font=F_MED, fill=FG, anchor="ma")
            if len(lines) > 1:
                l2 = lines[1]
                if d.textlength(l2, font=F_MED) > W - 44:
                    l2, rolls = marquee(l2, 20,
                                        t0=self._marquee_t0("now2", l2))
                d.text((W // 2, ty + 19), l2, font=F_MED, fill=FG,
                       anchor="ma")
        pos, dur = st.get("position"), st.get("duration")
        bar_y = H - 34  # below the B/Y markers (y 178-192) — no overlap
        d.rectangle([14, bar_y, W - 14, bar_y + 5], fill=(50, 50, 65))
        if pos and dur:
            frac = max(0.0, min(1.0, pos / dur))
            d.rectangle([14, bar_y, 14 + frac * (W - 28), bar_y + 5], fill=HILITE)
        left = fmt_time(pos) if pos is not None else "--:--"
        right = "live" if (pos is not None and dur is None) else fmt_time(dur)
        d.text((14, bar_y + 10), left, font=F_SMALL, fill=DIM)
        d.text((W - 14, bar_y + 10), right, font=F_SMALL, fill=DIM, anchor="ra")
        # Button markers sit where the PHYSICAL buttons are — the same
        # spots the carousel uses (A/X centers ~y=55, B/Y ~y=185, hugging
        # the screen edges). Drawn shapes: DejaVu has no media glyphs.
        # A (top left): play/pause — THE action, in the highlight color
        if st.get("playing"):
            d.rectangle([6, 47, 10, 63], fill=HILITE)
            d.rectangle([14, 47, 18, 63], fill=HILITE)
        else:
            d.polygon([(5, 47), (5, 63), (19, 55)], fill=HILITE)
        # X (top right): volume
        d.polygon([(W - 19, 52), (W - 13, 52), (W - 6, 46),
                   (W - 6, 64), (W - 13, 58), (W - 19, 58)], fill=DIM)
        # B (bottom left): previous |<
        d.rectangle([5, 178, 7, 192], fill=DIM)
        d.polygon([(19, 178), (19, 192), (9, 185)], fill=DIM)
        # Y (bottom right): next >|
        d.polygon([(W - 19, 178), (W - 19, 192), (W - 9, 185)], fill=DIM)
        d.rectangle([W - 7, 178, W - 5, 192], fill=DIM)
        self._volume_overlay(d)
        if not self._bt_overlay(d):  # speaker trouble outranks net trouble
            self._net_overlay(d)
        self._output_overlay(d)  # deliberate hold-X confirmation sits on top
        return rolls

    def _volume_overlay(self, d):
        """The transient card X opens, and the tab strip that says there
        is more than one of them.

        The strip is the whole reason a cycle is acceptable: a hidden
        sequence of presses has to be LEARNED, whereas three tabs with
        one lit say 'there are others' the first time you see them. Icons
        rather than words because 160px cannot hold three legible labels
        — and the shuffle tab draws the very same glyph that then appears
        in the top bar, so the symbol teaches itself.

        Drawn ABOVE the progress bar on purpose: on the seek card the bar
        is the readout. The number here is the size of the jump you just
        made; the bar below is where you landed."""
        if time.monotonic() >= self.volume_flash:
            return
        card = self._card() or "vol"
        # Tabs only in now-playing, where the cycle actually exists. The
        # browse views keep the exact card they always had, at the size
        # they always had it: X is volume and nothing else there, and a
        # strip advertising tabs X cannot reach would be a lie.
        if self.view != "now":
            d.rounded_rectangle([50, 84, 190, 136], radius=8, fill=(30, 30, 45))
            shown = "–" if self.volume_shown is None else self.volume_shown
            d.text((W // 2, 92), f"Volume {shown}", font=F_MED,
                   fill=HILITE, anchor="ma")
            d.text((60, 116), "B  -", font=F_SMALL, fill=DIM)
            d.text((W - 60, 116), "+ Y", font=F_SMALL, fill=DIM, anchor="ra")
            return
        d.rounded_rectangle([40, 80, 200, 148], radius=8, fill=(30, 30, 45))
        d.line([(45, 100), (195, 100)], fill=(60, 60, 75))
        for i, cx in enumerate((70, 120, 170)):
            on = CARDS[i] == card
            self._card_icon(d, CARDS[i], cx, HILITE if on else DIM)
            if on:
                d.rectangle([cx - 22, 99, cx + 22, 100], fill=HILITE)
        y1, y2, xl, xr = 107, 134, 54, 186
        if card == "seek":
            if self.seek_shown is None:
                d.text((W // 2, y1), "Seek", font=F_MED, fill=DIM, anchor="ma")
            else:
                sign = "+" if self.seek_shown > 0 else "-"
                d.text((W // 2, y1), f"{sign} {fmt_time(abs(self.seek_shown))}",
                       font=F_MED, fill=HILITE, anchor="ma")
            _seek_arrows(d, xl, y2 + 7, DIM, back=True)
            d.text((xl + 20, y2), "B", font=F_SMALL, fill=DIM)
            d.text((xr - 20, y2), "Y", font=F_SMALL, fill=DIM, anchor="ra")
            _seek_arrows(d, xr - 14, y2 + 7, DIM, back=False)
        elif card == "shuf":
            on = bool((self.status or {}).get("shuffle"))
            d.text((W // 2, y1), f"Shuffle {'on' if on else 'off'}",
                   font=F_MED, fill=HILITE if on else DIM, anchor="ma")
            d.text((xl, y2), "B  off", font=F_SMALL, fill=DIM)
            d.text((xr, y2), "on  Y", font=F_SMALL, fill=DIM, anchor="ra")
        else:
            shown = "–" if self.volume_shown is None else self.volume_shown
            d.text((W // 2, y1), f"Volume {shown}", font=F_MED,
                   fill=HILITE, anchor="ma")
            d.text((xl, y2), "B  -", font=F_SMALL, fill=DIM)
            d.text((xr, y2), "+ Y", font=F_SMALL, fill=DIM, anchor="ra")

    @staticmethod
    def _card_icon(d, kind, cx, colour):
        """One tab icon, centred on cx, occupying y 82-94. Shapes, not
        glyphs: DejaVu has no media characters (same reason the button
        markers are drawn by hand)."""
        if kind == "vol":
            d.polygon([(cx - 7, 86), (cx - 3, 86), (cx + 2, 82),
                       (cx + 2, 94), (cx - 3, 90), (cx - 7, 90)], fill=colour)
            d.arc([cx + 3, 82, cx + 9, 94], -60, 60, fill=colour)
        elif kind == "seek":
            _seek_arrows(d, cx - 6, 88, colour, back=False)
        else:
            _shuffle_glyph(d, cx, 83, 93, colour)

    def _output_overlay(self, d):
        """Hold-X output-switch confirmation, in the SAME rounded-box shape
        as the speaker/net popups (cosmetic parity, field ask 2026-07-21)
        rather than the old blocking full-screen message. Transient: it
        self-clears when output_flash expires, exactly like the volume card.
        Green box for a normal switch, warning-red when the target has no
        sound card (the same detail the old message carried)."""
        if time.monotonic() >= self.output_flash:
            return
        if self.output_warning:
            d.rounded_rectangle([22, 70, W - 22, 156], radius=10,
                                fill=(45, 30, 30))
            d.text((W // 2, 80), self.output_shown, font=F_MED, fill=WARN,
                   anchor="ma")
            d.text((W // 2, 108), "output switched", font=F_SMALL, fill=FG,
                   anchor="ma")
            d.text((W // 2, 130), "no sound card?", font=F_SMALL, fill=DIM,
                   anchor="ma")
        else:
            d.rounded_rectangle([22, 82, W - 22, 144], radius=10,
                                fill=(28, 45, 30))
            d.text((W // 2, 92), self.output_shown, font=F_MED, fill=HILITE,
                   anchor="ma")
            d.text((W // 2, 118), "output switched", font=F_SMALL, fill=FG,
                   anchor="ma")

    def _bt_overlay(self, d):
        """Speaker-state popup, driven entirely by /status (field log
        2026-07-17: the speaker came up 25s before anyone pressed play —
        nobody KNEW it was ready). bt_waiting = a play attempt hit a
        disconnected speaker: tell them, and offer X = a full connect of
        the configured device (incl. crash recovery — stronger than the
        kick that already happened). The daemon flips it to bt_ready the
        moment the transport is up: 'press A'. Painted LAST, over the
        volume card; self-clears because the daemon expires both states."""
        st = self.status or {}
        if st.get("bt_lost"):
            # the speaker DIED mid-play and the daemon stopped playback
            # (mpv skips episodes wildly into a dead device otherwise)
            d.rounded_rectangle([22, 70, W - 22, 156], radius=10,
                                fill=(45, 30, 30))
            d.text((W // 2, 80), "Speaker disconnected", font=F_MED,
                   fill=WARN, anchor="ma")
            hint = ("connecting..." if time.monotonic()
                    < self.bt_connecting_until else "X: reconnect")
            d.text((W // 2, 108), hint, font=F_SMALL, fill=FG, anchor="ma")
            if st.get("bt_local_ok"):
                d.text((W // 2, 130), "A: play on box speaker",
                       font=F_SMALL, fill=DIM, anchor="ma")
            return True
        if st.get("bt_waiting"):
            # identical shape to the bt_lost popup: X connects the
            # speaker, A plays on the built-in one instead (where present)
            d.rounded_rectangle([22, 70, W - 22, 156], radius=10,
                                fill=(45, 30, 30))
            d.text((W // 2, 80), "Speaker not connected", font=F_MED,
                   fill=WARN, anchor="ma")
            hint = ("connecting..." if time.monotonic()
                    < self.bt_connecting_until else "X: connect now")
            d.text((W // 2, 108), hint, font=F_SMALL, fill=FG, anchor="ma")
            if st.get("bt_local_ok"):
                d.text((W // 2, 130), "A: play on box speaker",
                       font=F_SMALL, fill=DIM, anchor="ma")
            return True
        if st.get("bt_ready"):
            d.rounded_rectangle([22, 82, W - 22, 144], radius=10,
                                fill=(28, 45, 30))
            d.text((W // 2, 92), "Speaker connected!", font=F_MED,
                   fill=HILITE, anchor="ma")
            d.text((W // 2, 118), "Press A to play", font=F_SMALL, fill=FG,
                   anchor="ma")
            return True
        return False

    def _net_overlay(self, d):
        """No-internet popup for an active Spotify source — the SAME
        shape as the speaker popups (field ask 2026-07-18: the thin text
        banner over the album art read as decoration, not as something
        to act on). X runs the on-demand wifi reconnect, exactly like X
        reconnects the speaker on the BT popup. Only when spotify is the
        active source: cached podcasts play fine offline and must not get
        a scary popup."""
        st = self.status or {}
        if not (st.get("spotify_offline") and st.get("source") == "spotify"):
            return False
        d.rounded_rectangle([22, 70, W - 22, 156], radius=10,
                            fill=(45, 30, 30))
        d.text((W // 2, 80), "No internet", font=F_MED,
               fill=WARN, anchor="ma")
        hint = ("reconnecting Wi-Fi..." if time.monotonic()
                < self.wifi_connecting_until else "X: reconnect Wi-Fi")
        d.text((W // 2, 108), hint, font=F_SMALL, fill=FG, anchor="ma")
        d.text((W // 2, 130), "Spotify needs internet",
               font=F_SMALL, fill=DIM, anchor="ma")
        return True

    def _wake_press(self, events):
        """The press that wakes a dark screen is swallowed — EXCEPT A
        while music plays: pausing is the most urgent action there is,
        and needing a second press read as 'won't let me pause' (field
        2026-07-18 20:18). Playing is checked with a FRESH probe —
        self.status can be hours stale in the dark, and acting on a
        stale playing=True would REPLAY the last target: surprise audio
        from a bag. Probe fails or shows idle -> plain wake, nothing
        else. B/Y stay wake-only too (buttons squeezed in a bag must
        not scramble the queue in the dark). A goes straight to
        /playpause, never through handle_now — its popup branches map A
        to 'play on box speaker'."""
        if "a" not in events:
            return
        try:
            st = api_get("/status", timeout=1.5)
        except OSError:
            return
        if not st.get("playing"):
            return
        self.status = st
        try:
            api_post("/playpause", timeout=CONTROL_TIMEOUT)
            self.last_status = 0.0
        except OSError as e:
            log(f"dark-pause failed: {e}")

    def _play_on_local(self):
        """The speaker popup's A action: drop back to the built-in
        speaker. If nothing's sounding (bt_lost stopped it, or a fresh
        play attempt), resume from the bookmark; if audio is ALREADY
        playing on the built-in one (you switched the output to a
        disconnected BT speaker), just make the output local — don't
        toggle playpause and pause it. Fire-and-forget; the popup clears
        via /status once the output is no longer bt."""
        was_playing = bool((self.status or {}).get("playing"))

        def go():
            try:
                api_post("/output", {"device": "local"}, timeout=30)
                if not was_playing:
                    api_post("/playpause", timeout=CONTROL_TIMEOUT)
            except OSError as e:
                log(f"play-on-local failed: {e}")
            self.last_status = 0
        threading.Thread(target=go, daemon=True).start()

    def _reconnect_for_spotify(self):
        """Pressing play on a Spotify tile with no net: don't dead-end
        with 'can't play' — that IS an explicit 'get me the net'. Run the
        reconnect (blocking, with a message, like the pair flow), then
        play if it worked."""
        self.draw_message("No internet —\nreconnecting Wi-Fi ...")
        try:
            r = api_post("/wifi/reconnect", {"secs": 30}, timeout=45)
        except OSError:
            r = {}
        self.last_system = 0  # refresh wifi state
        if not r.get("ok"):
            self.draw_message("Still no internet —\ntry again later")
            time.sleep(1.5)
            self.dirty = True

    def _wifi_reconnect(self):
        """The offline-Spotify popup's X action: fire-and-forget on-demand
        wifi reconnect (daemon quiesces A2DP, waits for a known network,
        unparks go-librespot on success). Progress shows in the banner;
        the daemon 409s overlaps, so mashing X is harmless."""
        if time.monotonic() < self.wifi_connecting_until:
            return
        self.wifi_connecting_until = time.monotonic() + 35

        def go():
            try:
                api_post("/wifi/reconnect", {"secs": 30}, timeout=45)
            except (OSError, ValueError):
                pass
            self.wifi_connecting_until = 0.0
            self.last_status = 0  # re-poll: banner clears when back online
            self.last_system = 0
        threading.Thread(target=go, daemon=True).start()

    def _bt_connect_last(self):
        """The popup's X action: fire-and-forget full connect of the
        configured speaker (bt.py use — includes firmware-crash
        recovery). Progress comes back via /status; the daemon 409s
        overlapping attempts, so mashing X is harmless."""
        if time.monotonic() < self.bt_connecting_until:
            return
        self.bt_connecting_until = time.monotonic() + 60

        def go():
            try:
                mac = (api_get("/bt", timeout=10) or {}).get("configured")
                if mac:
                    api_post("/bt/connect", {"mac": mac}, timeout=120)
            except (OSError, ValueError):
                pass
            self.bt_connecting_until = 0.0
        threading.Thread(target=go, daemon=True).start()

    def _marquee_t0(self, *key):
        """Phase anchor for the ONE visible marquee: remembers when the
        current label became selected, so marquee() rests at the START
        of the name first (field 2026-08-12: the wall-clock phase
        showed a random middle of the title on landing)."""
        if getattr(self, "_mq_key", None) != key:
            self._mq_key = key
            self._mq_t0 = time.monotonic()
        return self._mq_t0

    def _cover_surface(self, art, name, new=False):
        """One 176x176 cover as a standalone surface — the tile face,
        and what the shelf slide moves. Real art comes straight from
        the cache (copied only when the new-dot must be drawn on it); a
        missing cover becomes the same stable colored-initial tile, so
        placeholders slide exactly like art does (design review
        2026-08-12: one source of truth for the face, or the two
        renderings drift)."""
        if art is not None and not new:
            return art  # the cached object, pasted read-only
        if art is not None:
            surf = art.copy()
            d = _draw(surf)
        else:
            # no cover: a colored tile with the initial — stable color
            # per name so kids still recognise "their" tile
            surf = Image.new("RGB", (176, 176), BG)
            d = _draw(surf)
            palette = [(196, 92, 82), (206, 148, 70), (98, 158, 88),
                       (84, 138, 186), (142, 108, 178), (186, 98, 140)]
            color = palette[sum(name.encode()) % len(palette)]
            d.rounded_rectangle([0, 0, 176, 176], radius=14, fill=color)
            d.text((88, 88), (name[:1] or "?").upper(),
                   font=font(96), fill=FG, anchor="mm")
        if new:
            # fresh-content dot, top-right corner of the cover — a dark
            # ring lifts it off any artwork. Cleared once the show is
            # played; never changes what A does.
            cx, cy, r = 176 - 15, 15, 8
            d.ellipse([cx - r - 2, cy - r - 2, cx + r + 2, cy + r + 2],
                      fill=BG)
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=GOOD)
        return surf

    def _tile_chrome(self, d):
        """The static big-tile markers. Sit where the PHYSICAL buttons
        are: the Pirate Audio buttons are inset from the screen corners
        — centers land around y=55 (A/X) and y=185 (B/Y) on the 240px
        panel (field-calibrated). Drawn ON TOP of whatever the covers
        are doing (they pass under these during a slide)."""
        # flip chevrons < > (B / Y), dim outlines hugging the screen edges
        d.line([(17, 177), (6, 185), (17, 193)], fill=DIM, width=3,
               joint="curve")
        d.line([(W - 17, 177), (W - 6, 185), (W - 17, 193)], fill=DIM,
               width=3, joint="curve")
        # A (top left): the action here (open / play), so it gets the
        # highlight color; hugs the edge like the chevrons
        d.polygon([(5, 47), (5, 63), (19, 55)], fill=HILITE)

    def _cover_tile(self, d, img, art, name, new=False):
        """The shared big-tile layout: the cover face (_cover_surface),
        the B/Y flip chevrons, the A action marker, and the sliding
        name. Returns (drawn_name, marquee_flag); callers add any
        per-view overlay (the now-playing underline/progress)."""
        ax, ay = (W - 176) // 2, 24
        surf = self._cover_surface(art, name, new=new)
        img.paste(surf, ((W - surf.width) // 2, ay))
        self._tile_chrome(d)
        label, rolls = marquee(name, 20,
                               t0=self._marquee_t0("tile", name))
        d.text((W // 2, 206), label, font=F_MED, fill=FG, anchor="ma")
        return label, rolls

    def _slide(self, old, new, dx, label=None):
        """The ~150ms shelf glide between two cover surfaces (design
        review 2026-08-12). DEADLINE-DRIVEN: each frame has a scheduled
        time; frames the box is late for are DROPPED and the wall clock
        is hard-capped, so a slow compose degrades to fewer frames —
        never to a UI that lags the finger. MASH RULE: the inputs are
        polled between frames (>=50ms apart — faster corrupts the
        sampler's bounce guard and manufactures phantom holds); any
        event aborts the glide and lands in self._pending for the main
        loop. This is never the authoritative frame: the caller's
        render() paints the landed state (label, progress, overlays)
        immediately after."""
        ay = 24
        base = Image.new("RGB", (W, H), BG)
        bd = _draw(base)
        battery_corner(bd, self.system)
        if label:
            # the LANDING album's name, baked into the static base so
            # it swaps instantly on frame 1 instead of blinking away
            # for the whole glide (sofa 2026-08-12). A fresh-anchor
            # marquee shows the start of the name — exactly what the
            # landing render() paints, so the handover is seamless.
            win, _ = marquee(label, 20, t0=time.monotonic())
            bd.text((W // 2, 206), win, font=F_MED, fill=FG, anchor="ma")
        scratch = Image.new("RGB", (W, H))
        start = time.monotonic()
        last_poll = start - 0.05  # eligible from the FIRST frame on —
        #                           the guard spaces polls, it must not
        #                           delay the first abort chance
        shown, _c_ms, _s_ms = 0, 0.0, 0.0
        for t_i, p in SLIDE_SCHED:
            el = time.monotonic() - start
            if el >= SLIDE_MS:
                break                    # out of time — render() lands it
            if el > t_i + 0.02:
                continue                 # too late for this frame: drop
            if el < t_i:
                time.sleep(t_i - el)
            off = round(p * SLIDE_TRAVEL)
            ox = 32 - off if dx < 0 else 32 + off
            t_c = time.monotonic()
            scratch.paste(base, (0, 0))
            scratch.paste(old, (ox, ay))
            scratch.paste(new, (ox + (SLIDE_TRAVEL if dx < 0
                                      else -SLIDE_TRAVEL), ay))
            self._tile_chrome(_draw(scratch))
            t_s = time.monotonic()
            self.display.show(scratch)
            _c_ms += (t_s - t_c) * 1000
            _s_ms += (time.monotonic() - t_s) * 1000
            shown += 1
            now = time.monotonic()
            if now - last_poll >= 0.05:
                last_poll = now
                ev = self.inputs.poll(0)
                if ev:
                    self._pending.extend(ev)
                    break                # abort: the finger is ahead
        if os.environ.get("VIBB_UI_ANIM_LOG") == "1":
            # the rig verdict both reviews demanded: how many of the 4
            # scheduled frames the box actually managed, and in what time
            n = max(shown, 1)
            log(f"slide: {shown}/4 frames in "
                f"{(time.monotonic() - start) * 1000:.0f}ms "
                f"(compose {_c_ms / n:.0f}ms + push {_s_ms / n:.0f}ms "
                f"per frame)")

    def _flip(self, ents, step):
        """Advance the carousel index and (when eligible) run the shelf
        slide. The index is COMMITTED before the first frame, so an A
        caught mid-slide acts on the landed album by construction."""
        old_sel = self.car_sel % len(ents)
        self.car_sel = (old_sel + step) % len(ents)
        if (not UI_ANIM or self.inputs is None or not self.display.on
                or self.car_sel == old_sel):
            return
        st = self.status or {}
        if st.get("bt_lost") or st.get("bt_waiting") or st.get("bt_ready"):
            return  # a modal popup must not vanish for 150ms
        oe, ne = ents[old_sel], ents[self.car_sel]
        old = self._cover_surface(
            self.artwork_async(oe.get("image"), 176, square=True),
            oe.get("name") or "?", new=bool(oe.get("new")))
        new = self._cover_surface(
            self.artwork_async(ne.get("image"), 176, square=True),
            ne.get("name") or "?", new=bool(ne.get("new")))
        # direction from the BUTTON, never the indices: Y always slides
        # the shelf left, B always right — wrap included
        self._slide(old, new, -step, label=ne.get("name") or "?")

    def render_cats(self, d, img):
        """Nav mode 2: ONE big category tile — flip with B/Y, A opens that
        category's cover carousel. hold-B inside a category comes back
        here."""
        battery_corner(d, self.system)
        cats = self.carousel_cats()
        if not cats:
            d.text((W // 2, H // 2), "Library is empty", font=F_MED,
                   fill=DIM, anchor="mm")
            return False
        self.cat_sel %= len(cats)
        s = cats[self.cat_sel]
        # artwork_async: a local logo decodes inline, a remote one fetches
        # off-thread (parity with render_carousel) so a category with an
        # http image never blocks the render thread on urlopen (QA A3)
        art = self.artwork_async(s.get("image"), 176, square=True)
        # a category tile lights up if ANYTHING inside it is new
        cat_new = any(e.get("new") for e in s.get("entries") or [])
        _label, rolls = self._cover_tile(d, img, art, s.get("name") or "?",
                                         new=cat_new)
        self._volume_overlay(d)
        self._bt_overlay(d)
        return rolls  # a long category name slides like an entry's

    def render_carousel(self, d, img):
        """ONE big cover per entry — flip with B/Y, play with A. Doubles
        as now-playing: the playing entry shows its state and a progress
        bar. In nav mode 2 the entries are one category's; hold-B returns
        to the category carousel."""
        battery_corner(d, self.system)
        ents = self.carousel_entries()
        if not ents:
            msg = ("Nothing here yet" if self._nav_mode() == 2
                   else "Library is empty")
            d.text((W // 2, H // 2), msg, font=F_MED, fill=DIM, anchor="mm")
            return False
        self.car_sel %= len(ents)
        e = ents[self.car_sel]
        if "spotify" in e["target"] \
                and (self.status or {}).get("spotify_offline"):
            # warn BEFORE the kid presses play on a tile that can't work
            d.text((10, 4), "No internet", font=F_SMALL, fill=WARN)
        art = self.artwork_async(e.get("image"), 176, square=True)
        ax, ay = (W - 176) // 2, 24
        name, rolls = self._cover_tile(d, img, art, e["name"],
                                       new=bool(e.get("new")))
        st = self.status or {}
        if st.get("target") == e["target"]:
            # this tile is what's (or was) playing: a thick orange
            # underline beneath the name (playing or paused alike)
            tl = d.textlength(name, font=F_MED)
            d.rounded_rectangle([(W - tl) / 2, 228, (W + tl) / 2, 232],
                                radius=2, fill=HILITE)
            pos, dur = st.get("position"), st.get("duration")
            if pos and dur:
                frac = max(0.0, min(1.0, pos / dur))
                d.rectangle([ax, ay + 172, ax + 176, ay + 176],
                            fill=(50, 50, 65))
                d.rectangle([ax, ay + 172, ax + frac * 176, ay + 176],
                            fill=HILITE)
        self._volume_overlay(d)
        self._bt_overlay(d)
        return rolls

    # -- main loop -------------------------------------------------------------------

    def screen_should_sleep(self):
        # The timeout applies whether on charger or battery (0 = never
        # blank). No special charger behaviour — the screen just blanks
        # after screen_timeout_s of no button input, always.
        if self.view == "link" and self._link_open_s() < LINK_AWAKE_S:
            return False  # a QR nobody can see is not a QR
        t = self.settings.get("screen_timeout_s", 30)
        if t == 0:
            return False
        return time.monotonic() - self.last_input > t

    def _link_open_s(self):
        """Seconds the 'Link phone' screen has been up (0 when closed)."""
        since = getattr(self, "_link_since", 0.0)
        return time.monotonic() - since if since else 0.0

    def _render_watchdog(self):
        """Restart vibb-ui if the single render loop wedges. The loop
        stamps _loop_beat every pass; if it goes stale past UI_WATCHDOG_S
        the loop is stuck (a hung SPI push, a runaway decode) and no
        button will ever register again — so exit and let systemd
        (Restart=always) bring the screen back in seconds instead of the
        kid staring at a frozen frame until the idle shutdown. The
        threshold sits above the longest legitimate inline block (a BT
        pair, ~130s), so a parent mid-pairing is never killed."""
        while True:
            time.sleep(5)
            stale = time.monotonic() - self._loop_beat
            if stale > UI_WATCHDOG_S:
                log(f"render loop stalled {stale:.0f}s — restarting UI")
                os._exit(1)

    def _boot_landing(self):
        """Where the screen opens at power-on. A live session (boot
        resume) or a bookmarked-paused ghost lands on now-playing; an
        EXPIRED session (switched off longer ago than the resume window)
        wakes up on the browse root instead, with the remembered tile
        selected. Split out of run() so the rule can be pinned.
        """
        # An expired session means "wake up in the carousel": the daemon
        # still serves a ghost card for the remembered entry (a tile with
        # its progress is right), but the screen must not open INSIDE it.
        landed = (self.status.get("title")
                  and self.status.get("session", "fresh") != "expired")
        nav = self._nav_mode()
        if nav in (1, 2):
            # carousel modes: position on whatever is (or last was)
            # playing — opens on now-playing if live, else on the browse
            # root (the flat carousel, or the category carousel).
            tgt = self.status.get("target")
            root = "carousel"
            if nav == 2:
                self.car_section = None
                root = "cats"
                for s in (self.library or {}).get("sections", []):
                    for i, e in enumerate(s.get("entries") or []):
                        if e.get("target") == tgt:
                            self.car_section, self.car_sel = s.get("id"), i
                            root = "carousel"
                            break
                    if self.car_section is not None:
                        break
            else:
                for i, e in enumerate(self.flat_entries()):
                    if e["target"] == tgt:
                        self.car_sel = i
                        break
            if landed:
                self.stack, self.view = [(root, 0)], "now"
            else:
                self.stack, self.view = [], root
        elif landed:
            self.stack = [("home", 0)]
            self.view = "now"

    def run(self):
        # Show the splash immediately, then wait for vibbd — during boot
        # it is usually a few seconds behind us.
        ticks = 0
        while True:
            try:
                # gate on /settings only (a local file read — always fast);
                # /system can take seconds at boot (pisugar, go-librespot
                # flapping) and kept the splash up long after playback ran
                self.settings = api_get("/settings", timeout=2)
                break
            except (OSError, ValueError):
                self.splash("starting" + "." * (ticks % 4))
                ticks += 1
                time.sleep(0.7)
        try:
            self.system = api_get("/system", timeout=3)
        except (OSError, ValueError):
            pass  # refresh() fills it in on the next tick
        self.display.set_brightness(self.settings.get("screen_brightness", 100))
        msg = consume_extra_msg()
        if msg:
            # An extra (or its wrapper) left a word for the human — say
            # it now, while "why am I back at the music box?" is fresh.
            # Same draw_message primitive as every other error screen
            # (bt connect, network) and dismissable like them: any
            # button ends the 6s early.
            self.draw_message(msg)
            end = time.monotonic() + 6
            while time.monotonic() < end:
                if self.inputs.poll(0.2):
                    break
        self.load_library()
        threading.Thread(target=self._prewarm_art, daemon=True).start()
        # Come back where we were: a live session (boot resume) or a
        # bookmarked-paused ghost puts the screen straight on now-playing.
        try:
            self.status = api_get("/status", timeout=3)
            # The daemon may still be waiting for the RTC/NTP correction
            # before it can judge the session (it starts before both, on
            # purpose). Splash a moment longer rather than land on the
            # wrong screen — the panel already says "starting...".
            for _ in range(SESSION_WAIT_TICKS):
                if self.status.get("session") != "pending":
                    break
                time.sleep(0.5)
                self.status = api_get("/status", timeout=3)
        except (OSError, ValueError):
            self.status = {}
        self._boot_landing()
        # joins the animation thread: the splash and the real UI must
        # never both own the panel. getattr — tests build App directly.
        getattr(self, "splash_done", lambda: None)()
        t0 = getattr(self, "boot_t0", None)
        if t0 is None:
            log("ready")                       # tests build App directly
        else:
            up = time.monotonic() - t0
            log(f"READY at boot+{_uptime():.1f}s "
                f"({up:.1f}s in the ui, splash shown "
                f"{up - self.splash_at:.1f}s, landed on {self.view})")

        self._loop_beat = time.monotonic()
        if UI_WATCHDOG_S:
            threading.Thread(target=self._render_watchdog,
                             daemon=True).start()
        threading.Thread(target=self._poller, daemon=True).start()  # P1
        while True:
            self._loop_beat = time.monotonic()
            if self._card_repeat is not None:
                # B/Y held on a card: keep stepping while it is down. The
                # input layer fires a hold ONCE by design, so the repeat
                # is timed here, against the pin state the sampler
                # refreshes every poll.
                dirn, at = self._card_repeat
                if not self._card() \
                        or ("b" if dirn < 0 else "y") not in self.inputs.down:
                    self._card_repeat = None
                elif time.monotonic() >= at:
                    self._card_step(dirn, held=True)
                    self._card_repeat = (dirn,
                                         time.monotonic() + CARD_REPEAT_S)
            if self.shuffle_refused or self.seek_refused:
                # a card's POST came back with nothing routed: sonos, a
                # live stream, or no session at all. Drawing belongs on
                # this thread, never on the poster's — same rule as below.
                msg = ("Can't shuffle here" if self.shuffle_refused
                       else "Can't seek here")
                self.shuffle_refused = self.seek_refused = False
                self.vol_mode_until = self.volume_flash = 0.0  # the card
                #   would otherwise outlive the message that replaced it
                self.draw_message(msg)
                time.sleep(1.4)
                self.dirty = True
            if self.play_offline:
                # _play_async's background POST came back 'no-internet'.
                # The reconnect flow DRAWS (and sleeps), so it belongs on
                # this thread, never on the poster's.
                self.play_offline = False
                if self.view == "now":
                    # leave the optimistic now-playing view — but only if
                    # the user hasn't already navigated somewhere else,
                    # or back() would eat a level they chose themselves
                    self.back()
                self._reconnect_for_spotify()
            self.inputs.gesture_mode = (self.view == "now")
            # arm hold-B up-navigation in the category carousel (mode 2):
            # short B still flips tiles, a held B steps up a level
            self.inputs.b_hold = (self.view in ("cats", "carousel")
                                  and self._nav_mode() == 2)
            # Screen off = deep idle: long ticks, and a button press sets
            # the wake event so poll() returns INSTANTLY — no latency, and
            # 8x fewer wakeups than the old 0.6s polling
            if getattr(self, "_pending", None):
                # events caught by a slide's mid-frame poll: handle them
                # NOW, before polling — a mash chain gets zero added
                # latency and chronological order is preserved
                events, self._pending = self._pending, []
            else:
                events = self.inputs.poll(
                    TICK_S if self.display.on else 5.0)
            if events:
                woke = not self.display.on
                self.last_input = time.monotonic()
                _paths.touch_activity()  # vibb-idle: hands-on counts
                if woke:
                    self.display.set_backlight(True)
                    self.last_system = 0.0   # refetch battery/system now
                    self.last_status = 0.0
                    self._poll_wake.set()    # un-park the poller (dark = 5s)
                    self.dirty = True  # the waking press is swallowed
                    self._wake_press(events)
                else:
                    for ev in events:
                        self.handle(ev)
            if self.display.on:
                # P1: the background poller owns the HTTP; the loop only
                # reconciles view state from the cached /status here, so a
                # slow daemon can never stall a repaint. (No HTTP while dark:
                # the poller itself parks on self.display.on.)
                self._reconcile_view()
                # Browsing went idle while something plays: snap back to
                # now-playing. Only from the browse views — settings/BT
                # flows have their own long waits (scan, pair) and must
                # not be yanked away from.
                if (self.view in ("home", "entries", "episodes",
                                  "carousel", "cats")
                        and self.status.get("playing")
                        and time.monotonic() - self.last_input
                        > NOW_RETURN_S):
                    self.push("now")
            if self.view == "link" and self._link_open_s() >= LINK_AWAKE_S:
                # long enough for any scan — stop displaying the secret
                self._link_since = 0.0
                self.back()
                self.dirty = True
            if self.display.on and self.screen_should_sleep():
                self.display.set_backlight(False)
                # Release the browse latch: user_touched suppresses home's
                # auto-refresh + auto-snap-to-now while someone is actively
                # navigating — but it was never reset, so after the FIRST
                # press ever the home view stayed frozen for the whole
                # session (QA A6). Screen sleep = the session is over; the
                # next wake starts fresh.
                self.user_touched = False
                if PNG_PATH:  # dev: make the blanking visible
                    self.display.show(Image.new("RGB", (W, H), (0, 0, 0)))
            elif self.display.on and (self.dirty
                                      or time.monotonic() < self.catch_up_until
                                      or (self.marquee_active
                                          and time.monotonic()
                                          - self.last_render >= MARQUEE_STEP_S)
                                      or (self.last_render < self.volume_flash
                                          and time.monotonic()
                                          - self.last_render >= 0.5)
                                      or (self.last_render < self.output_flash
                                          and time.monotonic()
                                          - self.last_render >= 0.5)):
                # Repaints are change-driven (_set marks dirty): while
                # playing the 1s status poll moves the progress bar, while
                # paused NOTHING repaints — a full PIL compose + 115KB SPI
                # push per identical frame was measurable CPU on the Zero.
                # Time-based exceptions: the volume overlay (paint until one
                # frame lands after it expired) and a sliding long label in
                # the menus (marquee_active, ~3 fps while selected).
                self.dirty = False
                self.last_render = time.monotonic()
                self.render()


# --- the boot mark ------------------------------------------------------------
# The vibb logo, drawn from the artwork FILE (art/vibb-mark.svg) rather
# than from numbers copied out of it. The rings are not circles: their
# radius wanders about 4% around the turn — hand-shaped, and drawing
# them as ellipses flattened exactly the character the mark has. So the
# four ring outlines are read as the polylines they are, and the SVG
# stays the source: re-draw the logo, replace the file, done.
#
# Only the little that this one file uses is parsed — M/L polylines, the
# group transform, the core circle, the wordmark. Anything unexpected
# falls back to plain circles, because a splash must never break a boot.
def _art_dir():
    """Where the artwork lives — beside this file when run from the repo,
    under share/ once install.sh has copied ui.py into /usr/local/bin."""
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.join(here, "art"), "/usr/local/share/vibb/art"):
        if os.path.isdir(d):
            return d
    return os.path.join(here, "art")


MARK_SVG = os.path.join(_art_dir(), "vibb-logo-pust-240-natt.svg")
MARK_S = 1.9                       # the artwork's own group transform
MARK_CX, MARK_CY = 120.0, 96.0
MARK_FALLBACK_R = (25.45, 19.58, 14.39, 9.48)   # if the file is unreadable
MARK_CORE_R = 4.3
MARK_STROKE = 2.6                  # scales with the group, as in SVG
MARK_WORD_X, MARK_WORD_Y = 120.0, 196.0     # centred, on its baseline
MARK_WORD_SIZE = 52
MARK_WORD_TRACK = -1.56            # letter-spacing from the artwork
MARK_RING = (240, 168, 132)        # #f0a884
MARK_CORE_RGB = (251, 228, 220)    # #fbe4dc
MARK_WORD = (246, 231, 224)        # #f6e7e0
BREATHE_S = 4.0                    # one full in-and-out
SPLASH_FPS = float(os.environ.get("VIBB_SPLASH_FPS", "12"))
_SS = 3                            # supersample: PIL strokes are aliased
_RINGS = []                        # [[(x, y), ...], ...] in artwork units


def mark_rings():
    """The ring outlines from the artwork, biggest first. Parsed once."""
    if _RINGS:
        return _RINGS
    try:
        svg = open(MARK_SVG, encoding="utf-8").read()
        for dattr in re.findall(r'<path[^>]*\sd="([^"]+)"', svg):
            pts = [(float(x), float(y)) for x, y in
                   re.findall(r'[ML](-?[\d.]+)\s+(-?[\d.]+)', dattr)]
            if len(pts) > 8:       # a ring, not some stray decoration
                _RINGS.append(pts)
    except (OSError, ValueError) as e:
        log(f"boot mark: {e.__class__.__name__} reading the artwork — "
            f"drawing plain rings")
    if not _RINGS:
        _RINGS.extend([[(r * math.cos(a * math.pi / 90),
                         r * math.sin(a * math.pi / 90)) for a in range(180)]
                       for r in MARK_FALLBACK_R])
    _RINGS.sort(key=lambda p: -max(abs(x) for x, _ in p))
    return _RINGS


def _ease(u):
    """cubic-bezier(.42,0,.58,1) closely enough for a 10% breathe."""
    return u * u * (3.0 - 2.0 * u)


def _breathe(t, period=BREATHE_S, lo=0.94, hi=1.04, phase=0.0):
    """Value at time t on a symmetric eased in-out loop."""
    u = ((t + phase) % period) / period
    u = _ease(u * 2.0) if u < 0.5 else _ease((1.0 - u) * 2.0)
    return lo + (hi - lo) * u


def _blend(fg, bg, a):
    """Opacity against a KNOWN solid background — exact, and far cheaper
    than an RGBA composite for four strokes a frame."""
    return tuple(int(round(b + (f - b) * a)) for f, b in zip(fg, bg))


def _mark_font():
    """The wordmark's own typeface. Nunito ships as a VARIABLE font, so
    the Black weight has to be selected — and if this FreeType cannot
    (built without variable support), the default instance is
    ExtraLight, which is the opposite of the mark. So a failure there
    falls through to DejaVu Bold rather than quietly drawing a hairline
    wordmark."""
    nunito = os.path.join(_art_dir(), "Nunito.ttf")
    if os.path.exists(nunito):
        try:
            f = ImageFont.truetype(nunito, MARK_WORD_SIZE)
            f.set_variation_by_name("Black")
            return f
        except Exception as e:
            log(f"boot mark: Nunito Black unavailable ({e.__class__.__name__})"
                f" — using the system face")
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, MARK_WORD_SIZE)
    try:
        return ImageFont.load_default(MARK_WORD_SIZE)
    except TypeError:
        return ImageFont.load_default()


def _tracked_text(d, xy, text, font, fill, track):
    """Centred text with letter-spacing — PIL has no tracking, and the
    wordmark's is negative, so the glyphs go one at a time."""
    widths = [d.textlength(c, font=font) for c in text]
    total = sum(widths) + track * (len(text) - 1)
    x = xy[0] - total / 2.0
    for c, w in zip(text, widths):
        d.text((x, xy[1]), c, font=font, fill=fill, anchor="ls")
        x += w + track


def splash_frame(t, word_font=None):
    """One frame of the boot mark at time t seconds."""
    img = Image.new("RGB", (W * _SS, H * _SS), BG)
    d = ImageDraw.Draw(img)
    cx, cy = MARK_CX * _SS, MARK_CY * _SS
    rings = mark_rings()
    hw = MARK_STROKE / 2.0
    for i, pts in enumerate(rings):
        # the artwork staggers the rings 0.28s apart, so the breath
        # travels outward instead of pulsing as one blob
        phase = -0.28 * (len(rings) - i)
        k = MARK_S * _breathe(t, phase=phase) * _SS
        a = _breathe(t, lo=0.55, hi=1.0, phase=phase)
        # a BAND, not a stroked polyline: PIL puts a joint at every one
        # of the 160 vertices and they show as nicks all the way round.
        # The rings nest, so filling outer-then-inner in order lets the
        # next ring paint over the hole this one punches.
        mean_r = sum(math.hypot(x, y) for x, y in pts) / len(pts)
        for scale, colour in ((1.0 + hw / mean_r, _blend(MARK_RING, BG, a)),
                              (1.0 - hw / mean_r, BG)):
            d.polygon([(cx + x * k * scale, cy + y * k * scale)
                       for x, y in pts], fill=colour)
    r = MARK_CORE_R * MARK_S * _breathe(t, lo=0.9, hi=1.08) * _SS
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=MARK_CORE_RGB)
    img = img.resize((W, H), Image.Resampling.LANCZOS)
    _tracked_text(ImageDraw.Draw(img), (MARK_WORD_X, MARK_WORD_Y), "vibb",
                  word_font or _mark_font(), MARK_WORD, MARK_WORD_TRACK)
    return img


def _boot_splash(display):
    """Light the panel the instant the display is up — BEFORE the slower
    input (lgpio) init — so the screen shows life early instead of
    staying blank through the rest of startup.

    The mark BREATHES, on its own thread, filling dead time that already
    exists rather than adding any: everything after this call (lgpio,
    the library fetch, the first /status) runs while it animates.

    Returns a stop() that the caller MUST call before painting the real
    UI — it joins the thread, so the two can never fight over the panel.
    A splash must never block, or break, the real UI coming up."""
    stop = threading.Event()

    def loop():
        t0 = time.monotonic()
        interval = 1.0 / max(1.0, SPLASH_FPS)
        word = _mark_font()
        try:
            while not stop.is_set():
                frame = time.monotonic()
                display.show(splash_frame(frame - t0, word))
                stop.wait(max(0.0, interval - (time.monotonic() - frame)))
        except Exception as e:
            log(f"boot splash stopped: {e!r}")

    try:
        display.show(splash_frame(0.0))   # something lit before the thread
        th = threading.Thread(target=loop, daemon=True)
        th.start()
    except Exception as e:
        log(f"boot splash skipped: {e!r}")
        return lambda: None

    def done():
        stop.set()
        th.join(timeout=2.0)
    return done


def blank_screen(display):
    """Leave the panel DARK. The ST7789 holds its last frame forever and
    the backlight is ours to drive, so a plain exit left a frozen Vibb
    picture lit for the whole handoff — field 2026-08-04: the screen sat
    on the last menu through an entire RetroPie session. Backlight off
    FIRST (instant), then black pixels so nothing stale can flash if
    something lights the panel again."""
    try:
        display.set_backlight(False)
    except Exception:
        pass
    try:
        display.show(Image.new("RGB", (W, H), (0, 0, 0)))
    except Exception:
        pass


def main():
    # Boot timing, logged because it cannot be derived: the box's clock
    # JUMPS mid-boot when the PiSugar RTC lands, so journal timestamps
    # that straddle it lie about durations by ~20s. These deltas are
    # measured on the monotonic clock and are immune to that.
    t0 = _T_START
    log(f"imports took {time.monotonic() - t0:.1f}s "
        f"(we are {_uptime():.1f}s into this boot)")
    display = make_display()
    log(f"display up after {time.monotonic() - t0:.1f}s")
    splash_done = _boot_splash(display)   # lights up now, and BREATHES
    #                                       through the slow init below
    splash_at = time.monotonic() - t0
    log(f"splash lit at boot+{_uptime():.1f}s")

    def _term(*_a):
        # systemd stops us for a handoff (extras), a service restart or
        # shutdown — every one of those should leave a dark screen.
        blank_screen(display)
        sys.stderr.flush()
        os._exit(0)
    signal.signal(signal.SIGTERM, _term)

    app = App(display, make_input())
    log(f"inputs ready at boot+{_uptime():.1f}s")
    app.boot_t0 = t0
    app.splash_at = splash_at
    app.splash_done = splash_done   # run() stops the breathing mark the
    #                                 moment it is ready to paint for real
    try:
        app.run()
    finally:
        blank_screen(display)


if __name__ == "__main__":
    main()
