# PLAN: boot lands PAUSED; BT-reconnect mid-story still auto-resumes

> **IMPLEMENTED 2026-08-20** — and further than planned: instead of the
> two surgical deletions, `_boot_resume` was deleted WHOLE (function +
> thread), owner-approved. Post-plan findings that justified it: the
> landing survives because `ORCH.target` is restored from LAST_FILE in
> `__init__`, not by play(); the verdict has its own boot thread; change
> #2 removed the last reader of `was_playing`, making the flag
> write-only (still written — honest shutdown record, TERM-race contract
> pinned by spot_boot_flag.py); and btwatchd's own BOOT state pages the
> speaker at boot regardless, so dropping `_kick_bt_connect` costs
> nothing (see the section added below). play()'s `boot=True` guard is
> KEPT with no caller, as the documented contract for any future boot
> starter. Tests rewritten: boot_resume_guard (second half),
> session_stamp #6, resume_overlap #7, session_window #7. Suite green,
> 148 files. AWAITS the field test in Verify below. Part 2 (the Sonos
> hiccup) is NOT built. Part 3 resolved as not-a-bug.

Owner request (2026-08-18): the box must NOT start audio on its own after a
reboot, regardless of output — it should land on the now-playing screen for
what was in progress, PAUSED, so one tap continues. BUT when the box repairs
a BROKEN BT CONNECTION mid-session it should resume playing immediately.

Both reviewed by QA (two passes) + verified independently in code. The two
behaviours are ALREADY served by two independent paths; this change only
removes the boot auto-play and leaves the mid-session reconnect path
untouched. Rated low-risk, but it is the box's most sensitive code — do it
with a clear head, run the full suite, and field-test.

## Why it's safe (the load-bearing finding)

Three separate things can start playback (`daemon.py:812-814` comment):
1. `_boot_resume` — runs once at daemon start (the reboot case)
2. A-press / tap replay — `play()`
3. transport-up "blip" resume — BT speaker returned mid-session

Case 2 (owner's second rule) is path 3: `_bt_transport_lost` (POST /bt/lost
from btwatchd, `daemon.py:4712`) arms `_BT_WAIT["lost"]`; `_bt_wait_watcher`
(`:4924`, spawned `:5694`) → `_bt_wait_advance` (`:4876`) → `_speaker_back`
(`:4860`) → `_bt_blip_resume` (`:4683`) respawns on reconnect, no tap. This
chain is NOT owned or armed by `_boot_resume`, so removing boot auto-play
does not touch it.

Self-consistency (no special-casing needed): at boot the box is paused, so
`_bt_transport_lost` sees `_mpv_alive()` False and `spotify_playing()` False
and returns WITHOUT arming `lost` (`daemon.py:4757-4773`). A reconnect before
the first tap therefore does nothing — the kid must tap first. Only after a
tap starts playback does a later drop arm auto-resume. Exactly the intent.

The paused-and-shown state ALREADY EXISTS — B is a deletion, not new state:
- ghost-session card in `status()`: spotify `daemon.py:2717-2736`, mpv
  `2737-2776` — presents a bookmarked target as paused-at-position,
  `out["playing"]=False` when nothing spawned. (2026-08-10 field fix.)
- `ui.py:_boot_landing()` `3778-3819` — lands on now-playing for a live
  session OR a bookmarked-paused ghost; expired → carousel. Gated on
  `status.title` + `session != "expired"`, both satisfied without playback.

## Changes (Option D — two deletions)

### 1. `pi/daemon.py` — `_boot_resume` (~5269-5357)
Remove the PLAY TAIL only: the grace loop, the `_kick_bt_connect` at
`:5318`, the spotify/wifi waits, the blip-claim, and the final
`ORCH.play(target, reverse=..., resume=True, boot=True)` (`~5356-5357`).
Roughly lines `5301-5357`.
KEEP: the function itself and its thread (pinned by
`tests/session_window.py`), and all early guards
(`is_sonos` early-return, `resume_window_h==0`, the `LAST_FILE` read, the
`was_playing` one-shot consume + rewrite at `:5287`, the `session_verdict`
expired check). The function should return after consuming `was_playing`
and judging the verdict — it just no longer spawns audio.

### 2. `pi/daemon.py` — sonos reconcile `else` branch (~4188-4216)
Delete `4201-4216`: the `if load_settings().get("resume_window_h") != 0 …
ORCH.play(ORCH.target, resume=True, boot=True)` block that moves a prior
Sonos session onto the box on reboot.
KEEP `4193-4200` — these MUST still run every boot:
`_renderer.write("box")`, `content.PREFER_REMOTE = False`,
`_library._EXPAND_CACHE.clear()`, and the `ORCH.source` reset off "sonos".

### Why dropping `_kick_bt_connect` at :5318 costs nothing (verified 2026-08-20)

btwatchd already pages the speaker at boot on its own: `enter_boot()` /
`_boot_tick()` (`btwatchd.py:357-370`) run a BOOT state with
`BOOT_WINDOW_S = 120` and up to `BOOT_FAIL_LIMIT = 4` attempts, holding
while wifi is still associating (`_radio_yield`). That path is entirely
independent of `_boot_resume`, so a box with bt output still connects at
boot after this change.

