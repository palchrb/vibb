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

**ANSWERED IN THE FIELD (owner, 2026-09-01): the test already ran and
FAILED.** The son's regular adult account on the owner's Premium
Family plan gets the audio-key error via normal zeroconf on
go-librespot. Family Premium does not clear the cutoff — the account's
CREATION DATE is what matters. The trigger for this plan is therefore
REAL for the son's account; what keeps it parked now is only the bench
spike (kill criteria above) and the owner's call on timing. Silver
lining kept from the account model: his family-member account holds
Premium, so he can generate his OWN Soloist API key, and Premium means
NO ADS — two of the sign-off items soften for this household.

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
   OWNER'S READING (2026-09-01): the announcement caps queue metadata
   at ~80 entries. If confirmed on the bench, the verdicts refine to:
   song picker/PWA queue CAPPED at ~80 past the playing track (fine
   for the kid's real content — 5-book series, 25-episode shows; bites
   on long music playlists), the resume walk UNAFFECTED (sliding
   window — re-query as you skip), the sonos queue map BROKEN >80
   without the Web API fallback, cache warming UNAFFECTED. The Web API
   listing (credential plumbing exists in the daemon) lists any
   playlist completely and closes both gaps — at its own maintenance
   cost (Spotify's 180-day re-auth tightening applies there).
   **PROMOTED FROM FALLBACK TO REQUIRED P2 COMPONENT (owner
   2026-09-01): "the kids must be able to see the whole playlist when
   choosing a song." The ~80 window cannot page (anchored at the
   playing position, not scrollable), so full-picker visibility for
   soloist-routed entries IS the Web API path: one-time app
   registration + OAuth, picker/PWA queue served complete from it,
   and a PWA re-connect nag every ~180 days as the running cost.**
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
- **Bitrate not settable** (confirmed 2026-09-01 against the snapshot:
  the full CLI is device-name/api-key/ws/pair/single-track/
  initial-volume/pipewire-device/data-dir/cache-size/verbose — no
  quality flag; the options object has shuffle/repeat/speed/modes but
  no quality). RESOLVED ON DISK 2026-09-01: quality follows the
  account's **DOWNLOAD** setting, not the stream setting — the owner's
  app is 320 for wifi streaming but Normal for downloads, and the cache
  files compute to ~160 kbps (a 3-min song ~3-4MB, 320 excluded — that
  would be ~7MB). Soloist treats itself as a DOWNLOADING client (it
  fetched a whole 157-min episode from ~30s of play), so it inherits
  "Download quality". Consequences, all good: the fork's bitrate:160
  knob is NOT lost, just moved to the app (set the son's account's
  download quality: Low ~96 / Normal ~160 / High ~320); and it is the
  RIGHT knob for a radio-frugal box. Radio math beats the fork's:
  160 in one burst + play-from-disk, not a steady 160 stream, and
  replays cost zero radio.

## Bench spike protocol (merged, kill-risk order — a scripted day on a spare Pi)

