# PLAN: soloistd — Soloist as a coexisting Spotify engine (CONTINGENCY)

Three-agent review 2026-09-01 (architect + adversarial QA + implementation
planner), all working from a frozen snapshot of the official Soloist docs
(WS API / soloist-ctl / auth / downloads; the features and basic-
integration pages 404'd — absence rules applied strictly). Status:
DESIGNED, NOT BUILT. Trigger: needing a post-cutoff Spotify account on
the box (librespot#1649 audio-key denial), or Spotify sunsetting the
legacy path the old account rides.

## TEST ZERO — run before anything else (10 minutes, may void this plan)

QA's sharpest observation: the Soloist API key requires a PREMIUM
account and is personal ("each user must generate their own key") — a
child account cannot hold one, so "the son's own account" via Soloist
means running under a parent's key anyway. Meanwhile it is UNVERIFIED
whether a **Premium family sub-account** (created under the family
plan) even hits the librespot audio-key cutoff the same way a fresh
Free account does. If a family sub-account plays on go-librespot
today, the actual family need is met with ZERO new code.
→ Create the son's account as a family-plan member, pair it to
go-librespot on a bench Pi (device_auth if zeroconf refuses), press
play, watch for `audio key error`. THAT result decides whether this
whole plan stays parked.

## The honest disagreement, and what settles it

The ARCHITECT and CODER judge the two hardest gaps emulatable; QA calls
them blockers. Both sides agree the bench spike settles it — these are
the KILL CRITERIA, in order:

1. **Resume-at-the-right-second (the box's spine).** Soloist's `play`
   takes only a uri — no skip_to_uri, no position (go-librespot fork
   does all three atomically, player.py:251-273). Emulation:
   `play(context)` → immediate `pause` → `get_queue{limit:0}` → N×
   `skip_next` (paused) → `seek` → `play`. KILLED IF: skips do not fire
   while paused AND the set_volume-0 shroud is audibly leaky, or the
   walk draws server throttling, or deep-playlist walks exceed ~20s.
   Escape hatch for the box's CORE content (shows/audiobooks): play the
   episode uri directly + seek (exact, silent), with soloistd doing
   episode auto-advance itself from its own listing.
2. **Context enumeration without playing.** Soloist's only listing is
   `get_queue` on the CURRENT playback. The song picker for the loaded
   context is reconstructable (reversed previous + current + upcoming,
   autoplay-sourced rows excluded); browse-time enumeration and the
   sonos sharelink pre-map are NOT. KILLED IF: `get_queue{limit:0}` on
   a 500+ item context returns a window rather than the whole thing.
   Fallbacks: route enumeration through go-librespot (old account) or
   the Web API plumbing that already exists; otherwise the existing
   `spotify-listing-unavailable` refusal shape.
3. **Audio path.** Soloist speaks PipeWire/Pulse only; vibb is bare
   ALSA+bluealsa with hand-built pcms. PRIMARY: a scoped PipeWire shim
   (monitors and bluez5 OFF, one static `api.alsa.pcm.sink` opening the
   pcm named by output.json, suspend-timeout releasing the exclusive
   bluealsa device, run only while the engine is active; ~15MB, worst
   case ~25MB with wireplumber). FALLBACK: PulseAudio with an alsa-sink
   pinned the same way. bluealsa stays the only BT owner either way —
   PipeWire-bluez5 is explicitly rejected (platform migration through
   the most field-hardened layer). KILLED IF: Soloist refuses a
   session-manager-less PipeWire AND the Pulse fallback cannot open the
   bluealsa pcm reliably (exclusive-open contention).
4. **The kid-mash layer.** The fork's debounce/circuit-breaker exists
   IN the engine; against a closed binary soloistd can only coalesce
   presses into serialized `skip_next`s. KILLED IF: a 10-press mash
   draws account-level throttling/lockout (unobservable, unpatchable).
5. **Shows/artists/Liked Songs playability.** `play.uri` documents
   "track, album, playlist, or episode" — show/artist/collection are
   absent from the contract. Bench: try them anyway.

## Architecture (settled, all three agents agree)

**Dialect-preserving sidecar.** `pi/soloistd.py` clones the sonosd
pattern (127.0.0.1 JSON HTTP, closed error shapes, poller-owns-network,
single lock) and SPEAKS THE GO-LIBRESPOT REST DIALECT — same paths,
same field names. The daemon/player/spotify.py's ~30 call sites run
unmodified; engine choice is a base-URL + unit-name env pair
(`VIBB_GO_API`, `VIBB_GO_UNIT`) — instant rollback. Inside: one WS
client (hand-rolled stdlib RFC6455 preferred — loopback JSON only, and
it lets CI run the REAL sidecar against a stdlib fake server; venv
websockets lib as fallback), an event-fed status mirror
(position_sync interpolation with the speed field — push beats today's
1s polled cache), single-in-flight command correlation.

**soloistd SUPERVISES the soloist child** (not a sibling unit): exit
code 10 (build expired) must latch — `Restart=always` would brick-loop
it; `--pair` runs-and-exits; `--ws 127.0.0.1:0` needs ws.addr/ws.port
discovery from the data dir.

**Coexistence, not replacement.** Both engines installed; `source`
stays `mpv|spotify` (a third source forks every is_spotify branch —
rejected); routing = global setting `spotify_engine` + per-entry
`engine` field (contexts belong to accounts; entries are the natural
key). Distinct Connect names; soloist child runs ONLY when a
soloist-routed entry exists or pairing is active — zero cost otherwise.
Bookmark files stay engine-agnostic and shared.

**Lifecycle.** Weekly updater unit+timer (Persistent=true, defer swap
while playing, stable-latest archive from spotifycdn — no pinning, no
rollback, redistribution forbidden; TLS + exec-sanity is all the
verification available). Build expiry stamped at install
(`--version` → expires_at); `/soloist/health` carries days_left; PWA/
screen warn <21d, red <7d, and an expired build degrades to a CLEAR
"Spotify trenger oppdatering" state — never a silent dead box (the
bedtime rule). Pairing surfaced in the PWA: no QR needed — the phone's
Spotify app discovers the device; the UI is one instruction sentence
driving `--pair` via a oneshot unit.

## What is preserved / degraded / lost (condensed verdict table)

PRESERVED: bookmark fidelity (improved — push events + speed field),
volume (steps=100 degenerates the scaling to identity), pause/resume/
playpause/seek, prev-restart dance (client-side logic rides the proxy
unchanged), status card fields incl. covers, park-offline policy.
EMULATABLE (soloistd work): exact resume (the walk — bench-gated),
pending/next-track optimism (from queue_changed), active-context song
picker, shuffle pre-arm, autoplay suppression (with a possible
sub-second radio blip at context end), play_origin heuristic (persisted
box-context marker; the phone-clobber guard degrades from exact to
conservative-both-ways).
DEGRADED: browse-time enumeration + sonos sharelink pre-map (fallback
chain), account identity (auth_state has no username — PWA shows the
device name), output switching (no /player/output — falls back to the
existing restart+bookmark-resume path), bitrate pinning (gone).
LOST, owner sign-off required at trigger time:
- **No zeroconf lock**: the Soloist device is claimable by any Premium
  account on the LAN, forever; "session replacement" is documented.
  Mitigation: alarm on unexpected auth_state change + naming. This
  reopens a vibb-security-gate concern.
- **Free accounts play ADS** (`entity_type: ad` is first-class;
  available_actions is how skip disappears during one) — on a kids box.
  Premium-family routing is the real answer (see TEST ZERO).
- The fork's audio-key economy (debounce = 2 key requests per burst),
  proactive /cache/download precache for hotspot trips, /cache/snapshot
  gating, `disable_autoplay` as a hard switch.

## Bench spike protocol (merged, kill-risk order — a scripted day on a spare Pi)

1. The resume walk (kill #1): volume-shroud vs paused-skip variants,
   walk latency at N=5/50/300, event traces, throttling.
2. `get_queue{limit:0}` truth on 500-track playlist + 200-episode show
   (kill #2): completeness, decoration density, works-while-paused,
   works-while-inactive.
3. Audio (kill #3): what the binary actually opens; scoped-PipeWire
   static sink on a plug→bluealsa pcm; suspend/release; RSS of the
   whole stack; BT sink vanishing mid-track.
4. Mash (kill #4): 10 presses/3s for a minute; throttle signature and
   recovery.
5. URI acceptance (kill #5): show/artist/collection despite the docs;
   bare play on an already-playing session (the Sonos-hiccup class);
   idle set_shuffle persistence; context-end default behavior.
6. Lifecycle: --version format, --pair exit semantics, ws discovery,
   exit-10 via clock jump, session replacement from a second phone.

## Phases (after a surviving spike)

P1 minimal engine (~1.5-2k lines, the sidecar + install section +
units + env seam): soloist-routed play with exact resume, health,
pairing, expiry latch, updater. P2 parity (~500-800): optimism, picker
emulation, prev dance, autoplay suppression, origin marker, output
choreography. P3 coexistence UX (~300-500): per-entry engine field,
PWA editor, naming, sharelink fallback routing. Test strategy: a
frozen tests/soloist_contract.py (both fakes import it — the two-fakes
trap), a stdlib fake WS server so CI drives the REAL sidecar, and the
existing spotify behavioral pins re-run unmodified against it.

## Rejected alternatives (all three agents, consolidated)

Native WS client in the daemon (stdlib-only constraint); soloist-ctl
polling instead of WS (no push, fork-per-poll); a clean non-dialect
API (re-reviews 30 field-hardened call sites for purity); replacing
go-librespot outright (old account + two years of field fixes + a
90-day fuse); full PipeWire platform migration now (endgame only);
FIFO pipe-sink (clocking/pause/volume tarpit); "soloist" as a third
source; systemd-supervised soloist with soloistd as client (loses
exit-10 latching and pair orchestration); emulating the audio-key
circuit breaker against a closed binary (its official status IS the
mitigation).

## Bottom line

QA's framing stands as the epigraph: the documented Soloist surface is
a REMOTE CONTROL for a self-driving official client; vibb is built on
a PLAYER IT DRIVES. The adapter is buildable if and only if the spike
proves the walk, the queue and the audio shim — and TEST ZERO may
prove the family never needs it.