What `_kick_bt_connect` adds on the boot path (`daemon.py:4967-4984`) is
(a) the bt_waiting POPUP via `_BT_WAIT["since"]`, and (b) a KICK_FILE
write that bypasses btwatchd's blind-retry backoff. Both are right to
lose here: the popup asks "connect, or play on the box speaker?" which is
meaningless when nothing is trying to play, and the backoff bypass is
already issued at the moment sound is actually wanted — `play()` calls
`_kick_bt_connect()` at `daemon.py:831` on the first tap.

### Do NOT touch
- `_kick_bt_connect` inside `play()` (`daemon.py:831`) — real taps still
  wake the BT speaker.
- `_bt_transport_lost` and the whole `lost`/`_bt_wait_watcher`/
  `_bt_blip_resume` chain — that IS the owner's rule 2.
- The adopt branch (`4114-4186`, live Sonos session at boot re-attaches) —
  not the complaint, and it starts no box audio.

## Tests

Break — rewrite from "boot plays" to "boot does NOT play, does NOT kick":
- `tests/boot_resume_guard.py` tests 5-9 (assert `PLAYED == [...]` and
  `KICKED`). Tests 1-4 (the `play(boot=True)` race guard) are UNAFFECTED —
  `play()` is untouched. This file's second half is the bulk of the churn.
- `tests/session_stamp.py` point 6 (~101-109) — regex-pins
  `ORCH.play(…resume=True, boot=True)` in `_boot_resume`. Update/remove.
- `tests/resume_overlap.py` point 7 (~107-110) —
  `DSRC.index("ORCH.play(target, reverse=bool(last.get(\"reverse\")),")`
  throws ValueError once the call is gone. Update/remove.

Unaffected (verified) — leave alone, they guard the invariants we keep:
- `tests/bt_lost_pause_recover.py` — the mid-session drop→pause→auto-resume
  contract (rule 2). Does NOT reference `_boot_resume`. This is the
  regression guard proving rule 2 still works after boot lands paused.
- `tests/session_window.py` (only needs the thread to exist — kept),
  `tests/ui_session_landing.py` (already tests the paused-ghost landing —
  validates this change), `tests/sonos_renderer.py`,
  `tests/sonos_contract.py`, `tests/spotify_resume.py`,
  `tests/episode_resume.py`, `tests/output_switch_resume.py`.

Add one new pin (recommended): boot with `was_playing=True` leaves
`ORCH` NOT playing and lands on the paused now-playing target — the positive
assertion that boot is silent but remembered.

## Verify

`python3 tests/run_all.py` green (148 files today). In field:
1. Play something on the box speaker, power off, power on → lands on
   now-playing PAUSED, one tap continues from the right second.
2. Same after a Sonos session → box does NOT start the built-in speaker
   (the specific complaint); lands paused on the box.
3. Play on the box speaker over BT, pull the speaker's power briefly so BT
   drops, restore it → audio auto-continues on reconnect, no tap (rule 2
   intact).

## Rejected
- A (`resume_window_h = 0`): also kills the paused-at-position landing —
  wakes in the carousel, the "box forgot" regression. Rule 2 would still
  work (independent path) but rule 1's quality is lost.
- C (leave as-is): fails rule 1.

---

# PART 2: carousel play on the already-playing tile hiccups on Sonos

> **IMPLEMENTED 2026-08-20.** The guard lives in play(), before the
> is_sonos early return — NOT in sonos_start_target, which lacks `fresh`
> in its signature (the condition the plan below forgot; both local
> shortcuts gate on it). All three traps below are covered and pinned by
> tests/sonos_same_tile.py: playing -> no-op, paused -> one /resume with
> the optimistic flip, untrusted map / broken queue mapping / press in
> flight (8s pending+opt_tr settle window) / stale snap / fresh /
> explicit episode all fall through to the full transfer. Suite green,
> 149 files. AWAITS the field verify below.

Owner (2026-08-18): pressing play/A in the carousel on the tile that is
ALREADY playing over Sonos gives a small audible hiccup. Same root class as
Part 1 (playback-start), same "do it with a clear head" caution — Sonos code
is field-hardened.

## Root cause (confirmed in code)

`ORCH.play()` routes Sonos through an UNCONDITIONAL early return
(`daemon.py:797`):
```
if _renderer.is_sonos() and not boot:
    _radio.touch_busy()
    return self.sonos_start_target(target, episode=episode)
```
`sonos_start_target` (`daemon.py:1395`) always re-expands the queue, re-reads
the bookmark, and re-pushes the episode to the speaker. For Storytel that
re-mints a signed URL and re-pushes the DIDL — the hiccup.

The LOCAL paths already avoid this: `play()` has an "already loaded → unpause,
don't restart" shortcut for mpv (`~832`) and Spotify (`~849`). The Sonos path
has NO such guard. And `ui.py:handle_carousel`'s own comment states the
intent — "A never restarts anything" — so the Sonos path is simply missing a
guard that was always meant to be there. This is a real defect, not
unavoidable behaviour.

## Fix shape (NOT yet built)

