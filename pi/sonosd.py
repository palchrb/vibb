#!/usr/bin/env python3
"""vibb-sonos — the Sonos sidecar (UPnP via SoCo, venv python).

vibbd is stdlib-only on system python; SoCo lives in /opt/vibb/venv.
This process bridges the two: a small JSON API on 127.0.0.1 that vibbd
drives, same shape as the vibbd <-> go-librespot split.

Design (three-agent review 2026-08-08/09):
- THE POLLER LIVES HERE. /state is a memory read of the last snapshot —
  never a live SOAP call. Stall/takeover detection needs a SEQUENCE of
  samples (a cached sample re-served must be tellable from a fresh one),
  hence the monotonic `seq`. vibbd's /status must never wait on a
  sleeping speaker over PS-throttled wifi.
- ONE session, not per-uid: the box is a single sequencer and renderer.
  /play means "become the session"; other verbs take an optional if_uid
  and 409 on mismatch, closing the stale-command race.
- /adopt re-attaches to a session already playing (daemon or sidecar
  restarted) WITHOUT issuing transport commands — /play would restart
  the episode over music that never stopped.
- Success/error shapes are a closed set (tests/sonos_contract.py). An
  unknown condition must land in the conservative branch on the vibbd
  side, so unknown things are never invented here.
- uid -> ip persists in STATE_DIR/sonos.json: SSDP costs seconds and
  multicast-over-wifi drops; direct SoCo(ip) is the owner's own proven
  path (sonos-remotes). Discovery runs on cache miss, explicit rescan,
  or a failed play — never on a timer.
- soco imports LAZILY on first use: always-on process, ~20 MB deferred
  until Sonos is actually used (architect lifecycle review).

Content kinds (v1):
  url                plain http(s) audio the speaker fetches from origin
  nrk_program        NRK series via the NRK Radio Sonos service
                     (x-sonos-http, sid=277) — the owner's sonos-remotes
                     recipe, plus the <desc> element his version lacked
  spotify_sharelink  SoCo ShareLink; SONOS OWNS THE QUEUE for spotify
                     (owner decision 2026-08-09) — next/prev go to the
                     speaker's queue, not our sequencer

Never carried over from sonos-remotes: 0.0.0.0 binds, default secrets,
CIDR auth bypasses. This binds 127.0.0.1 only.
"""

import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

_here = os.path.dirname(os.path.abspath(__file__))
for _p in (_here, "/usr/local/lib/vibb-py"):
    if os.path.isdir(os.path.join(_p, "vibb")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
        break

from vibb.paths import STATE_DIR  # noqa: E402

PORT = int(os.environ.get("VIBB_SONOS_PORT", "3681"))
CACHE_FILE = os.path.join(STATE_DIR, "sonos.json")
POLL_S = float(os.environ.get("VIBB_SONOS_POLL", "5"))
POLL_PAUSED_S = float(os.environ.get("VIBB_SONOS_POLL_PAUSED", "15"))
POLL_STOPPED_S = float(os.environ.get("VIBB_SONOS_POLL_STOPPED", "60"))
# Mid-track PLAYING cadence (RF audit 2026-08-10 #3): the daemon
# extrapolates the bar and compensates measurement age, bookmarks are
# throttled to 25s, and every verb wakes the loop — nothing consumes 5s
# resolution mid-track. 5s survives only near the track end (boundary
# detection) and for a settle window after verbs.
POLL_CRUISE_S = float(os.environ.get("VIBB_SONOS_POLL_CRUISE", "15"))
TAIL_FAST_S = 45   # within this many s of track end -> fast cadence
VERB_FAST_S = 30   # after a verb/arm -> fast cadence (settle machinery)
AUX_EVERY = 12     # polls between GetVolume/GetZoneGroupState refreshes
# STOPPED/UNREACHABLE this long -> the session is OVER: disarm and let
# the radio doze. Only /play (or /adopt) arms again. Without this, a
# bedtime story that ended at 21:00 kept the full cadence all night
# (RF power audit 2026-08-10 #1: ~23k SOAP calls before morning).
DISARM_AFTER_S = float(os.environ.get("VIBB_SONOS_DISARM", "600"))
# Migration-follow (stage B2): when OUR coordinator is removed from its
# group, Sonos promotes another member and hands the stream over — our
# uid goes standalone STOPPED with an EMPTY TrackURI while the audio
# continues elsewhere. A short probe window separates that from a truly
# stopped session; it sits far inside DISARM_AFTER_S by design.
MIGRATE_TRIES = int(os.environ.get("VIBB_SONOS_MIGRATE_TRIES", "3"))
MIGRATE_WINDOW_S = float(os.environ.get("VIBB_SONOS_MIGRATE_WINDOW", "90"))
# NRK Radio's Sonos service id; its service type = sid*256 + 7
NRK_SID = 277
NRK_SVCTYPE = NRK_SID * 256 + 7
STALL_POLLS = 3   # PLAYING + frozen RelTime this many polls -> one retry


def log(msg):
    print(f"sonosd: {msg}", flush=True)


_HTTP = None


def _soco():
    """Lazy import — pay the ~20 MB only when Sonos is actually used."""
    global _HTTP
    import soco  # noqa: F401  (venv-only dependency)
    if _HTTP is None:
        # RF audit 2026-08-10 #4: SoCo fires a bare requests.post per
        # SOAP call — a fresh TCP handshake every time, ~5-6 RTTs where
        # 2-3 do, and under PS-on wifi each RTT can cost a beacon
        # interval. One keep-alive Session (a drop-in for the module:
        # same .post/.get surface) rides every SOAP call, topology
        # fetch and music-service lookup on pooled connections.
        import requests
        from soco import core as _c, services as _sv, soap as _sp
        _HTTP = requests.Session()
        for _m in (_sv, _c, _sp):
            _m.requests = _HTTP
    return soco


# --- speaker cache ---------------------------------------------------------

_cache_lock = threading.Lock()


def _load_cache():
    try:
        with open(CACHE_FILE) as f:
            d = json.load(f)
            return d if isinstance(d.get("players"), dict) else {"players": {}}
    except (OSError, ValueError):
        return {"players": {}}


def _save_cache(cache):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, CACHE_FILE)