**FIELD FINDING #1 (2026-09-01, before the WS even connected): the
binary requires glibc >= 2.38.** The owner's Pi 5 on Bookworm (glibc
2.36) refuses to start it; the announcement blog's Ubuntu 24.04 choice
was load-bearing, not taste. OS floor: Raspberry Pi OS Trixie
(glibc 2.41) or Ubuntu 24.04+. THE VIBB BOX ALREADY RUNS TRIXIE — the
floor is satisfied on the target, and a Trixie bench is MORE
representative than Bookworm would have been. Had the box been on
Bookworm this would have been a plan-level blocker; record it as a
hard deployment constraint alongside the missing armv6 build. Bench
setup: flash Trixie (desktop, for PipeWire + audible output) on the
spare Pi. Spike script: bench/soloist_spike.py.
   FIELD FINDINGS #2-3 (2026-09-01, bench up via the glibc loader
   trick on Bookworm): (a) the binary spawns a crashpad crash-handler
   child by re-exec'ing /proc/self/exe — harmless under the loader
   trick (the child dies, the daemon runs; no crash telemetry to
   Spotify), fine on Trixie; (b) startup logs "client expires in N
   days" in clear text — a BETTER health source for soloistd's
   days_left than the planned --version stamp parsing: parse the
   child's own startup line.

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
   ALSO: cache-fill semantics — play 3s of a track, disconnect network,
   replay it: served whole from cache or partial? (Decides the cache-
   warming feature's cost class, see the warming section.)
   bare play on an already-playing session (the Sonos-hiccup class);
   idle set_shuffle persistence; context-end default behavior.
6. Lifecycle: --version format, --pair exit semantics, ws discovery,
   exit-10 via clock jump, session replacement from a second phone.

## SPIKE RUN 1 RESULTS (2026-09-01, bench Pi 5 via loader trick)

The operator skipped the ear/network steps, but the OBJECTIVE numbers
settled most of the board:

- **Kill 1: PASSED on a real listen (run 2, 2026-09-01).** Skips fire
  while paused at 20-60ms each; audible=FALSE when play->pause landed
  in 60ms. BUT the play->pause gap VARIES run to run (0.06s vs 0.38s),
  and run 1 at 0.38s WAS faintly audible — so the walk is silent only
  when the pause wins the race, which is not guaranteed. DESIGN
  CONSEQUENCE: soloistd keeps the set_volume-0 shroud during the walk
  as belt-and-braces; do not rely on pause-beats-audio timing. The
  spine is confirmed viable; the shroud makes it reliable.
- **Kill 2: CONFIRMED WINDOWED at exactly 80** (`upcoming: 80` on a
  100+ playlist). The Web-API-listing-as-required-P2 decision stands.
- **Kill 4: PASSED WEAKLY — a single 3s burst only.** 10 skips at
  ~300ms each, zero error frames, playback progressed. Reassuring that
  basic mashing does not break immediately, BUT this did NOT test the
  fork's actual failure mode, which was CUMULATIVE (sustained mashing →
  the 51-track walk that kept the account rate-limited). The mash test
  is now sustained (10/burst x6 over ~a minute, tracking settle-latency
  drift) and needs a re-run. Two things remain UNOBSERVABLE in a closed
  binary regardless: the audio-key economy (the fork's whole
  optimization) and a delayed account-level cooldown — the honest
  residual risk of this whole engine for a mashing kid.
- **Kill 5: show and artist URIs PLAY** despite the docs' silence —
  QA's blocker B2 falls for the box's core content. Collection URI
  still untested.
- Idle `set_shuffle` ACCEPTED (pre-arm survives); bare play on a
  playing session is a NOOP (the sonos-hiccup class absent).
- INVALID from run 1: resume-walk audibility ("y" without listening)
  and cache_fill ("y" without disconnecting — and soloist ran WITHOUT
  -C, so there was likely no cache to measure at all). The cache test
  is now disk-based (bench/soloist_spike.py --cache-dir; requires
  starting soloist with -C ./cache -z 500) and needs a re-run.
- Script warts found by the run and fixed: swallowed track_changed
  events caused 15s audible gaps between tests (not a soloist
  behavior), and the mash prompt defaulted to STALLED on Enter.

- **FIELD FINDING #4: the cache exists BY DEFAULT at
  `~/.cache/soloist` (XDG cache dir)** — no -C flag needed; the
  truncated doc line was "default is no LIMIT", not "none". And it
  held 138MB after a run where nothing played longer than ~60s
  (~3-4 min of audio ≈ 5-8MB at stream bitrate) — Soloist fetches FAR
  beyond what is played. Strong whole-file/aggressive-prefetch signal
  for the warming feature; the disk-based growth test confirms
  formally. install.sh consequence: cache dir config is optional,
  size cap (-z) is the knob that matters on the box's SD card.