Add a same-target-already-playing guard for Sonos, mirroring the local
shortcut. Before the `is_sonos()` early return (or at the top of
`sonos_start_target`): if `target == self.target`, `self.source == "sonos"`,
`episode is None` (an explicit episode pick must still seek), and the speaker
is playing OUR live session of it — from `self.sonos_snap`: `ours` is True,
`transport == "PLAYING"`, and the snap is fresh — then it's a no-op: return
without touching the speaker (the UI just opens now-playing).

State to read: `self.sonos_snap` (`{"ours","transport","stale_s",...}`, set
in `_sonos_play_entry` ~1208 and refreshed by the poller). Freshness: the
poller already treats `transport == "PLAYING"` with `age < 60` as live
(`daemon.py:1039`); reuse that notion, don't invent a new one.

## The trap — why this needs a clear head, not now

- **Map drift / heal.** When `sonos_map_trusted` is False, a press is meant
  to RE-SYNC (re-transfer). A naive "already playing → no-op" must NOT
  swallow that heal. Only skip when the session is genuinely ours-and-live
  AND the map is trusted (spotify) / the url session is intact.
- **Paused vs playing.** If the same tile is PAUSED on the speaker, A should
  resume it (like the local unpause), not restart and not no-op. Decide both
  branches explicitly.
- **Optimistic holds.** `sonos_pending` / `sonos_opt_tr` mean a jump was JUST
  issued; a guard reading a stale snap mid-jump could wrongly skip a real
  press. Check these too, or scope the guard to "no press in flight".

## Tests

- Add: play on Sonos, then A on the SAME target while it's playing → no
  second `_renderer.post("/queue_play"...)` / no `_sonos_body` re-mint, and
  no DIDL re-push. Assert the speaker is not re-commanded.
- Add: same tile PAUSED on the speaker → A resumes (one command), does not
  restart from the bookmark.
- Add: `sonos_map_trusted=False` → A still re-transfers (the heal path is
  not swallowed).
- Check `tests/sonos_renderer.py` / `sonos_contract.py` for existing
  press-idempotence pins before writing new ones.