def rescan():
    """SSDP + zone topology via SoCo. Merges into the cache — a scan that
    misses a speaker (wifi multicast drops) must never delete the row the
    kid was aiming at; a play to a dead one fails cleanly instead."""
    soco = _soco()
    found = soco.discover(timeout=3) or set()
    with _cache_lock:
        cache = _load_cache()
        for z in found:
            try:
                if not z.is_visible:
                    continue  # bonded surrounds/subs — not pickable rooms
                cache["players"][z.uid] = {
                    "ip": z.ip_address, "name": z.player_name,
                    "seen_at": time.time()}
            except Exception as e:
                log(f"scan: skipping a zone ({e.__class__.__name__})")
        cache["fetched_at"] = time.time()
        _save_cache(cache)
        return cache


def refresh_topology():
    """The whole household in ONE call: GetZoneGroupState against any
    cached speaker returns every zone (uid, name, ip) and every group
    with its coordinator — the cheap primitive the RF audit already
    identified (it is ~90% of a poll's bytes, which is why the POLLER
    rides it on a slow sub-cadence; here it runs only when the picker
    opens, where 100-200ms against a LAN IP is instant).

    Contrast rescan(): SSDP multicast costs 3s+ and learns nothing
    about groups. This self-heals DHCP moves and renames for free (the
    topology carries fresh ips/names), so SSDP degrades to the cold-
    start fallback: empty cache, or nobody answering.

    Merges zones like rescan() does (never delete the row the kid was
    aiming at); GROUPS replace wholesale — a group list is only
    meaningful as a snapshot of one instant. Raises when no cached
    speaker answers; the caller serves the cache and says so."""
    soco = _soco()
    with _cache_lock:
        recs = sorted(_load_cache()["players"].items())
    last_err = None
    groups = None
    for _uid, rec in recs:
        try:
            groups = soco.SoCo(rec["ip"]).all_groups
            break
        except Exception as e:
            last_err = e
    if groups is None:
        raise last_err or RuntimeError("no cached speaker to ask")
    with _cache_lock:
        cache = _load_cache()
        gout = []
        for g in groups:
            members = []
            try:
                coord = g.coordinator.uid
            except Exception:
                continue  # a group we cannot address is not offerable
            for z in g.members:
                try:
                    if not z.is_visible:
                        continue  # bonded surrounds/subs — not rooms
                    cache["players"][z.uid] = {
                        "ip": z.ip_address, "name": z.player_name,
                        "seen_at": time.time()}
                    members.append(z.uid)
                except Exception as e:
                    log(f"topology: skipping a zone "
                        f"({e.__class__.__name__})")
            if members:
                # coordinator first: the picker labels the row by it,
                # and selecting the row selects it
                members.sort(key=lambda u: u != coord)
                gout.append({"coordinator": coord, "members": members})
        cache["groups"] = gout
        cache["topology_at"] = time.time()
        _save_cache(cache)
        return cache


def players():
    with _cache_lock:
        return _load_cache()


def players_payload(cache, stale=False):
    """The GET /players wire shape. uid + name only — speaker IPs are
    LAN topology and every GET is token-free by the box's SAFE rule.
    groups ride along so the picker can offer 'Stua + Kjøkken' as ONE
    row; only multi-member groups are worth the wire."""
    out = {"players": [
        {"uid": uid, "name": rec.get("name")}
        for uid, rec in sorted(cache["players"].items(),
                               key=lambda kv: kv[1].get("name") or "")
    ], "groups": [g for g in cache.get("groups") or []
                  if len(g.get("members") or []) > 1]}
    if stale:
        out["stale"] = True  # nobody answered — this is the old cache
    return out


def _speaker(uid):
    """SoCo instance for a cached uid — direct IP, no discovery. Verifies
    the uid still matches (DHCP moves IPs); one mismatch triggers rescan."""
    soco = _soco()
    with _cache_lock:
        rec = _load_cache()["players"].get(uid)
    if rec is None:
        raise KeyError(uid)
    s = soco.SoCo(rec["ip"])
    try:
        if s.uid != uid:
            raise KeyError(uid)  # IP moved under the cache
    except KeyError:
        raise
    except Exception:
        # unreachable — let the caller classify; do not rescan on the
        # poll path (that is the play path's job)
        pass
    return s


# --- DIDL ------------------------------------------------------------------

DIDL_NS = ('xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
           'xmlns:dc="http://purl.org/dc/elements/1.1/" '
           'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
           'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/"')


def _hms(sec):
    sec = max(0, int(sec or 0))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _hms_to_s(s):
    try:
        h, m, sec = (s or "0:00:00").split(":")
        return int(h) * 3600 + int(m) * 60 + float(sec)
    except (ValueError, AttributeError):
        return None