- **CACHE VERDICT SETTLED OBJECTIVELY (disk listing, run 1's own
  cache): WHOLE-FILE.** Track files at 3.7-6.5MB = full songs; and a
  91MB file = the 157-minute episode that played for ~30 SECONDS —
  Soloist fetched ~250x what was heard. The warming feature gets its
  best case: seconds of play per item caches the whole file; a trip
  playlist warms in minutes. (28 files/138MB from one spike run also
  says: the -z size cap is mandatory on the box's SD.)

REMAINING before build go/no-go: walk audibility (ears, once — the
window is <0.4s of track-1 at volume before the pause lands; the
show/audiobook path has no walk at all), collection URI, and the
audio-shim question (separate rig, the scoped-PipeWire experiment).

## OPEN: does a 90-day binary swap void the cache? (owner question 2026-09-01)

Undocumented, and it compounds with the never-run offline test. Two
unknowns, two experiments:
- **Offline playability — SCOPE, so "offline" is not misread:** the
  question is A, NOT B.
  A = "play an ALREADY-CACHED track with the network briefly down" —
    the file is on disk (proven), but does Soloist need a LIVE session
    to fetch the audio key per playback (the librespot-world limit)?
  B = "complete offline for days (airplane/cabin, no packets at all)"
    — NEVER on the table for ANY Spotify engine; the fork's own config
    says session+keys are live requirements. The box's real offline
    floor is the mpv side (Storytel downloads, podcast cache), full
    stop. soloistd does not change that and never promised to.
  Test A now: daemon up, pull the Pi's network, play a run-1-cached
  track (e.g. DAISIES). Plays = cache holds keys, real value even when
  the hotspot is off. Fails = the cache is a BANDWIDTH SHIELD ONLY
  (spares metered data WHILE there is net — still worth the warming,
  just not an offline mode). Either outcome only moves how hard we
  sell warming; neither is a blocker.
- **Cross-build survival:** the content-addressed filenames
  (cache/73/730d...file, SHA-style) SUGGEST keying by Spotify file-id,
  not client version — which would survive. BUT the 90-day fuse IS key
  rotation, and if cache files are re-encrypted with a build-embedded
  key they void on every swap. Only testable at the first real update
  (the canary step): note the cache, swap the binary, replay an
  old-cached track, watch `~/.cache/soloist` — growth = voided,
  quiet = survived.

MITIGATION regardless of outcome (cheap because warming is whole-file =
minutes): the updater unit re-warms the `cache: N` entries after a
successful binary swap, gated on charger + home wifi + idle like all
warming. Quarterly, overnight, invisible to the kid. So even
worst-case (voided every build) costs one background warm per quarter,
not a degraded bedtime.

## Cache warming — "silent play-through" (owner idea 2026-09-01, ACCEPTED into P2/P3)

The proactive precache dies with the fork (no download API in Soloist);
the owner's counter: script a SILENT PLAY-THROUGH to warm the organic
cache before a trip. Design, honest about its one hinge:

- **THE HINGE (bench spike item, added to the protocol):** does
  Soloist's cache store WHOLE tracks on play (or on seek-touch), or
  only the chunks actually played? Whole-file ⇒ warming is ~2s + a few
  seek-hops per track — a playlist warms in minutes. Chunk-based ⇒ a
  sparse cache is worthless and warming means REAL-TIME play-through —
  nights, not minutes, still viable for a kid's stable rotation
  (warm once, then only new content) but a different cost class.
- Silence done properly: during warming, soloistd retargets the
  PipeWire shim to a NULL SINK — no audio path to speaker/BT at all;
  volume-0 only as belt-and-braces (it is Connect-visible).
- The list already exists: the library's per-entry `cache: N` field is
  the same contract today's sweep uses; warming is a new branch in the
  cache sweeper for soloist-routed entries.
- Gating inherited from the sweep/backup culture: charger + idle +
  REAL home wifi only (never the hotspot — warming over the metered
  link pays the bill the cache exists to avoid); any button press
  aborts instantly (warming is always the side that can wait); a
  warming-marker so idle.py does not read it as kid-activity and hold
  the box awake — the warming window carries its own time budget.
- Play-history pollution: accepted — it is the kid's own content on
  the kid's own account; the oddity is 03:00 timestamps. No private-
  session command exists in the API.

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
