# PLAN: PipeWire audio platform + soloistd — implementation plan (2026-09-02)

Status: **DESIGNED, NOT BUILT.** Owner decision 2026-09-02: build it.
Two-agent round (architect + adversarial QA, two QA passes) on the
tree at `33af425`. This plan supersedes the audio half of
`docs/PLAN-soloistd.md` (the shim/PulseAudio path, §3/§3b/§3c) and the
NOT NOW verdict in `docs/NOTES-audio-stack.md`. Everything else in
PLAN-soloistd.md (the dialect-preserving sidecar, the install-time
engine toggle, the exit-10 latch, the updater, cache warming, the
spike results) stands and is Phase 3 here.

## The decision, and what it does NOT overrule

The 2026-09-01 round said "not now": end state simpler, trip not, and
the trip runs through the box's only field-hardened layer. The owner
overruled the **verdict**, not the **findings**. So this plan is built
the other way round from a normal design: the 2026-09-01 QA register
(BLOCKER-0..3, SEVERE-1..5, MODERATE-1..5) plus the 2026-09-02 round-1
findings (NEW-1..7) are the requirements list, and every item is
answered by a config fragment, a code change, a test pin, or an
explicitly signed residual (section N). Nothing is answered by "it
will probably be fine".

Design stance in one line (architect): **keep every field-hardened
vibb mechanism exactly where it is, and make PipeWire look like ALSA
to it.** The migration replaces the transport owner (bluealsa →
PipeWire/bluez5) and the pcm slave (`type bluealsa` / `hw:` → `type
pipewire` with a pinned node) — and nothing else until Phase 4. That
is why the test damage is "0 rewritten, 4 fixture-swapped, ~9 new"
instead of the earlier "5 rewritten + 17 swapped", and why
`reopen_go_output`, `_go_output_rebuild`, the deferred-switch
refusal and `set_output` survive verbatim.

## The invariant that decides everything

The owner's ban (`pi/daemon.py` ~4880, 2026-07-23): NO local
fail-over — the box speaker suddenly blasting next to a kid wearing
dead headphones is worse than a short gap in them. Today that is
enforced by the ABSENCE of a mechanism (an ALSA pcm name is the only
route). Under PipeWire a session manager exists whose default policy
does exactly the banned thing. The design answers with three
independent layers (a whitelist WirePlumber profile that never loads
the "find another sink" hooks; settings that turn moving/following
off; per-stream `node.dont-reconnect`/`node.dont-fallback`) AND a
boot-time self-test that asserts the BEHAVIOUR (a stream whose target
vanishes links nowhere) and refuses the local landing if it fails.
Section B. If the bench (B2/B8) cannot prove this, the migration is
dead regardless of everything else — that is the first kill criterion.

The second finding that reshaped the plan is **NEW-1**: Spotify on the
HAT is UNCAPPED TODAY (`local_fallback_cap` only ever reached mpv;
go-librespot sends `volume.json` straight to `/player/volume`). It has
never bitten because every such path has a human pressing something.
Soloist would be a third uncapped client. The fix (section F) ships in
Phase 0, on the current stack, before any PipeWire work.

## Phase gates (the short version — details in L)

| Phase | What | Where | KILL → back to |
|---|---|---|---|
| 0 | MediaTransport1 gate (shadow → flip), NEW-1 cap fix, NEW-2 pause-confirm, `pi/vibb/audio.py` resolver (unused in the field), 7-day baseline capture | the box, current stack | gate disagreement that maps to a field pattern → stay on PCM1 |
| 1 | bench protocol B0–B12 (QA §3) in the exact A/B topology | Trixie bench Pi, never the box | any HAT audio in B2/B3; drift that stays green in B8; a B10 tier needing a `pipewire.service` restart; AVDTP churn ≠ baseline |
| 2 | box cutover behind `VIBB_AUDIO_STACK=pipewire`, ≥14 days / ≥20 streaming h / ≥5 evening sessions soak | the box | any one of the eight abort criteria (QA §4) → `VIBB_AUDIO_STACK=bluealsa ./install.sh` |
| 3 | soloistd P1 (PLAN-soloistd.md) on the new stack | the box | soloist on a non-pinned sink; a node leaving `suspended` during warming |
| 4 | delete bluealsa paths, default toggle flips | repo | never starts if Phase 2 aborted |

Rollback drill (QA §4) is rehearsed and timed on the bench BEFORE
Phase 2, once offline: target < 15 min including reboot.

## How to read this document

- **Sections A–N** are the architect's design (2026-09-02), verbatim
  except where the QA round-2 amendments below changed a decision;
  each such place is marked `[QA-2: ...]`.
- **QA round 2** (right after this preface) is the adversarial pass
  over that design: per-section verdicts, the code claims verified,
  the go/no-go.
- **Appendix A** is QA round 1: the register re-verified against
  current code (with line numbers), NEW-1..7, the invariants I1–I13
  that must be pinned before cutover, the bench protocol B0–B12 with
  numeric PASS/KILL, the baseline/soak/abort/rollback plan, and the
  15 questions the design answers in section M.
- The 2026-09-01 register itself is summarised in
  `docs/NOTES-audio-stack.md`; its full text lived in the review
  session and is reproduced in Appendix B for the record.
- Every `(verify on bench)` tag is a PipeWire 1.4 / WirePlumber 0.5
  config key the architect could not confirm from the tree; each has
  a named bench step, and a wrong key fails loudly through the
  self-test rather than silently. Resolve them all in the Phase 1
  bench note before Phase 2. **`bench/pipewire_platform_rig.sh`** is
  the pre-code rig for exactly these tags: it writes the §A units (with
  AM-1/AM-2) and the §B fragments on a bench Pi and runs S1–S5 as
  PASS/KILL — no vibb code needed, Pi 5 is enough for everything but
  the RF/RSS numbers.

Standing rules that apply to this plan as to everything else: bench
first, never the box (`vibb-diagnose-before-fixing`); the go-librespot
fork is consumed, never changed from here (the design needs no fork
feature — `audio_backend: alsa` stays); nothing wifi-heavy during
active A2DP playback; kids-safety over convenience every time the two
collide.

---

## QA round-2 amendments — APPLIED to the design below

Every item is a NEEDS-CHANGE from QA round 2 (reproduced in full after
section N) that the plan adopts. Where the design text below still
shows the original decision, the `[QA-2: AM-n]` mark at the section
head wins. Two of these are judgment calls where QA's argument was
taken over the architect's — AM-7 and AM-17 — flagged for the owner.