MIME = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".m4b": "audio/mp4",
        ".aac": "audio/aac", ".flac": "audio/flac", ".wav": "audio/wav",
        ".ogg": "audio/ogg", ".opus": "audio/ogg"}


def _norm_uri(u):
    """Speaker-vs-ours uri equality, tolerant of the speaker's
    re-encoding: percent-escapes, and some firmwares swap the scheme
    prefix. An exact match called OUR OWN nrk episode foreign, which
    killed playing/progress for every url-kind card (field 2026-08-09).
    Module-level (stage B2) because the migration probe needs the same
    notion of "this is our stream" against OTHER speakers."""
    u = urllib.parse.unquote(u or "")
    for p in ("x-rincon-mp3radio://", "aac://", "https://",
              "http://"):
        if u.startswith(p):
            return u[len(p):]
    return u


def _mime_for(uri):
    path = urllib.parse.urlparse(uri).path.lower()
    for ext, mime in MIME.items():
        if path.endswith(ext):
            return mime
    return "audio/mpeg"


def didl(uri, title, artist=None, album=None, art=None, duration_s=None,
         upnp_class="object.item.audioItem.musicTrack",
         protocol=None, desc=None):
    """One DIDL-Lite item. escape() is applied HERE to each value exactly
    once; SoCo escapes the whole string again when it inlines it as the
    SOAP argument. Two levels, once each — dropping either is the classic
    'audio plays, metadata blank' (implementer review 2026-08-08; the
    &amp;amp; you will see on the wire is CORRECT)."""
    proto = protocol or f"http-get:*:{_mime_for(uri)}:*"
    dur = f' duration="{_hms(duration_s)}"' if duration_s else ""
    tags = [f"<dc:title>{escape(title or 'Vibb')}</dc:title>"]
    if artist:
        # both forms: different Sonos controller versions read different
        # ones — emitting both ends the guessing
        tags.append(f"<dc:creator>{escape(artist)}</dc:creator>")
        tags.append(f"<upnp:artist>{escape(artist)}</upnp:artist>")
    if album:
        tags.append(f"<upnp:album>{escape(album)}</upnp:album>")
    if art:
        # must be a LAN IP url — Sonos does not resolve mDNS (.local)
        tags.append(f"<upnp:albumArtURI>{escape(art)}</upnp:albumArtURI>")
    tags.append(f"<upnp:class>{escape(upnp_class)}</upnp:class>")
    if desc:
        # service items (x-sonos-http) need the cdudn descriptor for the
        # controller app to resolve service metadata — the element the
        # owner's sonos-remotes lacked, and the prime suspect for its
        # historical partial-metadata sore point
        tags.append('<desc id="cdudn" nameSpace='
                    '"urn:schemas-rinconnetworks-com:metadata-1-0/">'
                    f"{escape(desc)}</desc>")
    tags.append(f'<res protocolInfo="{proto}"{dur}>{escape(uri)}</res>')
    return (f"<DIDL-Lite {DIDL_NS}>"
            '<item id="vibb-1" parentID="-1" restricted="true">'
            + "".join(tags) + "</item></DIDL-Lite>")


# --- NRK Radio service recipe (ported from palchrb/sonos-remotes) ----------

_sn_cache = {}  # speaker ip -> account serial for sid=277


def _nrk_serial(ip):
    """The household's account serial for the NRK Radio service. The
    owner's original hardcoded sn=14 breaks silently if the service is
    ever re-linked, so it is looked up from the speaker instead."""
    if ip in _sn_cache:
        return _sn_cache[ip]
    import xml.etree.ElementTree as ET
    try:
        with urllib.request.urlopen(
                f"http://{ip}:1400/status/accounts", timeout=5) as r:
            root = ET.fromstring(r.read())
        for acct in root.iter("Account"):
            if acct.get("Type") == str(NRK_SVCTYPE):
                sn = acct.get("SerialNum")
                if sn is not None:
                    _sn_cache[ip] = sn
                    return sn
    except Exception as e:
        log(f"nrk serial lookup failed ({e.__class__.__name__}) — "
            "falling back to sn=14")
    return "14"  # the owner's known-working household value


def nrk_program_uri(ip, series, program_id):
    sn = _nrk_serial(ip)
    return (f"x-sonos-http:series%3a{urllib.parse.quote(series)}"
            f"%3a1%3a{program_id}.unknown?sid={NRK_SID}&flags=0&sn={sn}")


def nrk_desc():
    return f"SA_RINCON{NRK_SVCTYPE}_X_#Svc{NRK_SVCTYPE}-0-Token"


# --- the single session ----------------------------------------------------