## Verify in field
Play a Storytel book on Sonos, browse to carousel, press play on the same
tile → no hiccup, lands on now-playing. Then pause on the speaker, press play
on the tile → resumes cleanly. Then a real target-switch still starts the new
book (guard didn't over-match).

---

# PART 3: a long tile name never scrolls all the way across

Owner (2026-08-20): "in the carousel at least — if the tile name is too
long, the whole name doesn't scroll across the screen." Note the hedge:
they suspect it isn't only the carousel.

## STATUS: root cause NOT established — do not fix yet

Architect + QA ran on 2026-08-20 and DISAGREED. QA refuted the leading
hypothesis with arithmetic. The architect's plan was built on that
refuted premise and is discarded. What follows is what survived.

### Refuted: "the 20-char window is a character budget on a pixel screen"

The theory was that `_cover_tile` (`ui.py:3588`) calls `marquee(name, 20)`
with no pixel measurement — unlike `render_now`, which gates on
`d.textlength(title, font=F_MED) > W - 44` (`ui.py:3194`) — so a wide
20-char name is drawn centered (`anchor="ma"`) and clipped at both screen
edges, hiding head and tail. Two independent checks killed it:

- The slide DOES reach both ends. `span = len+n-maxlen`,
  `period = span+8`, `off = max(0, min(span, step-4))` — at `off=span` the
  window is `text[-maxlen:]`. Ran it for a 31-char name: both `text[:20]`
  and `text[-20:]` appear. `tests/ui_marquee.py:43` already pins this.
- 20 chars of DejaVuSans at `font(17)` is ~178px (mixed case) to ~232px
  (all caps) on a 240px panel. It does not clip. UNVERIFIED — no DejaVu
  in the dev container; measured from published metrics.

The real mismatch is the INVERSE and is cosmetic, not the reported bug:
the full-width tile gets `maxlen=20` while the NARROWER list rows get 24
(`draw_list`, `ui.py:1276`; `17 if art else 24` at `2988/2995/3004`). The
tile could carry ~25 mixed-case chars. It slides names that would have fit.

### Confirmed defect (real, but destroys the START, not the end)

The emoji surcharge `n` inflates `span` but is NOT applied to the raw
slice indices `text[off:off+maxlen]` (`ui.py:1265-1273`). When
`len(text) <= maxlen < len(text)+n`, `off=0` already holds the WHOLE
name, so the animation eats leading characters and reveals nothing new,
then rests on the mutilated version. Reproduced (18 chars, n=4): steps
0-4 show all 18, step 5 drops the first char, steps 7-9 rest at
`text[2:]`. Fix is small, but it is NOT what the owner reported unless
the failing name has emoji.

### FIELD READING (owner, 2026-08-20) — the candidates below are settled

Failing title: **`Jakten på jungelens dronning`** — 28 chars, no emoji.
Symptom: "stops after scrolling a few letters, then starts over at the
beginning."

Traced through `marquee(name, 20)` exactly: `span = 28-20 = 8`,
`period = 16`, full cycle **5.6s**. Steps 0-4 rest on
`'Jakten på jungelens '`; steps 5-12 slide ONE character each; steps
12-15 rest on `'å jungelens dronning'`; then it wraps. The label travels
**8 characters in 2.8s** and snaps back. That IS the reported symptom —
designed behaviour, not a fault.

This settles the candidate list:
- **Candidate 1 (`NOW_RETURN_S = 10`) — DEAD for this title.** The full
  cycle is 5.6s, well inside 10s, and the owner SEES the wrap-back,
  which is impossible if the view were yanked away.
- **Candidate 3 (`screen_timeout_s`) — DEAD.** Same reasoning.
- **The emoji surcharge defect — not this bug.** No emoji in the title.
  Still a real defect (see above); fix it separately or not at all.
- **The 20-char budget — THIS IS IT.** `'Jakten på jungelens '` is 20
  mostly-lowercase chars ≈ 180px on a 240px panel. The tile is
  throwing away ~60px of screen, which is what forces a 28-char title
  to scroll at all, and forces the crop to land mid-word
  (`'akten på jungelens d'`). A full-width tile has the SMALLEST budget
  in the UI — the narrower list rows get 24 (`ui.py:1276`).

### RESOLVED 2026-08-20: not a UI bug — the library entry name is 28 chars

Final field reading: the label jumps back to the start right after
`Jakten på`, with the screen still LIT and nothing playing. That is
decisive, because of one property of `marquee`: `off` maxes at
`span = len+n-maxlen`, so the resting window is `text[len-maxlen:]` —
**the last `maxlen` characters**. The final frame before a wrap therefore
always ends on the string's last character. Ending on `Jakten på` means
the string ENDS there.

The book is `Detektivbyrå nr.2: Jakten på jungelens dronning` (47 chars),
but the library entry is named `Detektivbyrå nr.2: Jakten på` — **28
chars**. `span = 28-20 = 8`, so it slides exactly 8 characters and wraps:
precisely "scrolls a few letters, then starts over", the owner's words
from the first report.

Nothing truncates it — it is a SERIES tile, and that IS the series name
on Storytel's side. Confirmed by the owner:
`storytel.com/no/series/detektivbyrå-nr-2-jakten-på-139545`. The tile
takes `si.get("name") or model.get("title")` (`storytel.py:469`), which
correctly prefers the series name for a series group; the individual book
title (`Jakten på jungelens dronning`) lives on the book inside it.

So the code is right at every layer: the PWA's entry-name input has no
`maxlength` (`pi/web/index.html:91` — the `maxlength="32"` at `:306` is
the wifi SSID field), `library.py:73` stores `ename` raw, and the shelf
endpoint (`daemon.py:3510`) is read-only, so renaming the entry in the
PWA sticks across syncs. **NOTHING TO FIX. Rename the entry if the
series name reads badly on a tile.**

Everything else in this section was chasing a bug that wasn't there.
Three hypotheses were raised and all three are dead:
- pixel-vs-character clipping — refuted by arithmetic and by the fact
  that the head of the name IS visible
- `NOW_RETURN_S` snap-back — requires `status.playing`; nothing was
  playing
- repaint starvation / screen sleep — the screen stayed lit

### STILL LATENT (worth fixing on its own merits)

With the FULL 47-char name the cycle would be `(27+8)*0.35 = 12.25s` and
the tail would first appear at `(27+4)*0.35 = 10.85s` — while
`NOW_RETURN_S = 10` (`ui.py:607`) snaps the view to now-playing at 10.15s
whenever something IS playing (`ui.py:3969-3974`, list includes
`"carousel"`). So the moment this entry is renamed to its real title, a
genuine bug appears: the end of the name becomes unreachable during
playback, by 0.7s.

Cheapest correct fix: stand the `NOW_RETURN_S` check down while
`marquee_active` is true — the box already tracks that flag
(`ui.py:3050`) and uses it to drive the repaint gate. Don't yank the
screen away mid-name. Raising `NOW_RETURN_S` only moves the threshold to
longer titles; a wider label budget alone gives 9.1s vs 10s, too close.

Also still open, independent of all the above: the emoji-surcharge
off-by-N in `marquee` (`ui.py:1265-1273`), which eats leading characters
and reveals nothing for names that overflow only because of the charge.

### SUPERSEDED — the 47-char reasoning, kept for the record

The first reading gave a partial name. The full tile name is
**`Detektivbyrå nr.2: Jakten på jungelens dronning`** — **47 characters**.
Everything below that reasons from 28 chars is wrong, including the
"candidate 1 is DEAD" call. Recomputed:

- `span = 47-20 = 27`, `period = 35`, full cycle **12.25s**
- the tail (`off=27`, `'å jungelens dronning'`) first appears at
  `(27+4) * 0.35 = ` **10.85s**
- `NOW_RETURN_S = 10` (`ui.py:607`) fires at **10.15s**, snapping the
  view to now-playing (`ui.py:3969-3974`, list includes `"carousel"`)

**The tail is unreachable by 0.7s whenever something is playing.** The
owner's "Jakten på is the last thing I see" matches: at 8.05s the window
is `'Jakten på jungelens '`, and the last ~2s before the yank slide it
off to the left. QA called this candidate at a ~45-char threshold; it was
wrongly dismissed on the truncated 28-char title.

Confirm by repeating with NOTHING playing: the snap-back is gated on
`self.status.get("playing")`, so the cycle should complete in 12.25s and
the tail should appear for 1.4s. If it does, this is settled and the
pixel/clipping theory is dead for good.

Fix candidates (decide after the confirmation):
- suppress the `NOW_RETURN_S` snap-back while `marquee_active` — the
  view must not be yanked mid-name. Cheapest and most targeted.
- raise `NOW_RETURN_S`, or make it "10s AND no marquee in flight".
- shorten the cycle so it fits inside 10s regardless: a wider label
  budget cuts `span` (a 47-char title at a pixel-correct ~25-char window
  needs `(22+4)*0.35 = 9.1s` — still uncomfortably close).
Note the first two are the real fix; the budget change alone only moves
the threshold to longer titles.

### SUPERSEDED — reasoning from the truncated 28-char title

Asked the owner whether `dronning` becomes readable at the end of the
slide. Answer: **no, the end never appears.**

That contradicts the arithmetic, which is not in doubt: the window rests
on `'å jungelens dronning'` for 4 steps (1.4s) before wrapping. If that
window is drawn and the word still isn't readable, the window is being
CLIPPED BY THE PANEL — i.e. the "refuted" pixel hypothesis is alive after
all, and the char-vs-pixel budget is a genuine defect, not cosmetics.

Ruled out as explanations of the contradiction:
- repaint starving the last steps — the gate is
  `marquee_active and now - last_render >= MARQUEE_STEP_S`
  (`ui.py:3991-3994`), ~0.35s, and ~3 renders land on the final rest.
- a `RichDraw` centering bug — a title with no emoji delegates straight
  to Pillow's own `anchor="ma"` (`ui.py:419-421`); the custom centering
  math at `ui.py:430-433` is never reached.
- `NOW_RETURN_S` / screen sleep — both far longer than the 5.6s cycle.

THE DECISIVE MEASUREMENT (run ON THE BOX, where DejaVu actually exists;
it touches nothing and does not disturb the running UI):

```
python3 -c "
from PIL import ImageFont
f = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 17)
for s in ['Jakten på jungelens ', 'å jungelens dronning',
          'Jakten på jungelens dronning']:
    print(round(f.getlength(s)), 'px  |', s)
"
```

Panel is 240px, label centered at `x=W//2`. If `'å jungelens dronning'`
measures **> 240** the tail is clipped: the pixel hypothesis is CONFIRMED
and the fix is a pixel-measured budget. If it measures **< 240** the word
IS on screen and the report is about legibility/pace, not clipping — then
mock up the font-shrink or two-line options below instead.

Note `font()` (`ui.py:623-631`) falls back to `load_default(size)` when
DejaVu is missing. Confirm the truetype load actually succeeds on the box
— a fallback bitmap font would change every width in this analysis.

### Fix direction (pending the answer above)

The title is only ~10-15px too wide for the panel (UNVERIFIED — no
DejaVu here). So widening the budget to a pixel-correct value does NOT
remove the scroll, it shrinks `span` to ~3 and makes the nudge look
MORE broken, not less. The window-slide model reads as a fault for
titles that barely overflow. Options, cheapest first — MOCKUP BEFORE
CODING (render with ui.py's own constants):
- **Shrink the label font for overflowing tile names** so the whole name
  fits and nothing animates. ~6% off F_MED(17) buys ~15px.
- **Wrap the tile label to two lines** like `render_now` does via
  `wrap_two`. Check the space between the label at y=206 and the
  playing-underline at y=228 — it may not be there.
- **Widen to a pixel-correct budget** and accept a small nudge.
- **A true ticker** (sweep the whole title past, off one edge and back)
  — biggest change, and it fights the "rest at the start" anchor added
  2026-08-12.

### Superseded candidates (kept for the record)

1. **`NOW_RETURN_S = 10` yanks the view away** (`ui.py:607`, applied at
   `ui.py:3969-3974` to `"home"/"entries"/"episodes"/"carousel"/"cats"`
   whenever `status.playing`). The tail first appears at
   `(span+4)*0.35`s, which passes 10s at ~45 characters. Browse with
   music playing, land on a long tile, and now-playing snaps in before
   the name finishes. Carousel-specific in effect — matches the hedge.
2. **The pace is slower than a kid.** 4 resting steps = 1.4s before the
   name moves at all, then 0.35s/char. A 40-char tile needs ~8.4s to
   show its tail, and every B/Y flip re-anchors `_marquee_t0("tile",
   name)` (`ui.py:3589`) back to step 0. No defect; fully reproduces the
   words.
3. **`screen_timeout_s = 15`** (user-settable, `ui.py:2728`; default 30
   at `ui.py:1367`) blanks the panel at ~46 chars. Only if the box is
   set to 15.
4. **`render_now`'s own gate/threshold mismatch** (`ui.py:3194`, `3209`):
   gates on pixels, then delegates to a 20-CHAR threshold, so an 18-wide-
   char title passes the gate and returns `scrolling=False`, drawn wider
   than the `W-44` the layout budgeted, under the side markers. Real, but
   not the carousel.

Falsified and not worth re-testing: the marquee not animating at all
(`rolls` → `marquee_active` `ui.py:3050` → repaint gate `ui.py:3991`
fires every ~0.4s; ~1 step in 8 is skipped, cosmetic only);
`_mq_key`/`_mq_t0` thrash (all 12 `_marquee_t0` sites are in mutually
exclusive `if/elif` branches, one key per frame); `_slide`'s
`marquee(label, 20, t0=time.monotonic())` (`ui.py:3615`) poisoning the
phase (it bypasses the anchor; the landing render re-anchors).

## The discriminating field test — do this BEFORE writing code

Land on the offending tile and DO NOT TOUCH THE BOX for 30 seconds.
Record which happens, plus the exact name, its character count, and
whether it contains emoji:

1. screen switches to the now-playing layout → candidate 1 confirmed
2. screen goes black → candidate 3 (`screen_timeout_s` is 15)
3. tile stays and the name completes its slide → no timing defect;
   it is the pace (candidate 2) plus the 20-char budget
4. name is ≤20 chars, has emoji, and SHRINKS FROM THE FRONT → the
   confirmed surcharge defect above

Repeat with nothing playing to separate 1 from 3 definitively.

One 30-second hands-off trial separates every remaining candidate. Until
that reading exists there is nothing to implement here.

---

# PART 4: Sonos group-awareness

Owner (2026-08-21): the picker should show and select speaker GROUPS,
refresh the list when the output picker opens, and the box should not
have to manage grouping — the Sonos app already does that well.

## Stage A — IMPLEMENTED 2026-08-21 (display + selection + fresh list)

The cheap primitive, already named by the RF audit at `sonosd.py`
(2026-08-10 #2): **GetZoneGroupState against any one cached ip returns
the whole household in one ~200ms call** — every zone (uid, name, ip),
every group, each group's coordinator. SSDP (3s+ multicast) degrades to
cold-start fallback.

- `sonosd.refresh_topology()`: merges zones (heals DHCP moves and
  renames for free), REPLACES the group map wholesale (a snapshot),
  filters bonded invisibles (stereo pairs / subs are not rooms),
  coordinator first in each member list. Raises when nobody answers;
  `/players?fresh=1` then serves the cache with `stale: true` — the
  cabin case shows the truth, not home's ghosts.
- daemon `/sonos?fresh=1` passes it through (timeout 6).
- ui: hold-X still gates the Sonos row on the CACHE (instant, per owner
  2026-08-09) and kicks the fresh fetch in the background, so the
  speaker submenu is current by the time a finger gets there. The
  submenu's old background SSDP became topology-first, SSDP only on
  the stale marker. `_sonos_choices()` is the ONE source for both
  display and selection: a multi-member group is one row
  ("Kjøkken + Stua", coordinator first), selecting it stores the
  COORDINATOR's uid — transport verbs on a coordinator drive the whole
  group, so everything downstream is unchanged. Absorbed members do
  not repeat; a group naming an unknown uid is skipped whole.
- Pinned by tests/sonos_groups.py.

Answers to the owner's two questions, as built: (1) yes — the list
refreshes when the output picker opens, but async behind the instant
cache read, so hold-X never waits on the network; (2) the Sonos row
does NOT disappear off-LAN, before or after the probe, ON PURPOSE:
hiding it up front would need a blocking probe in hold-X, and removing
a row from an OPEN menu moves the remaining rows under the finger that
was about to press one — the classic mid-menu trap. The cache's merge
semantics also never delete a speaker (a scan that misses one over
multicast drops must not delete the row the kid was aiming at). What
the stale marker does instead: the speaker submenu's hint line flips
from "A: play here" to "No speakers answered here" (owner follow-up
2026-08-21), and a press on a ghost fails cleanly as before. The row
heals itself the moment the box is back on a network where someone
answers.

## Stage B — reviewed 2026-08-23, IMPLEMENTED 2026-08-24

> Both halves built as planned below, suite green (153 files).
> B1: instant x-rincon detection, grouped-away unshadowed, position
> ours-gated, the three verb leaks closed (tests/sonos_grouped.py, the
> reclaim pin in sonos_same_tile #7, STATE_GROUPED). B2: the probe
> state machine in the sidecar + the daemon act block
> (ORCH._sonos_stream_moved — adopt FIRST, identity second, so a failed
> adopt changes nothing and retries free), STATE_MOVED,
> tests/sonos_migrate_probe.py + sonos_migrate.py. One deviation from
> the plan: the daemon act lives in a named ORCH method rather than
> inline in the poller, for testability. AWAITS the field checklist
> below — run it with the PHYSICAL buttons.

The original sketch ("follow the coordinator for verbs/poll/play, plus
group volume") was put through an adversarial round and is DEAD in that
form. The owner then supplied the real use case, and a second focused
round produced the design below. Three scenarios, three different
answers — the sketch's mistake was conflating them.

The owner's field reality (2026-08-23): the son starts audio on his
speaker via the box, then GROUPS BY HARDWARE — press-and-hold play on
other Sonos speakers joins them to the playing group, no app involved.
He also removes speakers again mid-play, sometimes including the
ORIGINAL one, so a different speaker than the one that started carries
the stream. The box must keep up. Hardware-join means topology can
change quickly and repeatedly: flap resistance is mandatory, and the
field tests below must be run with the physical buttons.

### Scenario map (settled)

| | What happens | Verdict |
|---|---|---|
| (A) rooms ADDED to our session | our speaker stays coordinator; verbs on a coordinator drive the group | WORKS TODAY, nothing to build (code-verified both rounds) |
| (B) our speaker PULLED into someone else's group | our audio is replaced by theirs; `ours` flips False next poll | KEEP today's behavior: bookmark freezes (ours-gates daemon.py ~1129/~4366), any play-shaped press reclaims OUR speaker only — QA verified this is already the ideal outcome, zero blast radius |
| (C) our COORDINATOR removed, stream promoted to another speaker | our uid goes standalone STOPPED while the son's audio continues elsewhere | THE FEATURE: migration-follow (design below) |

Rejected with evidence, do not re-litigate:
- **Broad follow-the-coordinator**: in (B) it turns the kid's four
  buttons into a house-wide remote for the parent's session, and a
  coordinator-derived `ours` would open the ONE bookmark-poisoning path
  today's double gate makes impossible.
- **Group volume (SetGroupVolume)**: the sonos path deliberately has no
  volume cap (owner 2026-08-09) — group volume makes a Y-mash a
  house-wide blast; readback is a membership-weighted average that
  fights the never-expiring `_sonos_vol_opt` hold; and it silently
  repurposes the frozen `volume` contract field. Per-room stays,
  in every grouping state. After migration the knob drives the room
  actually playing — which is the right room.

### B1 — hygiene (small, low-risk, do first)

1. **Instant grouped-away detection** in `_classify` (sonosd.py, between
   the `ours` computation and the aux block): a member's TrackURI is
   `x-rincon:<coordinator-uid>` — set `grouped_away` + `coordinator`
   from it on the SAME poll (today the aux lags up to ~3 min at cruise,
   ~12 min stopped). Clear only on a non-empty non-rincon uri (`elif
   track_uri:` — empty uris must not clear; transitions settle via aux).
   `startswith("x-rincon:")` is safe against `x-rincon-mp3radio://` and
   `x-rincon-queue:` (char 8). No wire change — both fields exist.
2. **Unshadow `renderer_state: "grouped-away"`** in status()
   (daemon.py ~2533): today `foreign_uri` (always set in (B), it holds
   the x-rincon string) wins first, so the grouped-away state is
   UNREACHABLE. Reorder: grouped_away, then taken-over, then
   lost-session. Existing pins keep passing (their snapshots have
   grouped_away False).
3. **Gate the status card's `position` on `ours`** (daemon.py ~2531):
   while grouped-away the member reports junk RelTime and the card
   extrapolates it under the kid's book title. Never persisted (the
   bookmark is ours-gated) — but stop painting it.
4. **Guard the three unguarded verb posts** on `ours` (raw snap, not
   _sonos_fresh — a stale ours-False must still suppress):
   `pause()` (~2061), `unpause()` (~2101), `_sonos_on_term` (~5248).
   Today they post /pause, /resume, /stop at the member regardless;
   most firmware refuses (UPnP 701 → 502), but a firmware that
   forwards them lets a card removal or an install-restart pause/stop
   the PARENT'S whole group. `_sonos_command`'s playpause needs no
   change — its ours-check already routes to the reclaim.
5. **Contract fixture**: additive `STATE_GROUPED` canonical example in
   tests/sonos_contract.py (ours False, uri/foreign_uri x-rincon,
   grouped_away True, coordinator set).

### B2 — migration-follow (scenario C, the owner's feature)

**Design: the sidecar detects and HINTS; the daemon owns identity and
ADOPTS.** The sidecar never changes SESSION.uid itself — the if_uid
guard (sonosd.py ~571) makes dual-writer identity a 409 machine, which
is why the hint travels on /state and the daemon acts.

Ground truths the design rests on (verified in code):
- The three signatures are distinguishable at `_classify`: hijack =
  non-empty `x-rincon:` TrackURI; natural episode end = STOPPED with
  TrackURI RETAINED (the `ends_near` queue advance depends on this —
  do NOT reuse the `lost` flag, it excludes sharelink); migration/lost
  = STOPPED with EMPTY TrackURI. Migration vs truly-lost is settled
  only by a live probe of other coordinators.
- The sharelink `ours` test is prefix-only — safe against our own uid,
  POISONOUS as a probe criterion (any stranger's Spotify matches).
  The probe needs exact-track + position continuity for sharelink.

**Sidecar state machine** (new Session fields: `_ours_at`,
`_last_ours_track`, `_last_ours_rel`, `_migrate_tries`, `_moved`;
constants `MIGRATE_TRIES = 3`, `MIGRATE_WINDOW_S = 90`):
1. LIVE: every ours+live classify stamps `_ours_at`, records the
   sharelink track/rel, re-arms tries, clears `_moved`.
2. SUSPECT (checked in poll_loop, never the 1.5s live-probe path):
   raw `transport == STOPPED and not track_uri` and ours seen within
   MIGRATE_WINDOW_S and tries remain and no pending hint. Hijack can
   never enter (uri non-empty); episode end can never enter (uri
   retained).
3. PROBE, one attempt per tick, `_cadence` returns POLL_S (5s) while
   tries pend so attempts land at 0/5/10s: call `refresh_topology()`
   (live, merges the promoted speaker's ip/name into the players cache
   — never the aux `coordinator`, which ghosts on failed refreshes);
   for each other-coordinator (cap 6): one GetPositionInfo +
   GetTransportInfo. Qualify IFF transport live AND — url/nrk:
   `_norm(candidate uri) == _norm(SESSION.uri)` (hoist the `_norm`
   closure to module level, refactor-only); sharelink: prefix AND
   decoded track == `_last_ours_track` AND rel within
   [-5, +45] of `_last_ours_rel`; `_last_ours_track is None` → never
   follow.
4. MOVED: publish `stream_moved: {uid, name, uri}` on every snapshot
   until cleared (cleared by play(), adopt(), or ours re-arming).
   Probe-once-per-transition = flap resistance. The `uri` echoed here
   is SESSION.uri — the snapshot's own uri is EMPTY during STOPPED and
   would clobber the session on adopt (trap!).
5. LOST: tries exhausted → exactly today's lost-session; DISARM at
   600s unchanged (90s window sits far inside).

**Daemon act block** in `_sonos_poller`, right after the /state fetch
and BEFORE the `not ours -> continue` gate (the migration window IS a
not-ours window): on `stream_moved` with fresh snapshot
(`stale_s < 12`) and `renderer.read().uid == snap.uid` (closes the
re-pick race — a stale hint after the user picked another room is a
no-op) and hint uid differs: `_renderer.write("sonos", uid, name)`,
clear `_sonos_vol_opt` (new speaker, new volume world), post `/adopt
{uid, kind, uri}` (the EXISTING endpoint — mid-session adoption is the
same operation as the startup reconcile's), wake the poller.

**Continuity (all traced)**: ours flips True against the new uid by
construction; bookmark freezes during the window (never worse than
today) and resumes on the first fresh ours snapshot; every verb reads
renderer.json at call time so if_uid holds after the write; volume
drives the new room; renderer_name shows the promoted room (name from
the players cache the probe just refreshed — same source as the
picker); the same-tile guard no-ops again post-adopt. A second hop
(promoted speaker removed too) re-fires cleanly: ours re-arms the
machinery. NO automatic retarget back when the home room rejoins —
the output follows the stream; the picker is the deliberate way home.

**Wire**: /state gains optional `stream_moved` — additive;
check_state ignores unknown fields. Add `STATE_MOVED` canonical
example + sub-shape check. No new endpoints.

**Trap list (full)**: sharelink prefix-follow (1); aux coordinator as
probe source (2); adopt with the snapshot's empty uri (3); re-pick
race (4); sidecar-internal retarget = if_uid split-brain (5); `lost`
flag reuse (6); episode-end/probe race (7); probe lock hold — cap
candidates, one attempt per tick (8); firmware variance — a removed
coordinator that RETAINS an x-rincon-queue: uri makes the probe never
fire, safe false negative but field-verify on THIS household,
especially for the hardware hold-play leave gesture (9); daemon
restart mid-window reverts to box, optional follow-up to honor
stream_moved in reconcile (10); keep the window inside DISARM (11).

**Tests**: tests/sonos_migrate.py (fake-sidecar: hint → renderer.json
+ one /adopt + vol-opt cleared; guards: same-uid hint no-ops, stale/
mismatched snapshot no-ops, foreign snapshot never follows; post-adopt
ours → playing + verbs carry new if_uid + bookmark resumes).
tests/sonos_migrate_probe.py (Session-level: probe fires only on
STOPPED+empty after ours-live; hijack never probes; 3 tries then lost,
call count pinned; sharelink exact-track rule; _norm variants;
probe-once until re-armed; second hop; cadence 5s while pending).
tests/sonos_contract.py: STATE_MOVED validates, frozen examples
untouched. New pin from round 1: the grouped-away reclaim (ours False
+ x-rincon foreign → play posts /play at OUR uid, never the
coordinator; bookmark writes nothing; same-tile falls through).

**Field checklist (with the PHYSICAL buttons, not the app)**:
1. Start on room A from the box; hold-play on room B → B joins;
   box verbs drive both (scenario A sanity).
2. Remove A (app or hardware — test both if the hardware gesture
   exists on this firmware) → stream continues on B; box card shows
   B's name and verbs/volume drive B within ~30s (typ. 8-15s).
   Bookmark advances; survives a power cycle.
3. Remove B too → lost-session; A-press resumes from the bookmark.
   Journal shows probe attempts, no match, no follow.
4. Hijack: pull the box's speaker into another group playing other
   audio → taken-over card, no retarget in the journal; play-press
   reclaims the kid's speaker only.
5. Journal the exact snapshot at the removal instant (trap 9): confirm
   STOPPED + empty TrackURI on this household's firmware.

**Implementation order**: B1 first (its ours-gates and detection are
independent and de-risk B2), then B2. Both suites green before field.

