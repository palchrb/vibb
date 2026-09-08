# Feature request: optional `skip_to` on `play` (start a context at a given track)

*Draft for github.com/spotify/soloist/issues — vibb, 2026-09-08. Numbers from a Pi Zero 2 W, build 1.3.8.13.*

## Summary

`play` takes a context URI and always starts at its first entry. A client that wants to
resume a playlist at track 22 (or let a user pick a song from a list) has no way to say so
and must walk there with `skip_next`. Proposal: an **optional** `skip_to` field on `play`
naming a track URI inside the context. Absent or not found → ignored, playback starts as
today. No new command, no new event, no change for clients that do not send it.

## What clients do today, and what it costs

Resume at track N = `play {uri}` → `pause` → N × (`skip_next`, wait for `track_changed`) →
`seek` → `play`, with the volume shrouded to 0 so the passed tracks stay silent.

Measured on a Pi Zero 2 W (`--burst-test`, 6 awaited skips vs 6 sent back to back):

| | per skip | 22-row playlist | 80-row playlist |
|---|---|---|---|
| skips awaited one by one | 0.15 s | 3.3 s | 12 s |
| skips sent in a burst | 0.16 s | 3.3 s | 12 s |

Skips are serialized inside the client at ~0.15 s each, so a burst gains nothing. Every
skip also prefetches the next item's first block (one 131 168 B fetch per passed track) and
every passed track is reported through `track_changed`, so the client's own UI shows 21
covers flashing by before the right one. On a slow link the first skip after a context load
took 0.7 s; a user picking the last row of a 22-row list waited 15 s for audio.

`play` with a **track** URI plus `add_to_queue` for the rest starts instantly (0.32 s +
0.05 s per queued row) but loses the context: `skip_prev` cannot go before the anchor,
shuffle/repeat-context are meaningless on a one-track context, and a drained queue pauses
on the anchor instead of continuing the list.

## Proposal

```json
{ "type": "command", "command": "play",
  "uri": "spotify:playlist:5E6tmZySzQwe88grhIrpbY",
  "skip_to": "spotify:track:2xnoPV3NLescauc0ZJ1MDZ" }
```

- `skip_to` (optional): a track URI. After the context is resolved, playback starts at the
  first entry whose URI matches, instead of at entry 0.
- Not set → exactly today's behaviour.
- Set but not found in the context (removed track, typo) → **silently ignored**, playback
  starts at entry 0. No error frame, so a client never has to know whether the build it
  talks to supports the field; the walk stays as its fallback.
- `seek` afterwards works as now; a second optional `position_ms` on `play` would be a
  nice-to-have but is not needed for this request.

Semantics identical to "the context was loaded and `skip_next` landed on that entry": same
`track_changed`, same queue (`previous` holds the skipped-over entries or stays empty —
either is fine, the docs can say which), same repeat/shuffle handling.

## Why it is cheap to build and keep

- The client already resolves the whole context on `play` (the queue shows it) and already
  has the code path that positions playback at an arbitrary index (`skip_next`).
- One optional field, parsed in one place, defaulting to "index 0". No new command, no new
  event type, no new query, nothing in `get_state`/`get_queue` changes.
- Backwards compatible in both directions: old clients never send it, old builds ignore
  unknown fields (the current build already ignores unknown fields on `play`).
- Failure mode is "start at the top", which is what happens today anyway.

## Who benefits

Any headless player that resumes where it left off: a kid's music box, a car head unit,
a wall panel. All of them keep a bookmark (track + position) and today either walk, or give
up the context. With `skip_to` a resume is one command and one `track_changed`.