class Session:
    """Exactly one; guarded by its own lock so the stall retry's four-call
    sequence is atomic against a concurrent /play."""

    def __init__(self):
        self.lock = threading.Lock()
        self.armed = False
        self.uid = None
        self.kind = None
        self.uri = None          # what WE set (ours-detection)
        self.snapshot = {"armed": False, "seq": 0, "stale_s": None}
        self._seq = 0
        self._frozen = 0         # consecutive PLAYING polls with same pos
        self._last_pos = None
        self._retried_at = None
        self._last_ok = None     # monotonic of last successful poll
        self._wake = threading.Event()
        # display-only fields on the slow sub-cadence (RF audit #2)
        self._aux = {"volume": None, "grouped_away": False,
                     "coordinator": None}
        self._aux_n = 0          # <=0 -> refresh aux on the next poll
        self._fast_at = 0.0      # last verb/arm -> fast-cadence window
        # migration-follow bookkeeping (stage B2)
        self._ours_at = None       # monotonic of last ours+live classify
        self._last_ours_track = None  # sharelink: last decoded track uri
        self._last_ours_rel = None    # sharelink: last rel_s while ours
        self._migrate_tries = 0    # probe attempts left this transition
        self._moved = None         # pending {"uid","name","uri"} hint

    # -- snapshot plumbing --

    def publish(self, **fields):
        self._seq += 1
        snap = {"armed": self.armed, "uid": self.uid, "kind": self.kind,
                "seq": self._seq, "retried_at": self._retried_at}
        snap.update(fields)
        if self._moved:
            # additive hint: the stream moved to another coordinator —
            # the DAEMON owns identity and acts on it (adopt); published
            # on every snapshot until cleared so no tick can drop it
            snap["stream_moved"] = self._moved
        self.snapshot = snap

    def state(self):
        snap = dict(self.snapshot)
        # stale_s computed HERE from monotonic — an age, never a wall
        # timestamp (the box's RTC jumps at boot; ages cannot)
        snap["stale_s"] = (None if self._last_ok is None
                           else round(time.monotonic() - self._last_ok, 1))
        return snap

    # -- transport verbs (called with self.lock held) --

    def _spk(self):
        return _speaker(self.uid)

    def play(self, body):
        soco = _soco()
        uid = body["uid"]
        kind = body.get("kind", "url")
        self.uid, self.kind = uid, kind
        # a new session voids any pending migration hint/window
        self._moved, self._migrate_tries, self._ours_at = None, 0, None
        spk = self._spk()
        start = float(body.get("start_s") or 0)
        unhush = self._hush(spk, start)
        built = None
        try:
            if kind == "url":
                uri = body["uri"]
                try:
                    # the transport plays this uri DIRECTLY — the queue is
                    # not involved. Clear it anyway: a leftover spotify queue
                    # in the Sonos app next to a playing podcast read as "the
                    # queue is wrong" (field 2026-08-09, cosmetic)
                    spk.clear_queue()
                except Exception:
                    pass
                built = didl(uri, body.get("title"), body.get("artist"),
                             body.get("album"), body.get("art"),
                             body.get("duration_s"))
                spk.avTransport.SetAVTransportURI([
                    ("InstanceID", 0), ("CurrentURI", uri),
                    ("CurrentURIMetaData", built)])
                spk.avTransport.Play([("InstanceID", 0), ("Speed", "1")])
            elif kind == "nrk_program":
                uri = nrk_program_uri(spk.ip_address, body["series"],
                                      body["program_id"])
                built = didl(uri, body.get("title"), body.get("artist"),
                             body.get("album"), body.get("art"),
                             body.get("duration_s"),
                             upnp_class="object.item.audioItem.show",
                             protocol="sonos.com-http:*:audio/mpeg:*",
                             desc=nrk_desc())
                spk.avTransport.SetAVTransportURI([
                    ("InstanceID", 0), ("CurrentURI", uri),
                    ("CurrentURIMetaData", built)])
                spk.avTransport.Play([("InstanceID", 0), ("Speed", "1")])
            elif kind == "spotify_sharelink":
                from soco.plugins.sharelink import ShareLinkPlugin
                # NORMAL kills family shuffle/repeat leftovers AND makes the
                # queue's play order equal its index order — the legality of
                # every positional jump rests on this (architect Q2).
                try:
                    spk.play_mode = "NORMAL"
                except Exception:
                    pass
                spk.clear_queue()
                r = ShareLinkPlugin(spk).add_share_link_to_queue(body["uri"])
                # FirstTrackNumberEnqueued: never assume the queue starts at
                # 1, even after clear_queue (architect Q1 outbound)
                try:
                    self.q_base = int(r) if r else 1
                except (TypeError, ValueError):
                    self.q_base = 1
                try:
                    self.q_len = int(spk.queue_size)
                except Exception:
                    self.q_len = None
                idx = int(body.get("track_index") or 0)
                spk.play_from_queue(self.q_base - 1 + idx)
                uri = body["uri"]
            else:
                raise ValueError(f"unknown kind: {kind}")
            self.uri = uri
            self.armed = True
            self._didl_checked = False
            self._frozen, self._last_pos, self._retried_at = 0, None, None
            # new session: forget the old speaker's volume/topology and
            # refresh on the first poll; poll fast while it settles
            self._aux = {"volume": None, "grouped_away": False,
                         "coordinator": None}
            self._aux_n = 0
            self._fast_at = time.monotonic()
            sought = self._seek_settled(spk, start) \
                if start >= 5 else True
        finally:
            unhush()
        self._wake.set()
        return {"ok": True, "uid": uid, "uri": uri,
                "sought": bool(sought), "didl": built,
                "base": getattr(self, "q_base", None),
                "queue_len": getattr(self, "q_len", None),
                "play_mode": "NORMAL" if kind == "spotify_sharelink"
                else None}

    def _hush(self, spk, start_s):
        """A resume cannot seek until the transport is PLAYING (701,
        see _seek_settled), so ~1s always plays from 0:00 first — and
        the field hears that second before the jump (2026-08-12). Mute
        the speaker across the Play->Seek window instead: same blip,
        silent. Returns a restore() that puts the OLD mute state back
        (a deliberately muted speaker stays muted); both directions are
        best-effort and restore never raises."""
        if start_s < 5:
            return lambda: None
        try:
            was = bool(spk.mute)
            spk.mute = True
        except Exception:
            return lambda: None

        def restore():
            try:
                spk.mute = was
            except Exception:
                pass
        return restore

    def _seek_settled(self, spk, start_s, timeout=8):
        """SetURI -> Play -> wait PLAYING -> Seek. Against a STOPPED
        transport Seek is UPnP 701 (nothing to seek in yet); costs ~1s
        of audio from 0:00. sought=false is a DEGRADE, not an error."""
        soco = _soco()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                info = spk.avTransport.GetTransportInfo([("InstanceID", 0)])
                if info.get("CurrentTransportState") == "PLAYING":
                    break
            except Exception:
                pass
            time.sleep(0.3)
        for _ in range(3):
            try:
                spk.avTransport.Seek([("InstanceID", 0),
                                      ("Unit", "REL_TIME"),
                                      ("Target", _hms(start_s))])
                return True
            except soco.exceptions.SoCoUPnPException as e:
                if str(getattr(e, "error_code", "")) not in ("701", "710",
                                                             "711"):
                    return False
                time.sleep(0.4)
            except Exception:
                return False
        log(f"seek to {_hms(start_s)} refused — playing from the top")
        return False

    def adopt(self, body):
        """Re-attach after a restart on either side: session bookkeeping
        only, ZERO transport commands — /play here would restart the
        episode over music that never stopped."""
        self.uid = body["uid"]
        self.kind = body.get("kind")
        self.uri = body.get("uri")
        self.armed = True
        self._frozen, self._last_pos, self._retried_at = 0, None, None
        self._moved, self._migrate_tries, self._ours_at = None, 0, None
        self._aux_n = 0                    # fresh volume/topology now
        self._fast_at = time.monotonic()
        self._wake.set()
        return {"ok": True, "uid": self.uid}

    def verb(self, name, body):
        want = body.get("if_uid")
        if want and want != self.uid:
            return None  # 409 at the HTTP layer
        self._fast_at = time.monotonic()  # settle window: poll fast
        spk = self._spk()
        if name == "pause":
            spk.avTransport.Pause([("InstanceID", 0)])
            self._wake.set()  # re-poll NOW: the cached snapshot still
            # says PLAYING, and the paused cadence is 15s (QA 2026-08-09)
        elif name == "resume":
            spk.avTransport.Play([("InstanceID", 0), ("Speed", "1")])
            self._wake.set()
        elif name == "stop":
            spk.avTransport.Stop([("InstanceID", 0)])
            self.armed = False
            self._wake.set()
        elif name == "seek":
            spk.avTransport.Seek([("InstanceID", 0), ("Unit", "REL_TIME"),
                                  ("Target", _hms(float(body["s"])))])
            self._wake.set()  # like pause/resume above: the cached rel_s
            # still says the OLD position, and the cruise cadence is 15s
        elif name == "volume":
            v = max(0, min(100, int(body["v"])))
            spk.volume = v
            self._aux["volume"] = v  # the snapshot must not lag OUR set
            #                          now that GetVolume rides the slow
            #                          sub-cadence
        elif name == "queue_play":
            # vibb owns the logic for EVERY kind now (v2) — this jumps
            # the speaker's queue to an absolute 0-based position and
            # optionally seeks. The old delegated next/prev died with v1.
            pos = int(body["index"])
            base = getattr(self, "q_base", 1) or 1
            qlen = getattr(self, "q_len", None)
            absidx = base - 1 + pos
            if qlen is not None:
                absidx = max(base - 1, min(absidx, base - 2 + qlen))
            start = float(body.get("start_s") or 0)
            unhush = self._hush(spk, start)
            try:
                spk.play_from_queue(absidx)
                sought = self._seek_settled(spk, start) \
                    if start >= 5 else True
            finally:
                unhush()
            self._wake.set()
            return {"ok": True, "sought": bool(sought)}
        return {"ok": True}

    # -- the poller --

    def _classify(self, spk):
        """One poll -> the fields vibbd's policy dispatch needs. One
        GetPositionInfo + one GetTransportInfo; volume and group
        topology ride the AUX_EVERY sub-cadence below."""
        pos = spk.avTransport.GetPositionInfo([("InstanceID", 0)])
        tr = spk.avTransport.GetTransportInfo([("InstanceID", 0)])
        transport = tr.get("CurrentTransportState") or "STOPPED"
        if transport == "TRANSITIONING":
            # buffering/track-change limbo: report the last REAL state —
            # downstream treats TRANSITIONING as not-playing, which
            # painted a pause icon during every fresh start (G1-d)
            transport = getattr(self, "_last_tr", None) or "PLAYING"
        else:
            self._last_tr = transport
        track_uri = pos.get("TrackURI") or ""
        rel = _hms_to_s(pos.get("RelTime"))
        dur = _hms_to_s(pos.get("TrackDuration"))
        ours = bool(self.uri) and (
            _norm_uri(track_uri) == _norm_uri(self.uri)
            or self.kind == "spotify_sharelink" and track_uri.startswith(
                ("x-sonos-spotify:", "x-sonosprog-spotify:")))
        if not ours and self.uri and track_uri                 and self.kind != "spotify_sharelink":
            if getattr(self, "_ours_logged", None) != track_uri:
                self._ours_logged = track_uri
                log(f"not-ours? speaker={track_uri!r} vs set={self.uri!r}")
        # Instant grouped-away detection (stage B1): a MEMBER follows its
        # coordinator and its own AVTransport NAMES it — x-rincon:<uid>.
        # Exact and zero extra SOAP, where the aux fetch below lags by up
        # to AUX_EVERY polls (minutes at cruise). The prefix cannot match
        # x-rincon-mp3radio:// or x-rincon-queue: (char 9 is '-'). An
        # EMPTY uri decides nothing (transitions, just-stopped) — only a
        # real track uri clears; the aux settles the rest on its cadence.
        if track_uri.startswith("x-rincon:"):
            self._aux["grouped_away"] = True
            self._aux["coordinator"] = track_uri.split(":", 1)[1]
        elif track_uri:
            self._aux["grouped_away"] = False
            self._aux["coordinator"] = None
        # RF audit 2026-08-10 #2: group + volume were HALF the SOAP
        # calls and ~90% of the bytes on every poll — GetZoneGroupState
        # returns the whole household's topology XML, and SoCo's 5s
        # cache expired at exactly the old cadence, so every single
        # poll refetched and re-parsed it. Both fields are display-only
        # (renderer_state + the volume card), so they ride a slow
        # sub-cadence: every AUX_EVERY-th poll, at session start, and
        # our own /volume verb writes the value straight into the aux.
        if self._aux_n <= 0:
            self._aux_n = AUX_EVERY
            try:
                g = spk.group
                away = bool(g is not None and g.coordinator is not None
                            and g.coordinator.uid != spk.uid)
                self._aux["grouped_away"] = away
                self._aux["coordinator"] = (g.coordinator.uid if away
                                            else None)
            except Exception:
                pass  # keep the last known topology
            try:
                self._aux["volume"] = spk.volume
            except Exception:
                pass  # keep the last known volume
        self._aux_n -= 1
        lost = (transport == "STOPPED" and not track_uri
                and self.kind != "spotify_sharelink")
        fields = {
            "reachable": True, "transport": transport,
            "rel_s": rel, "dur_s": dur, "uri": track_uri, "ours": ours,
            "foreign_uri": None if ours else (track_uri or None),
            "grouped_away": self._aux["grouped_away"],
            "coordinator": self._aux["coordinator"],
            "lost_session": lost, "volume": self._aux["volume"],
        }
        # track metadata for the sharelink path: the box's screen shows
        # what the SONOS queue is on, since sonos owns that queue
        if self.kind == "spotify_sharelink":
            fields["track_no"] = None
            try:
                fields["track_no"] = int(pos.get("Track"))
            except (TypeError, ValueError):
                pass
            fields["base"] = getattr(self, "q_base", None)
            fields["queue_len"] = getattr(self, "q_len", None)
            # the playing track's OWN uri, percent-decoded — exact under
            # every queue divergence, zero extra SOAP (architect Q1)
            if track_uri.startswith(("x-sonos-spotify:",
                                     "x-sonosprog-spotify:")):
                raw = track_uri.split(":", 1)[1].split("?")[0]
                fields["track_spotify_uri"] = urllib.parse.unquote(raw)
        if ours and self.kind in ("url", "nrk_program") \
                and not getattr(self, "_didl_checked", False):
            self._didl_checked = True
            m = pos.get("TrackMetaData") or ""
            if not m or m == "NOT_IMPLEMENTED":
                log("didl REJECTED by the speaker (no TrackMetaData) — "
                    "metadata will not render in the Sonos app")
            else:
                log(f"didl accepted ({len(m)} bytes echoed)")
        if ours and self.kind == "spotify_sharelink":
            meta = pos.get("TrackMetaData") or ""
            fields["track_title"] = _didl_field(meta, "title")
            fields["track_artist"] = _didl_field(meta, "creator")
            # album art: Sonos hands back a RELATIVE /getaa?... url that
            # the speaker itself serves — absolutize it so the box's
            # screen (and PWA) can fetch it directly
            art = _didl_field(meta, "albumArtURI", ns="upnp")
            if art and art.startswith("/"):
                art = f"http://{spk.ip_address}:1400{art}"
            fields["track_art"] = art
        # migration-follow LIVE bookkeeping (stage B2): every ours+live
        # sighting re-arms the probe machinery and clears a stale hint.
        # The sharelink track/rel pair is what the probe matches EXACTLY
        # later — prefix-matching another coordinator would follow any
        # stranger's Spotify (QA trap #1).
        if ours and transport in ("PLAYING", "PAUSED_PLAYBACK"):
            self._ours_at = time.monotonic()
            self._migrate_tries = MIGRATE_TRIES
            self._moved = None
            if self.kind == "spotify_sharelink":
                self._last_ours_track = (fields.get("track_spotify_uri")
                                         or self._last_ours_track)
                if rel is not None:
                    self._last_ours_rel = rel
        return fields

    def poll_loop(self):
        down_since = None  # start of the current STOPPED/UNREACHABLE streak
        fails = 0          # consecutive failed polls, for the backoff
        while True:
            if not self.armed:
                down_since, fails = None, 0
                self._wake.wait(timeout=30)
                self._wake.clear()
                continue
            with self.lock:
                if not self.armed:
                    continue
                try:
                    fields = self._classify(self._spk())
                    self._last_ok = time.monotonic()
                    fails = 0
                    self._stall_bookkeeping(fields)
                    self._maybe_probe_migration(fields)
                    self.publish(**fields)
                except Exception as e:
                    # speaker unreachable — sidecar-up-speaker-down is its
                    # own shape, distinct from ECONNREFUSED (sidecar down)
                    fails += 1
                    self.publish(reachable=False, transport="UNREACHABLE",
                                 error=e.__class__.__name__)
            snap = self.snapshot
            tr = snap.get("transport")
            # A session that is neither playing nor paused is winding
            # down: STOPPED polls slowly, a dead speaker backs off
            # (each failed SOAP holds the session lock for the full
            # request timeout, so hammering it also blocks verbs), and
            # after DISARM_AFTER_S the session stands down for good.
            if tr in ("PLAYING", "PAUSED_PLAYBACK"):
                down_since = None
            elif down_since is None:
                down_since = time.monotonic()
            if down_since is not None \
                    and time.monotonic() - down_since >= DISARM_AFTER_S:
                log(f"session stood down ({tr} for "
                    f"{int(time.monotonic() - down_since)}s) — "
                    "polling stops until the next play")
                self.armed = False
                # re-publish so /state says armed:false NOW — the old
                # snapshot would keep claiming an armed session
                self.publish(transport=tr,
                             reachable=bool(snap.get("reachable")))
                continue
            self._wake.wait(timeout=self._cadence(tr, snap, fails))
            self._wake.clear()

    def _cadence(self, tr, snap, fails):
        """Seconds until the next poll, from what the last one saw."""
        if tr == "PAUSED_PLAYBACK":
            return POLL_PAUSED_S
        if tr == "UNREACHABLE":
            return min(POLL_S * (2 ** min(fails, 6)), 300)
        if tr == "STOPPED":
            # migration window: attempts should land at ~0/5/10s after
            # the removal, not on the 60s stopped cadence. Only for the
            # migration SIGNATURE (empty uri) — an episode-end STOPPED
            # keeps its cadence.
            if (not snap.get("uri") and self._moved is None
                    and self._migrate_tries > 0
                    and self._ours_at is not None
                    and time.monotonic() - self._ours_at
                    <= MIGRATE_WINDOW_S):
                return POLL_S
            return POLL_STOPPED_S
        # PLAYING: cruise mid-track — fast only near the track end (the
        # ends_near/boundary machinery needs polls inside the last 20s)
        # and for a settle window after any verb (the daemon's pending/
        # optimistic holds expect quick confirmation).
        rel, dur = snap.get("rel_s"), snap.get("dur_s")
        tail = rel is not None and dur and dur - rel <= TAIL_FAST_S
        if tail or time.monotonic() - self._fast_at < VERB_FAST_S:
            return POLL_S
        return POLL_CRUISE_S

    def _stall_bookkeeping(self, fields):
        """PLAYING + our URI + frozen RelTime across STALL_POLLS -> ONE
        retry (Stop/SetURI/Seek/Play equivalent), then hands off. N whole
        polls, not one: a wifi rebuffer false-positives (the same lesson
        as the mpv watchdog's FREEZE_ESCALATE)."""
        if fields["transport"] != "PLAYING" or not fields["ours"] \
                or self.kind == "spotify_sharelink":
            self._frozen, self._last_pos = 0, None
            return
        rel = fields["rel_s"]
        if rel is not None and rel == self._last_pos:
            self._frozen += 1
        else:
            self._frozen = 0
        self._last_pos = rel
        if self._frozen >= STALL_POLLS and self._retried_at is None:
            self._retried_at = time.time()
            log("position frozen — one in-place retry")
            try:
                spk = self._spk()
                spk.avTransport.Stop([("InstanceID", 0)])
                spk.avTransport.Play([("InstanceID", 0), ("Speed", "1")])
                if rel and rel >= 5:
                    self._seek_settled(spk, rel)
            except Exception as e:
                log(f"stall retry failed ({e.__class__.__name__})")

    # -- migration-follow (stage B2) ---------------------------------------

    def _maybe_probe_migration(self, fields):
        """Called per poll tick, lock held, BEFORE publish. Trigger is
        the migration signature ONLY: STOPPED with an EMPTY TrackURI.
        The other two ways a session leaves us cannot enter here —
        hijack (grouped member) reports a non-empty x-rincon: uri, and
        a natural episode end reports STOPPED with the track uri
        RETAINED (the daemon's ends_near advance depends on that; do
        NOT reuse the `lost` flag, which excludes sharelink). Probe
        attempts are bounded per transition and re-armed only by an
        ours+live sighting; a hint, once found, ends the probing
        (probe-once — flap resistance for a kid mashing hold-play)."""
        if not (fields["transport"] == "STOPPED" and not fields["uri"]):
            return
        if self._moved is not None or self._migrate_tries <= 0:
            return
        if self._ours_at is None or \
                time.monotonic() - self._ours_at > MIGRATE_WINDOW_S:
            return
        self._migrate_tries -= 1
        try:
            self._probe_migration()
        except Exception as e:
            log(f"migration probe failed ({e.__class__.__name__})")

    def _probe_migration(self):
        """One attempt: is OUR stream now playing on another
        coordinator? LIVE topology (refresh_topology — which also
        merges the promoted speaker's ip/name into the players cache,
        exactly what verb dispatch and the screen name need), then each
        other-coordinator's OWN transport is probed. NEVER the aux
        `coordinator` field — it ghosts on failed refreshes (QA trap
        #2). Match rule: url/nrk by _norm_uri equality (signed/service
        uris are unique strings — cannot match a foreign stream);
        sharelink by exact decoded track uri PLUS position continuity,
        never prefix (trap #1). No match: quiet return — tries run out
        and the session lands in today's lost-session behavior."""
        cache = refresh_topology()
        soco = _soco()
        cands = [g for g in cache.get("groups") or []
                 if g.get("coordinator")
                 and g["coordinator"] != self.uid][:6]
        for g in cands:
            uid = g["coordinator"]
            rec = cache["players"].get(uid)
            if not rec:
                continue
            try:
                spk = soco.SoCo(rec["ip"])
                pos = spk.avTransport.GetPositionInfo([("InstanceID", 0)])
                tr = spk.avTransport.GetTransportInfo([("InstanceID", 0)])
            except Exception:
                continue  # a sleepy candidate is not the answer
            if (tr.get("CurrentTransportState") or "") not in \
                    ("PLAYING", "PAUSED_PLAYBACK"):
                continue
            turi = pos.get("TrackURI") or ""
            if self.kind == "spotify_sharelink":
                if self._last_ours_track is None:
                    return  # nothing exact to match — never follow
                if not turi.startswith(("x-sonos-spotify:",
                                        "x-sonosprog-spotify:")):
                    continue
                track = urllib.parse.unquote(
                    turi.split(":", 1)[1].split("?")[0])
                rel = _hms_to_s(pos.get("RelTime"))
                base = self._last_ours_rel
                if (track != self._last_ours_track or rel is None
                        or base is None
                        or not (base - 5 <= rel <= base + 45)):
                    continue
            else:
                if _norm_uri(turi) != _norm_uri(self.uri):
                    continue
            self._moved = {"uid": uid, "name": rec.get("name"),
                           # SESSION.uri, NOT the snapshot's — that one
                           # is EMPTY during STOPPED, and adopt writes
                           # it into the session (QA trap #3)
                           "uri": self.uri}
            self._migrate_tries = 0
            log(f"stream moved to {rec.get('name')} ({uid}) — hinting "
                "the daemon")
            return


