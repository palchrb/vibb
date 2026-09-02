# Audio stack: PipeWire migration — assessed 2026-09-01, VERDICT: NOT NOW

**SUPERSEDED 2026-09-02 (owner decision): build it.** The verdict was
overruled, the findings were not — every item below is answered in
`docs/PLAN-pipewire-soloist.md` (architect design + two QA passes).
This file stays as the record of why the trip is expensive.

Owner asked the sharp question after the shim spike: PipeWire has native
Bluetooth, so is the shim only needed because we insist on keeping
bluealsa — and would full PipeWire actually be SIMPLER? Architect + QA
round on the real code. Both converge: **the END STATE is simpler, the
TRIP is not, and the trip runs through the box's only field-hardened
layer.** Do not migrate now.

## The complexity number (architect, from the code)

~950 lines of churn (~415 deleted, ~530 added) across ~12 files, of
which only ~250 lines are genuinely NEW logic — the rest is deletion
and rewiring. 3 coding sessions + 1 install/units + 2 bench days, then
**~1 month of field soak** (the box's failures are evening/car/bedtime
events). Of 156 tests, 26 are structurally coupled; 5 get deleted or
rewritten, ~17 need a fixture swap.

The churn is survivable BECAUSE of work already done — btbus.py isolated
the transport primitives, renderer.py kept "who renders" orthogonal to
"which pcm", output.py extracted the pcm plumbing. The cost is not the
code. It is re-earning two years of field-hardness in the layer where
every bug is found by a child at bedtime.

## The finding that decides it (QA BLOCKER-1)

**WirePlumber's stream-rescue would defeat a kids-SAFETY invariant.**
When a BT sink node disappears, WirePlumber's default policy moves
orphaned streams to the next default sink — the HAT speaker. vibb's
volume cap (`output.py:22-45 local_volume`, default 35) is applied by
vibb at spawn (`player.py:543-545`) and at live retarget
(`daemon.py:1795-1801`); a WirePlumber-initiated move goes through
NEITHER. Net: a headphone dropout produces automatic FULL-VOLUME
playback from the box speaker next to a child's head — which is exactly
what the owner banned in 2026-07-23 (`daemon.py:4880-4885`, "the box
speaker suddenly blasting next to a kid wearing dead headphones is
worse than a short gap in them") AND what the cap exists to prevent.
It is configurable off — but a safety invariant currently enforced by
THE ABSENCE OF A MECHANISM would become enforced by a correct config
file in someone else's package, subject to upgrades.

## Other load-bearing findings

- **WirePlumber is MANDATORY for native BT** (the bluez5 monitor is
  driven by the session manager). So the "scoped, no session manager,
  ~15MB" containment that made the shim palatable is UNAVAILABLE here:
  full PipeWire means adopting a policy engine that makes autonomous
  routing decisions inside a daemon whose entire BT design is an
  explicit, auditable state machine. 13 concrete collision points
  (C1-C13) are listed in the QA output, incl. suspend-on-idle
  re-creating the AVDTP churn that `install.sh --keep-alive=120` was
  added to suppress — on the chip whose crash model is STILL OPEN.
- **The playback gate has no cheap replacement.** `a2dp_pcm_present`
  (btbus.py:372, D-Bus backend :593) gates ~8 call sites incl. /status
  at ~1/s. PipeWire has NO D-Bus surface and vibbd is stdlib-only, so
  the replacement is a fork+JSON-parse per poll or a new sidecar.
- **Two field fixes become silently decorative**, the worst failure
  mode for this codebase: mpv's `--audio-buffer=0.5` (tuned against
  bluealsa's ~100ms device buffer for RF gaps) keeps working
  syntactically while the real knob moves into PipeWire's quantum; and
  `local_volume_cap.py` keeps PASSING while WirePlumber moves streams
  around it. A green suite after this migration would be misleading.
- Resource: +30-45MB standing (7-10% of the box's ~430MB), and a
  realtime graph process added to the exact variable (coex/scheduling
  load) the open BT crash model is about.
- **Not field-reversible.** bluealsa and PipeWire cannot coexist (both
  register A2DP endpoints), so the cutover is atomic across
  asound.conf, btbus, bt.py, btwatchd, install.sh and the crash
  recovery path.

## What the migration WOULD genuinely buy (stated fairly)

Output switching stops being a distributed transaction (~250 lines of
choreography and three field bugs deleted); one BT stack instead of
two; Soloist native with no shim; mixing becomes possible; stable
per-speaker sink names for free (the NEXT-STEPS backlog item). None of
these is a problem the box HAS today; the exclusive-open cascade is
already mitigated with working code and a test.

## The cheapest path to the actual goal (the son's Spotify on the box)

0. **TODAY, zero code: Sonos.** The sharelink path hands the URI to the
   speaker, which streams using the account linked in the SONOS APP —
   no local engine, no cutoff. Link his account there and his cards
   play. Gap: needs a Sonos in range.
1. **Two hours: run the PulseAudio test that was never run.**
   `bench/pipewire_shim_rig.sh pulse` on a bare Trixie image or the box
   at a quiet moment. If `module-alsa-sink device=vibb_bt` opens and
   releases when idle, the Soloist audio question closes with a ~10-line
   ADDITIVE config: bluealsa untouched, zero field fixes at risk, PA
   running only while Soloist runs, instant rollback. **Nothing else
   should be decided before this runs.**
2. If PA fails: asymmetric output (PipeWire serves ONLY the HAT, which
   is a real card; BT stays bluealsa; Soloist refused on bt output).
3. **Regardless: make the gate backend-neutral now.** Replace
   `a2dp_pcm_present`'s implementation with an `org.bluez.
   MediaTransport1` presence check, same signature, testable against
   today's box and `fake_bluezd.py`. ~40 lines, removes a bluealsa
   dependency at zero risk, prerequisite for any future move.
4. Full PipeWire: defer to a HARDWARE GENERATION — a box with RAM
   headroom, a controller without a firmware-crash history, and the
   crash model closed. Not as a side effect of an engine swap that is
   itself parked.

## Why this is the right call by the project's own rules

`vibb-diagnose-before-fixing`: the BT crash model is an OPEN question —
replacing the layer under investigation is the opposite of measuring.
`vibb-scope-discipline`: a platform migration is the maximal answer to
a request whose minimal answer is one untested config line. And the
chain of contingency is four deep: PipeWire migration → so Soloist runs
→ a contingency → for one account → whose need is met on Sonos today →
and Soloist still carries an unresolved 90-day brick fuse.