| ID | Section | Amendment |
|---|---|---|
| AM-1 | A | `pipewire.service`: DROP `RuntimeDirectory=`/`RuntimeDirectoryMode=` — systemd removes a RuntimeDirectory on every service stop while `pipewire.socket` still holds the listener, so one PipeWire crash → `/run/pipewire/pipewire-0` ENOENT for every client until reboot (a silent dead box). `pipewire.socket` owns the directory (`DirectoryMode=0750`). |
| AM-2 | A | `wireplumber.service`: `[Install] WantedBy=pipewire.service` + `BindsTo=pipewire.service` (not `WantedBy=multi-user.target` + `Requires=`): a PipeWire crash must restart BOTH, or the core comes back with no session manager (no bluez endpoint, no HAT node). |
| AM-3 | A | DROP the `bluetooth.service.d/vibb-after-wp.conf` drop-in: `After=` on a `Type=simple` unit is satisfied at exec, not at endpoint registration, so it buys nothing and adds an edge. Keep `vibb-bt-reconnect` `After=bluetooth.service wireplumber.service`; B1 + btwatchd's existing nudge (`btwatchd.py:448-462`) is the mechanism. N5 restated: "a headset paging in between bluez-up and endpoint registration costs one nudge (~10 s)". |
| AM-4 | A | `Nice=0` on both daemons in Phase 2 (bluealsa runs at nice 0 today); `-11` is a one-line escalation exactly like RT, measured as a separate B6 run. |
| AM-5 | A/Q3 | `Environment=PIPEWIRE_PROPS={ node.dont-reconnect=true node.dont-fallback=true }` on `vibb-daemon`, `go-librespot` AND `vibb-soloistd` — makes the `client.conf.d stream.properties` verify tag non-fatal. |
| AM-6 | B | Profile: add `hooks.linking.find-default-target = disabled` and `hooks.linking.find-best-target = disabled` (verify names) — `policy.linking.standard` bundles the whole hook set, so without this a targetless stream still gets best-target linking to BT-then-HAT. B8 flips each. |
| AM-7 | B/Q11 | **Failure action REPLACED.** On `fail-safety`: CAP EVERY LANDING regardless of output — `local_volume`'s `pcm == local` test bypassed so mpv `--volume`, `_apply_box_volume`, and every retarget/reopen re-apply use `min(stored, cap)`; `Orchestrator.volume()` clamped to the same cap; screen line. Nothing refused. Reason: the drift the self-test detects is a stream moving to the HAT WITHOUT `set_output` — refusing `set_output(local)` does not touch that path, and "BT untouched" was exactly the BLOCKER-1 scenario; a local-only box would also have been dead at bedtime. `local_landing_allowed()` is dropped. |
| AM-8 | B | Self-test hygiene: probes are silence and bounded (`timeout 2 pw-cat --playback --raw --format=s16 --rate=44100 --channels=2 /dev/zero`); "no `default` metadata object" counts as PASS; re-runs rate-limited via `POLICY_FILE` mtime (vibb-daemon is `Restart=always RestartSec=5`); RE-RUN when the `pw-dump` core `info.cookie` changes (= PipeWire restarted). |
| AM-9 | B/D | The player's ≤5 s `sink_ready` wait before `Popen` FAILS CLOSED (exit nonzero, one log line), poll 0.5 s — never "spawn anyway". |
| AM-10 | C | `audio.ensure_bt_route(mac)`: tmp name `ASOUND + f".tmp.{os.getpid()}"` AND `acquire_process_lock()` around the rewrite (a second unlocked writer sharing `.tmp` can rename a truncated file into place — the exact catastrophe the fsync closed); writes the file ONLY (no go-librespot reopen — the announce path already reopens at `daemon.py:1749`; two reopens per reconnect is the 2026-07-17 storm class); hoisted to right after `pcm = OUTPUT_PCMS.get(device)` (`:1723`) under `device == "bt" and fallback` (the `:1732-1756` branch only runs when output is already bt). |
| AM-11 | D | The deferred apply at `daemon.py:1735-1740` (live mpv → `alsa/vibb_bt`) is ALSO gated on `sink_ready("bt", mac)` — one fork per announce, closes the ms window listed as residual N3. |
| AM-12 | D | Shadow mode: compare 1/10 s, not every call (the `/status` probe has a regression history, `daemon.py:2998-3001`); log direction AND duration. Flip criteria: (a) zero disagreements lasting ≥2 consecutive compares; (b) zero in the `transport=False/pcm=True` direction; (c) zero transport appearances that vanish within 3 s without PCM1 ever True (the flicker that would make `_await_pcm` commit on a refusing peer, `btwatchd.py:443-446`, `:239-248`). |
| AM-13 | F | The capped `/player/volume` POST goes BEFORE `/player/play` (after the session wait `player.py:243-249`), the post-play call at `:316` stays as belt (verify: `/player/volume` accepted with no track loaded). Without this every vibb-started Spotify landing on the HAT plays 0.5-2 s uncapped — the residual N2 wrongly attributed to phone-driven sessions only. The race with a concurrent volume press is benign (both orderings end ≤ cap). |
| AM-14 | G | `_confirm_spotify_paused`: `ConnectionRefusedError` → True (not running); TIMEOUTS → unknown, keep trying to the 2.5 s budget; on exhaustion with timeouts → `systemctl try-restart go-librespot` BEFORE spawning (the crash memory's deterministic medicine — an API that cannot answer a pause for 2.5 s IS the wedge signature; "OSError → True" would spawn mpv over a playing, wedged go-librespot = the exact double-audio NEW-2 exists for). Unit pin: a fake that times out `/player/pause` while `paused:false` must produce the restart, not a spawn. |
| AM-15 | H | `pcm.!default` in vibb's own `/etc/asound.conf` (loaded after `conf.d`, so it wins): `type plug, slave.pcm { @func getenv vars [VIBB_ALSA_DEFAULT] default "vibb_closed" }`, `pcm.vibb_closed` = a pipewire pcm with no node (fails closed); `extra.sh --run` exports `VIBB_ALSA_DEFAULT=hw:sndrpihifiberry` for the extra only (extras open ALSA `default`, `docs/extras.md:85`). In bluealsa mode the same line maps `default` → `hw:sndrpihifiberry`, which also neutralises an apt-recreated `99-pipewire-default.conf` after rollback (N9's second clause removed). Mask/stop order written down: wireplumber → pipewire.service → pipewire.socket. |
| AM-16 | I | Soloist bind check runs at child start (informational) AND within 2 s of the first `playback_state: playing` event (authoritative — the stream may be created lazily on first play): not linked to the intended sink → `pause` immediately, kill, closed shape. B9 records lazy vs eager. |
| AM-17 | J/Q8 | **`bluez5.enable-hw-volume = true`** (architect had `false`). Absolute-volume headsets (the JBL class) do not attenuate locally — they send `VolumeChanged` expecting the SOURCE to attenuate, which bluealsa does in software today; with hw-volume off the kid's headset buttons stop working (`vibb-buttons` only sees passthrough keys, `buttons.py:48-49`). With it on, PipeWire mirrors the headset's volume into the node and writes `MediaTransport1.Volume` only when the NODE volume changes — which vibb never does (I8) and restore is off — so RF-neutral except at most one sync PDU at acquire. B11 adds: press the headset's own volume button during playback → audible change; btmon shows only the headset-originated notification. |
| AM-18 | K | Four more pins before cutover: `player_sink_wait_fail_closed.py`, `spotify_volume_before_play.py` (HTTP-spy order), `asound_second_writer.py` (two concurrent writers never truncate — unique tmp + flock spy), the AM-14 wedge case in `spotify_pause_confirm.py`; `audio_client_props.py` asserts `PIPEWIRE_PROPS` on all three units. |
| AM-19 | E/B2 | B2 also records tracks advanced before the stop landed and the resume-position delta, against the bluealsa baseline (the bookmark is rewritten on every path change, `player.py:791-795`; a 3-track storm resumes 3 tracks late unless `survive_dead_audio`'s rollback wins the 3 s poll race — pre-existing, but the number the owner will notice). |
| AM-20 | N | Residuals restated: N2 is phone-only AFTER AM-13; N3 closed by AM-11; N5 per AM-3; N7 names Nice unless AM-4; N9 second clause removed. Added: N10 hw-volume decision (AM-17) — if the bench shows a sync PDU storm, flip and accept lost headset buttons; N11 the wedge case (AM-14) costs 2.5 s + one restart per play during a wedge; N12 a second `asound.conf` writer exists now, locked (AM-10); N13 extras' `default` semantics change (AM-15); N14 pre-existing: an AO-failure storm that finishes a SHORT queue exits 0 and wipes the bookmark (`player.py:826-827`) if it beats the 3 s poll — unchanged by the migration, closed for the "server not up" case by AM-9. |

**Phase 0 go/no-go (QA):** GO, provided NEW-2 ships with AM-14, NEW-1
ships with AM-13, and the shadow gate ships with AM-12. **Top three
before Phase 1 bench work:** AM-7, AM-1/AM-2 (+AM-8's cookie re-run),
AM-10/AM-11.

**Owner decisions embedded here (overridable):** AM-7 (cap everything
instead of refusing the local landing) and AM-17 (hw-volume on).

---

## Bench findings 2026-09-03 — container run, PipeWire 1.4.2 / WirePlumber 0.5.8

`bench/pipewire_platform_rig.sh` in a Trixie nspawn container on the
Bookworm Pi 5 (host cards, udev and BlueZ bound in; daemons as plain
processes, so S3c is a systemd fact still owed to a flash). Two runs:
without and with the linking-hook disables. Verdicts:

| Check | Result | Plan consequence |
|---|---|---|
| s3b client props | PASS | `client.conf.d stream.properties` DO reach pipewire-alsa streams (`node.dont-reconnect`/`dont-fallback` visible on the aplay node). Q3 verify tag resolved; AM-5's `PIPEWIRE_PROPS` stays belt. |
| s2a named pcm | PASS | `type pipewire` + `server` + `playback_node` → `target.object` set, linked to the pinned node. §C's template is real. |
| s1c no rescue | PASS | Target destroyed mid-stream → no link to any other sink. BLOCKER-1's mechanism is closable. |
| s2c errno | ERROR (rc=1, `xrun: prepare error: No such file or directory`) | The plugin surfaces a vanished target as a write-path ERROR, not a blocked write → §E's "advance storm" shape = today's contract; the 8 s SIGKILL path stays as belt only. |
| s1d pinned vs default | PASS | A pinned stream ignores `default.audio.sink`. |
| s1d targetless | **KILL without AM-6, PASS with it** | A targetless stream links to the default sink unless `hooks.linking.target.find-default` AND `hooks.linking.target.find-best` are `disabled`. Both names are real, and WirePlumber starts with them off (they are not hard-required). **AM-6 is REQUIRED, with these names.** |
| s1a settings | names exist | All eight `wireplumber.settings` keys in §B are real (`wpctl settings`). |
| s5 names | recorded | Resolver prop keys confirmed: `alsa.card_name` and `api.alsa.card.name` both present on ALSA sinks; node name shape `alsa_output.platform-<addr>.<card>.<profile>`. `monitor.alsa.rules update-props` works (suspend=5 landed). |
| s3a bus | PASS | The `pipewire` user can call `org.bluez` on the system bus. |
| s2d absent target | PASS (run 3) | A pcm whose `playback_node` does not exist fails at `snd_pcm_hw_params` ("Unable to install hw params") — closed at OPEN, before any link. mpv's AO init fails the same way a bluealsa pcm without a transport does today, so §C's deferred-switch refusal keeps its exact error shape. |
| S4 (BT), S3c (units) | not yet run | owed: BT speaker via the host's bluetoothctl; S3c on a Trixie flash |

**Amendments from the bench (applied to §A/§B):**

| ID | Amendment |
|---|---|
| AM-21 | **No `PIPEWIRE_CONFIG_DIR` anywhere.** It REPLACES the config search path, so `/usr/share/pipewire/pipewire.conf` is never read and the daemon fails to start ("can't load config pipewire.conf"). The §A unit drops that line; `/etc/pipewire/pipewire.conf.d/` is picked up by the default search. |
| AM-22 | **WirePlumber runs `-p main-embedded`.** 0.5.8 ships it: `main` + `mixin.systemwide-session` (no logind, no seat monitoring, no reserve-device, no portal) + `mixin.stateless` (`hooks.device.profile.state`, `hooks.device.routes.state`, `hooks.default-nodes.state`, `hooks.stream.state` all disabled). That IS §B's profile — the names §B guessed (`policy.linking.standard`, `policy.default-nodes`, `node.stream.restore`, `device.restore`) do not exist. The fragment only extends `main-embedded` with `hardware.video-capture`, `monitor.alsa-midi`, `monitor.bluez-midi` disabled, and the AM-23 hooks. `check` prints the full `provides` inventory so no name is guessed again. |
| AM-23 | **AM-6 with real names, mandatory:** `hooks.linking.target.find-default = disabled` and `hooks.linking.target.find-best = disabled` in the profile. Proven required (targetless stream lands on the default sink otherwise) and proven safe (WirePlumber starts and links pinned streams with them off). The self-test's targetless probe (B.6 assertion 1) is the boot-time guard for exactly this. |
| Note | WirePlumber logs `Failed to connect to session bus` (spa.dbus) and `telephony: failed to get session dbus connection` at every start in system mode — harmless (telephony = HFP, which roles exclude), but two journal lines per start; the install can set `DBUS_SESSION_BUS_ADDRESS=disabled:` on the unit to silence it (verify). |

---

## Consistency pass + build log — branch `pipewire`, 2026-09-03

Before code, the architect re-read the amended design as a whole (the
20 QA amendments had never had an architect rebuttal) and the bench
findings; the full pass is Appendix C. Its decisions, applied:

| ID | Decision |
|---|---|
| AM-24 | AM-7 mechanism: `audio.cap_everywhere()` = the tmpfs verdict says `fail-safety`; `local_volume(..., everywhere=)` at every cap site + the live knob clamped; `local_landing_allowed()` and the "refuse local landing" plumbing are gone. `/status.audio_policy` key-guarded like `bt_connected`. |
| AM-25 | `main-embedded` keeps default-node *election* (harmless with AM-23); probe 3 sets `default.configured.audio.sink`, never the apply hook's key; "no `default` object" is PASS. |
| AM-26 | AM-3 stands; boot readiness rests on btwatchd's nudge. The `NotAvailable`/endpoint-grace guard on `boot_fails` is OWED (not in the branch yet — bench B1 decides whether it is needed). |
| AM-27 | AM-14: the player's restart is `--no-block try-restart`, marks `note_go_restart()` for the daemon's dedup, and uses a STRICT status read (`spotify.status_strict`) — `status()` folds every OSError into {} and would have read a wedge as "no track". |
| AM-28 | AM-10 walk: `ensure_bt_route` hoisted above `daemon.py`'s `:1732` branch, file-only, non-blocking lock, one pw-dump per announce; the announce path's single reopen stays the one reopen. |
| AM-29 | AM-9: exit 75 (EX_TEMPFAIL) is never charged to the crash budget; the wait is in both spawn paths. |
| AM-30 | AM-15: `vibb_closed` pins a node that cannot exist (client-side `dont-fallback`, hook-independent), never a targetless pcm; `pcm.!default` only under pipewire, and under bluealsa only when pipewire-alsa's override file exists. |
| AM-31 | AM-17 consistent with I8; the self-test asserts the HAT gain only, never the bluez node's (it mirrors the headset). `WP_HW_VOLUME`/`WP_ROLES` are variables so bench S4 flips one place. |
| AM-32 | install.sh is written from the RIG's units/fragments (§A/§B's 20-name profile block is dead text). |
| AM-33 | The transport gate follows the stack in CODE (`audio.stack()`), not only the unit env — play.sh/ssh `bt.py` runs carry no env. Shadow cadence 1 s on bt-reconnect, 10 s on the daemon. |
| AM-34 | The post-reopen cap re-apply (set_output onto local, `_go_output_rebuild` on local) is Phase 0 too. |
| AM-35 | **S4's two open config questions are settled from the PipeWire 1.4.2 source, not the bench.** (a) `bluez5.roles` names the REMOTE device's role: `bluez5-device.c` `emit_nodes()` answers `SPA_BT_PROFILE_A2DP_SINK` with `emit_node(..., DEVICE_ID_SINK, SPA_NAME_API_BLUEZ5_A2DP_SINK, false)` — a PLAYBACK node, i.e. `bluez_output.*` — and `bluez5-dbus.c`'s `media_endpoint_to_profile()` maps our locally registered `A2DP_SOURCE_ENDPOINT` (UUID `0000110a`) to that same profile, which is the exact UUID btbus's transport gate matches. So `roles = [ a2dp_sink ]` is right (the rig's default all along; §B's `a2dp_source` was the guess), and listing only it means the `0000110b` sink endpoint is never registered — a phone cannot stream INTO the box by construction (MODERATE-3), which is what the rig's `s4_no_sink_role` checks. (b) **`bluez5.enable-hw-volume` must not be set at all** — it is a quirk-list OVERRIDE ("enable hardware volume for devices for which it is disabled"), so `true` would force absolute volume onto headsets blacklisted for mishandling it and `false` would merely decline to override. Neither the architect's `false` nor AM-17's `true` was right. 1.4.2's `DEFAULT_HW_VOLUME_PROFILES` already contains `SPA_BT_PROFILE_A2DP_SINK`, so the kid's headset keeps its own volume buttons by default, quirky devices stay protected, and I8 still holds (vibb never writes a node volume). `WP_HW_VOLUME` is gone; `WP_ROLES` stays as a knob. |
| AM-57 | **AM-35(a) was WRONG — field 2026-09-05, first headset on the Zero.** bluetoothd: `a2dp-sink profile connect failed for 2C:FD:...: Protocol not available` = no local A2DP *Source* endpoint registered. `bluez5-dbus.c` (1.4.2) registers endpoints through `endpoint_should_be_registered()`, gated on `get_codec_profile(codec, direction) & enabled_profiles`; for direction `SPA_BT_MEDIA_SOURCE` (we feed the headphone) that is `SPA_BT_PROFILE_A2DP_SOURCE`, the bit set by the role name **`a2dp_source`**. `media_endpoint_to_profile()` — the function AM-35 read — names the *transport* after the remote role and plays no part in registration. So `bluez5.roles = [ a2dp_source ]` (§B's original guess) is right; the rig default, AM-35's prose and the test pins are corrected. Listing only it still registers no `0000110b` sink endpoint (MODERATE-3 holds). btbus's transport gate keeps matching UUID `0000110a`: that is our source endpoint's UUID on the transport. AM-35(b) (hw-volume) stands. Also: a changed 50-vibb.conf now `try-restart`s wireplumber in apply — the fragment is read at start only. |

**Commits on `pipewire` (after `96673e1`), each with the suite green and
the production box (stack unset = bluealsa) behaviourally unchanged:**

| # | Commit | Phase 0 / cherry-pick to main |
|---|---|---|
| 1 | btbus: MediaTransport1 gate in shadow mode (+ fake_bluezd transports) | yes |
| 2 | audio.py: the resolver (stack, pw_dump, sinks by address/card, asound text) | yes (inert) |
| 3 | player: NEW-1 cap before /player/play | yes |
| 4 | daemon: cap re-applied after a reopen onto local | yes |
| 5 | player: NEW-2 confirm-paused + wedge restart; `status_strict` | yes |
| 6 | btbus: the gate follows the stack; shadow cadence | yes (inert) |
| 7 | output/btwatchd: readiness = transport AND node under pipewire | pipewire-only branch |
| 8 | bt: route from the graph, per-pid tmp, `route` verb, recover units | pipewire-only branch |
| 9 | daemon/audio: `ensure_bt_route` on the announce; node-gated switches | pipewire-only branch |
| 10 | player/daemon: sink wait exit 75, healer never charges it | pipewire-only branch |
| 11 | install: `pi/audio-stack.sh` — the toggle, both directions | inert until `VIBB_AUDIO_STACK=pipewire` |
| 12 | install/extra: wiring; ALSA default fails closed | inert |
| 13 | audio: the policy self-test | inert |
| 14 | daemon: self-test at boot / after recovery / on request; `/status.audio_policy` | inert |
| 15 | cap everywhere on `fail-safety` (AM-7) | inert |
| 16 | tests: I2, I4, I7, I8 pins + docstrings | tests |

Pins in the tree: I1 `local_volume_all_paths`, I2 `audio_client_props`,
I3 `bt_lost_pause_recover` (unchanged), I4 `output_file_single_writer`,
I5 `install_unit_order`, I6 `bt_transport_gate`, I7
`wp_policy_fragment`, I8 `no_volume_writer`, I9 `spotify_pause_confirm`,
I10 `audio_policy_selftest` + `audio_policy_daemon`, I11
`stop_child_group_kill` (unchanged), I12 `audio_route_discovery`, I13
`audio_stack_toggle`; AM-18's four: `player_sink_wait_fail_closed`,
`spotify_volume_before_play`, `asound_second_writer`,
`spotify_pause_confirm` (wedge case). The live half of
`bt_transport_gate` (fake_bluezd over a private bus) runs on the rig.

**Owed before the spare-Zero field test (Phase 2 entry):**
- bench S4 with the headset — now a CONFIRMATION, not a decision
  (AM-35 settled roles and hw-volume from the source): codec sbc,
  suspend 120, no `0000110b` transport, headset buttons audible; S3c on the flash
  itself (AM-1/AM-2 are systemd facts); the HAT node's real name
  (informational — the resolver reads it); s6 go-librespot live reopen
  through pipewire-alsa; `/player/volume` with no track loaded.
- AM-26's boot-fail grace in btwatchd if B1 shows `NotAvailable`
  failures during WirePlumber's init.
- the screen line for `fail-safety` (ui.py; mockup first per the UI
  rule) — the PWA can read `/status.audio_policy` today.
- the 7-day baseline on the production box (QA §4) before any cutover
  there; the spare Zero needs no baseline.

## Toward `--soloist` (Phase 3) — the sequence, 2026-09-04

The engine toggle has the same shape as the audio-stack toggle, and the
plan's premise (PLAN-soloistd.md decision 1) was that the daemon is
already engine-blind. Half true: the REST half was (`VIBB_GO_API`, the
dialect-preserving sidecar), the UNIT-NAME half was not — `go-librespot`
sat hardcoded in 14 systemctl calls across five files, so a swap would
have restarted the wrong process. Done now, in this order:

| Step | State | What it is |
|---|---|---|
| 1 | **done** | `paths.go_unit_cmd()` / `VIBB_GO_UNIT`: every systemctl call on the engine goes through one builder. `spotify_engine_seam.py` pins that no literal is left and that the play_origin VALUE `"go-librespot"` (the phone-clobber guard, not a unit) survived. |
| 2 | **done** | `install.sh --librespot|--soloist` (and `--bluealsa|--pipewire`, `--help`); the choice is remembered in `/etc/vibb/spotify-engine`. `--soloist` refuses EARLY, before anything is touched, and says what is missing. |
| 3 | **done** | `pi/spotify-engine.sh`, the twin of `audio-stack.sh` (AM-53 shape): `spotify_engine_resolve` refuses soloist EARLY unless `audio_stack_peek` says pipewire AND `pi/soloistd.py` exists, writing nothing; `spotify_engine_apply` installs the sidecar, writes `vibb-soloistd.service` as `$RUN_USER` (`EnvironmentFile=-/etc/vibb/soloist.env`, `StateDirectory`/`CacheDirectory` — Soloist honours both natively, `Restart=on-failure` never always, the pipewire client props), MASKS go-librespot, enables the sidecar, and records the engine LAST; `--librespot` disables+masks the sidecar, unmasks go-librespot, never touches `config.yml`. Env (`VIBB_GO_API=http://127.0.0.1:3688`, `VIBB_GO_UNIT`) on daemon, bt-reconnect, idle, buttons, rfid; `VIBB_GO_CONFIG` only under golibrespot; `extra.sh` derives the engine unit from `/etc/vibb/spotify-engine`. The soloist BINARY download is the updater's job (D1), not install.sh's. Pinned by `spotify_engine_toggle.py` (both directions, refusals write nothing, failed apply records nothing), `spotify_engine_seam.py`, `install_unit_order.py` 7, `extras_wrapper.py` 8. |
| 4 | **4a+4b+4c done**, 4d owed | `pi/soloistd.py` (650 lines): supervises the child (`-n -k -D -C -z -w 127.0.0.1:0 -d <node>`; exit 10 LATCHES in a persisted file, never respawned; other exits restart with bounded backoff), the hand-rolled RFC6455 client, an event-fed mirror with a SEQUENCED event log (a single last-event slot lost every `command_result` to the state event that follows it), the dialect (`/status` with `username` synthesized from `auth_state.device_name`, `spotify_state`, box/remote `play_origin` from the box-started context; `/player/*` 1:1; `/context/tracks` from `get_queue` for the ACTIVE context, autoplay rows dropped; `/cache/*` 404), the resume walk under the volume shroud, `/soloist/health` with days_left from the child's own expiry line. Driven for real by `tests/soloist_sidecar.py`: a stdlib fake Soloist (RFC6455 server scripted from the contract) + a fake child binary; six cases incl. exit-10 latch persistence and crash backoff. 4c: the AM-16 bind check — informational at start, AUTHORITATIVE whenever audio is playing and the binding is unproven (the stream may be lazy); a stream linked anywhere but the pinned node is paused, killed, `audio-unbound` (case 7, fake pw-dump on PATH). Owed: 4d warming (after the bench's skip-cancels-fetch fact); the warm-time null sink is just `/player/output {device: vibb_null}` by construction. |
| 5 | **5a+5b+5c done** | 5a: `POST /soloist/configure` (token + JSON gates; `soloist.env` KEY=VALUE 0600; unit restarted `--no-block`; empty key removes; 409 under go-librespot; never logged), `/status.spotify_state`, `play()` fast-fails `spotify-<state>` on needs-key/needs-pair/expired/bad-key/audio-unbound, the PWA key form with the local confirm, soloistd's bad-key net (exact line bench-owed). 5b: `POST /soloist/pair` (daemon proxy, token-gated) → the sidecar stops its child, runs `soloist --pair` on the same data dir, starts it again; the PWA's button shows on `needs-pair`. 5c: `vibb/soloist_update.py` (zero-byte ETag check, resumable Range fetch, FULL-OBJECT crc32c verified, `--version` exec-sanity, atomic swap, `.prev`), `vibb-soloist-update.service` + a MONOTONIC timer (AM-50), the idle hook's second slot after the backup (180 + 120 < 600), and the sidecar's `/soloist/updated`: latch dropped, restart when idle, deferred while playing, never on the way down (AM-52). Owed: the SCREEN popup for the engine states (ui.py, mockup first). |

Steps 3-5 have two hard prerequisites outside the repo: the box on the
pipewire stack (Soloist has no ALSA backend), and a personal Soloist
API key from a Premium account (the son's family-member account
qualifies). Step 4 needs the owner's green light and the bench with a
paired Soloist before it starts — it is the one piece that cannot be
unit-tested into existence.

### Phase 3 decisions (owner, 2026-09-04) — three things the plan had, moved

**D1. The updater rides the idle-shutdown hook, like the backup — and
the CDN gives us a free, zero-byte check.** Verified against
`https://soloist-builds.spotifycdn.com/soloist_release_arm64.tar.gz`
on 2026-09-04: a fixed URL (no versioned/"latest" variants, no
manifest, no signature — the docs say so), but the CDN answers `HEAD`
with `etag`, `last-modified`, `content-length` (12.8 MB),
`accept-ranges: bytes` and an `x-amz-checksum-crc32c`, and honours
`If-None-Match` (a wrong ETag + a 1-byte range returned 206; the stored
ETag returns 304). So the check is one round trip with no body:
`GET If-None-Match: <stored etag>` → 304 = nothing new. The build was
dated the same morning it was probed: Spotify rebuilds often, so
"latest" is always fresh and the 90-day fuse is a non-event for a box
that updates. Design:
- **Trigger:** the idle-shutdown hook (`idle.py`, the backup's slot:
  nothing plays, the radio is free, the run is bounded by
  `BACKUP_MAX_S`-style patience) PLUS the weekly timer as the safety
  net for boxes that are never shut down cleanly. Both call one
  `soloistd.update()`; both skip whenever `_audible_now`, and on ANY
  wifi otherwise — hotspot included (owner, 2026-09-04: the box does not
  ration the owner's data; 12.8 MB once per build change).
- **When:** the CHECK whenever the hook/timer fires (free); the DOWNLOAD
  only when the installed build nears expiry (AM-55 — superseding the
  "whenever the ETag changed" first draft). Expiry is read from
  the child's own startup line ("client expires in N days", FIELD
  FINDING #3), surfaced as `/soloist/health.days_left`; below 21 days
  with no successful update the screen warns, below 7 it is red, and
  an exit-10 child becomes the clear "Spotify trenger oppdatering"
  state — never a silent box.
- **How:** download to a tmp under the binary's dir, verify the
  `x-amz-checksum-crc32c` against the file (not a signature, but it
  catches every truncated or corrupted download, which "TLS +
  exec-sanity" did not), run `--version` on the new file (exec-sanity
  + the build timestamp), `os.replace`, store the ETag, restart the
  child only if it is idle (the plan's "defer swap while playing").
  Never `apt`-style pinning, never a rollback copy beyond the previous
  binary kept as `soloist.prev` for one cycle.
- **Redistribution:** the box downloads for itself from Spotify's URL;
  the repo never ships the archive (the docs forbid it).

**D2. The API key is entered in the PWA, not at install time.**
`--soloist` provisions units and config with NO key; soloistd starts
and sits in a clear `needs-key` state (screen + `/status`, the bedtime
rule) until `POST /soloist/configure {api_key}` — the backup page's
pattern: token-gated, local confirm in the PWA before submit, written
`0600` under `/etc/vibb/soloist.json`, SECRET tier of the backup
whitelist next to `storytel.json`/`spotify-api.json`. The child is
then started with `-k` from that file; the key never appears in a unit
file, an argv visible to `ps`, or a log line (the docs: "treat as
secret" — so it goes in via an env file the unit reads, `EnvironmentFile=`,
and the child gets `-k "$SOLOIST_API_KEY"` from a wrapper, not from
the daemon's argv). Rotating it = the same POST; removing it = the
`needs-key` state again. Pairing (`--pair`) stays a separate PWA step
after the key.

**D3. Cache warming fires on library ADD, gated on the radio, not on
the charger.** The plan gated warming on charger + idle + home wifi.
The owner's call: the moment a Spotify entry is saved in the PWA, warm
it — the bench proved whole-file caching from ~2 s of play (91 MB from
30 s of a 157-min episode), so a playlist warms in minutes, and the
kid's next tap is then radio-free. Gates that STAY, because they are
the same invariants the rest of the box lives by:
- **never during active A2DP playback** — the shared-radio rule every
  guard in `radio.py`/`_audible_now`/the sweep's `BUSY_CHECK` enforces;
  a warm that starts while the box is idle ABORTS the instant a play
  starts (any button/tap/card), exactly like the sweep yields;
- **any wifi counts, the hotspot included** (owner, 2026-09-04: "er den
  på wifi, så skal den jobbe med cachen, uansett"). The earlier
  home-wifi-only gate is dropped: a car trip on the phone's hotspot is
  exactly when the next tap should already be cached, and the owner
  owns that data budget, not the box. The updater follows the same
  rule (D1).
- **silent by construction** — the child is retargeted to `vibb_null`
  for the warm (§I), volume-0 as belt; the HAT and BT nodes stay
  `suspended` (bench B9 asserts it);
- **one engine, one session** — warming cannot overlap the kid's
  Spotify playback (same child), and while the kid plays a podcast on
  the HAT (no A2DP) warming may proceed: PipeWire mixes, the null sink
  is a separate node, and wifi is free.
Dropped from the gate: charger, idle, and the wifi kind. The trigger is the library
write (`/library` save → the sweeper's existing wake, a new branch for
soloist-routed entries with `cache: N`), plus the sweep's normal pass
for anything missed (offline at add time, aborted by a play).
Play-history pollution stays accepted (it is the kid's content on the
kid's account). The mechanism per item: `play uri` → wait for the first
`track_changed` and ~2 s → `skip_next` … until the listing is
exhausted, with the resume-walk's volume shroud; the cache canary's
ledger records what was warmed so a swap (D1) can re-warm the same
list if the cache turns out to be voided by a build change.

### QA pass on D1–D3 (2026-09-04) — amendments applied

The full pass is Appendix D. What it changed, by ID:

| ID | Amendment |
|---|---|
| AM-36 | **The warm is INVISIBLE in the dialect.** While warming, soloistd's `/status` returns the frozen pre-warm snapshot (track, paused/stopped, position, `play_origin` unchanged); the warm shows only on `/soloist/health.warming`. Otherwise `_spotify_bookmarker` writes the warm's track under the KID's context (bookmark poison: the next tap walks for a uri that is not in that context) and — with any non-box origin value — `_arbiter` reads the warm as a phone takeover and stops the podcast. A warm origin cannot be a `play_origin` value; both consumers treat non-box as phone. |
| AM-37 | **The daemon's sweeper drives the warm, not the sidecar.** `/run/vibb-radio-busy`/paging markers are root-owned and soloistd runs as `$RUN_USER`, so a sidecar "yielding like the sweep" silently no-ops. The sweeper already wakes on `/library` save (`_sync_wake`), coalesces bursts, re-checks `_busy()` per entry and POSTs `/cache/download {uri}` — under soloist that POST IS the warm, and the sweeper touches BUSY every ≤10 s while it runs and POSTs an abort when audible flips. `wait_paging_clear()` before the first play. |
| AM-38 | **Gate = `not (_audible_now() and _streaming_now())`**, not "no A2DP". A CACHED podcast on the HAT may run under a warm; a STREAMED one may not (the warm fetches at ~250× realtime; that is the "pausing a song a two-minute fight" load). Connected-but-idle headset with no audio is allowed (the crash memory ties no crash to idle-ACL + wifi). |
| AM-39 | **Paging under a warm.** With the speaker absent btwatchd's 120 s starvation belt pages through any warm — "BT paging + wifi", the thrice-observed flap. A `vibb-warming` marker suspends the belt (a page during a warm has nothing to gain); the marker is bounded like the warm. |
| AM-40 | **Paused ≥ 10 min before a session is warm-eligible.** A warm over a loaded-but-paused kid session turns the next unpause into a resume walk (seconds, shroud race) instead of a 60 ms resume — the backup's own "resumes any second" rule. |
| AM-41 | **Stop condition = autoplay.** After each `track_changed`, `get_queue limit=1`; stop when the item's `source == "autoplay"` or `upcoming` is empty — otherwise a 30-track playlist warms Spotify's radio forever. Not in the plan before; the field is in the docs. |
| AM-42 | **Bounds.** `cache: N` keeps its podcast meaning ("newest N") for soloist entries — a show is 50–150 MB per episode and would LRU-evict the kid's own warm; a byte budget per pass from `spotify_cache_gb`; pacing ≥ 3 s/item, ≤ 100 items per pass, the rest at the next sweep; ≤ 20 min per pass, smaller on battery (`plugged_cached()`). |
| AM-43 | **Idempotence from a LEDGER, not `/cache/snapshot`.** `_precache_due` fails OPEN when `GET /cache/snapshot` is absent → every sweep and every PWA save would re-walk every playlist. soloistd answers `/cache/download` idempotently from its own ledger (warmed uris + count + build id + "aborted at index k"); `/cache/snapshot` stays 404. The ledger also drives the re-warm after a binary swap (D1). |
| AM-44 | **idle.py hold is BOUNDED.** An invisible warm reads as idle → poweroff kills it mid-walk; a visible one holds the box awake forever. `/status.warming: true` while it runs, and idle.py treats it like `ssh_active`: a hold with a hard release at the pass budget. |
| AM-45 | **`cache` default under soloist.** Today `cache` defaults to 0 (opt-in per entry). Owner's stated intent ("warm the moment I add something") → `normalize_library` defaults Spotify entries to `cache: 1` when the engine file says soloist; the PWA toggle still turns it off per entry. |
| AM-46 | **D2 wording corrected.** Soloist takes the key ONLY as `-k` (no env var, no file: the CLI reference), so it IS visible in the CHILD's `/proc/<pid>/cmdline` to any local login for the process's lifetime — the same class as go-librespot's `credentials.json` being readable by `$RUN_USER`. Honest statement: never in a unit file, the journal, or the daemon's argv. Mitigation on the box: `hidepid=invisible` on `/proc` (a mount drop-in). The file is `soloist.env` (`KEY=VALUE`, for `EnvironmentFile=`), not `.json`. |
| AM-47 | **Key validation is asynchronous** (the unit reads `EnvironmentFile` at start): write prev, write new, restart the unit, answer 202; the PWA polls `/soloist/health`, which must tell three "no"s apart from the child's own output — exit 10 → `expired`; the auth-failure line → `bad-key` (restore prev, restart again); no network → `offline` (keep the key). Which log line means bad-key is NOT in the docs: bench it once with a mangled key. |
| AM-48 | **`spotify_state` string in `/status`** (`ok|offline|needs-key|needs-pair|expired|audio-unbound`), the bool kept for the PWA; `play()` fast-fails on any non-ok state (`{"error": "spotify-needs-key"}` — the UI already routes that class) instead of spawning a player that waits 30 s for `username` and exits; the popup renders when the TAPPED source is spotify, not only the current one. And the sidecar's `/status` synthesizes `username` (Soloist's `auth_state` has none) or `play_spotify` dies at its session wait every time. |
| AM-49 | **Backup:** `VIBB_SOLOIST_ENV` (the key) and `VIBB_SOLOIST_DATA` (the `--data-dir`: session, ws files) into the SECRET tier via env paths like the rest; the `-C` cache dir excluded. A restored session is "probably valid"; `needs-pair` is the graceful failure on a new box. |
| AM-50 | **Updater shares the shutdown slot in order:** backup first (irreplaceable), then the updater, each with its own unit and budget, sum < the 600 s marker window; download resumable with `Range:` (the CDN advertises `accept-ranges`); stale `*.tmp` deleted first; a tmp is trusted only with its stored ETag+length. Timer: MONOTONIC (`OnBootSec=15min` + `OnUnitActiveSec=6h`) like the backup — a persisted calendar timer fires at boot on the RTC-less bogus clock, inside the radio storm, with TLS failing against 1970; gated on `clock_trusted()` and `_audible_now`. |
| AM-51 | **crc32c settled — full-object, and the code is 15 lines.** Downloaded the 12.8 MB archive once (2026-09-04): a table-driven CRC-32C (reflected 0x82F63B78, check value 0xE3069283) over the whole file equals `x-amz-checksum-crc32c` exactly (`IrqQuw==`), so it is NOT a composite multipart checksum; 1.6 s on the dev box, ~15 s on a Zero, run nice-19. Archive members: `soloist`, `CHANGELOG.md`, `THIRD_PARTY_LICENSES.txt`. |
| AM-52 | **"Idle" for the post-update restart** = no track OR paused ≥ 10 min, AND not audible, AND no hands on the box; a paused session may be restarted because the bookmarker flushed on pause and `play()`'s resume falls through to the bookmark; mark with `note_go_restart()`; never on the poweroff path; if the new child fails N starts, swap `soloist.prev` back automatically. |
| AM-53 | **Engine toggle wiring:** a read-only `audio_stack_peek` (env > file > bluealsa, no write) for the refuse; the engine file written AFTER `audio_stack_apply` succeeds; `VIBB_GO_CONFIG` UNSET on the daemon under soloist (else the supervisor's zeroconf lock rewrites go-librespot's config and restarts the soloist unit every tick); `VIBB_GO_API`/`VIBB_GO_UNIT` on daemon, bt-reconnect, idle, buttons, rfid; `extra.sh` derives the engine unit from `/etc/vibb/spotify-engine`; the sidecar PERSISTS the exit-10 latch (the supervisor's park/unpark starts and stops the unit); the seam pin's allowlist becomes `{install.sh, spotify-engine.sh}`. |
| AM-55 | **Download only near expiry (owner, 2026-09-04).** D1 said "whenever the ETag changed"; Spotify rebuilds roughly daily (bygg `20260901` on the bench, the CDN archive dated 4 Sep 06:09 the same morning it was probed), so that pulled 12.8 MB most days for functionally identical builds. Now: the CHECK stays free and frequent (idle-shutdown + every 6 h), the DOWNLOAD happens only when the installed build has ≤ `VIBB_SOLOIST_UPDATE_DAYS_LEFT` (30) days left per the sidecar's `/soloist/health.days_left`, or is expired/latched, or its expiry is unknown, or `--force`. Otherwise the run records what is available (`state.available`) and answers `not-yet`. Net: one download per ~60 days, whatever Spotify's cadence. |
| AM-56 | **Cadence ~30 days, and an expired build kicks the download NOW (owner, 2026-09-04).** The threshold is 60 days left, i.e. the installed build is ~30 days old: a fresh build about monthly, which also keeps ≥ 60 days of margin for a long offline stretch (~12 × 12.8 MB a year). And an EXPIRED build no longer waits for the next shutdown or the 6 h timer: the daemon's `_kick_soloist_update()` starts the updater unit `--no-block` the moment anyone notices — a tap on a Spotify card (`play()`'s `spotify-expired` branch), the supervisor's tick after a boot onto an expired build, or the PWA's "Update Spotify now" (`POST /soloist/update`) — once per 10-minute cooldown; the unit's own gates still apply, and the sidecar drops the latch and restarts the child (idle, since nothing plays). |
| AM-54 | **Volume shroud recovery:** a sidecar dying mid-walk leaves Connect volume at 0; `_apply_box_volume` heals on the next tap, and the sidecar restores on its own start. |

### Branch state, 2026-09-04 evening (36 commits over `main`, suite 176 files green)

> **2026-09-05 (first Zero install, AM-57):** `--pipewire --soloist` on a fresh Trixie Lite died twice in install.sh: `BA_UNIT: unbound variable` (bluealsa-only block read outside its guard under `set -u`), and the final `enable --now` named the masked go-librespot. Both fixed and pinned. Same run showed Debian enabling `bluealsa-aplay` (phone→box player) and PipeWire's *user-session* units; `_as_mask_idle_units` now masks bluealsa-aplay on both stacks and `--global` masks the session PipeWire/WirePlumber whenever the packages exist, so an ssh login never starts a second WirePlumber against BlueZ.

Everything in steps 3–5 except 4d (warming) and the screen popup is in
the tree and pinned. What is left, by who can answer it:

| Owed | Who |
|---|---|
| 4d warming (AM-36..45): the daemon's sweeper drives `/cache/download` as an idempotent ledger walk, invisible in `/status`, autoplay stop, `cache: N` bound | after the bench fact below |
| the SCREEN popup for `spotify_state` (ui.py) | mockup first, per the UI rule |
| `hidepid=invisible` on `/proc` (AM-46's mitigation for the `-k` argv) | install.sh, one mount drop-in; decide with the owner |
| AM-26 boot-fail grace in btwatchd | only if bench B1 shows `NotAvailable` during WirePlumber's init |
| bench: does `skip_next` cancel the in-flight fetch; is a truncated file served partial or re-fetched | the Pi 5 with a paired Soloist (two lines in `soloist_spike.py`) |
| bench: which child log line means bad-key (AM-47); the order of `get_queue.previous` (assumed most-recent-first) | same session |
| bench S4 (codec/suspend/no-110b/headset buttons), S3c on a flash, go-librespot's live reopen through pipewire-alsa (s6) | the spare Zero on Trixie Lite |
| ~~the HAT node's name~~ — SETTLED from the kernel source 2026-09-04: Pimoroni's README says only `dtoverlay=hifiberry-dac` (nothing on card names or PipeWire); the overlay's `hifiberry,hifiberry-dac` binds `rpi-simple-soundcard.c`, whose table sets `.card_name = "snd_rpi_hifiberry_dac"` (driver_name `RPi-simple`, DAI `HifiBerry DAC`). ALSA's id `sndrpihifiberry` is that name stripped; PipeWire's `alsa.card_name` is that name verbatim, so `find_local_sink`'s `hifiberry` match is guaranteed. The full node name is **confirmed on the Zero 2026-09-05: `alsa_output.platform-soc_sound.stereo-fallback`**, card `snd_rpi_hifiberry_dac` (`sndrpihifiberry`). | closed |

**Owed to the bench before warming is coded (QA item 5):** does a
`skip_next`/`pause` CANCEL the in-flight fetch (then "2 s per item" is
too short and the ledger marks an item warm only when its cache file
stops growing), and is a truncated file served partial on replay or
re-fetched? Two additions to `bench/soloist_spike.py`.

**Go/no-go (QA):** GO for step 3 now; the sidecar's core (supervision,
persisted latch, health states, WS client, dialect `/status` and
`/player/*`, the walk, the binding) may be written against the fake WS
server now; warming waits for the bench fact above.

---

# PART 1 — Architect design (2026-09-02), with QA-2 marks

Grounded in tree `33af425`. Every PipeWire/WirePlumber key I am not certain of on Trixie (PipeWire 1.4.x, WirePlumber 0.5.x) is tagged **(verify on bench)**; the bench step that verifies it is named. Nothing in this document modifies the repo.

Design stance in one line: **keep every field-hardened vibb mechanism exactly where it is, and make PipeWire look like ALSA to it.** The migration replaces the *transport owner* (bluealsa → PipeWire/bluez5) and the *pcm slave* (`type bluealsa`/`hw:` → `type pipewire` with a pinned node), and nothing else. The previous architect's ~415 deleted lines came from going native (dropping `reopen_go_output`, `_go_output_rebuild`, half of `set_output`); this design deletes none of that until Phase 4 and even then only bluealsa-specific lines, which is why the test damage shrinks from "5 rewritten + 17 swapped" to "0 rewritten, 4 fixture-swapped, ~9 new".

---

## A. Topology: system-wide PipeWire + WirePlumber

> **[QA-2: AM-1, AM-2, AM-3, AM-4, AM-5]** — see the amendments table above; where this section's text conflicts, the amendment wins.

**Decision: system-wide, three hand-written units, one socket, no user session.** Justification against SEVERE-4 / QA Q1:

- vibbd is root with no `User=` (`pi/install.sh:1154-1158`) and spawns `player.py` → mpv with inherited env (`pi/daemon.py:789`, `pi/player.py:545`); go-librespot is `User=$RUN_USER` (`pi/install.sh:542`); soloistd will be `$RUN_USER`. A lingering `user@<uid>` session starts after logind, i.e. ~10-20 s later than `bluetooth.service`, which is exactly the SEVERE-5 regression; and a system unit cannot `After=` a user unit, so `vibb-bt-reconnect.service` (`pi/install.sh:626-641`) could never be ordered behind the endpoint owner. System-wide gives one socket, root ordering control, and the "unsupported configuration" cost is three unit files — install.sh already owns eight.
- No `restore-*` state problem in system mode because restore is disabled (B.3) and WirePlumber gets an explicit `XDG_STATE_HOME`.

### Units (written by install.sh under `VIBB_AUDIO_STACK=pipewire`)

`/etc/systemd/system/pipewire.socket`
```ini
[Unit]
Description=Vibb PipeWire (system) socket
[Socket]
Priority=6
ListenStream=/run/pipewire/pipewire-0
SocketUser=pipewire
SocketGroup=audio
SocketMode=0660
[Install]
WantedBy=sockets.target
```

`/etc/systemd/system/pipewire.service`
```ini
[Unit]
Description=Vibb PipeWire (system-wide, owns the I2S HAT + BT A2DP)
Requires=pipewire.socket
After=pipewire.socket
[Service]
Type=simple
User=pipewire
Group=audio
SupplementaryGroups=bluetooth
RuntimeDirectory=pipewire
RuntimeDirectoryMode=0750
Environment=PIPEWIRE_RUNTIME_DIR=/run/pipewire
Environment=PIPEWIRE_CONFIG_DIR=/etc/pipewire
Environment=HOME=/var/lib/vibb/pipewire
StateDirectory=vibb/pipewire
ExecStart=/usr/bin/pipewire
Nice=-11
# RT: deliberately NOT granted (Q13). Escalation = add LimitRTPRIO=95 here.
LimitMEMLOCK=64M
Restart=on-failure
RestartSec=2
[Install]
WantedBy=multi-user.target
Also=pipewire.socket
```

`/etc/systemd/system/wireplumber.service`
```ini
[Unit]
Description=Vibb WirePlumber (system-wide session manager, bluez5 + alsa only)
Requires=pipewire.service dbus.service
After=pipewire.service dbus.service
[Service]
Type=simple
User=pipewire
Group=audio
SupplementaryGroups=bluetooth
Environment=PIPEWIRE_RUNTIME_DIR=/run/pipewire
Environment=XDG_STATE_HOME=/var/lib/vibb/wireplumber
Environment=XDG_CONFIG_HOME=/etc/vibb/wp-empty-config
Environment=HOME=/var/lib/vibb/wireplumber
StateDirectory=vibb/wireplumber
ExecStart=/usr/bin/wireplumber
Nice=-11
Restart=on-failure
RestartSec=2
[Install]
WantedBy=multi-user.target
```

User: `useradd -r -M -d /var/lib/vibb/pipewire -s /usr/sbin/nologin -g audio -G bluetooth pipewire`. Group `bluetooth` is the same reason install.sh gives `$RUN_USER` that group (`pi/install.sh:437`): BlueZ's D-Bus policy. **(verify on bench: `busctl --system --user pipewire call org.bluez / org.freedesktop.DBus.ObjectManager GetManagedObjects` succeeds; on Debian the default-context policy already allows `send_destination=org.bluez`, so this is belt.)**

### Env plumbing (who reaches the socket, and how)

Three layers so that no client class can be "the one that was missed" (prior §3.5):

1. **The pcm carries the socket** — pipewire-alsa's `type pipewire` pcm takes a `server` key **(verify on bench: key name `server` in `pcm_pipewire.c`)**. `vibb_bt`/`vibb_local` in `/etc/asound.conf` name `/run/pipewire/pipewire-0` explicitly. This covers root mpv, `$RUN_USER` go-librespot (`audio_backend: alsa`, `pi/install.sh:268`), `aplay -D vibb_bt` in `pi/play.sh:99`, and any extra — with **zero** env.
2. **Native clients** (soloist, `pw-dump`, `pw-cat`, `wpctl`): `/etc/pipewire/client.conf.d/10-vibb.conf` with `context.properties = { remote.name = "/run/pipewire/pipewire-0" }` **(verify on bench: absolute path accepted as `remote.name`; fallback is `PIPEWIRE_REMOTE=/run/pipewire/pipewire-0`)**.
3. **Belt:** `Environment=PIPEWIRE_RUNTIME_DIR=/run/pipewire` added to `vibb-daemon.service` (inherited by player.py → mpv → pw-dump forks), `go-librespot.service`, `vibb-soloistd.service`. The self-test (B.6) asserts `pw-dump` works from vibbd's own environment.

Permissions: root bypasses; `$RUN_USER` is already in `audio` (`pi/install.sh:437`); the socket is `audio:0660`, `/run/pipewire` is `pipewire:audio 0750`.

### RT (Q13)

**No realtime in Phase 2.** The `pipewire.service` unit grants no `LimitRTPRIO`, there is no rtkit on the box, so the distro's `libpipewire-module-rt` (loaded `ifexists nofail`) degrades to a warning; `Nice=-11` on the unit gives the same scheduling class bluealsa has today (SCHED_OTHER). This holds the open scheduling variable (C13) as still as possible. B6's xrun count under the 25 s scan barrage is the decision input: if xruns > 2× the bluealsa baseline, the escalation is one line (`LimitRTPRIO=95` + `LimitRTTIME=200000`), re-measured as B6-RT. Both numbers go in the bench note.

### pipewire-pulse (Q12)

**Deliberately absent.** Packages installed: `pipewire pipewire-bin pipewire-alsa wireplumber libspa-0.2-bluetooth` with `--no-install-recommends` (`pi/install.sh:130`), never the `pipewire-audio`/`pipewire-pulse` metapackages. Consequence: Soloist's documented "falls back to PulseAudio" path has no server to fall back to, so a broken PipeWire target fails closed (B9). `pactl`/`paplay` are not on the box; `bench/pipewire_shim_rig.sh pulse` stays bench-only.

### Boot ordering (SEVERE-5, I5)

The endpoint owner is WirePlumber. To keep the A2DP endpoint as early as bluealsa's (which was `After=bluetooth.service` and up ~1 s after bluetoothd), the design orders **bluetoothd after WirePlumber**, not the reverse:

`/etc/systemd/system/bluetooth.service.d/vibb-after-wp.conf`
```ini
[Unit]
After=wireplumber.service
```
Ordering only (no `Requires=`): a failed WirePlumber never blocks bluetoothd. The rfkill drop-in (`pi/install.sh:446-453`) is untouched and still runs as bluetoothd's `ExecStartPre`. Cost: bluetoothd starts ~2-4 s later than today (WirePlumber's init on a Zero 2 W) — still an order of magnitude inside the 25-40 s deafness the rfkill fix removed. Benefit: I5 holds *by construction* — the radio never listens without an endpoint, so `boot_fails` (`pi/btwatchd.py:121-123`, `:707-724`) cannot burn on "endpoint not yet registered". `vibb-bt-reconnect.service` `After=` becomes `bluetooth.service wireplumber.service` (replacing `bluealsa.service bluealsad.service`, `pi/install.sh:629`). `pipewire.socket` is at `sockets.target`, so any client that connects before `pipewire.service` is up simply queues. vibbd stays unordered at basic.target (`pi/install.sh:1146-1152`); vibb-ui keeps `DefaultDependencies=no`. B1 measures the time-to-transport delta; PASS is baseline + 5 s.

---

## B. WirePlumber policy as code

> **[QA-2: AM-6, AM-7 (failure action replaced), AM-8, AM-9]** — see the amendments table above; where this section's text conflicts, the amendment wins.

**Decision (Q7): a custom whitelist profile PLUS explicit settings PLUS per-stream properties.** Three independent layers because the safety property is enforced by config in someone else's package (BLOCKER-1): a profile that never loads the "find another sink" hooks; settings that turn moving/following off even if a future package re-enables the hooks; and stream properties that tell the linking policy "never fall back, never reconnect" even if both of the above drift. The self-test (B.6) asserts the *behaviour*, not the file.

One fragment, `/etc/wireplumber/wireplumber.conf.d/50-vibb.conf` (WirePlumber 0.5 SPA-JSON):

```
# --- 1. profile: load only what a two-sink appliance needs ------------------
wireplumber.profiles = {
  main = {
    hardware.audio                 = required
    hardware.bluetooth             = required
    monitor.alsa                   = required
    monitor.bluez                  = required
    policy.linking.standard        = required   # find-defined-target + link
    policy.device.profile          = required   # picks the a2dp-sink profile
    support.settings               = required
    support.dbus                   = required
    # never in system mode / never on this box
    monitor.alsa.reserve-device    = disabled   # session-bus ReserveDevice1
    monitor.bluez.seat-monitoring  = disabled   # logind seats
    monitor.alsa-midi              = disabled
    monitor.bluez-midi             = disabled
    monitor.libcamera              = disabled
    monitor.v4l2                   = disabled
    hardware.video-capture         = disabled
    # the opinions (verify component names on bench: `wireplumber --list-components`
    # or /usr/share/wireplumber/wireplumber.conf 'wireplumber.components')
    policy.default-nodes           = disabled   # no default-sink election
    node.stream.restore            = disabled   # restore-stream
    device.restore                 = disabled   # restore-device (profiles/routes)
    policy.linking.role-based      = disabled
  }
}

# --- 2. settings belt (verify names: `wpctl settings`) ----------------------
wireplumber.settings = {
  linking.allow-moving-streams            = false
  linking.follow-default-target           = false
  node.stream.restore-props               = false
  node.stream.restore-target              = false
  device.restore-profile                  = false
  device.restore-routes                   = false
  bluetooth.autoswitch-to-headset-profile = false
  bluetooth.use-persistent-storage        = false
}

# --- 3. bluez monitor pins (NEW-4, I8) --------------------------------------
monitor.bluez.properties = {
  bluez5.roles              = [ a2dp_source ]   # host = A2DP SOURCE (feeds a headset)
                                                # (verify host-centric naming: B11 must show
                                                # bluez_output.* nodes and NO 0000110b endpoint)
  bluez5.codecs             = [ sbc ]
  bluez5.enable-sbc-xq      = false
  bluez5.enable-msbc        = false
  bluez5.enable-hw-volume   = false             # 0.5 form may be `bluez5.hw-volume = []` (verify)
  bluez5.dummy-avrcp-player = false             # vibb-mpris is the ONLY MediaPlayer1 (J)
  bluez5.default.rate       = 44100             # same SBC rate as bluealsa today (verify key)
  # bluez5.auto-connect deliberately ABSENT: btwatchd owns every page (verify default = off)
}
monitor.bluez.rules = [
  { matches = [ { node.name = "~bluez_output.*" } ]
    actions = { update-props = {
      session.suspend-timeout-seconds = 120   # replaces --keep-alive=120 (SEVERE-1, I7)
      node.pause-on-idle              = true
    } } }
  { matches = [ { device.name = "~bluez_card.*" } ]
    actions = { update-props = {
      bluez5.autoswitch-profile = false
    } } }
]

# --- 4. HAT ---------------------------------------------------------------
monitor.alsa.rules = [
  { matches = [ { node.name = "~alsa_output.*" } ]
    actions = { update-props = {
      session.suspend-timeout-seconds = 5     # default; fast release for extras' raw hw: (Q9)
      api.alsa.soft-mixer             = true  # MAX98357A has no mixer; gain stays a 1.0 constant
      node.pause-on-idle              = true
    } } }
]
```

`/etc/pipewire/pipewire.conf.d/10-vibb.conf` (server side; the distro `pipewire.conf` stays the base):
```
context.properties = {
  default.clock.rate          = 44100    # mpv pins 44100 (player.py:134); go-librespot is 44100;
  default.clock.allowed-rates = [ 44100 48000 ]  # SBC at 44100 = zero graph resamplers
  default.clock.quantum       = 2048     # ~46 ms: fewer wakeups; no low-latency need on this box
  default.clock.min-quantum   = 1024
  default.clock.max-quantum   = 4096
  mem.allow-mlock             = false
  log.level                   = 2
}
context.objects = [
  # the warming sink for soloistd (PLAN-soloistd "Silence done properly") — always present,
  # costs nothing suspended, and is the null target the self-test uses (B.6)
  { factory = adapter
    args = { factory.name = support.null-audio-sink  node.name = "vibb_null"
             media.class = "Audio/Sink"  audio.position = [ FL FR ]
             monitor.channel-volumes = false  node.passive = true } }
]
```

`/etc/pipewire/client.conf.d/10-vibb.conf` (every libpipewire client on the box, including pipewire-alsa's plugin and soloist) — **this is how Q3 is answered**:
```
context.properties = { remote.name = "/run/pipewire/pipewire-0"  mem.allow-mlock = false  log.level = 1 }
stream.properties = {
  node.dont-reconnect = true    # target vanished -> stream destroyed, client gets an ERROR (E)
  node.dont-fallback  = true    # target absent at open -> error, never "some other sink"
  node.autoconnect    = true
}
```
**(verify on bench: `pw_stream_new_full` applies `stream.properties` from the client config to pipewire-alsa streams — B2 asserts it via `pw-dump` showing both props on the mpv stream.)** Belt for soloist only: `Environment=PIPEWIRE_PROPS={ node.dont-reconnect=true node.dont-fallback=true }` in `vibb-soloistd.service`.

Per-client target (`target.object`): comes from the pcm's `playback_node` for mpv and go-librespot (C), from `--pipewire-device` for soloist (I). Nothing in vibb ever sets a default sink; `wpctl status`'s "default" is meaningless on this box and the self-test asserts that changing it moves nothing.

### Suspend semantics (SEVERE-1, I7, Q9)

120 s on `bluez_output.*` only. Mechanism parity: bluealsa's `--keep-alive` (`pi/install.sh:475-522`) delays the transport Release after the last PCM close; PipeWire's node suspend triggers the same `MediaTransport1.Release` → AVDTP Suspend. B4 counts the PDUs. The HAT stays at 5 s: there is no transport to renegotiate, a running-but-silent I2S node keeps the MAX98357A's SD_MODE awake (battery), and `pi/extra.sh:72` frees the ALSA device by stopping go-librespot — under PipeWire the holder is `pipewire.service`, so `extra.sh --run` additionally stops `wireplumber pipewire.service pipewire.socket` in pipewire mode and the restore set gains them (H). One divergence accepted: a mpv **paused** > 120 s suspends the node today's bluealsa would hold (open PCM = held transport); the resume costs one AVDTP Start, no chime on the JBL class. Listed in N.

### The policy self-test (I10, Q11)

`pi/vibb/audio.py: policy_selftest() -> {"safety": [..], "rf": [..], "down": bool}`, stdlib, forks `pw-dump`, `wpctl settings`, `pw-cli`, `pw-cat`. Runs (a) in a daemon thread at vibbd start, after `/run/pipewire/pipewire-0` accepts a connection (≤60 s wait), (b) after every `bt.py recover()` run, (c) on `POST /audio/selftest`. Verdict is written to `/run/vibb-audio-policy` (JSON, mtime) and surfaced as `/status.audio_policy` (`ok` | `fail-safety` | `fail-rf` | `down`) plus a screen line and a journal line per failed assertion. What it asserts, **behaviourally** (I2):

SAFETY class (any failure → `fail-safety`):
1. `pw-cat --playback --target vibb_selftest_missing /dev/zero`-style probe with a target that does not exist ends with the stream **not linked to any sink** and gone within 2 s (`pw-dump` shows no `PipeWire:Interface:Link` whose output is that client node).
2. `pw-cli create-node adapter { factory.name=support.null-audio-sink node.name=vibb_selftest_sink media.class=Audio/Sink }`, start a 4 s silent `pw-cat --target vibb_selftest_sink`, `pw-cli destroy <sink id>` at 1 s: the stream is destroyed (dont-reconnect) and **no link appears to `bluez_output.*`, `alsa_output.*` or `vibb_null`**.
3. `pw-metadata 0 default.audio.sink` set to `vibb_null` then restored: the live stream from (2)'s sibling (a second probe pinned to `vibb_null`) did not move (`linking.follow-default-target`).
4. `wpctl settings`: `linking.allow-moving-streams=false`, `linking.follow-default-target=false`, `node.stream.restore-target=false`, `node.stream.restore-props=false`.
5. The HAT sink node (if present) reports channel volumes 1.0 and `mute=false` (I8); no `restore-stream` metadata object exists.
6. `pw-dump` from vibbd's own env works (the env plumbing itself).

RF class (`fail-rf`, warn only): `api.bluez5.codec == "sbc"` and no `sbc_xq` on any present bluez node; no `bluez_input.*`/HFP nodes and no MediaPlayer1 other than `/org/vibb/mpris` under org.bluez (`busctl`); `session.suspend-timeout-seconds == 120` on bluez nodes; `bluez5.roles` visible on the monitor object **(verify where 0.5 exposes monitor props; fallback: hash the conf fragment against the installed one)**.

**Failure action (Q11): refuse the LOCAL landing only, keep BT working, be loud.** Reasoning: every safety-class drift is a *HAT* hazard (uncontrolled landing, uncontrolled gain); BT playback into the chosen speaker under a drifted policy is at worst silence, which is the failure the owner already chose (`pi/daemon.py:4880-4885`, `pi/btwatchd.py:729-739`). Refusing all playback would make the box dead at bedtime (the bedtime rule, `docs/PLAN-soloistd.md` "never a silent dead box"); warn-only leaves the blast path open. So `set_output("local", ...)` and `ORCH.play()` with `current_output()=="local"` return the closed refusal shape `{"error": "audio-policy-failed"}` while `fail-safety` stands; the popup text says so in one Norwegian line; BT is untouched. A local-only box (no `MAC_FILE`) has no BT to fall back to — it gets the same refusal, because the alternative is exactly the uncapped-HAT hazard the test detected; the fix is one `install.sh` re-run, and B8 proves each drift is caught at the next boot. `fail-rf` refuses nothing. `down` (no socket) makes `audio_ready()` false for both outputs — today's "device absent" semantics, popup, no heal budget burnt (NEW-3, see D).

---

## C. Node naming + the `OUTPUT_PCMS` seam (Q2, Q5, NEW-5, SEVERE-3)

> **[QA-2: AM-10]** — see the amendments table above; where this section's text conflicts, the amendment wins.

**Decision (Q2), per client:**
- **mpv: pipewire-alsa pcm.** `--ao=alsa --audio-device=alsa/vibb_bt|vibb_local` unchanged (`pi/player.py:130,144`); the live retarget `mpv_ipc(["set_property","audio-device", f"alsa/{pcm}"])` unchanged (`pi/daemon.py:1739, :1791`); deferred-switch refusal unchanged (`:1780-1787`); `tests/mpv_launch_flags.py:29,36` keep passing *and keep meaning something* (the pcm IS the route).
- **go-librespot: pipewire-alsa pcm.** `audio_backend: alsa`, `audio_device: vibb_bt` (`pi/install.sh:268-269`) unchanged; `reopen_go_output` (`pi/vibb/output.py:139-155`) and `_go_output_rebuild` (`pi/daemon.py:4945-5013`) unchanged; the fork's v0.1.5 `snd_config_update_free_global` on every PCM open (`pi/install.sh:153-157`) is exactly what makes a rewritten `playback_node` take effect on the live reopen. Whether the fork has a pulse/pipewire backend is **OPEN and irrelevant** to this design — nothing consumes it.
- **soloist: native** `--pipewire-device <node.name>` (only option; no ALSA). soloistd resolves the name through the same resolver as `_route_alsa` (I).

`OUTPUT_PCMS` (`pi/vibb/output.py:14-15`), `output.json`'s `pcm` field (`pi/daemon.py:1759-1761`), `output_pcm()` (`pi/player.py:82-89`) and `local_volume`'s `pcm == OUTPUT_PCMS["local"]` test (`pi/vibb/output.py:39`) all survive literally.

### The pcm template under pipewire (written by `bt.py:_route_alsa`)

```
# Managed by vibb (bt.py) — stack: pipewire
# bt speaker 2C:FD:B3:FA:DA:04  (colon MAC kept for the idempotence check)
pcm.vibb_bt {
    type pipewire
    server "/run/pipewire/pipewire-0"
    playback_node "bluez_output.2C_FD_B3_FA_DA_04.1"     # DISCOVERED from the graph, never composed
    hint.description "vibb: BT speaker"
}
pcm.vibb_local {
    type pipewire
    server "/run/pipewire/pipewire-0"
    playback_node "alsa_output.platform-soc_sound.stereo-fallback"   # DISCOVERED (hifiberry card)
    hint.description "vibb: built-in speaker"
}
```
**(verify on bench: `playback_node` populates `target.object` in 1.4's plugin; whether `server` is honoured; the exact HAT node name.)**

### `_route_alsa` replacement (Q5, NEW-5, I12)

`pi/vibb/bt.py:430-483` keeps its shape; the body branches on `audio.stack()`:

```python
def _route_alsa(mac):
    from vibb import audio
    if audio.stack() == "pipewire":
        bt_node, local_node = audio.resolve_route(mac, tries=10, delay=1.0)  # forks pw-dump ≤10×
        text = audio.asound_text(mac, bt_node, local_node)
        if bt_node is None:
            log("no bluez sink node for %s yet — route not rewritten" % mac); return
    else:
        text = _bluealsa_asound_text(mac)                    # today's template, verbatim
    try:
        with open(ASOUND) as f:
            cur = f.read()
        if mac in cur and (audio.stack() != "pipewire" or bt_node in cur):
            return                                            # idempotent: MAC AND node unchanged
    except OSError:
        pass
    ... tmp + fsync + os.replace exactly as today (:442-463) ...
    ... reopen/restart go-librespot exactly as today (:465-482) ...
```

Discovery (`pi/vibb/audio.py`): `find_bt_sink(mac, dump)` = the `PipeWire:Interface:Node` whose `info.props["media.class"] == "Audio/Sink"` and `props["api.bluez5.address"].upper() == mac.upper()` → returns `props["node.name"]`. `find_local_sink(dump)` = the `Audio/Sink` node whose `alsa.card_name`/`api.alsa.card.name` contains `hifiberry` **(verify prop key on bench; `/proc/asound/cards` says `sndrpihifiberry`)**. Both shapes QA named (`.1` vs `.a2dp-sink`) and any future suffix are handled because the name is *read*, not built. The colon MAC stays in the file as a comment so `:433-436` and `tests/bt_state_fsync.py:44,47-48` keep their semantics; a node-name change (package upgrade) rewrites once, which is correct — that is a real route change.

When `_route_alsa` runs: (1) `bt.py connect()` after the transport wait (`pi/vibb/bt.py:396-398`), as today; (2) **new**: on btwatchd's announce, `set_output("bt", fallback=True)` calls `audio.ensure_bt_route(mac)` before the deferred apply (`pi/daemon.py:1732-1756`) — cheap because it reads the file first and forks `pw-dump` only when the MAC or node is missing (once per connect, never per second).

### What a pcm does when its node is absent, and the re-established invariant

With `node.dont-fallback=true` an open against an absent `playback_node` **fails** (stream error at connect → `snd_pcm_open`/first write returns an error in the plugin **(verify errno on B2)**). That is today's semantics for a bluealsa pcm with no transport (open fails) — so `set_output`'s "NEVER point a live mpv at a device with no transport" guard (`pi/daemon.py:1780-1787`, `:1823-1830`) keeps working unchanged, gated by `_bt_transport_ready()` (D). The ms-scale gap between MediaTransport1 appearing and the node appearing is closed by gating the *announce* (btwatchd) and the *spawn* paths on node presence (D), not the 1/s readers.

---

## D. Gate replacement (BLOCKER-3, Q4, I6)

> **[QA-2: AM-9, AM-11, AM-12]** — see the amendments table above; where this section's text conflicts, the amendment wins.

### `a2dp_pcm_present(mac)` → `org.bluez.MediaTransport1` (Phase 0, stack-neutral)

Same signature, same call sites, same fallback ladder (`pi/vibb/btbus.py:372-379`). New D-Bus body replacing `:593-603`:

```python
_A2DP_SOURCE_UUID = "0000110a-0000-1000-8000-00805f9b34fb"   # the HOST's endpoint role

def _dbus_a2dp_pcm_present(mac):
    """A MediaTransport1 for dev_<MAC> whose local endpoint is A2DP Source
    (state idle|pending|active all count) = the peer accepted our
    SetConfiguration. Backend-neutral: BlueZ creates it whoever owns the
    endpoint (bluealsa today, PipeWire tomorrow). A 0000110b transport
    (a phone streaming INTO the box) must never count."""
    frag = "/dev_" + mac.upper().replace(":", "_") + "/"
    for path, ifaces in _managed(_BLUEZ, "/").items():
        tr = ifaces.get("org.bluez.MediaTransport1")
        if tr is None or frag not in str(path):
            continue
        if str(tr.get("UUID", "")).lower() == _A2DP_SOURCE_UUID:
            return True
    return False
```
`_managed(_BLUEZ, "/")` is the existing ObjectManager read at the BlueZ root (the fake exports it at `/`, `tests/fake_bluezd.py:517`). CLI fallback (`:382-384`): under `stack()=="bluealsa"` keep `bluealsa-aplay -L`; under pipewire use `busctl --system --json=short call org.bluez / org.freedesktop.DBus.ObjectManager GetManagedObjects` and apply the same filter with `json` (busctl is systemd, always present). Phase 0 ships it in **shadow mode** first: return the bluealsa answer, log `bt gate: transport=%s pcm=%s` on disagreement (rate-limited 1/min), one week on the box; then flip. The prior architect's timing caveat (bluealsa PCM ≈ hundreds of ms after the transport) is measured, not assumed.

`fake_bluezd.py` extension (additive, `:46-47` frozen contract): `TRANSPORTS = {}`; `BluezRoot.GetManagedObjects` also exports `/org/bluez/hci0/dev_<MAC>/sep1/fd0` → `{"org.bluez.MediaTransport1": {"Device": path, "UUID": uuid, "State": state, "Codec": 0}}`; new Mock methods `SetTransport(mac, uuid, state)` and `DropTransport(mac)`; **`SetPcm(mac, True)` also creates an `0000110a` transport** so `bt_parity.py`, `bt_avdtp_refusal.py`, `bt_play_kick.py`, `status_bt_probe_local.py`, `bt_output_policy.py` run unchanged. New case in `tests/bt_transport_gate.py`: only a `0000110b` transport → `False`.

### The second predicate: `sink_ready(output, mac=None)` (node presence)

**Needed, fork-per-call, only at spawn/retarget points.** Reasoning: the long-lived `pw-dump -m` watcher is a child + reader thread + supervision inside vibbd for a predicate polled ≤0.2/s; a fork of `pw-dump` on a two-sink graph is ~30-60 ms on a Zero 2 W and sits on paths that already wait seconds for the radio (`pi/player.py:536-538`). 1/s readers never call it.

Implementation (`pi/vibb/audio.py`):
```python
def pw_dump(timeout=3.0) -> list          # subprocess ["pw-dump"], json.loads; [] on any failure
def server_up() -> bool                    # socket exists and pw_dump() != []
def sink_ready(output, mac=None) -> bool   # "bt": find_bt_sink(mac) is not None
                                           # "local": find_local_sink() is not None
```

Call-site map (the 8 + 3):

| Site | Predicate under pipewire |
|---|---|
| `/status` icon `pi/daemon.py:3006` (1/s) | `_bt_transport_ready()` = D-Bus only |
| `_bt_wait_state` `:5104`, `_bt_wait_advance` `:5056`, `_kick_bt_connect` `:5128` | D-Bus only |
| `set_output` deferred/live retarget `:1735, :1780, :1823` | D-Bus only (node presence guaranteed by the announce gate below) |
| btwatchd `_await_pcm` `pi/btwatchd.py:428-465` (commit gate, ≤10 × 1 s) | D-Bus **and** `sink_ready("bt", mac)` |
| `bt.py connect()` transport wait `pi/vibb/bt.py:382-394` | D-Bus; the node wait lives inside `_route_alsa`'s resolver |
| `audio_ready()` `pi/vibb/output.py:174-186` → watchdog respawn `pi/daemon.py:591`, crash heal `:655`, blip `:4839`, player watchdog `pi/player.py:653, :745` | bt: D-Bus **and** `sink_ready`; local: `_i2s_card_present()` **and** `sink_ready("local")` |
| new: `play_mpv` before `Popen` (`pi/player.py:545`) | wait ≤5 s for `sink_ready(output)` in pipewire mode (covers the first tap after reboot before WirePlumber has created the HAT node, NEW-3) |

NEW-3's budget burn: `_heal_crashed_child` increments `_crash_respawns` only after `_audio_ready()` passed (`pi/daemon.py:655-660`); with `audio_ready()` now including `sink_ready`, "server not up yet" returns `dead_since` without spending the 2/boot budget.

---

## E. Loss contract (SEVERE-2, I3, I4, prior §3.4)

> **[QA-2: AM-19]** — see the amendments table above; where this section's text conflicts, the amendment wins.

Headphone battery pulled mid-track, output=bt, policy as in B:

1. **BlueZ**: ACL drops → `Device1.Connected=false` PropertiesChanged; BlueZ removes the `MediaTransport1` object.
2. **WirePlumber** (same signal, own clock): bluez5 monitor removes `bluez_output.<MAC>.1`. The linking policy re-scans the stream whose `target.object` was that node: `node.dont-reconnect=true` → the stream node is **destroyed**, the client gets a stream error; `node.dont-fallback=true` and no default/best-target hooks loaded → no other sink is considered. Nothing links to the HAT. The HAT node stays `suspended` (B2's 4 Hz sampler proves it).
3. **btwatchd** (same signal): `_props_changed` → `_notify_lost` → `POST /bt/lost` (`pi/btwatchd.py:546-559`).
4. **vibbd** `_bt_transport_lost` (`pi/daemon.py:4868-4933`): guard `current_output()=="bt"` (`:4901`), heal probe thread (`:4903`), then per client:
   - **mpv**: between (2) and (4) mpv's `ao_alsa` sees the write error → "audio device failed" → AO reload → reopen fails (node absent, dont-fallback) → the audio-only file ends → next file → same: the advance storm at ~100-300 ms/track. That is **today's contract verbatim** (`pi/btwatchd.py:547-551`, `pi/daemon.py:4869-4872`: "errors each episode and auto-advances"). It ends when `_stop_child()` lands (`:4914-4917`, sub-second after the BlueZ signal since btwatchd is signal-driven) and the player-side belt is unchanged: a track change with `not audio_ready()` → `survive_dead_audio(stable)` rollback (`pi/player.py:744-749`). If B2 instead shows pipewire-alsa surfacing the destroy as a *blocked write*, the existing 8 s `proc.kill` (`pi/player.py:559`) and `killpg` (`pi/daemon.py:699`) handle it — both shapes are already covered; B2 records which one it is.
   - **go-librespot**: write error → its "output device failed" path, track burns silently — today's contract (`pi/daemon.py:4875-4877`) → `go("/player/pause")` (`:4926`), `_BT_WAIT["lost_spotify"]` (`:4929`).
   - **soloist**: native `pw_stream` error; soloistd receives the daemon's `/player/pause` in the dialect and pauses; its own state after a destroyed stream is a B2/B9 measurement — soloistd treats "stream gone" as `stopped` for `/status` purposes and does not touch the transport.
5. `_BT_WAIT["lost"]` armed (`:4921`); heal probe self-discriminates; `_bt_wait_watcher` → `_bt_wait_advance` → `_bt_transport_ready()` (MediaTransport1 back) → `_speaker_back` → ≤150 s: `_bt_blip_resume` (`:4839-4865`): mpv respawn (a fresh pcm open against the re-created node — `ensure_bt_route` ran on btwatchd's announce, so the name is current), spotify `_go_output_rebuild` → `reopen_go_output` (a fresh stream to the re-created node) → replay from the bookmark.

**output.json cannot be silently wrong**: the only writer stays `set_output` (`:1759-1761`; grep pin), no policy hook exists that moves or relinks, and a stream that loses its target is destroyed rather than re-homed. The bench belt is the I4 4 Hz sampler comparing every vibb stream's link target with `output.json` for the whole of B2/B3/B7.

**Prior §3.4 ("streams stop failing")**: false under this policy by construction — `dont-reconnect` makes them fail, which is what keeps every existing detector meaningful. The frozen-position branch (`pi/daemon.py:540-578`) still fires for the case it was built for, the dead-but-connected zombie (BlueZ still lists the transport, `hci_tx_bytes` flat) — that is HCI-level and unchanged. The one *new* silent shape would be "linked and running into a node that emits nothing"; there is no such node on this box (HAT = hardware, `vibb_null` is soloistd-only and B9 asserts vibb's own streams never target it).

---

## F. Cap semantics (NEW-1, Q6, I1)

> **[QA-2: AM-13]** — see the amendments table above; where this section's text conflicts, the amendment wins.

**Decision: per-engine clamp, applied by vibb, for all three engines; never a graph gain; HAT sink gain pinned 1.0; never a write to a bluez node.** A HAT-sink multiplier would change what "35" means on the screen (`pi/ui.py` exposes `local_fallback_cap`), would be subject to the restore machinery this design disables, and would silently multiply the softvol. `local_volume` (`pi/vibb/output.py:22-41`) stays the single rule.

Every HAT landing path and where the cap is applied:

| Path | Engine | Cap today | Cap after |
|---|---|---|---|
| fresh mpv spawn `pi/player.py:542-546` | mpv | yes | unchanged |
| live retarget `pi/daemon.py:1799-1802` | mpv | yes | unchanged |
| `_apply_box_volume` on every spotify spawn `pi/player.py:162-176, :316` | go-librespot **and soloist** (dialect) | **no (NEW-1)** | `v = local_volume(v, output_pcm(), read_settings().get("local_fallback_cap", 35))` before the `/player/volume` POST |
| `set_output(local)` live reopen `pi/daemon.py:1831` | go-librespot/soloist | no | after a successful `reopen_go_output(pcm)` with `device=="local"`: POST `/player/volume` with `_local_volume(self._volume_setting(), pcm)` scaled by `volume_steps` (outside `ORCH.lock`, same place the reopen runs) |
| `set_output(local)` restart path `:1853-1873` | go-librespot | no | the respawn goes through `_apply_box_volume` (fixed above) |
| `_go_output_rebuild` with output=local `:4979-4990` | go-librespot/soloist | no | same post-reopen re-apply as `set_output` |
| `_bt_blip_resume` spotify `:4858-4865` | go-librespot/soloist | no | via `_spawn` → `_apply_box_volume` |
| `_heal_crashed_child` respawn `:664`, watchdog respawn `:612`, first tap `ORCH.play` | both | mpv yes / spotify no | both via the spawn paths above |
| Phone-driven Spotify session landing on the HAT (Connect from the phone) | go-librespot | no | **accepted residual** (a person is holding the phone; `_apply_box_volume` cannot run for a session vibb did not start) — listed in N |

`Orchestrator.volume()` (`pi/daemon.py:988-1055`) keeps clamping to `volume_cap` only: it is the user's live knob with a person present; the landing cap is about *arrival*, not ceiling. The HAT node's channel volume is pinned 1.0 (B.4 `api.alsa.soft-mixer` + no restore) and the self-test asserts it (I8). Grep pin: no `wpctl set-volume`, `pw-metadata`, or `set-mute` writer anywhere in `pi/`.

---

## G. Mixing consequences (NEW-2, NEW-7, Q14, I9, I11)

> **[QA-2: AM-14]** — see the amendments table above; where this section's text conflicts, the amendment wins.

**NEW-2 fix (Q14): in `player.py`, before the mpv `Popen`, not under `ORCH.lock`.** Today the best-effort pause lives at `pi/player.py:432-438` (1 s timeout) and EBUSY was the real exclusion. Replace with:

```python
def _confirm_spotify_paused(budget_s=2.5):
    """Mixing (PipeWire) removed the EBUSY that used to stop Spotify
    playing UNDER a fresh mpv. Pause and CONFIRM (status.paused) within a
    bounded budget; an unreachable go-librespot means nothing is playing."""
    deadline = time.monotonic() + budget_s
    while True:
        try:
            spotify.go("/player/pause", timeout=1)
            st = spotify.status(timeout=1)
        except OSError:
            return True                          # not running = not playing
        if not st.get("track") or st.get("paused") or st.get("stopped"):
            return True
        if time.monotonic() >= deadline:
            log("spotify pause unconfirmed after %.1fs — spawning anyway" % budget_s)
            return False
        time.sleep(0.3)
```
Why here and not in `ORCH.play`: `play()` holds `ORCH.lock` across `_stop_child` + `_spawn` (`pi/daemon.py:873`, `:946-947`); a 2.5 s confirm under it would freeze every `/status` reader (the R2 rule, `:1818-1820`). The player child already owns the mpv launch and already runs this exact call, so the fix is a confirmation loop where the fire-and-forget was. The reverse direction (spotify after mpv) is already exclusive: `_stop_child` waits for the whole group to be gone before `_spawn` (`:706-714`). Engine alternation mpv → spotify → mpv → soloist therefore never double-plays; the dialect means soloistd's `/player/pause` + `/status.paused` satisfy the same confirm. Unit pin (I9): a fake go-librespot answering `/player/pause` slowly must see `paused` before the `mpv_command` Popen (spy on `subprocess.Popen`).

**Orphans (NEW-7, I11):** `tests/stop_child_group_kill.py`, `_stop_child`'s `killpg` + group-gone wait (`pi/daemon.py:680-715`), `start_new_session=True` (`:789`), and player's 8 s `proc.kill` (`pi/player.py:559`) stay exactly as they are; the docstring of the test gains the NEW-7 sentence ("under PipeWire an orphan on a live sink keeps *playing* and holds the node out of suspend"). B5's orphan case (`kill -STOP` the player, `_stop_child`, respawn, assert one stream and node suspend after stop) is the bench proof.

---

## H. bluealsa removal + rollback toggle (MODERATE-5, NEW-6, Q10, Q15, I13)

> **[QA-2: AM-15]** — see the amendments table above; where this section's text conflicts, the amendment wins.

`VIBB_AUDIO_STACK=bluealsa|pipewire` (default `bluealsa` through Phase 2's soak; flips to `pipewire` in Phase 4). install.sh writes it to `/etc/vibb/audio-stack` and as `Environment=VIBB_AUDIO_STACK=` on `vibb-daemon`, `vibb-bt-reconnect`, `go-librespot`, `vibb-soloistd`; `pi/vibb/audio.py:stack()` reads env, then the file, default `bluealsa` (same precedent as `VIBB_BT_BACKEND`, `pi/vibb/btbus.py:64-81`).

`install.sh` under **pipewire**: apt-install the five packages (missing-only, `:123-133` logic); create the `pipewire` user; write the three units, the four conf fragments, the `bluetooth.service.d/vibb-after-wp.conf` drop-in; `systemctl mask --now bluealsa.service bluealsad.service` (mask, never `apt remove`: `bluez-alsa-utils libasound2-plugin-bluez` stay installed so an offline rollback works, NEW-6/`:127-130`); `systemctl enable --now pipewire.socket pipewire.service wireplumber.service`; re-point `vibb-bt-reconnect.service` `After=`; rewrite `/etc/asound.conf` via `python3 pi/vibb/bt.py route` (a new verb that calls `_route_alsa(mac)` with the MAC from `MAC_FILE`, or writes both pcms with `playback_node "vibb-unresolved"` when no speaker is configured — a name that cannot link, i.e. fail-closed like today's `plug -> null` placeholder is *not*; `:231-246`). The bluealsa keep-alive block (`:475-522`) is skipped, not deleted.

`install.sh` under **bluealsa** (the rollback): `systemctl disable --now` and `mask` **all three** `pipewire.socket pipewire.service wireplumber.service` (the socket too, or it revives the service on the first client, NEW-6); `rm -f /etc/systemd/system/bluetooth.service.d/vibb-after-wp.conf`; remove Debian's `default`→pipewire ALSA override (`rm -f /etc/alsa/conf.d/99-pipewire-default.conf`; an apt upgrade of `pipewire-alsa` may recreate it — the rollback state is temporary by definition and `tests/audio_stack_toggle.py`'s rollback assertion plus the post-rollback self-check catch it); unmask + `enable --now` bluealsa; the keep-alive block runs as today; `/etc/asound.conf` back to the `type bluealsa` template with the MAC (`bt.py route`); `After=` back to `bluealsa.service bluealsad.service`. Under pipewire the `default` override stays: `default` then maps to a pipewire pcm with no `playback_node`, which fails closed under `dont-fallback` rather than landing on the HAT — I2-consistent, and nothing in vibb uses `default` (`pi/play.sh:99` names `vibb_bt`).

`pi/extra.sh`: `RESTORE` (`:41-42`) and the start loop (`:112-113`) gain `pipewire.socket pipewire wireplumber` when `/etc/vibb/audio-stack` says pipewire (and keep `bluealsa` — a masked unit's start is a harmless no-op, so one list works for both stacks); `--run` (`:72`) additionally `stop`s `wireplumber pipewire pipewire.socket` in pipewire mode so an extra gets raw `hw:`. `tests/extras_wrapper.py:132-135` gains the three names in a pipewire-mode run (env `VIBB_AUDIO_STACK=pipewire`).

`bt.py recover()` (`pi/vibb/bt.py:244-245`, Q15): the `try-restart bluealsa|bluealsad` line becomes `for unit in audio.recover_units(): _run(["systemctl", "try-restart", unit])` where `recover_units()` = `("bluealsa","bluealsad")` under bluealsa and `()` under pipewire. WirePlumber re-registers its endpoints on `NameOwnerChanged org.bluez` by itself (the same mechanism `pi/mpris.py:272-281` relies on); B10 verifies across all three tiers. If B10 finds a tier that needs it, the escalation is `("wireplumber",)` — never `pipewire.service` (restarting the core kills every client stream; a WirePlumber restart at recover() time costs nothing because the lost path has already stopped playback). The env knob `VIBB_BT_HEAL_RESTART_WP=1` exists from day one so the bench can flip it without a code change.

`btbus` keeps both gate backends: D-Bus MediaTransport1 is stack-neutral; only the CLI fallback branches (D).

Every dying reference (Phase 4 deletes; until then they live behind the toggle):
- `pi/install.sh:105` (`bluez-alsa-utils libasound2-plugin-bluez` in PKGS), `:231-258` (bluealsa asound template + `vibb_local` migration), `:436` (echo), `:471-473` (enable bluealsa), `:475-522` (keep-alive/loglevel block), `:589` (poll-loop comment), `:629` (`After=`).
- `pi/vibb/bt.py:244-245` (try-restart), `:442-458` (bluealsa template), `:393-394` (the `bluealsa-aplay -L` debug hint), `:472-482` (comment text only).
- `pi/vibb/btbus.py:1-5` docstring, `:382-384` (`bluealsa-aplay -L` cli path), `:395` (`_BLUEALSA`), `:593-603` (PCM1 body — replaced in Phase 0).
- `pi/btwatchd.py:429-434` docstring wording ("A2DP PCM"), `pi/daemon.py:466-479, :583-589, :2998-3001, :4683, :4875-4877, :4946-4950` comments; `pi/player.py:555, :787` comments.
- `pi/extra.sh:38, :41, :113`; `pi/play.sh:233` comment; `tests/fake_bluezd.py:26, :341-353` (`org.bluealsa` root + PCM1 — kept for `bt_parity.py` until Phase 4).

---

## I. Client binding (MODERATE-1)

> **[QA-2: AM-16]** — see the amendments table above; where this section's text conflicts, the amendment wins.

**mpv** (`pi/player.py:122-146`): every flag stays. `--audio-samplerate=44100 --audio-channels=stereo` are pre-AO filters and with `default.clock.rate=44100` they also mean the graph never resamples mpv. `--audio-buffer=0.5`: its *client-side* job survives literally — pipewire-alsa honours the requested buffer size in the plugin ring **(verify on bench: 0.5 s × 44100 × 2 ch × 2 B ≈ 88 kB is inside the plugin's max)**, so the decoder still runs half a second ahead of the sink, which is what protected against mpv's own scheduling starvation. What it no longer governs is the graph→radio hop: that is the bluez5 sink's own packet timing plus the quantum. The replacement knob is `default.clock.quantum` (2048 = ~46 ms, B) and, if B6's barrage shows clicks, the bluez node's `node.latency` **(verify exact bluez5 latency property on bench)**. The `tests/mpv_launch_flags.py:46` comment gets one sentence recording this split so the pin stops being decorative. `--ao=alsa` stays (skips autoprobe; now one hop through the plugin).

**go-librespot**: config unchanged (`audio_backend: alsa`, `audio_device: vibb_bt`, `pi/install.sh:268-269`); `/player/output` live reopen unchanged. Bench verifies (B7) that the reopen through pipewire-alsa keeps the session and lands on the new node. A pulse/pipewire backend in the fork: **OPEN, unused, not a dependency.** Runs as `$RUN_USER` (group `audio` → socket access).

**soloist** (P1, `docs/PLAN-soloistd.md` binding): `soloist ... --pipewire-device <node.name>` where soloistd computes the name as `audio.find_bt_sink(mac)` / `audio.find_local_sink()` from `output.json`'s `output`, with `PIPEWIRE_RUNTIME_DIR=/run/pipewire` + `PIPEWIRE_PROPS` (B) in the child's env. Warming: restart the child with `--pipewire-device vibb_null` (the static null sink from `pipewire.conf.d/10-vibb.conf`), volume-0 as belt; B9 asserts both real nodes stay `suspended` and btmon TX is flat for 10 min. Fail-closed: after the child starts, soloistd polls `pw-dump` (≤5 s) until it finds a node with `application.name ~ soloist`; if that node is not linked to the intended sink it kills the child and answers the closed shape `{"error": "soloist-audio-unbound"}` on every play. Output switch = restart the child (the plan's "falls back to restart+bookmark-resume" line); the `/player/output` dialect call maps to that restart. Users: `vibb-soloistd.service` and its child run as `$RUN_USER`.

**pw-* tools** (self-test, resolver) run as root from vibbd with the env belt and `client.conf.d` remote name.

---

## J. Resource + RF budget (MODERATE-4, NEW-4)

> **[QA-2: AM-17 (hw-volume = true)]** — see the amendments table above; where this section's text conflicts, the amendment wins.

Expected standing cost on arm64 Trixie, two sinks, no pipewire-pulse: `pipewire` 8-14 MB RSS, `wireplumber` 12-20 MB with the whitelist profile (fewer Lua scripts loaded) → **+20-34 MB against bluealsa's 3-5 MB**, i.e. ≈5-8 % of the ~430 MB (`pi/install.sh:906`). B6 PASS ≤ +40 MB, KILL > +60 MB. Wakeups: idle-suspended ≈ 0 (no graph timer with no running node; WirePlumber idles on D-Bus/epoll); streaming ≈ 21.5 graph wakeups/s at 2048/44100 plus the bluez5 socket writes, comparable to bluealsa's own I/O thread. CPU: one SBC encode either way; no graph resampler for 44100 sources.

Coex variable, itemised (what B11 must show identical to baseline): codec SBC only, no XQ (bitpool and rate as baseline: `bluez5.default.rate=44100`), no HFP/HSP RFCOMM SLC at connect (roles = `a2dp_source` only), no phone-as-source transport (`0000110b` never registered), no AVRCP `SetAbsoluteVolume` from vibb (hw-volume off, no node volume writer), no second MediaPlayer1 (`bluez5.dummy-avrcp-player=false`), no WirePlumber-initiated pages (`bluez5.auto-connect` absent). What genuinely changes: a nice -11 graph process wakes ~21/s during playback instead of bluealsa's writer thread; that is the C13 delta and B6 (with the scan barrage) is the only honest measurement.

`pi/mpris.py` vs WirePlumber on `org.bluez.Media1` (prior architect's finding): PipeWire's bluez5 plugin registers a *dummy* `MediaPlayer1` by default so that headsets emit volume events; two players would make the Skoda's addressed-player polling ambiguous — the known channel-ops-while-streaming crasher (`pi/install.sh:607-610`). Pin `bluez5.dummy-avrcp-player=false` **(verify key on bench)**; the RF-class self-test asserts exactly one `MediaPlayer1` under `/org/bluez/hci0` and it is `/org/vibb/mpris`. `vibb-mpris.service` ordering unchanged (`:615`); its re-register-on-`NameOwnerChanged` (`pi/mpris.py:272-281`) is exercised by B10.

---

## K. File-by-file change list, tests, new module

> **[QA-2: AM-18]** — see the amendments table above; where this section's text conflicts, the amendment wins.

| File | Change | LOC (+/−) |
|---|---|---|
| **new** `pi/vibb/audio.py` | stack(), pw_dump(), server_up(), find_bt_sink(), find_local_sink(), sink_ready(), resolve_route(), ensure_bt_route(), asound_text(), recover_units(), policy_selftest(), selftest_state(), local_landing_allowed() | +240 |
| `pi/vibb/btbus.py` | `_dbus_a2dp_pcm_present` → MediaTransport1 (Phase 0, shadow then flip); cli fallback branch (busctl); docstring | +45/−12 |
| `pi/vibb/bt.py` | `_route_alsa` stack branch + node-aware idempotence; `route` CLI verb; `recover()` units; connect() debug hint | +50/−8 |
| `pi/vibb/output.py` | `audio_ready()` adds `sink_ready` per output under pipewire | +10/−2 |
| `pi/player.py` | `_apply_box_volume` cap (NEW-1); `_confirm_spotify_paused` (NEW-2); ≤5 s `sink_ready` wait before Popen; comment on `--audio-buffer` | +40/−7 |
| `pi/daemon.py` | cap re-apply after go-librespot reopen on local (set_output + `_go_output_rebuild`); `ensure_bt_route` on announce; `audio_policy` in `/status`; local-landing refusal in `set_output`/`play`; self-test thread + `POST /audio/selftest`; comments | +75/−0 |
| `pi/btwatchd.py` | `_await_pcm` also waits for `sink_ready` under pipewire; docstring | +12/−2 |
| `pi/install.sh` | packages branch, pipewire user, 3 units, 4 conf fragments, bluetooth drop-in, masks both ways, `default` override handling, env lines on 3 units, `After=` re-point, `/etc/vibb/audio-stack`, `bt.py route` call, rollback branch | +210/−0 (Phase 4: −60) |
| `pi/extra.sh`
| `pi/extra.sh` | RESTORE set + start loop + `--run` stop of the pipewire trio under pipewire mode | +8/−0 |
| `pi/play.sh` | none (`aplay -D vibb_bt` works through the pcm's `server`) | 0 |
| `pi/soloistd.py` (P1, own plan) | audio binding: name resolution, `--pipewire-device`, null-sink warming, fail-closed bind check | ~+60 inside P1's budget |
| `tests/fake_bluezd.py` | MediaTransport1 objects, `SetTransport`/`DropTransport`, `SetPcm` also creates a `110a` transport | +40/−0 |

Total ≈ **+790 / −30** through Phase 3, then **−~150** in Phase 4 (bluealsa template, keep-alive block, `bluealsa-aplay` cli path, PKGS entries, extra.sh names, PCM1 in the fake). Genuinely new logic ≈ 350 lines (audio.py + the confirm + the self-test).

### Test plan

Existing coupled tests, disposition:
- **Unchanged** (the fake grows additively): `bt_parity.py`, `bt_avdtp_refusal.py`, `bt_play_kick.py`, `status_bt_probe_local.py`, `bt_output_policy.py`, `bt_lost_pause_recover.py`, `bt_stall.py`, `orch_lock_io.py`, `output_reopen.py`, `go_output_rebuild.py`, `go_restart_dedup.py`, `output_switch_resume.py`, `stop_child_group_kill.py` (docstring only), `boot_resume_guard.py`, `player_crash_heal.py`, `sonos_*`, `ui_poller.py`, `wifi_reconnect.py`.
- **Fixture-swapped** (4): `bt_state_fsync.py` (run twice: `VIBB_AUDIO_STACK=bluealsa` as today; `=pipewire` with `audio.resolve_route` stubbed to fixed names — asserts colon MAC in body, one fsync, no rewrite on repeat, ONE rewrite when the node name changes); `extras_wrapper.py` (pipewire-mode run asserts the three extra units); `mpv_launch_flags.py` (extended, not rewritten: still asserts `alsa/vibb_bt`; adds `--audio-buffer` comment pin); `local_volume_cap.py` (premise lines 11-14 extended; adds the `_apply_box_volume` grep + a behavioural fake-API case).
- **Deleted**: none before Phase 4; Phase 4 deletes only the `org.bluealsa` half of `fake_bluezd.py` and `bt_parity.py`'s cli-vs-dbus comparison (the cli path becomes busctl-only).

New pins that must exist and be green **before cutover** (Phase 2 entry gate), one file each, `tests/run_all.py` style:

| Invariant | Test | Mechanism |
|---|---|---|
| I1 | `local_volume_all_paths.py` | fake mpv IPC + fake go-librespot HTTP; drive `set_output(local)` mpv-alive / spotify-live / spotify-resuming, `_go_output_rebuild`, `_bt_blip_resume`, `_heal_crashed_child`, `ORCH.play`; assert observed volume == `min(stored, cap)`, `volume.json` untouched |
| I2 | `audio_client_props.py` | grep the install.sh client.conf fragment for `node.dont-reconnect`/`node.dont-fallback`; every spawn argv (`mpv_command`, config.yml template, soloist argv builder) carries a pinned target |
| I3 | existing `bt_lost_pause_recover.py:48-62` | unchanged |
| I4 | `output_file_single_writer.py` | grep: exactly one `OUT_FILE + ".tmp"` writer in `pi/` |
| I5 | `install_unit_order.py` | grep install.sh: `vibb-bt-reconnect` `After=` names `wireplumber.service` in pipewire mode; `bluetooth.service.d/vibb-after-wp.conf` written |
| I6 | `bt_transport_gate.py` | fake_bluezd: `110a` idle/pending/active → True; `110b` only → False; none → False; cli busctl path against canned JSON |
| I7 | `wp_policy_fragment.py` | parse the fragment install.sh writes: suspend 120 on `~bluez_output.*` only, 5 on alsa; codec/roles/xq/msbc/hw-volume/dummy-player values |
| I8 | `no_volume_writer.py` | grep pin: no `wpctl set-volume`/`set-mute`/`pw-metadata` writer in `pi/` |
| I9 | `spotify_pause_confirm.py` | fake go-librespot answers pause slowly; `Popen` spy sees `paused` before `mpv_command` |
| I10 | `audio_policy_selftest.py` | `policy_selftest()` against canned `pw-dump`/`wpctl settings` outputs: good, and each single drift → the right class; `local_landing_allowed()` false only on `fail-safety` |
| I11 | existing `stop_child_group_kill.py` | unchanged |
| I12 | `audio_route_discovery.py` | canned `pw-dump` with `.1` and `.a2dp-sink` shapes, two bluez sinks, one hifiberry: `find_bt_sink(mac)` picks by `api.bluez5.address`, never composes; `find_local_sink` by card name |
| I13 | `audio_stack_toggle.py` | run install.sh's stack section with a fake `systemctl` (the `extras_wrapper.py:24-31` pattern): pipewire → masks bluealsa; bluealsa → masks socket+service+wireplumber, removes the drop-in and the `default` override, rewrites asound to `type bluealsa` |

### `pi/vibb/audio.py` signatures

```python
STACK_FILE = os.environ.get("VIBB_AUDIO_STACK_FILE", "/etc/vibb/audio-stack")
POLICY_FILE = os.path.join(_RUN, "vibb-audio-policy")
SOCKET = "/run/pipewire/pipewire-0"

def stack() -> str                                   # "bluealsa" | "pipewire"
def server_up() -> bool
def pw_dump(timeout=3.0) -> list                     # [] on failure; never raises
def find_bt_sink(mac, dump=None) -> str | None       # node.name by api.bluez5.address
def find_local_sink(dump=None) -> str | None         # node.name by hifiberry card name
def sink_ready(output, mac=None) -> bool
def resolve_route(mac, tries=10, delay=1.0) -> tuple # (bt_node|None, local_node|None)
def asound_text(mac, bt_node, local_node) -> str
def ensure_bt_route(mac) -> bool                     # True when the file was rewritten
def recover_units() -> tuple
def policy_selftest() -> dict                        # {"safety": [...], "rf": [...], "down": bool}
def selftest_state() -> dict                         # last verdict from POLICY_FILE
def local_landing_allowed() -> bool                  # False only on fail-safety
```

---

## L. Phases with kill criteria

> **[QA-2: Phase 0 ships with AM-12/13/14]** — see the amendments table above; where this section's text conflicts, the amendment wins.

**Phase 0 — on the current stack, shippable now.** (a) MediaTransport1 gate in shadow mode → flip after one week of no disagreement lines; (b) NEW-1 cap fix in `_apply_box_volume` + post-reopen re-apply; (c) NEW-2 `_confirm_spotify_paused`; (d) `pi/vibb/audio.py` with `stack()` returning `bluealsa` and the pure resolver functions (unit-tested against canned dumps, unused in the field); (e) the 7-day baseline capture from QA §4. KILL: any disagreement between the transport and PCM predicates that maps to a field pattern (`bt_avdtp_refusal`'s 1-3 s loop, `_nudge_a2dp`'s connected-without-A2DP) — then the gate stays on PCM1 and the design needs a bench answer before Phase 2.

**Phase 1 — bench (Trixie Zero 2 W, BT speaker + HAT, never the box).** Run QA's B0-B12 in the exact topology of A/B. **Must PASS before Phase 2:** B1, B2, B3, B4, B7, B8, B10, B11; plus the verify tags in this document resolved (each becomes a line in the bench note). B5 and B6 must be measured; B6 may pass with the RT escalation as a documented alternative. B9 gates Phase 3, not Phase 2. B12 is recorded. KILL for the whole migration: any HAT audio in B2/B3, any drift that stays green in B8, any tier in B10 needing a `pipewire.service` restart, AVDTP churn in B4 not matching baseline.
*Optional Phase 1b — "PipeWire owns only the HAT" field trial:* a third toggle value `pipewire-hat` (no `libspa-0.2-bluetooth`, bluealsa unmasked, `vibb_bt` stays `type bluealsa`, `vibb_local` becomes `type pipewire`), ~40 lines in install.sh. It yields real-box RSS/idle-wakeup/xrun numbers at zero BT risk. Recommended **only if the bench is not a Zero 2 W**; otherwise B6 answers the same question. KILL for 1b: idle-suspended wakeups > 50/s or RSS > +60 MB on the box.

**Phase 2 — box cutover behind the toggle.** Rollback drill rehearsed and timed on the bench first (QA §4, < 15 min incl. reboot, once offline). `VIBB_AUDIO_STACK=pipewire ./install.sh`; verify the self-test green, `/status.audio_policy == ok`, one card on BT, one on local, the popup path. Soak per QA §4 (≥14 days, ≥20 streaming hours, ≥5 evening sessions, car if in scope). KILL/abort: any one of QA §4's eight abort criteria → `VIBB_AUDIO_STACK=bluealsa ./install.sh`.

**Phase 3 — soloistd P1 on the new stack** (`docs/PLAN-soloistd.md` P1, with the binding in I). Entry: B9 PASS. KILL: soloist landing on any non-pinned sink, a node leaving `suspended` during warming, or the fail-closed bind check not killing a mis-bound child.

**Phase 4 — delete bluealsa paths.** Entry: Phase 2 soak complete with zero aborts; default toggle flips to `pipewire`; the H "dying references" list is deleted; `fake_bluezd.py` drops `org.bluealsa`; PKGS drops the two bluealsa packages for *new* installs (existing boxes keep them installed but masked; no `apt remove`). KILL: none — this phase is bookkeeping; if the soak aborted, Phase 4 never starts.

---

## M. Answers to QA Q1-Q15

> **[QA-2: Q1, Q3, Q7, Q8, Q11, Q14 as amended]** — see the amendments table above; where this section's text conflicts, the amendment wins.

**Q1.** System-wide (A): three units in `/etc/systemd/system`, user `pipewire` in `audio`+`bluetooth`, socket `/run/pipewire/pipewire-0` at `audio:0660`, `PIPEWIRE_RUNTIME_DIR=/run/pipewire` in the two daemons and as a belt on `vibb-daemon`/`go-librespot`/`vibb-soloistd`; `bluetooth.service` gets `After=wireplumber.service` (ordering only) so the endpoint exists before the radio listens; `vibb-bt-reconnect` `After=bluetooth.service wireplumber.service`. Restore state does not exist because restore is disabled, and WirePlumber still gets `XDG_STATE_HOME=/var/lib/vibb/wireplumber` so it never writes into a nonexistent home.

**Q2.** Mixed, stated per client (C): mpv and go-librespot via pipewire-alsa pcms `vibb_bt`/`vibb_local` with `playback_node` — `OUTPUT_PCMS`, `set_output`, the deferred-switch refusal and `reopen_go_output` survive unchanged and `mpv_launch_flags.py` is extended, not rewritten; soloist native via `--pipewire-device` because it has no ALSA.

**Q3.** `stream.properties { node.dont-reconnect = true, node.dont-fallback = true }` in `/etc/pipewire/client.conf.d/10-vibb.conf`, which every libpipewire client on the box loads (pipewire-alsa's plugin, soloist, pw-cat); `PIPEWIRE_PROPS` on the soloistd unit as a belt; the target itself comes from the pcm (`playback_node`) or `--pipewire-device`. The I2 unit test greps the fragment install.sh writes and the spawn argvs; the self-test asserts the behaviour at boot.

**Q4.** Two predicates (D). `a2dp_pcm_present(mac)` = MediaTransport1 with the host's `0000110a` UUID, any state, D-Bus with a busctl fallback — for every 1/s reader, `set_output`'s gates, `_bt_wait_*`, `_kick_bt_connect`. `sink_ready(output, mac)` = node present in a fork-per-call `pw-dump` — for btwatchd's commit gate, `audio_ready()` (watchdog respawn, crash heal, blip, player watchdog), and a ≤5 s wait before the mpv Popen. No long-lived watcher: it would add a supervised child to vibbd for a predicate polled ≤0.2/s.

**Q5.** `_route_alsa` calls `audio.resolve_route(mac)` which reads `pw-dump` and picks the `Audio/Sink` node whose `api.bluez5.address` equals the MAC (both `.1` and `.a2dp-sink` shapes handled by reading, never building) and the sink whose card name says hifiberry; the file keeps the colon MAC as a comment, and the idempotence check becomes "MAC present AND node name present", so a package upgrade that renames the node rewrites exactly once (C, I12).

**Q6.** Per-engine `min()` clamp applied by vibb for mpv, go-librespot and soloist (via the dialect), fixing NEW-1 in `_apply_box_volume` and after every go-librespot reopen onto local; the HAT sink's graph gain is a pinned 1.0 asserted by the self-test; nothing ever writes a bluez node's volume (F). A HAT-level gain was rejected because it changes what "35" means, stacks with the softvol, and depends on the restore machinery this design disables. The phone-driven Connect session on the HAT stays uncapped and is signed off in N.

**Q7.** Custom whitelist profile PLUS the settings belt PLUS per-stream properties (B); the self-test asserts behaviour (a stream whose target vanishes is destroyed and links nowhere; a targetless stream never links; a default-sink change moves nothing), so a format drift in any one layer is caught at the next boot (B8).

**Q8.** `bluez5.roles = [ a2dp_source ]` (host-centric: the host is the source — B11 must show `bluez_output.*` nodes and no `0000110b` endpoint; if the bench shows the naming is peer-centric, the value flips to `a2dp_sink` and the test stays), `bluez5.codecs = [ sbc ]`, `bluez5.enable-sbc-xq = false`, `bluez5.enable-msbc = false`, `bluez5.enable-hw-volume = false` (or `bluez5.hw-volume = []` in the 0.5 form), plus `bluez5.dummy-avrcp-player = false`, `bluez5.default.rate = 44100`, `bluez5.autoswitch-profile = false`, and `bluez5.auto-connect` deliberately absent.

**Q9.** 120 s on `bluez_output.*` only; the HAT keeps the 5 s default. No transport to renegotiate on I2S, a running silent node keeps the amp awake, and extras need raw `hw:` — `extra.sh --run` stops the pipewire trio in pipewire mode so that path stays deterministic rather than waiting on a suspend timer.

**Q10.** `VIBB_AUDIO_STACK=bluealsa|pipewire` (H): mask never remove, both directions; the rollback masks `pipewire.socket` as well as the two services; removes the `bluetooth.service` drop-in and Debian's `default`→pipewire override; rewrites `/etc/asound.conf` to the bluealsa template via `bt.py route`; `extra.sh`'s restore set carries both stacks' units (masked starts are no-ops); `btbus` keeps one stack-neutral D-Bus gate and a per-stack CLI fallback. I13 is a fake-`systemctl` test plus the rehearsed drill.

**Q11.** Refuse the local landing only (B): safety-class drift → `set_output(local)` and local-output `play()` answer `{"error": "audio-policy-failed"}`, screen line, journal, BT untouched; RF-class drift → warn only; server down → both outputs read not-ready (today's "device absent" popup), no heal budget burnt. This is the only action that satisfies both rules at once: the box is never dead at bedtime (BT works, the state is clear on screen) and the HAT can never be reached through a drifted policy.

**Q12.** Absent, deliberately: five explicit packages with `--no-install-recommends`, no `pipewire-pulse`, no `pulseaudio-utils` on the box. Soloist's Pulse fallback has nothing to fall back to (B9 fail-closed).

**Q13.** No RT in Phase 2 (`pipewire.service` grants no `LimitRTPRIO`, no rtkit; `Nice=-11` = bluealsa's scheduling class). B6 measures xruns under the scan barrage; the escalation is the one-line `LimitRTPRIO=95` with a second B6 run, and the bench note carries both numbers.

**Q14.** `player.py`, `_confirm_spotify_paused()` where the fire-and-forget pause is today (`:432-438`), before the mpv `Popen`, bounded 2.5 s, not under `ORCH.lock` (the R2 rule); the reverse direction is already exclusive via `_stop_child`'s group-gone wait (G, I9).

**Q15.** Nothing, by default: WirePlumber re-registers on `NameOwnerChanged org.bluez` like `vibb-mpris` does, and B10 proves it for all three tiers. `recover_units()` returns `()` under pipewire; if B10 finds a tier that needs it, the escalation is `try-restart wireplumber` behind `VIBB_BT_HEAL_RESTART_WP=1` — never `pipewire.service`, which would kill every client stream.

---

## N. Residual risks accepted (one line each, for the owner to sign)

> **[QA-2: AM-20 (N2, N3, N5, N7, N9 restated; N10-N14 added)]** — see the amendments table above; where this section's text conflicts, the amendment wins.

1. The no-fail-over ban and the cap are now enforced by config in another package plus a boot self-test, not by the absence of a mechanism; B8 and Phase 2's abort criterion 4 are the compensating controls.
2. A Phone-driven Spotify Connect session (not started by vibb) landing on the HAT is not capped — a person is holding the phone.
3. A ~ms window exists where a user-initiated `set_output(bt)` can beat node creation; it self-heals through the player watchdog's `not audio_ready()` rollback, at the cost of one audible hiccup.
4. A mpv **paused** longer than 120 s now releases the transport (bluealsa held it); resume costs one AVDTP Start, no chime on the JBL class, battery-positive.
5. bluetoothd starts ~2-4 s later than today (behind WirePlumber) so the radio never listens without an endpoint; B1's baseline + 5 s bound is the check.
6. +20-34 MB standing RSS (two daemons) on a 430 MB box; B6 bounds it at +40, abort criterion 6 watches growth.
7. The C13 scheduling variable changes shape (a nice -11 graph wakes ~21/s during playback); the open crash model stays open, and abort criterion 5 (≥2× hardware-error rate and ≥3 events per streaming hour) is the field guard.
8. Every `(verify on bench)` key in this document is a config name I could not confirm against 1.4/0.5 from the tree; each has a named bench step, and a wrong name fails loudly through the self-test rather than silently.
9. Rollback correctness depends on the two bluealsa packages staying installed-but-masked and on the `default` override being re-removed; the drill is rehearsed offline before cutover and re-run once after the first `apt full-upgrade`.
10. The go-librespot fork's pulse/pipewire backend is unverified and unused; if a future need arises it is a fork feature, not a vibb one.

---

Design read in full (619 lines; N has 9 items, not 10). Verification of the code claims is done from the tree; findings below.

# PART 2 — QA round 2 (verbatim): attack on the design

Ground rules kept: tree `33af425`, read-only, every code claim cited. Where I assert a PipeWire/WirePlumber fact from knowledge rather than the tree I say so; the design's own **(verify on bench)** tags stay in force.

---

## 1. Per-section verdicts

### A. Topology — NEEDS-CHANGE (four unit-file bugs, one of them a dead-box class)

1. **`RuntimeDirectory=pipewire` on a socket-activated service deletes the socket on every service stop.** systemd removes a `RuntimeDirectory` when the service stops (crash → `Restart=on-failure` → stop → directory unlinked) while `pipewire.socket` still holds the listening fd: every later client connecting by path (`/run/pipewire/pipewire-0`, which the design hard-codes in the pcm `server` key and `remote.name`) gets ENOENT until someone restarts the socket unit. One PipeWire crash = every output dead until reboot. Minimal fix: remove `RuntimeDirectory=`/`RuntimeDirectoryMode=` from `pipewire.service` and let the socket unit own the directory (`DirectoryMode=0750` on `pipewire.socket`), or add `RuntimeDirectoryPreserve=yes`.
2. **`wireplumber.service` is `WantedBy=multi-user.target` with `Requires=pipewire.service`.** A PipeWire crash stops WirePlumber through `Requires=` (a dependency stop is a *clean* stop, so WirePlumber's `Restart=on-failure` does not fire), PipeWire restarts alone, and there is no session manager — no bluez endpoint, no HAT node — until reboot. Upstream avoids this with `WantedBy=pipewire.service`. Minimal fix: `[Install] WantedBy=pipewire.service` + `BindsTo=pipewire.service` on `wireplumber.service`.
3. **The `bluetooth.service` `After=wireplumber.service` drop-in does not deliver its stated guarantee.** `After=` on a `Type=simple` unit is satisfied the moment WirePlumber is *exec'd*, not when its bluez monitor has registered endpoints (which happens only after it sees `org.bluez` on the bus). So "the radio never listens without an endpoint" (A, "Boot ordering") is false; the true statement is "bluetoothd starts a few hundred ms later". The rfkill `ExecStartPre` (`install.sh:446-449`) runs after that wait, so the deafness delta is the spawn latency only — not 25-40 s — but the drop-in buys nothing and adds a dependency edge. Minimal fix: drop the drop-in, keep `vibb-bt-reconnect` `After=bluetooth.service wireplumber.service`, and let B1 + btwatchd's existing nudge (`btwatchd.py:448-462`) be the mechanism; restate N5 accordingly.
4. **`Nice=-11` on both daemons is a scheduling change, not parity.** bluealsa runs at nice 0 today (stock unit, `install.sh:490-522` only edits `ExecStart`). WirePlumber is control-plane and should be nice 0; PipeWire at −11 is defensible but is then part of the C13 delta N7 must name. Minimal: `Nice=0` on both for Phase 2, −11 as the same one-line escalation as RT.
5. Missing belt: `Environment=PIPEWIRE_PROPS={ node.dont-reconnect=true node.dont-fallback=true }` is only on `vibb-soloistd`. Put it on `vibb-daemon` and `go-librespot` too (see 3b) — zero cost, and it makes the `client.conf.d` verify tag non-fatal.

### B. Policy — NEEDS-CHANGE (profile leaks the hooks it claims to omit; failure action does not address the drift it detects)

1. **`policy.linking.standard = required` almost certainly bundles `find-default-target` and `find-best-target`** (in 0.5 the "standard" linking feature is the whole hook set). E step 2's "no default/best-target hooks loaded" is then false: with `policy.default-nodes` disabled a targetless stream still gets *best-target* linking to the highest-priority sink — BT, then HAT. The self-test would catch it on the bench, but the fragment should not ship wrong. Minimal: add `hooks.linking.find-default-target = disabled` and `hooks.linking.find-best-target = disabled` **(verify names)** and make B8 flip each one.
2. **Failure action (Q11) refuses the wrong thing.** Safety assertions 1-3 detect *rescue / fallback / follow-default* — i.e. a stream moving to the HAT **without** `set_output`. Refusing `set_output("local")` and local `play()` does nothing against that: BT playback under a drifted policy is exactly the BLOCKER-1 scenario (headset drops → stream re-homed to the HAT at headphone softvol). "BT untouched" is the unsafe half. Minimal alternative that satisfies both rules: on `fail-safety`, **apply `local_fallback_cap` to every landing regardless of output** (mpv `--volume`, `_apply_box_volume`, the retarget/reopen re-applies all use `min(stored, cap)` with the pcm test at `output.py:39` bypassed) and clamp `Orchestrator.volume()` (`daemon.py:1035, :1047`) to the same cap, plus the screen line. Nothing is refused, so the bedtime rule holds; any landing anywhere is at ≤35 (×1.5 worst-case restored gain = 52 equivalent, still below headphone levels), so the blast rule holds. This also answers 3d: a local-only box keeps playing.
3. Self-test hygiene: the probe must be *silence* and *bounded* (`timeout 2 pw-cat --playback --raw --format=s16 --rate=44100 --channels=2 /dev/zero`; `/dev/zero` never EOFs); assertion 3 must treat "no `default` metadata object exists" as PASS (with `policy.default-nodes` disabled it may not); rate-limit re-runs via `POLICY_FILE` mtime because `vibb-daemon` is `Restart=always RestartSec=5` (`install.sh:1157-1158`) and a crash-looping daemon would fork probes every 5 s. The self-test must also **re-run after a PipeWire/WirePlumber restart** (see A.2): cheapest trigger is the core object's `info.cookie` in the `pw-dump` that `sink_ready` already parses — cookie changed since the last verdict → re-run. No new HTTP surface, no token problem (the `pipewire` user cannot read `/etc/vibb/api-token`).
4. `local_landing_allowed()` is consulted only in `set_output`/`play` (K row for daemon.py). It is not consulted by the watchdog respawn (`daemon.py:609-614`), the crash respawn (`:657-665`), or `_go_output_rebuild` on local (`:4979-4990`). If the cap-everything alternative is adopted this becomes moot; if refusal is kept, hook it into `audio_ready()`'s local branch (`output.py:177-178`) so all respawn paths see it, and make the player's ≤5 s `sink_ready` wait **fail closed** (exit nonzero, log) rather than "spawn anyway" — the design does not say what happens at 5 s.
5. `bluez5.enable-hw-volume = false` — see J; I think this is the wrong value.

### C. Node naming / `_route_alsa` — NEEDS-CHANGE (a new second writer of `asound.conf` with no lock)

- **Race:** today `_route_alsa` has exactly one writer, `bt.py` under the process flock (`bt.py:684`, `_RADIO_CMDS`). The design adds `audio.ensure_bt_route(mac)` from the daemon's announce thread with no lock. Two concurrent writers share `ASOUND + ".tmp"` (`bt.py:442`): B's `open("w")` truncates A's tmp between A's fsync and A's `os.replace` → A renames a truncated file into place → the "both pcms gone, every output silent, nothing heals it" catastrophe the fsync was added for (`bt.py:459-463`); B's `os.replace` then raises unhandled. Minimal: tmp name `ASOUND + f".tmp.{os.getpid()}"` **and** `acquire_process_lock()` around the rewrite in `ensure_bt_route`.
- **Double reopen:** `_route_alsa`'s tail reopens go-librespot when output is bt (`bt.py:472-479`); the announce path it is inserted into also reopens at `daemon.py:1749`. Two live reopens per reconnect is the 2026-07-17 "storm" class (`daemon.py:4812-4816`). Minimal: `ensure_bt_route` writes the file only (a lower-level writer) and leaves the reopen to the caller.
- **Placement:** the design inserts it "before the deferred apply (`:1732-1756`)", but that branch runs only when output is *already* bt (`:1732`). When output is local and btwatchd announces bt, the full path (`:1757+`) retargets mpv at `alsa/vibb_bt` (`:1789-1792`) with a possibly stale node. Hoist to right after `pcm = OUTPUT_PCMS.get(device)` (`:1723`) under `if device == "bt" and fallback`.
- Confirmed: the fork's v0.1.5 `snd_config_update_free_global` before every PCM open (`install.sh:153-157`) is what makes a rewritten `playback_node` take effect on `reopen_go_output` — the claim holds.

### D. Gate — CONFIRMED with three fixes

- Confirmed: `_heal_crashed_child` increments `_crash_respawns` only after `_audio_ready()` passed (`daemon.py:655` returns before the `+= 1` at `:660`), so "server not up" burns no budget. Also confirmed: `_stop_child` waits for the group to be gone, bounded at 2 s (`:709-714`).
- **Fix 1:** the deferred apply at `daemon.py:1735-1740` is gated on `_bt_transport_ready()` (D-Bus) and points a *live* mpv at `alsa/vibb_bt`; the design says node presence is "guaranteed by the announce gate" — but the announce and this call are the same HTTP request, and btwatchd's `_await_pcm` commit is one process removed from mpv's open. That is N3's window. It is on the announce path (not 1/s), so use `sink_ready("bt", mac)` there too. Cost: one fork per announce.
- **Fix 2:** shadow mode needs numbers. Expected disagreement: only `transport=True/pcm=False`, only inside a connect, never persisting. Criteria: (a) zero disagreements lasting ≥ 2 consecutive polls; (b) zero in the `transport=False/pcm=True` direction; (c) zero transport appearances that vanish within 3 s without PCM1 ever True (the flicker that would make `_await_pcm` commit on a refusing peer — `btwatchd.py:443-446` commits on the first True, and a committed drop is treated as a blip that resets the ladder, `:239-248`). Log direction and duration, not just "disagree". Rate-limit the shadow read itself (it doubles bluez `GetManagedObjects` traffic at 1/s while output=bt — the `/status` probe already has a regression history, `daemon.py:2998-3001`): compare 1/10 s, not every call.
- **Fix 3 (3f):** the ≤5 s wait is in the player child (design row "before `Popen`", `player.py:545`), so it holds no daemon lock; next/prev are IPC to the live mpv, no respawn, no wait. Latency lands only on spawns (tap, blip/watchdog/crash respawn) and only when the node is absent. Worst-case forks: a kid mashing tiles → one child per tap → ≤ (5 s / poll interval) forks per child; the python start-up (~300 ms on a Zero 2 W) dominates. Specify the poll interval (0.5 s) and the fail-closed exit at 5 s.

### E. Loss contract — CONFIRMED, with the nuance stated honestly

- Sub-second stop: btwatchd's `_notify_lost` is a 3 s-timeout POST fired from the BlueZ signal handler (`btwatchd.py:546-559`); the daemon stops under `ORCH.lock` (`daemon.py:4913-4918`) with `terminate()` + `wait(10)`. Sub-second holds when the player exits promptly on SIGTERM — true for an advancing mpv, not for a write-blocked one (then 8 s `player.py:559`). Both shapes are covered; the design says so.
- Storm shape: whether pipewire-alsa surfaces the destroyed stream as EIO (AO reload → reopen fails → advance) or EPIPE (mpv `snd_pcm_recover` loop → reload) is the errno the design already flags; either way the end state is "advance storm or frozen position", both already detected (`player.py:744-746`; `daemon.py:465-479`). What the design should *also* record in B2: **tracks advanced before stop and the resume-position delta**, against the bluealsa baseline — the bookmark is rewritten on every path change (`player.py:791-795`) so a 3-track storm resumes 3 tracks late unless `survive_dead_audio`'s rollback (`:635-647`) wins the 3 s poll race. Pre-existing, not new, but it is the number the owner will notice.
- 3g: `audio_ready()` in the player's track-change branch (`player.py:744-745`) now forks `pw-dump` once per track change on the 3 s poll thread, not on the button path. A kid mashing next → one fork per change (30-60 ms). Acceptable; note it in the docstring.

### F. Cap — NEEDS-CHANGE (the fix is in the right function but at the wrong moment)

- Scaling verified: `/player/volume` takes `round(v * steps / 100)` with `steps = volume_steps` (`player.py:172-173`, `daemon.py:1044-1049`). Inserting `v = local_volume(v, output_pcm(), cap)` before that line clamps the 0-100 value once — no double scale. The post-reopen re-apply in `set_output` uses the same scaling — consistent.
- **Ordering bug:** `_apply_box_volume()` is called at `player.py:316`, *after* `/player/play` at `:290`. Audio starts on play; the (now capped) volume lands 0.5-2 s later. That is a burst of uncapped HAT audio on every vibb-started Spotify landing — the residual N2 attributes to phone-driven sessions only. Minimal: call the capped apply **before** the play POST (after the session wait at `:243-249`), keeping the post-play call as belt **(verify `/player/volume` is accepted with no track loaded)**.
- Race with a concurrent volume press (3h): `Orchestrator.volume()` POSTs under `self.lock` (`daemon.py:1029-1052`); the re-apply runs outside it. Both orderings end ≤ cap (the re-apply writes `min(saved, cap)`, the press writes `≤ volume_cap` from a fresh base at `:1043-1046`), so the race is benign — the worst case is one lost press. Acceptable; say so.
- Phone-driven residual: correctly does not cover vibb's own paths — the one-tap and blip resume go through `_spawn` → `play_spotify` → `_apply_box_volume` (fixed), `_go_output_rebuild` gets the re-apply. With the ordering fix above, N2 is genuinely phone-only.

### G. Mixing — NEEDS-CHANGE (the wedge case is the field case)

`_confirm_spotify_paused` returns **True on any `OSError`** ("not running = not playing"). The crash memory's self-heal gap is precisely a go-librespot that is *playing* while its API is in a timeout storm for minutes; `spotify.go("/player/pause", timeout=1)` then raises on timeout → True → mpv spawns → double audio, which is the case NEW-2 exists for. Minimal: distinguish `ConnectionRefusedError` (not running → True) from timeouts (unknown → keep trying to the 2.5 s budget); on budget exhaustion with timeouts, `systemctl try-restart go-librespot` before spawning — the memory's own "deterministic medicine", and the API being unresponsive to a pause for 2.5 s *is* the wedge signature. Cost: 2.5 s + a restart once per play during a wedge (vs double audio). Boot warm-up (`player.py:433-435`) is handled by `status()` answering "no track" quickly. Unit pin: fake go-librespot that times out `/player/pause` while reporting `paused: false` must produce the restart, not a spawn.

### H. Toggle — NEEDS-CHANGE (one functional regression for extras)

- **Extras lose `default`.** `docs/extras.md:85` says extras play out the I2S DAC; SDL/pygame extras open ALSA `default`. Under pipewire mode the design keeps the override so `default` fails closed; `extra.sh --run` stops the trio, after which `default` → pipewire pcm → ECONNREFUSED. Today `default` is the HAT card. Minimal: in the vibb-written `/etc/asound.conf` (loaded after `conf.d`, so it wins) define `pcm.!default { type plug slave.pcm { @func getenv vars [ VIBB_ALSA_DEFAULT ] default "vibb_closed" } }` with `pcm.vibb_closed` a pipewire pcm with no node; `extra.sh --run` exports `VIBB_ALSA_DEFAULT=hw:sndrpihifiberry` for the extra only. Everything else stays fail-closed.
- `play.sh test` (`play.sh:96-100`) runs `bt_py ensure` first, so the tone follows a resolved route; with no speaker the fail-closed `vibb-unresolved` **errors** where today's `plug->null` **silently succeeds** — that is the false-pass trap the rig documents (`pipewire_shim_rig.sh:58-73`); the loud error is better. CONFIRMED.
- `systemctl mask --now` on the socket: stop order must be wireplumber → pipewire.service → pipewire.socket; `mask --now` on the socket closes the listener, existing client fds survive until the service stops. Fine as long as the order is written down.
- Apt-recreated `99-pipewire-default.conf` after rollback is neutralised by the same `pcm.!default` in `/etc/asound.conf` (bluealsa mode: `default` → `hw:sndrpihifiberry`). That removes N9's second clause.

### I. Client binding — NEEDS-CHANGE (soloist lazy-stream case)

3j: the fail-closed check "polls `pw-dump` ≤5 s after the child starts". Soloist is a closed binary; nothing in `PLAN-soloistd.md` says whether its `pw_stream` exists before the first play. If lazy, "no node within 5 s" must be *unknown*, not pass; and a mis-bound stream created on first play is never checked. Minimal: run the bind check at start (informational) **and** within 2 s of the first `playback_state: playing` event (authoritative): not linked to the intended sink → `pause` immediately, kill, closed shape. B9 records which case soloist is.

### J. Resource/RF — NEEDS-CHANGE on one key, CONFIRMED otherwise

- `bluez5.dummy-avrcp-player` is a real property (PipeWire ≥ 0.3.5x; "some devices require an AVRCP player to send volume events"), and with `vibb-mpris` registered (`mpris.py:256-270`) the headset has a player anyway. CONFIRMED (keep the verify tag).
- **`bluez5.enable-hw-volume = false` is likely the wrong value.** With hw-volume off PipeWire ignores the transport's `Volume` entirely; an absolute-volume headset (JBL class) does not attenuate locally — it sends `VolumeChanged` expecting the *source* to attenuate — and bluealsa does exactly that today in software. Result: the kid's headset volume buttons stop working (vibb-buttons only sees *passthrough* keys, `buttons.py:48-49`, which an absolute-volume headset does not send). With hw-volume **on**, PipeWire mirrors the headset's volume into the node without attenuating and writes `MediaTransport1.Volume` only when the *node* volume changes — which vibb never does and restore is disabled — so it is RF-neutral except possibly one sync PDU at acquire. Minimal: set `true`, add to B11: press the headset's own volume button during playback → audible change; btmon shows only the headset-originated notification and at most one `SetAbsoluteVolume` at connect.

### K. Change list / tests — NEEDS-CHANGE (four missing pins)

Coverage of I1-I13 is complete by name. Missing: (1) `player_sink_wait_fail_closed.py` — the 5 s wait exits nonzero, never spawns; (2) `spotify_volume_before_play.py` — the capped `/player/volume` POST precedes `/player/play` (Popen/HTTP spy order); (3) `asound_second_writer.py` — two concurrent `ensure_bt_route`/`_route_alsa` calls never truncate (unique tmp + flock spy, the `bt_state_fsync.py` style); (4) the G wedge case above. Also `audio_client_props.py` should assert `PIPEWIRE_PROPS` on all three units, not one.

### L. Phases — CONFIRMED with Phase-0 caveats

Phase 0 items (a) shadow gate: not zero-risk at 1/s — rate-limit the shadow compare (D fix 2). (b) NEW-1: user-visible (the phone shows 35 % when the box is on local); fine, but ship with the F ordering fix or it is half a fix. (c) NEW-2: **not** zero-risk as written — the wedge shortcut (G); with the fix it is safe. (d) `audio.py` pure resolver: zero-risk. Phase 1b (`pipewire-hat`) is a good idea only if the bench is not a Zero 2 W — agreed.

### M. Answers — as modified above

Q1 (A.1-A.4), Q3 (add `PIPEWIRE_PROPS` everywhere; the `client.conf.d` `stream.properties` claim is ~80 % from my knowledge — `pw_stream_new` merges the context's `stream.properties`, and the ALSA plugin creates a normal context — but the env belt costs nothing), Q7 (B.1 hook names), Q8 (hw-volume), Q11 (cap-everything), Q14 (G). Q2, Q4-Q6, Q9, Q10, Q12, Q13, Q15: CONFIRMED.

---

## 2. Code-claim verification summary

| Claim | Verdict |
|---|---|
| `_apply_box_volume` scales by `volume_steps`; cap before scaling = no double scale | True (`player.py:171-173`); but applied **after** play (`:290` vs `:316`) — fix F |
| `_heal_crashed_child` spends budget only after `_audio_ready()` | True (`daemon.py:655` before `:660`) |
| `_stop_child` waits for the group to be gone | True, bounded 2 s (`:709-714`) |
| fork v0.1.5 `snd_config_update_free_global` makes a rewritten pcm take effect on live reopen | True (`install.sh:153-157`) |
| `mpris.py` re-registers on `NameOwnerChanged` | True (`mpris.py:272-281`, 2 s delay) — WirePlumber's equivalent is a bench claim (B10) |
| `ensure_bt_route` insertion at `:1732-1756` | Wrong branch (only when output already bt) and it double-reopens — fix C |

---

## 3. Specific attacks — resolved above by letter

a → A.3 (ordering is exec-time, not endpoint-time; rfkill unaffected; no 25-40 s regression; `DefaultDependencies` default is fine for both units; `boot_bind_first.py` is untouched — vibbd stays unordered). b → B.1/E (destroy-on-`dont-reconnect` matches my knowledge of `policy-node`'s `request_destroy` path; errno is the open item; `PIPEWIRE_PROPS` is the fallback and should be default). c → B.3/B.4 (`pw-cat`/`pw-cli`/`pw-dump`/`pw-metadata` are in `pipewire-bin`, `wpctl` in `wireplumber`; probe = zeros, bounded; `local_landing_allowed()` misses the respawn paths). d → B.2. e → D fix 2 (`_await_pcm` commits on the first True and a committed drop resets the ladder, `btwatchd.py:239-248, :443-446` — hence the flicker criterion). f → D fix 3. g → E. h → F. i → H. j → I. k → J. l → K/L.

---

## 4. Residual risks (N)

1. AGREE, but the compensating control is incomplete until (i) the failure action caps everything (B.2) and (ii) the self-test re-runs after a PipeWire/WirePlumber restart (B.3).
2. LARGER than stated: vibb-started sessions are also uncapped for the first 0.5-2 s (`player.py:290` → `:316`). Fixable; should not be a residual.
3. LARGER than stated: "one audible hiccup" is up to several advanced tracks plus rollback (E); and it is cheap to close (D fix 1).
4. AGREE; add the chime check for the son's headset to B4 (pause 130 s, resume, listen).
5. RESTATE: the ordering does not make the endpoint precede the radio (A.3); the honest residual is "a headset paging in the seconds between bluez up and endpoint registration costs one nudge (~10 s)".
6. AGREE.
7. AGREE; add that `Nice=-11` is itself part of the delta unless A.4 is taken.
8. AGREE.
9. AGREE; `pcm.!default` in vibb's own `asound.conf` removes the apt-recreation clause (H).

Forgotten residuals: (10) absolute-volume headsets lose their buttons under hw-volume=false (J); (11) `_confirm_spotify_paused` vs a wedged go-librespot (G); (12) a second `asound.conf` writer (C); (13) extras' `default` (H); (14) pre-existing, worth one line: an AO-failure storm that finishes a *short* queue exits 0 and wipes the bookmark (`player.py:826-827`) if it beats the 3 s poll — unchanged by the migration, but now also reachable via "server not up" on a two-file queue unless the player's 5 s wait fails closed.

---

## 5. Go / no-go

**Go for Phase 0 now**, provided two of its four items ship in corrected form: the NEW-2 confirm loop must treat timeouts as *unknown* and restart a wedged go-librespot rather than spawning over it (G), and the NEW-1 cap must land before `/player/play`, not after (F); the shadow gate needs the rate limit and the three numeric disagreement criteria (D). The design's stance — keep every vibb mechanism and make PipeWire look like ALSA — is the right one and survives this round; what does not survive is the failure action and the unit lifecycle. **Top three to fix before Phase 1 bench work:** (1) B.2 — replace "refuse local, BT untouched" with "cap every landing on `fail-safety`", because the refusal does not address the drift the self-test detects and the refusal leaves a local-only box dead at bedtime; (2) A.1/A.2 — drop `RuntimeDirectory=` from the socket-activated `pipewire.service` and make `wireplumber.service` `WantedBy=pipewire.service`/`BindsTo=`, then add the cookie-triggered self-test re-run — without these a single PipeWire crash is a silent dead box, which is the one outcome every rule in this codebase forbids; (3) C — a locked, uniquely-named, reopen-free `ensure_bt_route` hoisted above `daemon.py:1732`, plus `sink_ready` on the deferred apply at `:1735`, so the migration does not introduce the truncated-`asound.conf` failure the fsync work closed and does not reintroduce the reconnect double-bounce.

---

# APPENDIX A — QA round 1 (verbatim): register re-verified, NEW-1..7, invariants I1-I13, bench protocol B0-B12, soak/rollback, the 15 questions

Scope note: everything below is grounded in the tree at `33af425` (2026-09-01). Where I state a PipeWire/WirePlumber default I mark it **(verify on bench)** unless I am confident of it; the design is not in hand, so anything that depends on it is phrased as the question the design must answer.

---

## 1. Register re-verification against current code

### BLOCKER-1 — WirePlumber stream-rescue defeats the no-fail-over ban and the cap

**Still true under default policy, and the two cited anchors are intact:** the ban is `pi/daemon.py:4880-4885`; the cap is `pi/vibb/output.py:22-41` (a `min()` clamp, applied at use, keyed on `pcm == OUTPUT_PCMS["local"]` at `:39`), applied at mpv spawn `pi/player.py:542-546` and at the live retarget `pi/daemon.py:1799-1802`. A WirePlumber-initiated move goes through neither.

**What it understated:** the cap is not "on every path" even today. See NEW-1 — Spotify on the HAT is uncapped on every path. The migration is where that has to be fixed, because Soloist adds a third uncapped client.

**What it overstated:** "configurable off" is the whole mechanism, and it is more than one config line: PipeWire stream properties `node.dont-reconnect` and `node.dont-fallback` (per client), plus the WirePlumber 0.5 settings `linking.follow-default-target` / `linking.allow-moving-streams` / `node.stream.restore-target` **(verify names on bench, `wpctl settings`)**. The honest residual is exactly the register's point: a safety property enforced by absence becomes one enforced by config in another package. That is what I2/I10 below convert into a behavioral boot self-test, so a wrong config fails loudly rather than blasting.

### BLOCKER-2 — WirePlumber is mandatory, so "scoped" containment is unavailable

**Mandatory: true** (PipeWire's bluez5 monitor is loaded and driven by the session manager). **"Unavailable": overstated.** WirePlumber 0.5 (Trixie) has a profile system; a custom profile can load the bluez and alsa monitors and the defined-target linking hook while *omitting* `find-default-target`, `restore-stream`, `default-nodes` and friends **(verify component names on bench)**. That is a whitelist, not a blacklist — the same containment posture the shim rig took (`bench/pipewire_shim_rig.sh:124-128`). The self-test must then assert behavior, not just settings (I2).

### BLOCKER-3 — `audio_ready()` has no cheap replacement; vibbd is stdlib-only

**Call-site map is still accurate.** `output.py:174-186` → `btbus.a2dp_pcm_present` (`btbus.py:372-384`, D-Bus backend `:593-603` filtering on `/dev_<MAC>/` `:596` and `Mode == "sink"` `:601`). Consumers: watchdog respawn loop `daemon.py:583-601`, crash heal `:655`, `_bt_transport_ready` `:4682-4689` used by `set_output` `:1735/:1780/:1823`, `/status` icon `:2997-3006`, `/system` `:3214`, `_bt_wait_advance` `:5056`, `_bt_wait_state` `:5104`, `_kick_bt_connect` `:5128`; btwatchd `_await_pcm` `btwatchd.py:428-465`; `bt.py connect()` `:382-394`; player watchdog `player.py:653, :745`.

**Two corrections.** (a) "stdlib-only" is already "apt-only": `btbus.py:75` imports `dbus` (python3-dbus, installed by `install.sh:122`). (b) "fork per poll or a venv sidecar" is a false dichotomy: one long-lived `pw-dump -m` (or `pw-mon`) child with a reader thread maintaining a node-name set is stdlib-compatible, one process, no per-poll fork. Combined with the register's own MediaTransport1 suggestion (which I endorse — it is the backend-neutral "AVDTP configured" signal for btwatchd's commit gate and the icon), this is a design item, not a blocker. The gate then has two levels, see I6.

### SEVERE-1 — keep-alive becomes node-suspend with different semantics, default 5 s

**The default trap is real** (WirePlumber `session.suspend-timeout-seconds` default 5 s). **"Not transferable" is overstated:** at the radio the mechanism is the same — bluealsa's `--keep-alive` (`install.sh:475-522`) delays the transport release, i.e. the AVDTP Suspend; PipeWire's node suspend triggers the same transport Release/Suspend. 120 s transfers as the starting value; B4 verifies the PDU count is equal. What genuinely differs: the suspend timer is per *node*, so an orphan stream that keeps the node running defeats it entirely (NEW-7).

### SEVERE-2 — a second, faster, invisible loss detector

**Conditional, not standalone.** If I2 holds (no rescue, no fallback), WirePlumber's reaction to the BlueZ event is "unlink the stream" — silence — and vibbd's stop (`daemon.py:4913-4918`, spotify pause `:4924-4930`) arrives milliseconds later to an already-silent box. The ordering hazard exists only when rescue is on. What survives from SEVERE-2 is the *output.json truth* point (I4): nothing may change routing without `set_output` writing `daemon.py:1759-1761`. The one open measurement is what the *client* does when its stream is unlinked — blocked write (today's zombie shape, handled by the 8 s SIGKILL `player.py:559` and killpg `daemon.py:699`) or an AO error that makes mpv advance tracks (the "jumps like crazy" class). B2 measures it for both `--ao=alsa`-via-pipewire and `--ao=pipewire`.

### SEVERE-3 — the deferred-switch protocol and the v0.0.7 live reopen are at risk

**Mitigated if the design keeps the ALSA-name indirection.** `vibb_bt`/`vibb_local` as pipewire-alsa pcms with `playback_node` preserve `OUTPUT_PCMS` (`output.py:14-15`), the deferred-switch refusal (`daemon.py:1780-1787`, `:1823-1830`), and `reopen_go_output` unchanged (`output.py:139-155`; go-librespot keeps `audio_backend: alsa`, `install.sh:268-269`). The risk moves to node-name discovery in `_route_alsa` (NEW-5) and to what a pcm does when its node is absent (B2/B7). Native `--ao=pipewire` is the higher-churn path (`tests/mpv_launch_flags.py:29,36` pin `--ao=alsa` and `alsa/vibb_bt` literally — they fail loudly, which is fine, but the reopen endpoint semantics for a native go-librespot backend are unknown).

### SEVERE-4 — two sessions or an unsupported system-wide instance

**Still true.** vibbd has no `User=` (`install.sh:1154-1158`) so every mpv is root; go-librespot is `User=$RUN_USER` (`:542`); soloistd will be the service user. System-mode needs the socket dir/env in all three units, socket group perms, and the `pipewire` user in `bluetooth` (bluez's D-Bus policy) and `audio`. Additional item the register missed: system-mode WirePlumber has no sane state dir for `restore-*` modules — either disable them (preferred, I2) or set `XDG_STATE_HOME` explicitly, or restores silently no-op on one box and work on another.

### SEVERE-5 — boot-time BT readiness regresses

**True for a user session; a measurement for system mode.** The rfkill drop-in (`install.sh:446-453`) is unaffected. The register missed that btwatchd already self-heals a late endpoint: `_await_pcm` (10 tries, `btwatchd.py:428-465`) → one `Device1.Connect` nudge (`:471-512`) → commit-anyway (`:462`). So the failure mode is a 10-20 s penalty plus a burnt boot-fail (`:707-724`), not deafness. `vibb-bt-reconnect.service` is ordered `After=bluealsa.service bluealsad.service` (`install.sh:629`) and must be re-pointed. B1 measures.

### MODERATE-1 — mpv flags

**Accurate.** `--audio-samplerate/--audio-channels` (`player.py:134`) are mpv filters and survive; `--audio-buffer=0.5` (`:142`) "becomes decorative" is a *hypothesis* — B2/B6 test it with the 25 s-cadence scan barrage from the crash memory and count xruns.

### MODERATE-2 — a third volume authority

**Overstated by one:** the headset's AVRCP volume is already a second authority under bluealsa (its software volume scaler follows the peer's absolute-volume notifications). What PipeWire genuinely adds: (a) a gain stage on the **HAT** path, which is single-gain today (`plug -> hw:sndrpihifiberry`, `bt.py:454-457`, no softvol between mpv and the amp); (b) persistence via restore-stream/restore-device. And one new hazard the register did not name: if vibb ever implements the cap by writing the *BT sink node's* volume, `bluez5.enable-hw-volume` (default true, I am confident) turns that into AVRCP SetAbsoluteVolume over the air mid-stream — the channel-ops-while-streaming class the crash memory now ranks as the mechanism "entirely inside our control" (2026-08-13 snoop section). See I8. `pi/mpris.py` registers no `Volume` property (`mpris.py:109-115`), so it is not a writer.

### MODERATE-3 — a2dp-sink role and default-sink theft

**Overstated.** bluealsa registers the A2DP sink profile by default too (`install.sh:105` installs stock `bluez-alsa-utils`, no `-p` override in `:490-522`), so a bonded phone can already stream into the box today and produce silence. `Pairable=true` is permanent and pre-existing (`btwatchd.py:784-786`, `bt.py:106`); Discoverable is only up in `visible` windows with a dead-man timeout (`btbus.py:816-819`). WirePlumber does not auto-loopback an A2DP source by default **(verify B3)** — PulseAudio did. Pinning `bluez5.roles` to the source role only makes the surface *smaller* than today. The intersection with `vibb-security-gate` is nil: that gate is HTTP-token (memory), the BT surface is BlueZ bonding, unchanged. "Default-sink theft" is real only for a stream that is not pinned — I2.

### MODERATE-4 — resources

**Correctly marked unmeasured.** B6 measures with the same method on both stacks. Note: `pipewire-pulse` is optional — not installing it means Soloist cannot silently fall back to Pulse and land on the default sink (B9). The memory ceiling context is `install.sh:906-925` (backup capped, zram present).

### MODERATE-5 — cannot coexist; cutover is atomic

**Reason wrong, conclusion right.** BlueZ accepts multiple endpoint applications; two registered A2DP-source endpoints do not error — the peer's SetConfiguration lands on whichever endpoint BlueZ tries first, so coexistence is *undefined ownership*, which is worse. Mask bluealsa. **"Not field-reversible" is overstated** if the design ships an install toggle with both backends kept, the `VIBB_BT_BACKEND=cli|dbus` precedent (`btbus.py:64-81`). NEW-6 lists what a real rollback must cover.

### Register housekeeping
- `NOTES-soloist.md:518-524` does not exist (the file is 72 lines); the 90-day fuse is `docs/NOTES-soloist.md:23-24`.
- "boot resume" as a HAT-landing path is gone: boot starts no audio (`daemon.py:874-892`, `tests/boot_resume_guard.py:1-16`). The one-tap-after-reboot (`daemon.py:5429-5435`) is the path that remains.
- The `fallback=True` local path is now only "speaker forgotten" (`btwatchd.py:349-350`); the drop path stopped calling it on 2026-08-13 (`btwatchd.py:729-741`). In `set_output` that path caps mpv (`daemon.py:1799-1802`) and does not cap go-librespot (`:1831`).

### NEW findings

**NEW-1 — Spotify on the HAT is uncapped, today.** `player.py:162-176 _apply_box_volume` sends `volume.json` straight to `/player/volume` (called at `:316` on every spotify spawn); `daemon.py:1043-1052` clamps to `volume_cap` only (`:992`), never `local_fallback_cap`; the live reopen onto `vibb_local` (`daemon.py:1831`), the restart retarget (`:1853`) and `_go_output_rebuild` (`:4980`) carry whatever volume the session had. The cap test's own premise enumerates only "a fresh mpv, and the live retarget" (`tests/local_volume_cap.py:11-14`). Today every such path has a person pressing something (PWA/screen output switch, the popup's A at `ui.py:3620`, or the phone as Spotify remote), which is why it never bit. Under PipeWire the cap's implementation point is being re-decided anyway, and Soloist is a third client — decide once, for all three (I1, architect Q6).

**NEW-2 — EBUSY was the accidental mutual exclusion; mixing removes it.** Switching from a spotify target to an mpv target pauses go-librespot only best-effort with a 1 s timeout (`player.py:432-438`); `ORCH.play` stops only the mpv child (`daemon.py:946`), and for spotify that child has long exited. Today a failed pause meets bluealsa's exclusive PCM. Under PipeWire both play. Unit-testable (I9).

**NEW-3 — the local readiness gate is a kernel check, not a server check.** `output.py:97-102 _i2s_card_present` reads `/proc/asound/cards`; `audio_ready()` returns it for local (`:177-178`); `set_output` uses it at `daemon.py:1726, :1882`, the popup at `:3011`. Under PipeWire "HAT present" is not "HAT playable": the first tap after a reboot (`daemon.py:5429-5435`, vibbd deliberately unordered at basic.target `install.sh:1146-1152`) and the crash respawn (`daemon.py:655`) can spawn before the socket exists; `_heal_crashed_child`'s 2-per-boot budget (`:645, :660`) is then burnt by "server not up yet" and the box goes silent until a tap.

**NEW-4 — the default codec/profile set changes the RF variable under investigation.** Today: SBC only, bitpool default (~328-345 kbps), A2DP-only profiles, no HFP (bluealsa defaults). WirePlumber defaults enable `bluez5.enable-sbc-xq = true` (512-552 kbps, ~50% more airtime — the inverse of mitigation ladder item (3) in the crash memory), `bluez5.enable-msbc`, `bluez5.enable-hw-volume`, and roles including HFP/HSP AG — a second profile the headset will bring up at connect (RFCOMM SLC, AT exchange) exactly in the fragile window. Additional codecs (AAC/aptX/LDAC if built) change which codec the JBL negotiates. Every one of these is a change to the coex/scheduling variable the memory calls open. Must be pinned and verified with btmon (B11).

**NEW-5 — `_route_alsa`'s idempotence and its test depend on the colon-MAC literal.** `bt.py:433-436` returns early when `mac` is in the file; `tests/bt_state_fsync.py:44,47-48` asserts the MAC in the body and no rewrite on repeat. PipeWire node names use underscores and a version-dependent suffix (`bluez_output.AA_BB_..._FF.1` vs `.a2dp-sink`) — a template without the colon MAC rewrites on every connect (SD wear, and a live go-librespot reopen every time, `bt.py:472-479`). The node name must be *discovered* from the graph, never composed, and the file must still carry the colon MAC (a comment is enough).

**NEW-6 — rollback has more moving parts than "re-run install.sh".** `install.sh:127-130` only apt-installs *missing* packages, so a cutover that removes `bluez-alsa-utils`/`libasound2-plugin-bluez` (`:105`) makes an offline rollback impossible — mask, never remove. `pipewire.socket` revives a masked service on first client connect; pipewire-alsa's `conf.d` file rewrites ALSA `default` (vibb never uses `default`, but `play.sh:99` and extras might). `pi/extra.sh:41,72,113` restore/stop `bluealsa` by name and `tests/extras_wrapper.py` pins that set. All must be in the drill (section 4).

**NEW-7 — orphan semantics invert.** Today an orphan mpv blocks (EBUSY, silent); under PipeWire an orphan on a live sink keeps *playing* under the next spawn and keeps the node running — no suspend, no transport release, so I7 and battery die silently. `tests/stop_child_group_kill.py` (real processes, `:50-71`), `daemon.py:680-715` (`killpg` `:699`, group-gone wait `:706-714`) and `player.py:559` protect *more* under PipeWire, not less — keep them exactly as they are, and add the orphan check to B5.

---

## 2. INVARIANTS — must be pinned by an automated test before cutover

Test style: plain python scripts under `tests/`, run by `tests/run_all.py:2-15` one process each, stdlib + optional python3-dbus, fakes like `tests/fake_bluezd.py` (Mock signatures frozen, grow additively, `:46-47`). "Source-grep" tests (the `local_volume_cap.py:58-79` style) are acceptable only as belts on top of a behavioral pin.

**I1 — no-blast: the cap is applied on every HAT landing, for every engine.**
Property: any stream that lands on the HAT plays at `min(stored, local_fallback_cap)`; never written back.
Today: mpv spawn `player.py:542-546`; mpv live retarget `daemon.py:1799-1802`; **go-librespot: nowhere** (NEW-1).
PipeWire break: a rescued/moved stream bypasses both; a graph gain (HAT sink volume, restored stream volume) multiplies the softvol; Soloist is a third client.
Test: (a) enumerate landing paths by *behavior*: fake mpv IPC + fake go-librespot API; drive `set_output(local)` for mpv-alive, spotify-live, spotify-resuming (`daemon.py:1814-1817`), `_go_output_rebuild` with output=local, `_bt_blip_resume`, `_heal_crashed_child` respawn, `ORCH.play` first-tap — assert the volume observed by the fake sink equals `min(stored, cap)` on each, and `volume.json` unchanged. (b) Extend `tests/local_volume_cap.py` premise line 11-14 to list all paths. (c) Bench: B7.

**I2 — no landing path exists that vibb did not choose.**
Property: a stream with no explicit target does not link anywhere; a stream whose target disappears is not relinked; default-sink changes move nothing; restore-* modules restore nothing.
Today: enforced by absence (ALSA pcm names are the only routes, `output.py:14-15`, `bt.py:443-457`).
PipeWire break: default policy does all four.
Test: this is the policy self-test (I10) — *behavioral*: at boot, after WirePlumber is up, spawn a 1 s silent stream (`pw-cat`/`aplay` of zeros to a pcm with no `playback_node`) and assert via `pw-dump` it has no link; spawn a pinned stream, remove its target (`pw-cli destroy` of a null sink used as the target), assert no link appears to any other sink. Unit-level: assert every spawn argv (`player.mpv_command`, go-librespot config, soloist argv) carries a pinned target and `dont-reconnect`/`dont-fallback` (extend `tests/mpv_launch_flags.py`).

**I3 — single-writer loss ordering: vibb pauses; nothing re-routes.**
Property: on transport loss with output=bt, mpv is stopped / spotify paused, `_BT_WAIT["lost"]` armed, no retarget to local, heal probe spawned.
Today: `daemon.py:4868-4933`; pinned by `tests/bt_lost_pause_recover.py:48-62` and `bt_output_policy.py` rules 1-4.
PipeWire break: rescue (I2) — and the client's own reaction to an unlinked stream (track advance).
Test: existing pins stay; add a bench-only assertion (B2) and a unit pin that `_bt_transport_lost` never issues `audio-device` (already `bt_lost_pause_recover.py:61-62`).

**I4 — output.json is the truth of where sound goes.**
Property: the pcm any live stream is linked to equals `current_output()["pcm"]` (`output.py:105-112`) at all times; only `set_output` writes it (`daemon.py:1759-1761`).
Today: trivially true (routing = pcm name). Consumers that read it: `_bt_transport_lost` guard `:4901`, `_bt_playback_active`/probe_hold `:3018-3037`, `_ps_want_off` `:5569`, `/status` `:2997`, `_kick_bt_connect` `:5128`, player `output_pcm()` `player.py:82-89`.
PipeWire break: any routing change outside vibb (I2 failure).
Test: bench: a 4 Hz sampler over `pw-dump` comparing each vibb stream's linked sink to output.json for the whole B2/B3/B7 runs; unit: no new writer of `OUT_FILE` (grep pin).

**I5 — boot A2DP readiness window.**
Property: a headset that pages in during btwatchd's BOOT window gets an A2DP transport without the nudge; `boot_fails` never increments for "endpoint not yet registered".
Today: bluealsa ordered right behind bluetoothd (`install.sh:470-473`), btwatchd `After=bluealsa` (`:629`), window 120 s / 5 s (`btwatchd.py:85-86`), fail limit 4 (`:121-123`).
PipeWire break: endpoint registered by WirePlumber, later; wrong unit ordering.
Test: bench B1; unit: assert the btwatchd unit orders after whatever owns the endpoint (grep pin in an install test, style of `tests/extras_wrapper.py`).

**I6 — transport-ready gate semantics.**
Property: `a2dp_pcm_present(mac)` keeps its signature and means "the peer accepted our A2DP SetConfiguration" (org.bluez.MediaTransport1 for `dev_<MAC>` with the local A2DP-Source UUID `0000110a`, state idle|pending|active all count; absent = refused/not yet). A second predicate `sink_ready(mac)` means "the sink node exists" and gates spawn/retarget.
Today: `btbus.py:593-603` (PCM1, `Mode == "sink"`); the refusal ladder depends on absence meaning refusal (`btwatchd.py:414-426`, `tests/bt_avdtp_refusal.py`); the zombie case is deliberately *not* detectable here (`daemon.py:473-479`, `bt.py:125-133` TX counter).
PipeWire break: no org.bluealsa; the phone-as-source transport (UUID `0000110b`) must not count.
Test: extend `fake_bluezd.py` additively with MediaTransport1 objects (SetTransport(mac, uuid, state)); re-run `bt_parity.py`, `bt_avdtp_refusal.py`, `bt_play_kick.py`, `status_bt_probe_local.py` unchanged; add a case where only a `0000110b` transport exists → False.

**I7 — keep-alive/suspend semantics.**
Property: after the last stream stops, the transport stays acquired ≥ 120 s (`install.sh:485`), then exactly one AVDTP Suspend; a play within the window produces zero AVDTP Start/Suspend PDUs; a track change never suspends.
Today: bluealsa `--keep-alive=120` (`install.sh:475-522`).
PipeWire break: default 5 s; an orphan keeps the node running (NEW-7); a paused client whose stream goes inactive suspends on the node timer.
Test: bench B4 (btmon PDU count); install-level pin that the WirePlumber rule sets `session.suspend-timeout-seconds = 120` on bluez nodes only.

**I8 — volume single-authority.**
Property: vibb's `/volume` writes exactly one gain (mpv softvol or engine volume, `daemon.py:1029-1055`); vibb never writes a bluez node's volume; the HAT sink's graph gain is pinned to a constant (1.0) and not restored from state; no AVRCP volume PDUs are caused by a vibb action.
Today: single softvol; headset AVRCP volume is the peer's business.
PipeWire break: restore-stream/device; hw-volume turns node-volume writes into AVRCP traffic.
Test: bench B7 with btmon (count `SetAbsoluteVolume`); boot self-test asserts HAT sink volume == 1.0 and restore settings off; grep pin: no `wpctl set-volume`/`pw-metadata` volume writer in `pi/`.

**I9 — engine alternation without EBUSY and without double audio.**
Property: at most one vibb stream is linked to the active sink 2 s after any switch; mpv → spotify → mpv → soloist cycles never fail to open.
Today: EBUSY (accidental), `player.py:432-438`, `daemon.py:946`.
PipeWire break: mixing.
Test: unit — with a fake go-librespot that answers `/player/pause` slowly (>1 s), `ORCH.play(mpv target)` must confirm paused (or refuse/delay the spawn) before `_spawn`; bench B5 with `pw-dump` link count.

**I10 — policy self-test: config drift fails loudly at boot.**
Property: on every boot, before the box will land audio on the HAT, a self-test asserts (behaviorally, I2) no-fallback/no-reconnect/no-follow-default, the HAT sink gain, roles/codec pins (`pw-dump` node props `api.bluez5.codec`), suspend timeout, and the loaded profile; failure is surfaced in `/status` + screen + journal, and the daemon refuses the *local* landing (or whatever the architect decides, Q11).
Today: `install.sh` re-derives the bluealsa `ExecStart` on each run (`:490-522`) — install-time only, no boot-time check.
PipeWire break: package upgrades changing the config *format* (0.4 Lua → 0.5 conf happened once already) silently drop overrides.
Test: unit — the self-test module against canned `pw-dump`/`wpctl settings` outputs (good, and each single drift); bench B8 after `apt full-upgrade`.

**I11 — orphan/group kill (existing, keep).** `tests/stop_child_group_kill.py` as-is; add to its docstring why it matters more now (NEW-7). Bench B5 orphan case.

**I12 — route file idempotence.** `_route_alsa` with the same MAC twice writes once (`tests/bt_state_fsync.py:47-48` already pins; keep it passing with the new template, NEW-5), and the node name comes from discovery by MAC, not string composition (unit: fake graph dump with both `.1` and `.a2dp-sink` shapes).

**I13 — rollback round-trip.** Install with `VIBB_AUDIO_STACK=pipewire`, then `=bluealsa`: assert masked units include socket units, `/etc/asound.conf` back to `type bluealsa`, pipewire-alsa's `default` override neutralised, bluealsa packages still installed, `a2dp_pcm_present` backend flips (I6). Unit-level with a fake `systemctl` (the `extras_wrapper.py:14-20` pattern); bench: section 4 drill.

---

## 3. BENCH PROTOCOL (Trixie bench Pi, BT speaker + I2S HAT; never the box)

**B0 — prerequisites, or every number below is meaningless.**
1. Run the whole protocol on **bluealsa first** on the same bench (same headset, same AP, same distance) — the baseline numbers are the PASS thresholds.
2. Capture tooling: `vibb-power btsnoop-on` / `pi/btsnoop.sh` + `pi/snoopdigest.py` for AVDTP/AVRCP counts; a `pw-dump` sampler (4 Hz to a file: node states, links, `api.bluez5.codec`, stream `target.object`); `journalctl -f -k -u bluetooth -u wireplumber -u pipewire -u vibb-*`.
3. Ears are an instrument: every "no audio on the HAT" verdict is ear + `pw-dump` HAT node state (`suspended` throughout) + optionally a phone mic recording.
4. The false-pass traps the shim rig already found apply: a `null` slave passes everything (`pipewire_shim_rig.sh:58-73`), and a bench with a session PipeWire measures the wrong instance (`:83-89`, `:251-264`) — bench in the exact unit topology the design specifies (system vs user).

**B1 — A2DP endpoint timing at boot.** 5 cold boots, headset already on and paging. Observe: timestamp of `bluetooth.service` active; first MediaEndpoint registration (bluetoothd at `-d`, or first MediaTransport1 object via `busctl monitor org.bluez`); btwatchd `steady:` and `output -> bt` lines; whether the nudge (`connected but no A2DP transport — nudging`) fired. PASS: transport within (bluealsa baseline + 5 s) of bluetoothd, `output -> bt` without nudge in ≥ 4/5 boots, `boot_fails` never > baseline. KILL: any boot reaching `_await_pcm`'s announce-anyway (`btwatchd.py:462`), or median time-to-`output -> bt` > baseline + 15 s.

**B2 — dropout, no rescue.** Mid-track (mpv cached file, then Spotify), pull the headset battery. 5 trials each, both client modes (pipewire-alsa pcm; native `--ao=pipewire`). Observe: HAT node state 4 Hz for 60 s; ear; mpv log for `audio device failed`/track change; daemon `bt transport lost mid-play — stopping` vs btwatchd `Connected=false` timestamps; bookmark position before/after; then power the headset on within 150 s → blip resume. PASS: HAT node `suspended` throughout, zero audible HAT output, zero track advances, stop within ≤ 5 s of the BlueZ event, bookmark within ±5 s, blip resume plays on the headset at the same position 5/5. KILL: any HAT audio (one is enough), any track advance, any stream link to a non-pinned sink in the sampler.

**B3 — pinned target vs default-sink theft; phone as source.** (a) Pair a second speaker; let it self-connect while the target is alive (btwatchd kicks it after 3 s, `btwatchd.py:586-599`) — inside those 3 s start playback 10×. (b) Bond a phone and stream music from it into the box. (c) Move a vibb stream by hand with `wpctl` mid-play, then spawn again. PASS: 10/10 spawns link to the pinned node regardless of `wpctl status` default; phone streaming produces zero audio on HAT and headset and (with roles pinned to source-only) no transport at all; a hand-moved stream is not re-targeted next spawn. KILL: any spawn on a non-pinned sink; phone audio audible anywhere.

**B4 — suspend/release vs keep-alive=120.** Play 10 s, pause, wait 130 s; then play, pause, play within 60 s; then a track change while playing. btmon: count AVDTP Suspend/Start/Close/Abort, the headset chime (ear). Run identically on bluealsa. PASS: exactly one Suspend at 120 ± 5 s after pause; zero AVDTP PDUs and no chime for the in-window resume; zero Suspend on track change; counts equal to bluealsa. KILL: Suspend before 60 s, any Close/Abort, chime inside the window.

**B5 — engine alternation, double audio, orphan.** 20 cycles mpv → spotify → mpv (later → soloist). At each switch sample links after 2 s. Then `kill -STOP` the player parent (simulated wedge) and let the daemon's `_stop_child` run (`daemon.py:680-715`), then spawn. PASS: never > 1 running vibb stream linked to the sink; no EBUSY (expected trivially); the orphan case ends with the group gone and one stream. KILL: two audible sources at once, or a node that never suspends after stop (orphan holding it).

**B6 — RSS / CPU / wakeups, both stacks, same method.** Three states × 10 min: idle-suspended (no stream, transport released), idle-held (inside the 120 s window), streaming. Measure per process (`pipewire`, `wireplumber`, `pipewire-pulse` if present; `bluealsa`; `mpv`; `go-librespot`): RSS (`/proc/<pid>/status` VmRSS), CPU (`pidstat -u 60`), wakeups (`voluntary_ctxt_switches` delta / 60 s), plus system `vmstat 5` cs/in. Include the scan-barrage variant from the crash memory (one scan per 25 s, associated, on battery) and count xruns (`pw-top` / mpv log) — this is the `--audio-buffer=0.5` decorative-or-not test. PASS: standing RSS ≤ bluealsa + 40 MB; idle-suspended wakeups ≤ 10/s combined; streaming CPU ≤ baseline + 25 %; xruns under barrage ≤ baseline. KILL: idle-suspended > 50 wakeups/s (the idle campaign, `ui.py:4098`, `mpris.py:291-297`), RSS > +60 MB, or xruns > 2× baseline.

**B7 — live output switch with the cap.** bt → local → bt, 10× mpv, 10× go-librespot (session must survive: same track, position ±2 s). Observe mpv `volume` via IPC after landing on local; go-librespot volume via `/status`; btmon AVRCP `SetAbsoluteVolume`/VolumeChanged counts; audio follows within 2 s. PASS: mpv volume == `min(stored, cap)` 10/10; go-librespot capped 10/10 **once the design fixes NEW-1** (record the current uncapped value as evidence); zero vibb-caused AVRCP volume PDUs; session kept 10/10. KILL: any uncapped HAT landing, any AVRCP volume traffic from the switch, a session drop.

**B8 — policy self-test survives `apt full-upgrade`.** Run the self-test (I10) green; then in turn: remove the override fragment; flip `linking.follow-default-target`; restore default roles; re-enable sbc-xq; then a real `apt full-upgrade` (or `apt reinstall pipewire wireplumber`). PASS: each single drift is caught at the next boot with the screen/journal/`/status` signal, and the box's local landing is refused (per Q11); after the real upgrade the test is either green or loud. KILL: any drift that stays green.

**B9 — Soloist on the pinned sink; null sink for warming.** Start soloist with `--pipewire-device <pinned>`; play; `pw-dump`: its stream's target/link. Warming: retarget to the null sink; run 10 min. Then start soloist with a *non-existent* device name. PASS: linked to the pinned sink only; during warming both HAT and BT nodes stay `suspended` and btmon TX is flat for the whole run; with a bad target soloist plays nowhere (or soloistd kills it) — fail-closed. KILL: soloist landing on the default sink in any case; any node leaving `suspended` during warming.

**B10 — crash-heal path with PipeWire in the loop.** `sudo python3 pi/vibb/bt.py recover` (tier 1: `systemctl stop/start bluetooth`, `bt.py:241-243`); then simulate tier 2 (`rfkill block`, 2 s, `unblock`, `:255-263`); then tier 3 (`modprobe -r/hci_uart`, `:273-283`) and the serdev unbind/bind (`:187-223`). 5× each. Observe: hci0 re-added; headset reconnects; MediaTransport1 + `bluez_output` node appear; `wireplumber` never restarted; `vibb-mpris` re-registers (`mpris.py:272-281`). PASS: node back within 30 s of `bluetooth.service` active, 5/5 per tier, no WirePlumber restart. KILL: any tier needing a WirePlumber restart → `recover()` must gain that tier before cutover, and the failure is a finding against the design.

**B11 — codec/profile pin (NEW-4).** btmon SetConfiguration on every connect: codec SBC, bitpool == bluealsa baseline, no XQ; no HFP/HSP RFCOMM SLC; `api.bluez5.codec` in `pw-dump` matches. PASS: identical airtime profile to baseline in all 10 connects. KILL: any AAC/aptX/XQ negotiation or HFP connection.

**B12 — headset-initiated reconnect role (bonus, from the crash memory's open item).** While at it: headset off, start playback (box waits), headset on, `hcitool con` → CENTRAL or SLAVE. Not a PASS/KILL item; it settles a live question on the box's variable at zero extra cost.

---

## 4. Field cutover + soak plan

**Baseline on the CURRENT box first (7 days minimum, before any change):**
- Crash rate: `journalctl -k -g 'hardware error' --since <start>` count, `command 0x.* tx timeout` clusters (`bt.py:164`), `bt heal:` lines (`daemon.py:4757`), per **streaming hour** — derive streaming minutes from a 1-minute cron sampling `hciconfig hci0` TX bytes (`bt.py:125-133`; > 1 MB/min = streaming). Split home vs car.
- BT reconnect time: btwatchd `target dropped` → `steady:` → `output -> bt`; speaker off/on → first `steady`; blip-resume latency (`speaker back within the blip window`). Median and p90.
- RSS: `bluealsa`, `mpv` streaming, `go-librespot`; free memory at idle; zram usage.
- Boot-to-ready: `systemd-analyze`, first `/status` 200, first `steady:` with the headset pre-on.
- `/status` latency: 200 samples with `curl -w %{time_total}`, p50/p95, idle and streaming (the screen polls 1/s; `GO_STATUS_TIMEOUT` 1.5 s `daemon.py:4825`).
- AVDTP Suspend/Start per hour (btsnoop digest) — the keep-alive baseline.
- Battery: hours of BT playback per charge (memory: 5h04 on the old cell) — the wakeup budget in real units.

**Soak:** ≥ 14 days AND ≥ 20 streaming hours AND ≥ 5 evening sessions on the son's headset; include ≥ 3 car sessions only if the car is in scope for this generation (it is the worst peer). Keep the toggle rollback rehearsed (below) so the soak is cheap to abort.

**Abort criteria — any ONE sends the box back to bluealsa via the install toggle:**
1. Audible audio from the HAT without a person having chosen local (one event).
2. Two audio sources at once (one event).
3. A reconnect or heal that needed a WirePlumber/PipeWire restart (one event).
4. Policy self-test red at boot (one event).
5. `hardware error` per streaming hour ≥ 2× baseline **and** ≥ 3 events (small counts are noise; state this explicitly).
6. `/status` p95 > 2× baseline over a session; or RSS of pipewire+wireplumber growing > 20 MB across the soak.
7. Blip-resume failing (headset back inside 150 s, no resume) twice.
8. Any AVDTP Suspend/Start rate > 1.5× baseline (the keep-alive semantic drifting).

**Rollback drill — rehearsed on the bench BEFORE cutover, timed, and repeated once from a no-network state:**
1. `VIBB_AUDIO_STACK=bluealsa ./install.sh` offline (no `apt-get`, `install.sh:127-130`) → must complete.
2. Verify: `pipewire.service`, `pipewire.socket`, `wireplumber.service` masked (both service and socket); pipewire-alsa's `default` override gone; `/etc/asound.conf` back to `type bluealsa` with the MAC; `bluealsa` active with `--keep-alive=120`; `bluealsa-aplay -L` lists the headset; `a2dp_pcm_present` true via org.bluealsa; `tests/bt_parity.py` PARITY OK on the rig; `aplay -D vibb_bt` tone (`play.sh:99` pattern) audible; `pi/extra.sh --restore` set correct.
3. Reboot; headset auto-reconnects; play a card; `/status` icon correct.
4. Target: < 15 min including reboot. Record the exact command sequence in the docs before cutover day.

---

## 5. Questions the architect must answer (each changes the test plan)

1. **System-wide PipeWire+WirePlumber or a lingering user session?** Decides B1's unit ordering (`After=` for `vibb-bt-reconnect`, `install.sh:629`), the env/socket plumbing for root vibbd (`install.sh:1154-1158`), `$RUN_USER` go-librespot (`:542`) and soloistd, group memberships, and whether `restore-*` state exists at all.
2. **pipewire-alsa pcm names (`playback_node`) or native AOs (`--ao=pipewire`, go-librespot `audio_backend: pipewire`, soloist `--pipewire-device`)?** Decides whether `OUTPUT_PCMS`/`set_output`/`reopen_go_output` survive unchanged (SEVERE-3) and which `mpv_launch_flags.py` assertions are rewritten. Mixed is allowed but must be stated per client.
3. **How are `node.dont-reconnect` / `node.dont-fallback` delivered to every client** — pcm definition keys, `PIPEWIRE_PROPS` in each unit's environment, or per-client flags? The I2 unit test greps whatever you choose.
4. **Gate: MediaTransport1 presence (UUID `0000110a`, any state) for btwatchd/icon, plus node presence (long-lived `pw-dump -m` watcher) for spawn/retarget — or one predicate?** Decides the `fake_bluezd.py` extension and which of the 8 call sites use which.
5. **Node-name discovery:** how does `_route_alsa` (`bt.py:430-483`) learn `bluez_output.<...>` and the HAT node name (both version-dependent), and does the file keep the colon MAC for `:433-436` (NEW-5)?
6. **Cap semantics under a graph:** stay a per-engine clamp (`min()`, `output.py:22-41`) applied by vibb for mpv, go-librespot *and* soloist (fixing NEW-1), or a HAT-sink-level gain (a multiplier, changes what "35" means, but covers every client incl. a phone-driven Spotify session)? Never on the BT node (I8).
7. **WirePlumber profile:** defaults plus overrides, or a custom whitelist profile? Decides what the self-test asserts and how format-drift-proof it is.
8. **Pins for `bluez5.roles`, `bluez5.codecs`, `enable-sbc-xq`, `enable-msbc`, `enable-hw-volume`** — exact values, and the host-centric role naming verified on the bench (NEW-4, B11).
9. **Suspend timeout:** 120 s on bluez nodes only, or also the HAT (battery vs the extras' raw-`hw:` access, `extra.sh:72`)?
10. **Rollback toggle design:** env name, mask-not-remove, socket units, the ALSA `default` override, `extra.sh` restore set — and does `btbus` keep both gate backends behind it (I13)?
11. **Self-test failure action:** refuse the local landing only, refuse all playback, or warn only? The bedtime rule ("never a silent dead box", `PLAN-soloistd.md:222-225`) pulls one way, the no-blast rule the other.
12. **`pipewire-pulse`: installed or deliberately absent** (fail-closed Soloist fallback, B9)?
13. **Realtime:** `module-rt` with `LimitRTPRIO` in system mode, or no RT at all? It is the scheduling variable in the open crash model (C13) — the design should state which and B6 measures both if undecided.
14. **Who fixes NEW-2 (spotify pause confirmed before an mpv spawn)** — in `ORCH.play` under the lock, or in soloistd/player? It must exist before mixing is live.
15. **What runs `bt.py recover()`'s bluealsa `try-restart` (`bt.py:244-245`) under PipeWire** — nothing, or a WirePlumber tier if B10 finds one is needed?

---

# APPENDIX B — The 2026-09-01 QA register (verbatim, for the record)

The adversarial review that produced the NOT NOW verdict recorded in
`docs/NOTES-audio-stack.md`. Kept in full because its findings are this
plan's requirements list and its text otherwise lived only in a chat
session.

## VERDICT UP FRONT

**Do not migrate.** The proposal rests on a premise error, overturns a decision the plan already made twice (`docs/PLAN-soloistd.md:86-87`, `:481`), and pays a platform-migration cost through the box's single most field-hardened layer — to enable an engine that is *itself* still disqualified by an unresolved 90-day brick fuse (`docs/NOTES-soloist.md:518-524`).

---

## BLOCKER-0 — THE PREMISE IS FALSE

> "a bench spike proved PipeWire's ALSA sink CANNOT open bluealsa's plugin pcm — so any 'shim' approach is dead or unproven"

Half of that sentence is true and the other half is a rhetorical merge. What the bench proved (`docs/PLAN-soloistd.md:127-144`) is that **PipeWire's `api.alsa.pcm.sink` cannot open a plugin pcm** — it resolves a card index and `plug -> bluealsa` has none. The plan's own text then says the design **flips to PulseAudio**, which "passes device= to snd_pcm_open with no card lookup."

The PulseAudio shim is not dead. It is **untested, and untested only because the bench was the wrong machine** (`:146-155`: `pactl` there talked to pipewire-pulse, which deliberately omits `module-alsa-sink`). The rig even *detects and refuses* this exact false measurement (`bench/pipewire_shim_rig.sh:251-264`).

The test costs one command on a bare Trixie image or the box at a quiet moment — `bench/pipewire_shim_rig.sh pulse` (`:243-285`) — and it already includes the S3 device-release check. **Rejecting the shim as "dead or unproven" and adopting a platform migration instead is rejecting on absence of evidence, when the evidence is two hours away and the script is written.**

There is also a third option the plan already named and the proposal ignores (`:156-162`): the box speaker's pcm is `plug -> hw:sndrpihifiberry`, a **real card** — PipeWire can serve it directly. Asymmetric output (Soloist restricted to local/Sonos, bluealsa keeps BT) is strictly cheaper than full migration and needs zero BT changes.

---

## RANKED FINDINGS

### BLOCKER-1 — WirePlumber's stream-rescue re-instates the local fail-over the owner explicitly banned, at uncapped volume

Two independent safety decisions collide here, and PipeWire's default policy defeats both.

1. `pi/daemon.py:4880-4885` — *"NO local fail-over (e81a53b's keep-playing branch reverted, owner decision 2026-07-23) ... the box speaker suddenly blasting next to a kid wearing dead headphones is worse than a short gap in them."*
2. `pi/vibb/output.py:22-45` (`local_volume`) — the headphone level a parent set is capped to `local_fallback_cap` (default 35, `pi/player.py:543-545`) the moment audio lands on the HAT amplifier, because *"the loudest event this box can produce was, until now, the one action it offers in exactly that moment."*

Under full PipeWire, when the BT device disconnects the sink node is **removed**, and WirePlumber's default policy moves orphaned streams to the next default sink — the HAT. Nobody re-applies the cap: the cap is applied by vibb at spawn (`player.py:543-545`) and at live retarget (`daemon.py:1795-1801`), and a WirePlumber-initiated move goes through neither path. Net behavior: a headphone dropout produces **automatic full-volume playback from the box speaker next to a child's head**, which is precisely the two things the codebase forbids.

It is configurable off. That is not the point — the point is that a kids-safety invariant currently enforced by *the absence of a mechanism* becomes enforced by *a correctly-written config file in someone else's package*, subject to upgrades. That is a materially worse safety posture.

### BLOCKER-2 — WirePlumber is mandatory for BT, so the "scoped, no session manager" containment strategy is unavailable

PipeWire's `bluez5` monitor is loaded and driven by the session manager; `pipewire.conf` does not create BT nodes on its own. The plan's entire containment idea — "monitors and bluez5 OFF, one static sink, no session manager" (`docs/PLAN-soloistd.md:81-84`) — exists specifically to avoid adopting policy. **PipeWire owning Bluetooth means adopting WirePlumber's full policy engine**, i.e. the opposite of the scoping that made the shim palatable.

So the proposal is not "PipeWire instead of a shim." It is "a policy engine that makes autonomous routing decisions, inside a daemon whose entire BT design is an explicit, auditable state machine." Every one of vibb's ~12 hand-tuned BT behaviors becomes a negotiation with a second decision-maker that has no knowledge of `output.json`, `_BT_WAIT`, the radio lock, or the blip window.

### BLOCKER-3 — `audio_ready()` has no cheap, in-process replacement, and vibbd is stdlib-only

`audio_ready()` (`pi/vibb/output.py:174-186`) → `btbus.a2dp_pcm_present()` (`pi/vibb/btbus.py:372-388`) is the box's universal playback gate. It is called from:

- the stall watchdog's respawn loop (`daemon.py:585-600`)
- the crashed-child heal (`daemon.py:655`)
- `_bt_transport_ready()` (`daemon.py:4682-4689`), which gates `set_output`'s mpv retarget (`daemon.py:1781`), the go-librespot retarget (`daemon.py:1828`), `_kick_bt_connect` (`daemon.py:5124`), `_bt_wait_advance` (`daemon.py:5057`), and the `/status` BT icon at ~1/s (`daemon.py:2998-3006`)
- btwatchd's `_await_pcm` commit gate (`pi/btwatchd.py:428-450`)
- `bt.py`'s connect verification (`pi/vibb/bt.py:382-394`)

Today this has a **D-Bus** implementation (`btbus.py:593-618`) — the payoff of the whole PLAN-bt-dbus investment. PipeWire has **no D-Bus interface**; it is a custom protocol over a unix socket, with no stdlib binding. vibbd is stdlib-only on system python (`pi/sonosd.py:6-8` states the constraint explicitly; the venv exists precisely to keep it that way). So the replacement is either:

- fork `pw-dump`/`pactl` at ~1/s from `/status` — a full graph dump + JSON parse per poll, on the box where `daemon.py:2998-3006` already records a regression from forking `bluealsa-aplay` too often against a wedged controller; or
- a new venv sidecar with a PipeWire client, adding a process, an IPC hop, and a supervision problem to the box's most latency-sensitive gate.

**The one good mitigation, worth doing regardless of this decision:** gate on **`org.bluez.MediaTransport1`** instead. It exists on the system bus whoever owns A2DP — bluealsa *or* PipeWire — is arguably a truer "transport ready" signal than "a PCM is listed", and would make `a2dp_pcm_present` backend-neutral. That is a small, additive change to `btbus.py` that de-risks any future audio-stack move and costs nothing today.

### SEVERE-1 — The keep-alive tuning becomes a node-suspend setting with different semantics, and its *default* re-creates the crash trigger

`pi/install.sh:475-518` is a field-derived value with an explicit RF rationale: stock bluealsa tears the transport down the instant the last PCM client closes, so *every pause/play and episode change* forced a full AVDTP renegotiation — "signalling load on the SHARED wifi/bt radio (the Zero 2 W firmware-crash trigger), plus the headset's reconnect chime each time." 120s, deliberately not maxed, because a live-but-silent transport costs battery.

PipeWire's analogue is `session.suspend-timeout-seconds`, **default 5 seconds**. Out of the box, full PipeWire re-creates the exact failure this tuning fixed, on the box whose BT firmware crashes under coexistence load (`pi/vibb/bt.py:225-232`, `pi/daemon.py:5578-5584`). It is settable to 120, but:

- it is a different mechanism (node suspend vs. transport hold), so the 120s value is **not transferable** — it must be re-derived in the field;
- it interacts with WirePlumber's device/profile handling in ways the 120s was never validated against;
- the install-time detection logic (`install.sh:487-518`, which carefully re-reads the distro's own `ExecStart` and only rewrites on value change) is replaced by dropping a config fragment into a package-managed directory.

### SEVERE-2 — The transport-loss contract gets a second, faster, invisible detector

Today the contract is single-writer and ordered: BlueZ signal → btwatchd `_notify_lost` (`btwatchd.py:546-560`) → `POST /bt/lost` → `daemon._bt_transport_lost` (`daemon.py:4868-4905`) → guard on `current_output()=="bt"` → stop mpv / pause spotify → arm `_BT_WAIT` under a documented lock order → heal probe → `_bt_wait_watcher` (`daemon.py:5080`) → `_speaker_back` (`daemon.py:5016`) → `_bt_blip_resume` (`daemon.py:4839`).

Under PipeWire, **WirePlumber reacts to the same BlueZ event first**, on its own clock, and it acts on the audio graph directly. vibbd's pause arrives *after* WirePlumber has already moved or killed the stream. The observable result of a headphone drop becomes: brief blast on the HAT (BLOCKER-1), *then* vibbd's pause, *then* a blip-resume that resumes onto whatever WirePlumber currently considers default. Worse, `output.json` — the box's persisted "where sound goes" truth, read by `current_output()` (`output.py:105-112`) and by every guard above — can now be **silently wrong**, because a routing change happened without going through `set_output` (`daemon.py:1710-1875`).

Every UI affordance downstream inherits the lie: the BT icon (`daemon.py:3006`), the "disconnected — X: reconnect, A: play on the box speaker" popup (`daemon.py:4816-4824`), and the `bt_waiting`/`bt_lost`/`bt_ready` triple (`daemon.py:5093-5115`).

### SEVERE-3 — `set_output`'s deferred-switch protocol has no PipeWire equivalent, and the v0.0.7 live-reopen win is at risk

`daemon.py:1781-1790` is a load-bearing refusal: *"NEVER point a live mpv at a bluealsa device with no A2DP transport: it errors the track and skips to the next, over and over (field: 'jumps between episodes like crazy')."* Same rule for go-librespot at `daemon.py:1825-1830`, with the added RF reason: "the restart's wifi burst lands exactly during AVDTP setup on the SHARED radio (the coexistence load that crashes the Zero's BT firmware)."

This protocol works because vibb *names an ALSA pcm* and can therefore decline to name it yet. Under PipeWire both clients open one endpoint and routing is WirePlumber's decision. Recoverable by naming pipewire-alsa pcms with `playback_node` (so `output.py`'s `OUTPUT_PCMS` structure survives) — but the node name for a BT sink is `bluez_output.<MAC>.<n>`, which is MAC- and profile-dependent, so `bt.py:_route_alsa` (`pi/vibb/bt.py:430-465`) must be rewritten to discover and write it, and the "wait for the transport before naming it" invariant now has to be re-established against node-appearance timing rather than PCM presence.

Also at risk: `reopen_go_output` (`output.py:139-158`) — the v0.0.7 live output reopen that keeps the Spotify session across an output switch, which `NEXT-STEPS.md:100-135` recommended *deferring* the whole PipeWire idea in favour of. If the pcm→node binding cannot be re-pointed live, output switching regresses to restart + bookmark-resume, i.e. the migration **undoes** a shipped improvement while claiming to enable one.

### SEVERE-4 — Two PipeWire sessions, or one unsupported system-wide instance

`vibb-daemon.service` runs as **root** (`install.sh:1143-1162`, no `User=`), so `player.py` and every `mpv` it spawns are root. `go-librespot.service` runs as `$RUN_USER` (`install.sh:541-542`). PipeWire is a per-user session daemon.

So the migration needs either:
- a **system-wide PipeWire + WirePlumber**, which upstream explicitly discourages and Debian does not package as a supported configuration (custom D-Bus policy, custom units, and you own every future breakage); or
- a lingering user session (`loginctl enable-linger`) plus `XDG_RUNTIME_DIR`/`PIPEWIRE_RUNTIME_DIR` plumbed into a root daemon's child processes.

Neither is a config line. Both are new boot-order dependencies on a box with an explicit boot-shave culture (`install.sh:1009-1015` `DefaultDependencies=no` for the UI; `install.sh:1146-1152` deliberately *not* ordering the daemon behind the network; `tests/boot_bind_first.py`).

### SEVERE-5 — Boot-time BT readiness regresses

`install.sh:437-444` documents a fixed field bug: *"the box was DEAF to the headset's inbound reconnect for the first 25-40s of every boot."* The fix moved `rfkill unblock` into a `bluetooth.service` `ExecStartPre` so the radio listens from boot, and btwatchd's BOOT window (`btwatchd.py:11-14`, `:85-86`, 120s at 5s cadence) plus the poll fallback's boot branch (`install.sh:589-594`) assume the A2DP endpoint is up early — bluealsa is a plain system service ordered right behind bluetoothd (`install.sh:471-473`, `vibb-bt-reconnect.service` `After=bluealsa.service`).

Under PipeWire, the A2DP endpoint is registered by **WirePlumber**, which under the user-session model starts after `user@.service` — much later in boot. `_await_pcm` (`btwatchd.py:428-450`) will time out on early reconnects and fall through to the nudge/announce-anyway path, and the boot-window fail counter (`BOOT_FAIL_LIMIT=4`, `btwatchd.py:121-123`) will burn on "too early" failures it was designed to distinguish from "speaker away."

### MODERATE-1 — mpv's tuned flags: two survive, one changes meaning

Honest breakdown of `player.py:130-145`:

- `--audio-samplerate=44100 --audio-channels=stereo` (`:134`) — these are **mpv-side filters**, applied before the AO. They survive a PipeWire sink unchanged, and `tests/mpv_launch_flags.py` still passes. The silent-A2DP-audiobook bug does not return. (PipeWire would also resample independently, making them redundant-but-harmless — do **not** let that reasoning delete them; the comment at `:113-118` exists for a reason.)
- `--audio-buffer=0.5` (`:142`) — survives syntactically, **loses its purpose**. Its documented job is riding out RF gaps because "over A2DP the device buffer was ~100ms ... any coex hiccup on the shared radio clicked immediately." Under PipeWire, mpv's ring feeds the *graph*, and what actually reaches the radio is governed by PipeWire's quantum and the bluez5 headroom settings. The 0.5s no longer buys what the comment says it buys; the equivalent must be re-tuned in PipeWire's own knobs, in the field, on the shared-radio box. **This is a field-fix that silently becomes decorative** — the worst failure mode for a codebase this heavily commented.
- `--ao=alsa` (`:130`) — a startup trim ("skip the AO autoprobe"). Fine either way, but now points at pipewire-alsa, adding a hop.

### MODERATE-2 — WirePlumber becomes a third volume authority

The box's design is one volume number (`output.py:22-45`, `player.py:_apply_box_volume`, `daemon.py:1795-1801`, `pi/ui.py:2886` exposes the cap). WirePlumber persists and restores per-device and per-stream volumes from its own state directory. A restored device volume, or a restored stream volume for mpv, will fight `--volume` and can defeat `local_fallback_cap`. Add PipeWire-bluez5's AVRCP absolute-volume sync and the speaker's own hardware buttons become a fourth writer — on a box that already has an AVRCP bridge of its own (`pi/mpris.py`, `install.sh:604-610`) written specifically because the Skoda head unit's player polling *during live A2DP* is "the known channel-ops-while-streaming crasher on this chip."

### MODERATE-3 — A2DP-sink role and default-sink theft

WirePlumber enables both `a2dp_source` and `a2dp_sink` roles by default. Today a phone that pairs and streams into the box produces nothing, because vibb never opens a bluealsa sink PCM. Under PipeWire it produces a node, and depending on the policy scripts shipped by the distro (PulseAudio's `module-bluetooth-policy` auto-looped A2DP source to the default sink; WirePlumber's behavior is version- and config-dependent — **verify, do not assume**), it can produce audio. On a kids box with no pairing gate this is a new nuisance-and-security surface that intersects `vibb-security-gate` work.

Related and more mundane: WirePlumber picks the default sink by priority. BT sinks rank high. So "phone connects → becomes default → next mpv spawn lands somewhere vibb didn't choose" is a real class, mitigated only by pinning per-stream targets everywhere — which is exactly what today's ALSA pcm names already do, for free.

### MODERATE-4 — Resource honesty

The plan's own budget line is "~15MB, worst case ~25MB with wireplumber" (`docs/PLAN-soloistd.md:84`) — that was for the *scoped, session-manager-less* shim. Full PipeWire is a different number.

Realistic aarch64 headless RSS: `pipewire` ~10-15 MB, `wireplumber` ~15-25 MB (Lua scripting engine, many scripts loaded), `pipewire-pulse` ~8-12 MB. **~35-50 MB standing**, against bluealsa's ~3-5 MB — a net **+30-45 MB, roughly 7-10% of the ~430 MB usable** (`install.sh:906`). That headroom is already contested: the backup runs under `MemoryHigh=160M` (`install.sh:906-920`) and `vibb-ui` has been seen with a 31 MB swap peak (`install.sh:913-915`).

CPU: the honest answer is *unmeasured*, and that is itself the finding. bluealsa is app→SBC→socket. PipeWire is app→graph→resampler→SBC→socket, waking at the quantum (default ~21 ms) for the whole duration of playback, with extra context switches per period. On a box where the governing hypothesis for BT firmware crashes is **coexistence and scheduling load** (`bt.py:225-232`; `daemon.py:5578-5584` records latency spikes under BT coexistence starving go-librespot's control plane; `btwatchd.py:100`, `:328` and the whole `pi/vibb/radio.py` BUSY/PAGING protocol exist to shave RF and timing load), adding a real-time-scheduled graph process into the audio path is a change to the exact variable under investigation — while `vibb-bt-crash.md` still records the crash model as an **open question**.

Idle power: broadly acceptable. PipeWire suspends idle nodes and does not run the graph with no streams, so the steady-state cost is mostly the resident memory of two extra processes plus D-Bus/BlueZ monitoring. But note that the box's idle discipline is not "mostly acceptable" — `pi/idle.py`, the wifi-PS governor (`daemon.py:5576-5600`), the mpris tick fix (`mpris.py:291`), the sonosd cadence work (`sonosd.py:67`, `:79`, `:106`, `:696`) and `ui.py:4098` ("8x fewer wakeups") are a documented, itemized campaign. Two new always-on daemons with unaudited timer behavior is a regression *against the culture*, even if the milliamps are small.

### MODERATE-5 — bluealsa cannot coexist; the cutover is all-or-nothing

Both bluealsa and PipeWire register A2DP endpoints with BlueZ. The migration must remove/mask bluealsa, which invalidates `type bluealsa` in `/etc/asound.conf` — written by `bt.py:_route_alsa` (`pi/vibb/bt.py:430-465`) on **every speaker pair/swap**, with an explicit brown-out-safety `fsync` because *"an empty asound.conf is WORSE: both pcms gone, every output silent, and nothing heals it"* (`:459-463`). There is no partial rollout and no A/B on one box: the moment bluealsa goes, `a2dp_pcm_present`, `bluealsa-aplay -L`, the `/org/bluealsa` D-Bus tree, `_route_alsa`'s generated config, and every install.sh migration path (`install.sh:231-258`) go with it, simultaneously. `bt.py:recover()` also `try-restart`s bluealsa as part of the firmware-crash cure (`bt.py:244-246`) — the recovery sequence needs re-deriving too.

### FINE — What full PipeWire would genuinely buy

Stated fairly, because a review that only lists costs is not a review:

1. **Mixing eliminates a real bug class.** bluealsa's PCM is exclusive-open, which produced the "orphan mpv holds the PCM → 'Device or resource busy' for every later spawn" cascade (`player.py:551-558`, `daemon.py:686-697`) and forced the `killpg` escalation and the 8s SIGKILL timer. PipeWire makes that impossible. This is the single best argument for the migration.
2. **Live output moves without reopening a device** — the `NEXT-STEPS.md:100-135` "seamless BT↔built-in" item. But that item's own recommendation is *"Utsett"* (defer), and its motivation was substantially retired by go-librespot v0.0.7's live reopen (`output.py:139-158`).
3. Soloist works on both outputs, not just local.
4. bluez-alsa is a niche project; PipeWire is the maintained mainstream.

None of these is a *problem the box currently has*. (1) is already mitigated with working code and a test (`tests/stop_child_group_kill.py`).

---

## COLLISION POINTS (WirePlumber policy vs. vibb machinery)

| # | WirePlumber behavior | vibb machinery it fights | Consequence |
|---|---|---|---|
| C1 | Rescue/move streams when their sink node disappears | `_bt_transport_lost` no-fail-over rule, `daemon.py:4880-4885` | HAT plays automatically on headphone drop |
| C2 | Moved stream bypasses the volume-cap call sites | `local_volume`, `output.py:22-45`; `player.py:543-545`; `daemon.py:1795-1801` | Uncapped HAT volume next to a child |
| C3 | Node suspend after `session.suspend-timeout-seconds` (default 5s) | `--keep-alive=120`, `install.sh:475-518` | AVDTP renegotiation per pause = the documented Zero 2 W crash trigger |
| C4 | Default-sink election on node appearance | `set_output` deferred-switch refusal, `daemon.py:1781-1790`, `:1825-1830` | Routing changes mid-AVDTP-setup; the "skips 15 episodes in 3s" class |
| C5 | Routing decided outside vibb | `output.json` / `current_output()`, `output.py:105-112` | Persisted output truth silently diverges from reality; every guard downstream reads a lie |
| C6 | WirePlumber sees the BlueZ disconnect first | btwatchd `_notify_lost` → `_bt_transport_lost` ordering, `btwatchd.py:546-560` | vibbd's pause arrives after the graph already acted |
| C7 | No D-Bus surface | `a2dp_pcm_present` D-Bus backend, `btbus.py:593-618`; stdlib-only vibbd | The ~1/s playback gate becomes a fork+JSON-parse or a new sidecar |
| C8 | Per-device/per-stream volume restore | one-volume-number design, `output.py:22-45` | Third volume writer; cap defeatable |
| C9 | AVRCP absolute-volume sync in bluez5 | `pi/mpris.py` AVRCP bridge; `install.sh:604-610` | Channel-ops-during-A2DP on the crash-prone chip |
| C10 | `a2dp_sink` role enabled by default | no pairing gate on the box | Any phone in the house can become an audio source |
| C11 | User-session lifecycle | root vibbd + `$RUN_USER` go-librespot, `install.sh:541`, `:1154` | Two sessions or an unsupported system-wide instance |
| C12 | WirePlumber starts at `user@.service` | btwatchd BOOT window, `btwatchd.py:11-14`, `:85-86`, `:121-123`; `install.sh:437-444` | Boot-deafness regression; boot-fail counter burns on "too early" |
| C13 | Graph wakes at the quantum during playback | RF/scheduling audit culture, `radio.py`, `daemon.py:5576-5600`, `btwatchd.py:100`, `:328` | Adds load to the exact variable in the open crash model |

---

## TEST DAMAGE

156 test files in `tests/`. 87 mention bt/output/mpv/pcm at all; **26 are structurally coupled** to bluealsa/ALSA-pcm semantics and would need rewriting or would silently become meaningless:

`bt_lost_pause_recover.py`, `bt_output_policy.py`, `bt_avdtp_refusal.py`, `bt_stall.py`, `bt_play_kick.py`, `bt_state_fsync.py`, `bt_parity.py`, `fake_bluezd.py`, `output_reopen.py`, `output_switch_resume.py`, `output_bt_quiet_marker.py`, `go_output_rebuild.py`, `go_restart_dedup.py`, `mpv_launch_flags.py`, `mpv_mash_rollback.py`, `local_volume_cap.py`, `player_crash_heal.py`, `status_bt_probe_local.py`, `stop_child_group_kill.py`, `orch_lock_io.py`, `boot_resume_guard.py`, `sonos_renderer.py`, `spotify_bitrate.py`, `extras_wrapper.py`, `ui_poller.py`, `wifi_reconnect.py`.

Three categories, with the third being the dangerous one:

1. **Break loudly (fine).** `fake_bluezd.py` exports `org.bluealsa` and its `PCM1` objects; `bt_parity.py` compares `a2dp_pcm_present` across backends. These fail immediately and honestly.
2. **Need mechanical rewrite.** `output_reopen.py`, `go_output_rebuild.py`, `output_switch_resume.py`, `bt_output_policy.py` — all assert on pcm-name strings and the reopen/retarget contract.
3. **Keep passing and stop meaning anything — the real hazard.** `mpv_launch_flags.py` pins `--audio-samplerate/--audio-channels` and will keep passing while `--audio-buffer=0.5` quietly stops doing its documented job (MODERATE-1). `local_volume_cap.py` pins `local_volume()` and will keep passing while WirePlumber moves streams around it entirely (BLOCKER-1/C2). `bt_lost_pause_recover.py` pins vibbd's pause-and-arm behavior and will keep passing while WirePlumber wins the race to the audio graph (SEVERE-2/C6). **A green suite after this migration would be actively misleading**, which for this codebase — where the tests *are* the memory of the field bugs — is the worst outcome available.

**New rig required.** Nothing in `tests/` can validate a PipeWire BT path, because none of the collisions above are observable in-process. You would need a bench Pi with a real BT speaker and a scripted physical-event harness: power the speaker off mid-track and assert the HAT stays silent (C1/C2); pause for 130s and assert no AVDTP renegotiation in the BlueZ log (C3); connect a phone and assert nothing plays and the default sink does not move (C4/C10); cold-boot with the speaker already on and measure time-to-first-audio against today's baseline (C12); and a long-run soak measuring `hci0: hardware error 0x00` frequency against the pre-migration rate (C13) — which, given the open crash model in `vibb-bt-crash.md`, needs weeks of field data, not an afternoon.

---

## RISK/REWARD VERDICT

**Not justified.** The chain of contingency is:

> full-PipeWire platform migration → so Soloist can run → which is a contingency → for one account → whose need is already met on Sonos today → and Soloist is *itself* still disqualified by a 90-day brick fuse on a bedtime appliance (`NOTES-soloist.md:518-524`, unresolved).

Four conditionals deep, and the migration is the most expensive and least reversible link. Every other link has a documented rollback (`PLAN-soloistd.md:110-112`: "Rollback = re-run install.sh"). This one does not: bluealsa cannot coexist (MODERATE-5), so the cutover is atomic across `asound.conf`, `btbus`, `bt.py`, `btwatchd`, `install.sh` and the recovery path, on the box's most field-hardened layer, with roughly 20 documented field fixes (each carrying a dated field-log citation) either dead, re-derived, or silently decorative.

This also violates two stated project norms: **`vibb-diagnose-before-fixing`** — the BT crash model is explicitly an open question, and the correct move is to measure, not to replace the layer under investigation; and **`vibb-scope-discipline`** — a platform migration is the maximal answer to a request whose minimal answer is one untested config line.

The one honest counter-argument is BLOCKER-2's inverse: if you *ever* migrate, you must adopt WirePlumber, so a scoped-PipeWire "toe in the water" is not available. That is an argument for the migration being a **hardware-refresh-generation decision** — a box with real RAM headroom, a controller without a firmware-crash history, and a deliberate re-derivation of every field fix — not for doing it now on a Zero 2 W to unblock a contingency.

---

## THE CHEAPEST PATH TO THE ACTUAL GOAL

The goal is *the son's Spotify account playing on the box*. Ranked by cost:

**0. Today, zero code — Sonos.** `pi/sonosd.py:35` and `:490-535`: the `spotify_sharelink` kind hands the URI to the Sonos speaker via SoCo ShareLink, and **Sonos owns the queue** using the account linked in the Sonos app. `daemon.py:1419-1480` (`_sonos_start_spotify`) already routes box targets there, and `renderer` is orthogonal to `output` (`daemon.py:1075-1082`). Link the son's account in the Sonos app and his content plays from the box's cards, today, with no code and no risk. The only gap is that it requires a Sonos in range.

**1. Two hours — run the PulseAudio test that was never run.** `bench/pipewire_shim_rig.sh pulse` (`:243-285`) on a bare Trixie image or the box at a quiet moment. If `module-alsa-sink device=vibb_bt` opens (the expected answer — PA passes the string to `snd_pcm_open` with no card lookup) and S3 shows the device is released when idle, then the Soloist audio question is **closed with a ~10-line additive config**: bluealsa untouched, zero field fixes at risk, PA running only while Soloist runs, and instant rollback. **This test is the only thing standing between the current design and a settled answer. Do not decide anything else until it has run.**

**2. If PA fails — asymmetric, as the plan already sketched** (`PLAN-soloistd.md:156-162`). The HAT's pcm is `plug -> hw:sndrpihifiberry`, a real card, so PipeWire *can* serve it (proven by the same bench that killed the plugin-pcm path). Soloist becomes available on local/Sonos output only; BT stays bluealsa; `set_output` refuses Soloist entries when `output=="bt"` with the existing closed refusal shape. Bounded, reversible, no BT layer touched.

**3. Regardless of the above — make the gate backend-neutral now.** Replace `a2dp_pcm_present`'s implementation with an `org.bluez.MediaTransport1` presence check (`btbus.py:372-388`, `:593-618`), keeping the function signature and both existing tests. Small, testable against `fake_bluezd.py`, useful today, and it removes BLOCKER-3 from any future audio-stack decision.

**4. Full PipeWire — defer to a hardware generation.** Revisit only when the box is not a Zero 2 W, the crash model is closed, and the migration can be budgeted as a project with its own bench rig — not as a side effect of an engine swap that is itself parked.

---

# APPENDIX C — Architect consistency pass (verbatim, 2026-09-03, tree at `57e07f8`)

Three Phase 0 commits landed while this pass ran (`42893ea` transport gate/shadow, `cd5b106` `pi/vibb/audio.py` resolver, `57e07f8` NEW-1 cap before play). Findings are against that tree; the commit order starts after them. Line numbers: `daemon.py` unchanged; `player.py` +16 below `:263`; `btbus.py` per `42893ea`.

## 1. Findings and decisions

**a. AM-7 mechanism (what survives of the self-test plumbing).** Decision: one boolean, `audio.cap_everywhere()` = `selftest_state().get("verdict") == "fail-safety"`, read from `POLICY_FILE` (`/run/vibb-audio-policy`, JSON, tmpfs) with an mtime-cached read so the 1/s readers never parse; `/status.audio_policy` is the verdict string, key-guarded like `bt_connected` (`daemon.py:2997-3006`) so bluealsa boxes emit nothing. The cap bypass is one kwarg on the pure rule: `output.local_volume(stored, pcm, cap, everywhere=False)` — `if not cap or (pcm != OUTPUT_PCMS["local"] and not everywhere): return stored` — keeping `tests/local_volume_cap.py:36-40` green. Call sites pass `everywhere=audio.cap_everywhere()`: mpv spawn `player.py:557-559`, `_apply_box_volume` `player.py:162-185` (now capped, landed in `57e07f8`), `daemon._local_volume` `:163-166` (which feeds the live retarget `:1799-1802` and the post-reopen re-apply below), and `Orchestrator.volume()`: `cap = load_settings()["volume_cap"]` at `:992` becomes `min(volume_cap, local_fallback_cap)` only when `cap_everywhere()`, both clamps `:1035` and `:1047` unchanged otherwise. Delete from the plan: the K-table row "local-landing refusal in `set_output`/`play`" (`PLAN:699`), `local_landing_allowed()` in the signature block (`:694, :754`), the I10 test row clause (`:730`), and mark B.6's failure-action paragraph (`:433`) and Q11 (`:800`) superseded by AM-7. Nothing in `set_output`/`play` reads the verdict; the only daemon-side consumers are the cap sites and `/status`.

**b. AM-22 vs probe 3 / default nodes.** `main-embedded` disables `hooks.default-nodes.state` (persistence) only; the find/apply hooks stay on, which is why the bench saw a `*` default (HDMI). With AM-23 (`find-default`/`find-best` disabled) that election is inert for linking, so it is harmless — nothing in vibb reads or sets a default, and no assertion may depend on the `default` metadata being absent. Probe 3 (`PLAN:77`) survives as written under AM-8 ("no `default` object = PASS"), but its set is racy: WirePlumber's apply hook owns `default.audio.sink` and may overwrite it immediately. Decision: probe 3 sets `default.configured.audio.sink` (the client-facing key) to the sibling null sink, asserts the pinned stream did not move, then deletes it; the targetless probe (assertion 1) stays the real hook guard. Assertion 5 keeps "no restore-stream metadata" (`hooks.stream.state` off) but must not mention `default`. `wpctl status`'s election is harmless with AM-23; add one RF-class line asserting `hooks.linking.target.find-default`/`find-best` are absent from `wpctl status`'s hook list if 0.5.8 exposes it, else hash the fragment (already the plan's fallback).

**c. AM-3 vs boot_fails.** `_attempt_failed` (`btwatchd.py:707-724`) only exempts `NotReady`. With no A2DP endpoint yet, BlueZ's `Connect()` on a paired headset takes two shapes: `NotAvailable` (no connectable profile — counts as a boot fail) or an ACL-only AVRCP connect that drops (`:239-271`: `_committed` False → `refusals += 1`, ladder climbs, five in a row parks). The nudge (`:428-465`, `:471-512`) covers the second shape within 10 s; the first burns `BOOT_FAIL_LIMIT` at 5 s cadence (`:121-123`). One-line guard, stack-neutral: in `_attempt_failed`, `if "NotReady" not in detail and "NotAvailable" not in detail and now - self.boot_started >= BOOT_ENDPOINT_GRACE_S:` with `boot_started` stamped in `enter_boot` (`:357-364`) and `VIBB_RECON_ENDPOINT_GRACE=15` (bounded by B1's measured WirePlumber init). Under bluealsa the endpoint is up within ~1 s, so the grace never changes a verdict there; pin in `bt_output_policy.py` style.

**d. AM-14 vs restart dedup / blip rebuild.** Yes, the player-child restart must mark itself: `paths.note_go_restart()` (`paths.py:65-71`, already importable in player.py via `vibb.paths`) right after the `try-restart`, because `_go_output_rebuild`'s `fresh` test honours the cross-process marker (`daemon.py:4976-4977`) and would otherwise issue a second restart on a coincident blip resume (`:4996-5006`) — the 2026-07-17 storm class. `_net_changed` (`:3051-3055`) only reads the in-process stamp, so a player-initiated restart does not dedup a net-change restart; that race is bounded by the 60 s cooldown and acceptable. The supervisor (`:5163-5254`) never fights it: `try-restart` on a parked unit is a no-op, which is the desired behaviour offline. Two spec corrections the plan's pseudo-code misses: (1) `spotify.status()` swallows every `OSError` and returns `{}` (`spotify.py:195-199`), so a timed-out status reads as "no track" → True → spawn, the exact wedge; the loop needs a raising probe (`spotify.status_strict(timeout)` = the same `urlopen` without the except, cache untouched) and must classify via `getattr(e, "reason", e)` since `urlopen` wraps `ConnectionRefusedError` in `URLError` and surfaces read timeouts as bare `TimeoutError`; (2) the restart must be `systemctl --no-block try-restart go-librespot` — `go-librespot.service`'s `ExecStartPre` waits up to 60 s for DNS (`install.sh:553`), and a blocking call sits on the tap→audio path in the player child. After the restart, one more ≤1 s probe: refused/no-track → spawn.

**e. AM-10 walk (btwatchd announce → `set_output(bt, fallback=True)`).** btwatchd commits once per transition (`_output` dedups on `self.announced`, `btwatchd.py:520-522`) → `POST /output` (`daemon.py:3619-3620`) → `set_output` → sonos guard `:1713-1722` → `pcm` at `:1723` → **[new, outside `ORCH.lock`]** `if device == "bt" and fallback and audio.stack() == "pipewire": audio.ensure_bt_route(mac)`: read the file; if MAC and a non-`UNRESOLVED` node are present, one `pw-dump` (≤1 per announce, ~30-60 ms) confirms the pinned name still exists; rewrite only when it differs, under `acquire_process_lock(blocking=False)` — `None` means `bt.py` owns the radio and will write the route itself (`bt.py:396-398`), so skip, never block the HTTP thread; unique tmp `ASOUND + f".tmp.{os.getpid()}"`, fsync, `os.replace` → ≤1 rewrite. Then the existing `:1732` branch (output already bt): deferred mpv apply gated on transport AND `sink_ready` (AM-11), then exactly the one live reopen at `:1749`. If output was local, the full path `:1757+` runs with the route already current. Count per reconnect: 0-1 asound rewrites, 1 reopen on the announce path — identical to today; the second reopen from `_bt_blip_resume` → `_go_output_rebuild` (`:4858-4859`, `:4980`) is pre-existing and unchanged, and `bt.py connect()`'s own `_route_alsa` reopen (`:472-479`) only runs on a user-initiated `use`, also as today. AM-10 adds no reopen.

**f. AM-9 fail-closed exit vs the crash budget.** `_heal_crashed_child` treats any nonzero rc as a crash (`daemon.py:641`) and charges the budget at `:660` only after `_audio_ready()` passed (`:655`); with `sink_ready` folded into `audio_ready()` the "node absent" case waits inside `BT_RESUME_S` without charging, but the respawn that follows the node's appearance is charged, and two flaps silence the box. Decision: `player.SINK_WAIT_EXIT = 75` (EX_TEMPFAIL), one log line, no now-playing rewrite; in `_heal_crashed_child` `if child.poll() != SINK_WAIT_EXIT: self._crash_respawns += 1` — everything else (window, published intent `:648-656`, `_audio_ready` gate) unchanged, so a rc-75 child is respawned exactly when the node exists and never burns the 2/boot budget; the stall-watchdog respawn (`:609-614`) already gates on `_audio_ready()` and touches no budget. The player publishes `now-playing.json` with `paused: False` before the wait (`player.py:509-523`), which is what lets the healer see intent; keep that order. `play_spotify` gets the same ≤5 s wait before `/player/play` for symmetry (exit 75; no healer for spotify, `:646`).

**g. AM-15 `vibb_closed`.** A targetless pipewire pcm is closed only by the WirePlumber hooks (bench s1d: KILL without AM-23) — one layer, the one most exposed to package drift. A pinned non-existent node fails at `snd_pcm_hw_params` (s2d) through the client-side `dont-fallback` property, delivered twice (client.conf.d + `PIPEWIRE_PROPS`), independent of the hooks. Decision: `pcm.vibb_closed { type pipewire server "/run/pipewire/pipewire-0" playback_node "vibb-closed-never" }` — same mechanism as `audio.UNRESOLVED` (`audio.py:24`), distinct name so a log line says which it was. The self-test's targetless probe stays as the hook guard. Extras: `extra.sh --run` exports `VIBB_ALSA_DEFAULT=hw:sndrpihifiberry` after stopping the trio; nothing in `pi/` opens ALSA `default` (grep clean), so `pcm.!default` is written in pipewire mode only, and in bluealsa mode only when `/etc/alsa/conf.d/99-pipewire-default.conf` exists (a box that has been through the migration) — the production box's `asound.conf` stays byte-identical.

**h. AM-17 hw-volume=true vs I8.** Consistent: I8 forbids vibb writing a bluez node's volume; hw-volume only governs how PipeWire mirrors the headset's AVRCP level and writes `MediaTransport1.Volume` on node-volume changes, which no vibb path makes (mpv softvol `player.py:143`; go-librespot software volume, no `mixer_device` in `install.sh:264-286`). The self-test's gain assertion applies to the HAT node only; it must never assert the bluez node's volume (it will mirror the headset). One new pin worth adding to I8: pipewire-alsa's ctl plugin maps `Playback` to the default sink, so `no_volume_writer.py` also greps `pi/` for `amixer`/`snd_mixer`. Code does nothing with the value: install.sh carries it as `WP_HW_VOLUME=true`, `wp_policy_fragment.py` asserts the fragment matches the variable, and if S4 flips it the change is one variable plus N10's line ("headset buttons lost").

**i. Rig vs §A/§B — install.sh is written from the rig (`pipewire_platform_rig.sh:172-353`).** Differences: `pipewire.socket` adds `DirectoryMode=0750` (rig `:199`); `pipewire.service` drops `RuntimeDirectory=`/`RuntimeDirectoryMode=` (AM-1), `PIPEWIRE_CONFIG_DIR` (AM-21, `PLAN:225`) and `StateDirectory=` (rig makes the dirs at `:184-185`), `Nice=0` not `-11` (AM-4, `PLAN:229,256,277,804`); `wireplumber.service` is `BindsTo=pipewire.service` + `After=pipewire.service dbus.service` + `WantedBy=pipewire.service` (no `Requires=`), `ExecStart=/usr/bin/wireplumber -p main-embedded` (AM-22), `Nice=0`; no `bluetooth.service.d/vibb-after-wp.conf` (AM-3; delete `PLAN:287-291, :643, :645, :725`); `10-vibb.conf` and `client.conf.d` identical to §B; `50-vibb.conf` extends `main-embedded` with only `hardware.video-capture`, `monitor.alsa-midi`, `monitor.bluez-midi` and the four `hooks.*.state` disables (`rig:283-293`) — §B's 20-name profile block (`PLAN:305-331`) is dead text; settings block identical; `bluez5.roles = [ a2dp_sink ]` (rig default, peer-centric; §B `:347` says `a2dp_source`), `bluez5.enable-hw-volume = true` (§B `:353` false); `51-vibb-hooks.conf` adds `hooks.linking.target.find-default/find-best = disabled` (AM-23; merge into one file); rules identical. Neither rig unit carries `PIPEWIRE_PROPS` (AM-5 belongs on vibb-daemon/go-librespot/soloistd, not the audio units). Enable order `socket → service → wireplumber` (`rig:373`); stop/mask order the reverse. Verify tags resolved by S1-S3/S5: `stream.properties` reach pipewire-alsa (s3b); `server`+`playback_node` honoured, `target.object` set (s2a); no rescue (s1c); errno shape = write error (s2c); pinned ignores default (s1d); targetless needs AM-23 (s1d); all eight settings names (s1a); prop keys `alsa.card_name`/`api.alsa.card.name` and node-name shape (s5); `monitor.alsa.rules update-props` works; `pipewire` user reaches org.bluez (s3a); absent target fails at hw_params (s2d); AM-21/22/23 names. Still open: S4 (roles naming → `bluez_output.*`, codec sbc, suspend 120 on the bluez node, no 110b/`bluez_input`, dummy-avrcp-player, hw-volume buttons, `bluez5.default.rate` key), S3c crash survival of AM-1/AM-2 on a real flash, the HAT node's exact name on the Zero (resolver reads it, so informational), go-librespot live reopen through pipewire-alsa (s6) and `/player/volume` with no track (AM-13 belt — already tolerated by `player.py:186-188`'s `except OSError`), soloist binding (s7, Phase 3), `--audio-buffer=0.5` plugin ring size, `remote.name` without env (s3a's "without env" line was not recorded — keep AM-5's env belt mandatory), `DBUS_SESSION_BUS_ADDRESS=disabled:` journal-noise line, B10 re-registration tiers, all RF/RSS numbers.

**j. New: the landed gate ignores the stack.** `GATE_MODE` is env-only, default `shadow` (`btbus.py:389`); under pipewire, shadow answers with PCM1 → `_managed("org.bluealsa")` raises (no such name) → cli `bluealsa-aplay -L` against a masked daemon → False forever, so every gate site reads "no transport". Decision: `a2dp_pcm_present` returns `a2dp_transport_present(mac)` when `GATE_MODE == "transport" or audio.stack() == "pipewire"` (lazy import; one line at `:395-397`), plus `Environment=VIBB_BT_GATE=transport` on the units as belt — the code coupling is required because `play.sh`/ssh invocations of `bt.py` carry no unit env. Also AM-12 criterion (c) (a <3 s transport flicker) is unobservable at `SHADOW_S=10`; make it `VIBB_BT_GATE_SHADOW_S` and set 1 on `vibb-bt-reconnect` only (its `_await_pcm` polls 1/s for ≤10 s per connect — bounded), 10 on vibb-daemon.

**k. New: F-table rows still owed after `57e07f8`.** The post-reopen re-apply on local in `set_output` (`daemon.py:1831`) and `_go_output_rebuild` (`:4980`) is not in the tree; it is stack-neutral and belongs in Phase 0 (commit 1 below). `local_volume_cap.py:72-80` and `spotify_volume_before_play.py` cover only the player side.

## 2. Commit order for `pipewire` (after `57e07f8`)

Invariant stated per commit as **P:** suite green + production box (stack unset/bluealsa) unchanged, with the reason. CP = safe to cherry-pick to `main` now.

1. **daemon: re-apply the cap after a go-librespot reopen onto local.** `daemon.py`: helper `_go_volume_cap(pcm)` (POST `/player/volume` with `_local_volume(self._volume_setting(), pcm)` scaled by `volume_steps`, outside `ORCH.lock`), called after a successful `reopen_go_output` at `:1831` when `device == "local"` and in `_go_output_rebuild` after `:4980` when `current_output()["output"] == "local"`. Test: `tests/local_volume_all_paths.py` v1 (fake go HTTP: reopen onto local → capped POST, onto bt → none). ~60 lines. **P:** adds one POST on a path that already reopens; bt path untouched. CP.
2. **player: confirm Spotify paused before an mpv spawn; restart a wedged API (NEW-2, AM-14).** `player.py:448-454` → `_confirm_spotify_paused(budget_s=2.5)` per finding d (strict status probe, `.reason` classification, `--no-block try-restart`, `paths.note_go_restart()`). `spotify.py`: `status_strict()`. Test: `tests/spotify_pause_confirm.py` (slow pause → `paused` seen before `Popen`; timeout wedge → restart + marker, spawn after; refused → immediate; "no track" → immediate; never a restart on refused). ~90 lines. **P:** the common case (pause answers <1 s) is today's call plus one status read; the wedge case replaces double audio with 2.5 s + a restart. CP.
3. **btbus: the gate follows the stack; shadow cadence tunable (finding j).** `btbus.py:389-397`, `tests/bt_transport_gate.py` case 6 (stack pipewire → transport answers even in shadow). ~20 lines. **P:** `audio.stack()` is bluealsa on main → no branch taken. CP.
4. **output/btwatchd: readiness under pipewire = transport AND sink node.** `output.audio_ready()` `:174-186` (bt: `a2dp_pcm_present` AND `audio.sink_ready("bt", mac)`; local: `_i2s_card_present()` AND `sink_ready("local")`, pipewire only); `btwatchd._await_pcm` `:438-442` ANDs `sink_ready` under pipewire, docstring. Test: `tests/audio_ready_pipewire.py` (stubbed `pw_dump`, both stacks). ~40 lines. **P:** every new branch is behind `audio.stack() == "pipewire"`; `bt_output_policy.py`, `player_crash_heal.py` unchanged.
5. **bt: `_route_alsa` resolves nodes under pipewire; `route` verb; recover units.** `bt.py:430-483` stack branch (`audio.resolve_route(mac, tries=10, delay=1.0)`, node-aware idempotence, colon MAC kept), unique tmp name `ASOUND + f".tmp.{os.getpid()}"` (both stacks), `route` verb in `main()` `:672-730` (MAC from `MAC_FILE` or `asound_text(None, None, None)`), `recover()` `:244-245` → `audio.recover_units()` (+ `VIBB_BT_HEAL_RESTART_WP`), debug hint `:393-394`. Tests: `bt_state_fsync.py` run twice (bluealsa as today; pipewire with `resolve_route` stubbed: colon MAC, one fsync, no rewrite on repeat, one rewrite on a node rename), `go_restart_dedup.py` unchanged. ~90 lines. **P:** bluealsa branch is today's template verbatim; the tmp rename is invisible.
6. **audio/daemon: `ensure_bt_route` on the announce, file-only and locked; `sink_ready` gates the deferred apply (AM-10/11).** `audio.ensure_bt_route(mac)` per finding e; hoist after `daemon.py:1723`; AM-11 at `:1735`. Tests: `tests/asound_second_writer.py` (two concurrent writers, flock spy, never a truncated file), `tests/output_announce_route.py` (announce → ≤1 `pw-dump`, ≤1 rewrite, reopen count equals today's). ~70 lines. **P:** guarded by `fallback and device == "bt" and stack() == "pipewire"`; `output_bt_quiet_marker.py`, `orch_lock_io.py`, `output_switch_resume.py` unchanged.
7. **player/daemon: the ≤5 s sink wait fails closed with exit 75; the healer never charges it (AM-9).** `player.py` before `:559` (and before `/player/play`), poll 0.5 s; `daemon.py:660` skip for rc 75. Tests: `tests/player_sink_wait_fail_closed.py`, `player_crash_heal.py` case 13. ~45 lines. **P:** wait runs only under pipewire; rc 75 never occurs on main.
8. **install: `VIBB_AUDIO_STACK` toggle, pipewire user, the rig's units and fragments.** Parse the env, write `/etc/vibb/audio-stack`; pipewire branch: packages (missing-only), `groupadd`/`useradd` + dirs (`rig:182-186`), three units and four fragments verbatim from `rig:190-338` (51-hooks merged into 50; `WP_ROLES=a2dp_sink`, `WP_HW_VOLUME=true` as variables), `systemctl mask --now bluealsa.service bluealsad.service`, enable socket → service → wireplumber, keep-alive block skipped. ~180 lines (heredocs). **P:** with the stack unset/bluealsa the whole block is skipped; the only new artefact is the `audio-stack` file containing `bluealsa`.
9. **install: unit env lines, `After=` re-point, asound via `bt.py route`, `pcm.!default` + `vibb_closed`, extra.sh.** `Environment=VIBB_AUDIO_STACK= PIPEWIRE_RUNTIME_DIR=/run/pipewire PIPEWIRE_PROPS={...} VIBB_BT_GATE=transport` on vibb-daemon `:1154-1158`, vibb-bt-reconnect `:626-641`, go-librespot `:541-556` (pipewire mode only); `After=bluetooth.service wireplumber.service`; `bt.py route`; `pcm.!default`/`vibb_closed` per finding g; `extra.sh:41-42, :72, :112-113` gain the trio + `export VIBB_ALSA_DEFAULT` under pipewire. Tests: `extras_wrapper.py` pipewire-mode run, `tests/install_unit_order.py` (I5), `audio_client_props.py` half (PIPEWIRE_PROPS on three units). ~110 lines. **P:** every write is inside the pipewire branch; `extras_wrapper.py` default run asserts today's set.
10. **install: the rollback branch.** bluealsa mode when the pipewire units exist: `disable --now` + `mask` wireplumber → pipewire.service → pipewire.socket, `rm` `99-pipewire-default.conf`, unmask + enable bluealsa, keep-alive block as today, `bt.py route` back to `type bluealsa`, `After=` back, `bt-quiet` untouched. Test: `tests/audio_stack_toggle.py` (I13, fake `systemctl` both directions, order asserted). ~80 lines. **P:** the branch triggers only when a pipewire unit file exists.
11. **audio: the policy self-test (I10).** `policy_selftest()`, `selftest_state()`, `cap_everywhere()`, `POLICY_FILE`, cookie tracking (AM-8), probes per B.6 with finding b's probe-3 key; daemon: thread at `main()` `:5754+` (pipewire only, ≤60 s socket wait, mtime rate limit), re-run after `_bt_recover` `:4663-4679`, `POST /audio/selftest`, `/status.audio_policy`. Test: `tests/audio_policy_selftest.py` (canned `pw-dump`/`wpctl settings`; good + each single drift → class; cookie re-run; rate limit). Split 11a (audio.py + test, ~150) / 11b (daemon wiring, ~50). **P:** thread not started under bluealsa; `/status` key absent.
12. **cap everything on `fail-safety` (AM-7).** `output.local_volume(..., everywhere=)`, the five call sites and the `volume()` clamp per finding a, ui.py screen line. Tests: `local_volume_all_paths.py` v2 (I1 full enumeration + `everywhere` on every path, `volume.json` untouched), `local_volume_cap.py` premise. ~80 lines. **P:** `cap_everywhere()` is False without `POLICY_FILE`.
13. **pins: I2 `audio_client_props.py` (spawn argv targets + fragment greps), I4 `output_file_single_writer.py`, I7 `wp_policy_fragment.py` (reads `WP_HW_VOLUME`/`WP_ROLES` variables), I8 `no_volume_writer.py` (+ `amixer`/`snd_mixer`), docstrings (`stop_child_group_kill.py` NEW-7, `mpv_launch_flags.py:41-47` `--audio-buffer` split).** ~150 lines of tests. **P:** tests only.
14. **docs: PLAN amendments AM-24..AM-31 from this pass** (a-k above), the K-table and `audio.py` signature block corrected, §A/§B marked "rig is the source", the verify-tag status table from finding i. **P:** docs only.

Field test on the spare Zero starts after commit 10 (platform complete, self-test still absent); commits 11-13 land before the ≥14-day soak begins. soloistd (Phase 3) follows and is out of scope here.

---

# APPENDIX D — QA pass on the Phase 3 decisions D1–D3 (verbatim, 2026-09-04, tree at d0ce095)

Scope respected: the soloistd design (dialect sidecar, exit-10 latch, walk, 80-window, whole-file cache) is taken as settled. Everything below is about how D1–D3 sit on the real code.

---

## D3 — warming on library ADD

### 1. Preemption / bookmark poison / play_origin — **NEEDS-CHANGE**

**Evidence.**
- The abort itself is free: every kid action reaches the engine through the dialect (`daemon.py:2304-2325 _spot_control` → `spotify_command`; `player.py:220-330 play_spotify` → `/player/play {uri, skip_to_uri, position}`). The sidecar can abort the walk on *any* inbound dialect command with no daemon change — as long as it serializes "abort walk → restore → execute the command" under its single lock. That is the "lands at the right track" guarantee; nothing in the daemon has to know a warm existed.
- **Bookmark poison is real** if the warm is visible in `/status`. `_spotify_bookmarker` (`daemon.py:4649-4714`) computes `context = to_uri(ORCH.target)` whenever `ORCH.source == "spotify"` (`:4690-4692`) — i.e. the *last thing the kid tapped*, still true while the box sits idle/paused — and `spotify.bookmark_step` (`spotify.py:236-260`) writes `{context_uri: <kid's X>, uri: <warm's track from Y>}` whenever the status shows a playing track with origin in `("go-librespot","",None)`. Result: X's bookmark points at a track not in X; the next tap does `skip_to_uri` for a uri the walk can never find → worst case a full-context walk. It is also the `_SPOT_LAST_PLAYING`/shutdown snapshot (`:4672-4677`).
- **The opposite choice (a non-box origin for warm tracks) kills the podcast D3 explicitly allows.** `_arbiter` (`daemon.py:447-486`): `source == "mpv" and mpv alive and spotify_playing(st) and origin not in ("go-librespot","",None)` → `"spotify took over (phone) — yielding mpv"` → `_stop_child()`. A warm during a HAT podcast with any third origin value is a phone takeover to the arbiter. Same test at `:1893` (output-switch resume) and `spotify.py:249`.
- So a "warm origin" marker cannot be a play_origin *value*: both consumers interpret non-box as phone.

**Minimal fix.** The warm must be **invisible to the dialect**: while warming, the sidecar's `/status` returns a frozen pre-warm snapshot (track/paused/stopped/position as they were, `play_origin` unchanged) and exposes the warm only on `/soloist/health {warming: {uri, idx, n}}`. Then `bookmark_step` sees the paused/stopped pre-warm state → writes nothing (`spotify.py:247`), the arbiter sees no playing session, `_audible_now` reads False (the warm *is* the sweeper's job, see #3). Add the origin question to the contract test: `soloist_contract.py` must pin "status during warm == status before warm".

Also: D3 says warming may run while the kid is **paused**. On resume the daemon's shortcut (`daemon.py:909-921`) calls `/player/resume` expecting an unpause; the sidecar then has to run the *resume walk* back to the kid's track (N skips + shroud, seconds) instead of a 60 ms unpause, and if the walk's `pause` loses the race (PLAN-soloistd "0.38 s WAS faintly audible") the kid hears a stranger's track. **Recommend:** a loaded-but-paused session is *not* warm-eligible until paused ≥ 10 min (the backup's own "resumes any second" rule, `backup.py:736-770`).

### 2. "No active A2DP" as the only gate vs the crash memory — **NEEDS-CHANGE**

**Evidence.**
- The sweep's gate is `_audible_now` (`daemon.py:5840-5873`, installed at `:5876`): it is **not** an A2DP gate — it holds for mpv unpaused on *any* output, for sonos, and for a busy engine API. Its stated reason (`library.py:262-270`) is the Spotify **stream** and the offline prober, not only the A2DP link. D3's "podcast on the HAT while warm runs, wifi is free" drops that rule: a HAT podcast streamed from a URL plus a warm fetching at "~250× realtime" (PLAN-soloistd cache verdict) is exactly the "pausing a song a two-minute fight" load (`library.py:266`). A *cached* podcast on the HAT is fine; a *streamed* one is not. Gate: `not (_audible_now() and _streaming_now())` — i.e. allow the warm only when what plays is local/cached (`_streaming_now`, `daemon.py:5636-5669`, already distinguishes URL mpv from file mpv).
- **Connected-but-idle headset:** `bt_connected` = `_bt_transport_ready()` = `btbus.a2dp_transport_present` which counts *any* transport state, idle included (`btbus.py:406-430`; `daemon.py:3048`). The backup treats that as busy (`backup.py:772-773`) unless poweroff-imminent. For the warm it is "not active A2DP" and the crash memory does **not** tie idle-ACL+wifi to a crash — the three observed patterns are disconnected scan loops during A2DP, paging + wifi, and the AVRCP storm (`vibb-bt-crash.md` 2026-08-13 entries). So heavy wifi on an idle link is defensible. Under pipewire the transport also releases after 120 s paused (`audio-stack.sh` bluez rule `session.suspend-timeout-seconds = 120`).
- **The pattern D3 re-creates is paging, not streaming:** headset OFF (car trip on the phone's hotspot — D3's own motivating case) → btwatchd blind-pages on its backoff. It defers while `radio.busy()` is fresh (`btwatchd.py:391`, `radio.py:BUSY_TTL_S=20`), but the starvation belt `YIELD_GIVEUP_S=120` (`btwatchd.py:114,386-389`) pages anyway after 2 min. A 25-minute warm therefore guarantees "BT paging + wifi" — the thrice-observed flap pattern — every backoff cycle. And the sidecar **cannot** even keep BUSY fresh: `/run/vibb-radio-busy` is created by root vibbd (`radio.py:44-50`, no chmod; `/run` root 755) and soloistd runs as `$RUN_USER` (§I) — its `_touch` fails silently by design.

**Minimal fix.** (i) The warm is driven from the daemon's sweeper thread (it already fires on library save, `daemon.py:3582`), which touches BUSY every 10 s while polling the warm and POSTs an abort when `_busy()` flips — the markers stay where root can write them. (ii) Add "speaker configured and absent" to the gate, or teach btwatchd a `vibb-warming` marker that suspends the starvation belt (a page during a warm has nothing to gain: nobody asked for sound). (iii) `wait_paging_clear()` before the first `play`, like `player.py:230`.

### 3. The trigger and "which entries are new" — **CONFIRMED-OK with two corrections**

**Evidence.** The hook already exists and needs no new code: `/library` POST (`daemon.py:3565-3583`) saves under `LIB_LOCK` then `_sync_wake.set()` outside it; the sweeper (`library.py:511-660`) coalesces bursts (`SYNC_SETTLE_S`), re-checks `_busy()` per entry, and the spotify branch (`:544-570`) posts `/cache/download {uri}` for every entry with `cache != 0` whose `_precache_due(uri)` is True. Under soloist the sidecar implements `/cache/download` as the walk. Offline-at-add: `_precache_done` is only stamped on success (`:566`), so the next 6 h sweep (`SYNC_INTERVAL_S`) retries — "entries added while offline" is covered by construction.

Two corrections:
- **`_precache_due` fails open** (`library.py:403-418`): playlists gate on `spotify.snapshot(uri)` = `GET /cache/snapshot` (`spotify.py:51-60`), a fork endpoint. If the sidecar does not implement it, `snap` is None → `return True` → **every sweep and every library save re-walks every playlist** (a 500-track walk 4×/day plus each PWA edit). The sidecar must either serve `/cache/snapshot` (needs the Web API or a stable hash of the first-80 queue) or make `/cache/download` idempotent from its own ledger (warmed uris + item count + build id — the canary's ledger, `bench/soloist_cache_canary.py`). The ledger is the better answer: it also records "aborted at index k" for the retry.
- **`cache` defaults to 0** (`library.py:79`, PWA `app.js:524` "no pre-cache"/"pre-cache"). "The moment an entry is saved, warm it" is only true if the parent toggles pre-cache on. OWNER QUESTION: should `normalize_library` default spotify entries to `cache: 1` when the engine file says soloist (a one-line install-time-engine read), or is opt-in the intent?

### 4. Scale, end-of-context, battery, idle.py — **NEEDS-CHANGE**

- **Reaching the end:** the 80-window does not limit *skipping* (kill 2: "resume walk UNAFFECTED — sliding window"). What stops the walk at the end is unaddressed: Spotify continues into autoplay, and the docs give the field for it — `get_queue` entries carry `source: context | queue | autoplay` (websocket-api reference, fetched today). Rule: after each `track_changed`, `get_queue{limit:1}`; stop when the current/upcoming item's `source == "autoplay"` or `upcoming` is empty. Without this a warm of a 30-track playlist caches Spotify's radio forever. P2's Web API listing gives an exact count but is not required for stopping.
- **Bound the walk by `cache: N` and bytes.** For a **show**, `play show` + skip walks every episode at 50–150 MB each (91 MB from one episode on the bench); a 200-episode show is 10–20 GB into a `-z` cap — LRU eviction of the kid's just-warmed content. `cache: N` already means "newest N" for podcasts (`library.py:80`, `:546`); apply the same meaning to soloist entries, and cap a single warm pass at a byte budget read from `spotify_cache_gb` (`app.js:929`).
- **idle.py:** if the warm is invisible in `/status` (fix #1), `daemon_playing()` (`idle.py:70-75`) reads False and `_cycle` (`:160-180`) counts idle → a 25-min warm on a 5-min timeout is **killed by poweroff** mid-walk. If visible, it holds the box awake indefinitely. The plan's "warming-marker so idle.py does not read it as kid-activity" has the sign wrong: the warm *needs* a bounded hold. Minimal: the warm has its own budget (e.g. ≤ 20 min per pass), and while it runs the daemon's `/status` exposes `warming: true`; idle.py treats it like ssh (`ssh_active`, `:96-150`): a hold with a hard release, never an unbounded one. Battery: BUSY forces wifi PS off (`_streaming_now`, `daemon.py:5649`) for the whole walk — that is the +30–50 mA the governor exists to avoid; on battery with no charger gate this is the owner's call, but the per-pass budget should be smaller on battery (`plugged_cached()` is already there, `:5802`).

### 5. Abort semantics and the half-fetched file — **OPEN-QUESTION-FOR-OWNER (bench)**

Nothing in the plan or spike measured what a `skip_next`/`pause` does to an in-flight fetch. Two unknowns that decide the ledger design: (a) does Soloist *cancel* the previous track's fetch on skip (then "2 s per item" is too short for whole-file — the walk must wait until the cache file stops growing, not until `track_changed`+2 s); (b) is a truncated file served partial later (stall at the truncation point) or re-fetched? Bench items to add to `soloist_spike.py`: fetch-completes-after-skip (watch the cache dir per item), and abort-mid-fetch → net down → replay. Until then the ledger marks an item warm only when its file size is stable for 2 s and roughly `duration × ~20 kB/s`.

### 6. History pollution / skip storms / pacing — **OPEN-QUESTION (accepted risk, needs a cap)**

A warm is the mash shape (kill 4: 60 skips/min, zero errors, one minute) stretched to 25 min at ~20/min. The documented API says nothing about limits (websocket-api fetched today: no rate-limit text); the residual is the "delayed account cooldown" the spike could not observe. Minimal pacing: ≥ 3 s/item (a third of the tested rate), ≤ 100 items per pass, continue at the next sweep; the ledger makes the resumption free. Play-history: accepted by the owner; note the Connect side effect — the phone app shows the box "playing" at volume 0 for the whole walk.

---

## D2 — API key via PWA

### 7. `/soloist/configure` vs the backup pattern; the `-k` argv claim — **NEEDS-CHANGE (the statement is false as written)**

**Pattern that carries over** (`daemon.py:3785-3795`, `backup.py:434-489`): default-deny (not in `SAFE`, `:4045`), JSON content-type gate (`:3523-3548`, pinned by `api_csrf_content_type.py`), `_write_secret` 0600 tmp+fsync+`os.replace` (`backup.py:606-623`), previous bytes restored on failure (`:463-487`).

**What is different:** backup validates *before* committing by running rclone/restic against the written files. The key can only be validated by the child, which needs a restart (the unit's `EnvironmentFile=` is read by PID 1 at start — the running sidecar cannot see a rewritten file). So the POST cannot be synchronous-validated. Shape: write `prev`, write new, `systemctl restart vibb-soloistd`, return 202; the PWA polls `/soloist/health`, which must distinguish **three "no"s** the sidecar can tell apart only by the child's own output: exit 10 / "expired" line → `expired`; auth failure line → `bad-key` (restore `prev`, restart again); no network → `offline` (keep the key, `_SPOT_OFFLINE` semantics). Which exact log line means "bad key" is **not in the docs** — bench it once with a mangled key.

**The argv claim.** The CLI reference lists only `-k, --api-key KEY` — no env var, no file (fetched today). The auth page's `--api-key "$SOLOIST_API_KEY"` is shell expansion, not env support; the same page warns "treat shell history, **process-manager configuration**, screenshots, and logs containing the command as sensitive". A wrapper doing `exec soloist -k "$KEY"` puts the key in the **child's** `/proc/<pid>/cmdline`, world-readable by default. Honest statement for the doc: *"never in a unit file, the journal, or the daemon's argv; it IS visible in the child's cmdline to any local login for the process's lifetime (same class as go-librespot's credentials.json being readable by $RUN_USER)."* Mitigation available without Soloist's help: `hidepid=invisible` on `/proc` (a `/proc` remount in a `.mount` drop-in, or `ProtectProc=invisible` only protects other units' views — not a shell). Note also EnvironmentFile needs `KEY=VALUE`, so the file is `soloist.env`, not `soloist.json` — D2 names the wrong shape.

### 8. `needs-key` vs the offline popup / bedtime rule — **NEEDS-CHANGE**

`_SPOT_OFFLINE` is a bool (`daemon.py:203`), surfaced as `spotify_offline` (`:2638`); the screen slot exists (`ui.py:3560-3580`, the rounded popup with an X action at `:2032-2035`) **but only when `source == "spotify"`**. A never-paired/never-keyed box has source None/mpv → nothing shows. And a tap on a Spotify tile goes `_ensure_spotify_backend` (`:745-768`, unit active → True) → spawns `player.py` → waits 30 s for `username` (`player.py:262-267`) → `exit 1` → 30 s of dead box: exactly the bedtime failure. Minimal: (i) `/status` grows a string `spotify_state: ok|offline|needs-key|needs-pair|expired|audio-unbound`, keep the bool for the PWA; (ii) `play()` fast-fails on any non-`ok` state the way it does for `no-internet` (`:930-936`), returning `{"error": "spotify-needs-key"}` — the UI already routes that class (`ui.py:2837`); (iii) the popup renders when the *tapped* source is spotify or the state is non-ok, not only when the current source is. Also the sidecar's dialect `/status` must synthesize `username` (the plan says auth_state has no username, PLAN-soloistd:244) or `play_spotify` dies at `:262-267` every time — pin it.

### 9. Backup SECRET tier — **NEEDS-CHANGE / OPEN**

`backup.py:36-70` reads paths from env, never literal names ("a literal … here would silently back up nothing"); go-librespot's `credentials.json`+`state.json` come from `VIBB_GO_CONFIG`'s dir. Add `VIBB_SOLOIST_ENV` (the key) and `VIBB_SOLOIST_DATA` (the `--data-dir`: the stored session, `ws.addr/ws.port`, `cache/Users/<id>-user`) — **exclude** anything under the cache dir (`-C`, GBs). Portability: the docs say "keep the same data directory if you want the device to stay paired" and say nothing about machine binding — treat a restored session as *probably* valid, and make `needs-pair` (item 8) the graceful failure, so a restore onto a new box degrades to one PWA pairing step, never a silent box. The restore path reboots (`daemon.py:3838-3860`), so the sidecar re-reads everything.

---

## D1 — updater

### 10. Sharing the idle-shutdown slot; poweroff mid-download; the timer — **NEEDS-CHANGE**

- `_backup_before_off` (`idle.py:186-220`) stamps `poweroff-imminent` and runs `systemctl start --wait vibb-backup.service` with `timeout=BACKUP_MAX_S=180`; the marker's freshness window is 600 s (`backup.py:720-733`). Two jobs: run **backup first** (irreplaceable), updater second, each with its own budget, sum < 600 s so the marker still holds for the second; give the updater its own unit for the same cgroup reason the backup has one (`idle.py:189-196`, `install.sh:980-1010`). 12.8 MB on a bad hotspot can exceed 120 s — cap and let the *timer* path finish it (resume with `Range:` — the CDN advertises `accept-ranges: bytes`).
- Poweroff mid-file: tmp under the binary's dir + crc32c + `--version` + `os.replace` is safe *provided* the next run deletes stale `*.tmp` first and never trusts a tmp without a stored ETag/length pair. Replacing a running binary by rename is fine (old inode stays mapped; crashpad's `/proc/self/exe` re-exec resolves to the old inode).
- **Timer:** `Persistent=` applies only to `OnCalendar=` (correct), and on an RTC-less Zero a persisted calendar timer fires **at boot on a bogus clock** — mid-boot, inside the radio storm the supervisor's 180 s `park_grace` exists for (`daemon.py:5334-5342`), and TLS to the CDN fails against a 1970 clock (the very reason go-librespot orders after `vibb-rtc`, `install.sh:596-598`). Do what the backup does: monotonic `OnBootSec=15min` + `OnUnitActiveSec=6h`, cadence in code, gated on `clock_trusted()` and `_audible_now` (`backup.py:790-805`). Since the check is one 1-byte round trip, 6 h is cheap and the "weekly" net becomes "within 6 h of the next clean-clock idle moment".

### 11. crc32c — **CONFIRMED (not stdlib) + one blocker question**

`zlib.crc32(b"123456789") == 0xCBF43926` (ISO-HDLC); CRC-32C's check value is `0xE3069283`. Verified today: a 256-entry table over reflected polynomial `0x82F63B78`, init/xorout `0xFFFFFFFF`, `crc = T[(crc ^ b) & 0xFF] ^ (crc >> 8)` per byte, yields `0xE3069283`; the header compares as `base64(crc.to_bytes(4, "big"))` (= `4waSgw==` for the check string). Pure-Python over 12.8 MB is ~10 s on a Zero 2 W — acceptable once per build, run nice-19.

**Blocker to verify on the bench:** S3 `x-amz-checksum-crc32c` on a **multipart** upload is a *composite* checksum with a `-N` suffix (checksum-of-part-checksums), and a 12.8 MB object is above the usual 8 MB multipart threshold. The owner verified the header *exists*, not that it *matches* a full-object CRC. Look at `x-amz-checksum-type` (FULL_OBJECT vs COMPOSITE) and the value's suffix. If composite: verify `content-length` + gzip/tar integrity + `--version` instead, or reconstruct the composite (part size is not advertised — do not).

### 12. "Restart the child only if idle" — **NEEDS-CHANGE (define it)**

Idle = sidecar `/status` shows no track **or** paused for ≥ 10 min, AND `_audible_now()` False, AND `_hands_on_box()` False (`backup.py:707-717`). Restarting a *paused* session is acceptable **because** the bookmarker flushes on pause (`daemon.py:4700-4712`) and `play()`'s resume shortcut falls through to a bookmark respawn when the session is empty (`:909-921` → `_spawn`); mark it with `note_go_restart()` so the ip watchdog does not double-restart (`:3105-3110`). On the poweroff path skip the restart entirely — the box is going down. After an update, if the new child fails to start N times, swap `soloist.prev` back automatically; that is the only rollback that matters.

### 13. Terms — **CONFIRMED-OK**

Fetched today: downloads-and-updates says "Do not redistribute Spotify Soloist archives or binaries directly. Link users to this page instead." and describes updating as "download the archive for the same architecture, replace the installed `soloist` executable, and restart the daemon." No text on automated/scripted downloads; the box fetching its own copy from the documented URL is the documented procedure. The API-key page prohibits *sharing keys*, not automation.

---

## Engine toggle (step 3)

### 14. `spotify-engine.sh` vs `audio-stack.sh` — **NEEDS-CHANGE (four concrete points)**

- **Resolution order:** the engine resolves at `install.sh:64-82`, *before* `audio_stack_resolve` at `:180` — and `audio_stack_resolve` **writes** `/etc/vibb/audio-stack` (`audio-stack.sh:52-53`), so "refuse soloist before anything is touched" cannot reuse it. Add a read-only `audio_stack_peek` (env > file > bluealsa, no write) to `audio-stack.sh` and call it from the engine refuse. Rollback `--librespot` must not touch `config.yml` — but note the daemon still reads it under soloist (`VIBB_GO_CONFIG` on the daemon unit, `install.sh:1233`): the supervisor's `_spotify.lock()` (`daemon.py:5313`, `spotify.py:145-153`) would read go-librespot's `zeroconf_enabled` and, if the sidecar's status reports a username, **rewrite go-librespot's config.yml and restart the soloist unit** every tick. Under soloist `VIBB_GO_CONFIG` must be unset (then `_conf_dir()` is "" and `lock()`/`logout()`/`logged_in_user()` all no-op cleanly, `spotify.py:78-96,156-163`).
- **Which units get `VIBB_GO_API`/`VIBB_GO_UNIT`:** everything that imports `vibb.spotify` or calls `go_unit_cmd` — daemon (player inherits via `Popen`, `daemon.py:817`), bt-reconnect (`vibb/bt.py`), **idle** (`idle.py:161` daemon-down fallback; its unit has no env, `install.sh:1183-1195`), **buttons** (`buttons.py:72-76` volume via `spotify.go`), **rfid** (`rfid.py:103`), mpris only if it reads status. Today `audio_stack_unit_env` is injected into daemon/go-librespot/bt-reconnect only (`install_unit_order.py` item 4); the engine env must reach the extra three or a soloist box has dead volume buttons and a blind idle fallback. Also `extra.sh` RESTORE/HANDOFF names `go-librespot` literally (`extra.sh:41,80,132`); read `/etc/vibb/spotify-engine` there like it reads `audio-stack` (`:47-50`), or `--run` will leave the soloist child alive with a destroyed stream (dont-reconnect) and never restart it.
- **Masking go-librespot** breaks `spot_supervisor.py` only if the fake's default changes — it pins argv with `VIBB_GO_UNIT` unset (`tests/spot_supervisor.py:87,111,144`), fine. But the daemon's `_ensure_spotify_backend` `systemctl start` (`:762`) and the supervisor's park/unpark (`:5307,5364`) now start/stop **the sidecar unit**, killing the sidecar's child supervision and its exit-10 latch state; the sidecar must persist the latch (a file) so a supervisor `start` after "internet is back" does not brick-loop an expired child.
- **The seam pin will break:** `spotify_engine_seam.py:56-65` asserts `hits == ["pi/install.sh"]`; `spotify-engine.sh` will contain `systemctl mask go-librespot`. Amend the pin to `{"pi/install.sh", "pi/spotify-engine.sh"}` with the same justification (they own the unit files) — nothing else.

---

## (a) NEW findings D1–D3 missed

1. **`/cache/snapshot` fail-open → perpetual re-warm** (item 3). Highest practical risk: turns D3 into a 500-track walk several times a day.
2. **Root-owned radio markers**: soloistd (`$RUN_USER`) cannot touch BUSY/PAGING; every "the sidecar yields like the sweep" sentence silently no-ops (`radio.py:44-50`, `/run` 755 root). Drive the warm from the daemon.
3. **btwatchd's 120 s starvation belt** pages through any warm longer than 2 min with the speaker absent — D3's car-trip case.
4. **`username` in the dialect**: `player.py:262-267` exits after 30 s without it; the plan records that Soloist's `auth_state` has none.
5. **Autoplay at context end** is the only thing that stops a warm; the API's `source: autoplay` field is the stop condition and appears nowhere in the plan.
6. **`VIBB_GO_CONFIG` under soloist** makes the supervisor's zeroconf lock rewrite go-librespot's config and restart the wrong unit (item 14).
7. **Supervisor park/stop of the sidecar unit** loses the exit-10 latch unless persisted.
8. **Volume shroud crash**: a sidecar dying mid-walk leaves Connect volume at 0; `_apply_box_volume` (`player.py:163-176,266`) re-applies on the next tap, so it heals — but the sidecar must also restore on its own start.
9. **The `-k` secret and `hidepid`**: no `/proc` hardening on the box today; without it D2's wording is wrong (item 7).
10. **Updater needs `clock_trusted()`**: TLS to the CDN against the RTC-less boot clock fails exactly like the go-librespot AP case (`install.sh:596`).
11. **Warm eligibility while paused** costs the kid a resume walk instead of an unpause (item 1).
12. **`spotify-engine` file is written before the audio stack is known** (`install.sh:181`) — a `--soloist` run that later fails in `audio_stack_apply` leaves `spotify-engine=soloist` remembered with go-librespot still serving.

## (b) Pins before each step lands (`tests/run_all.py` style, one file each)

**Step 3 (`spotify-engine.sh`)**
- `spotify_engine_toggle.py` — against a fake systemctl + `VIBB_FS_ROOT`: soloist refused when `audio-stack` ≠ pipewire *without writing any file*; `vibb-soloistd.service` written as `$RUN_USER` with `EnvironmentFile=-/etc/vibb/soloist.env`, `Restart=on-failure` never `always`; go-librespot masked never removed; `--librespot` unmasks, removes the engine env, leaves `config.yml` byte-identical; `VIBB_GO_CONFIG` absent from every unit under soloist.
- `install_unit_order.py` (extend) — `VIBB_GO_API`/`VIBB_GO_UNIT` present on daemon, bt-reconnect, idle, buttons, rfid; the engine file is written *after* `audio_stack_apply` succeeds.
- `spotify_engine_seam.py` (amend) — the literal-unit allowlist is exactly `{install.sh, spotify-engine.sh}`; `extra.sh` derives the engine unit from `/etc/vibb/spotify-engine`.

**Step 4 (`soloistd.py`)**
- `soloist_contract.py` — dialect parity: `/status` carries `username`, `track`, `paused`, `stopped`, `play_origin == "go-librespot"` for box plays; `/player/play {uri, skip_to_uri, position}` lands at the exact item/position via the walk; `/cache/download` idempotent from the ledger; `/cache/snapshot` answers or the ledger gates.
- `soloist_warm_invisible.py` — during a warm `/status` equals the pre-warm snapshot; `bookmark_step` returns None for the whole walk; the arbiter never yields mpv.
- `soloist_warm_abort.py` — any dialect command mid-walk aborts, restores volume, then executes; ledger records the index; walk stops on `source: autoplay`/empty upcoming; ≤ N items per `cache: N`; ≥ 3 s pacing.
- `soloist_exit10_latch.py` — exit 10 persists across a unit stop/start; `/soloist/health` reports `expired`, `needs-key`, `needs-pair`, `offline`, `audio-unbound` distinctly; `play()` fast-fails on each.
- `sweep_warm_gate.py` — the sweeper holds the warm on `_audible_now() and _streaming_now()`, touches BUSY every ≤ 10 s while it runs, and posts abort when audible flips.

**Step 5 (pairing / updater / key)**
- `soloist_configure.py` — token + JSON-CT gate, 0600 `soloist.env`, previous file restored on `bad-key`, `needs-key` on removal, key never in the daemon log.
- `soloist_updater.py` — fake CDN: 304 → no-op; 200 with bad crc32c → tmp removed, binary untouched; good → `--version` runs, `os.replace`, `.prev` kept, ETag stored; stale `*.tmp` cleaned; refuses when `clock_trusted()` False or `_audible_now()`; composite `-N` checksum detected and refused/fallback.
- `crc32c.py` — check value `0xE3069283`, base64 form, and a 1 MB timing sanity.
- `idle_shutdown.py` (extend) — backup then updater, each bounded, poweroff regardless; the warm hold releases at its budget.

## (c) Go/no-go and slice order

**Go for step 3 now; hold step 4's warm and step 5's updater until two bench facts land.** `spotify-engine.sh` is mechanical, fully testable against the fake systemctl, and its four defects above are all install-time wiring the owner can fix from this review. `soloistd.py`'s core (child supervision with the persisted exit-10 latch, WS client, dialect `/status`+`/player/*`, the resume walk with shroud, the §I binding and AM-16 bind check) can be written now against the fake WS server — but **do not code warming until the bench answers** (i) does a skip cancel the in-flight fetch and what a truncated file does on replay (item 5), and (ii) is the CDN's `x-amz-checksum-crc32c` full-object or composite (item 11). Order: **3a** `audio_stack_peek` + engine refuse + engine-file-after-apply → **3b** `spotify-engine.sh` + `vibb-soloistd.service` + env on all five units + `extra.sh` → **3c** seam/order pins → **4a** sidecar skeleton: supervisor, latch, health states, `username` in `/status` → **4b** dialect play/controls + walk → **4c** binding + bind check (B9) → **4d** warm as an idempotent `/cache/download` driven by the daemon's sweeper, invisible in `/status`, autoplay stop, `cache: N` bound → **5a** `/soloist/configure` + `needs-key` in `/status`/UI + play fast-fail → **5b** `--pair` oneshot → **5c** updater unit + monotonic timer + the idle hook's second slot.