SESSION = Session()


def _didl_field(meta_xml, tag, ns="dc"):
    """Pull one text field out of a TrackMetaData DIDL without namespace
    gymnastics — display only, never used for control decisions."""
    import re as _re
    m = _re.search(rf"<{ns}:{tag}[^>]*>([^<]*)</{ns}:{tag}>",
                   meta_xml or "")
    import html as _html
    return _html.unescape(m.group(1)) if m else None


# --- HTTP ------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        out = json.dumps(obj).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        except OSError:
            # client gave up waiting (mash of controls) — the work was
            # done; a BrokenPipe traceback per press is just journal spam
            pass

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/":
            self._send(200, {"ok": True})
        elif u.path == "/state":
            q = urllib.parse.parse_qs(u.query)
            if q.get("live", ["0"])[0] == "1" and SESSION.armed:
                # switch-back wants the EXACT second: one live probe with
                # a hard budget, falling back to the last snapshot — the
                # caller never waits on a sleeping speaker (owner ask
                # 2026-08-09)
                done = threading.Event()

                def probe():
                    try:
                        with SESSION.lock:
                            f = SESSION._classify(SESSION._spk())
                            SESSION._last_ok = time.monotonic()
                            SESSION.publish(**f)
                    except Exception:
                        pass
                    done.set()

                threading.Thread(target=probe, daemon=True).start()
                done.wait(timeout=1.5)
            self._send(200, SESSION.state())
        elif u.path == "/players":
            q = urllib.parse.parse_qs(u.query)
            stale = False
            try:
                if q.get("rescan", ["0"])[0] == "1":
                    cache = rescan()
                elif q.get("fresh", ["0"])[0] == "1":
                    # one topology call (~200ms) instead of a 3s SSDP
                    # scan; a household nobody answers for serves the
                    # cache marked stale — the picker stays usable and
                    # honest (cabin case: home speakers cached, none
                    # on this LAN)
                    try:
                        cache = refresh_topology()
                    except Exception:
                        cache = players()
                        stale = True
                else:
                    cache = players()
            except Exception as e:
                self._send(502, {"error": "scan-failed",
                                 "detail": e.__class__.__name__})
                return
            self._send(200, players_payload(cache, stale=stale))
        else:
            self._send(404, {"error": "not-found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            self._send(400, {"error": "bad-json"})
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/play":
                with SESSION.lock:
                    self._send(200, SESSION.play(body))
            elif path == "/adopt":
                with SESSION.lock:
                    self._send(200, SESSION.adopt(body))
            elif path in ("/pause", "/resume", "/stop", "/seek", "/volume",
                          "/queue_play"):
                with SESSION.lock:
                    r = SESSION.verb(path[1:], body)
                if r is None:
                    self._send(409, {"error": "uid-mismatch",
                                     "uid": SESSION.uid})
                else:
                    self._send(200, r)
            else:
                self._send(404, {"error": "not-found"})
        except KeyError as e:
            self._send(404, {"error": "unknown-uid", "detail": str(e)})
        except ValueError as e:
            self._send(400, {"error": "bad-request", "detail": str(e)})
        except Exception as e:
            # a SoCoUPnPException lands here too: the speaker said no.
            # 502 = "speaker problem", never a crash — vibbd's policy
            # maps it to the conservative branch.
            self._send(502, {"error": "speaker",
                             "detail": f"{e.__class__.__name__}: {e}"})


def main():
    threading.Thread(target=SESSION.poll_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    assert srv.server_address[0] == "127.0.0.1"  # never a LAN bind
    log(f"up on 127.0.0.1:{PORT} (soco loads on first use)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
