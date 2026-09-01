# Spotify Soloist — assessment for vibb (2026-09-01)

Spotify shipped an OFFICIAL closed-source headless Connect client
(announced 2026-08-13): https://developer.spotify.com/documentation/soloist
Owner flagged it; verified from the docs, not the blog post alone.

## What it has that matters here

- Local **WebSocket JSON API** (opt-in, bind 127.0.0.1): playback
  control (play/pause/seek/volume/shuffle/repeat), **push events**
  (nicer than our /status polling), queue access with full track
  metadata; `soloist ctl` CLI rides the same socket, discovers the
  endpoint via ws.addr/ws.port files in the data dir.
- Playback **cache with a size limit** — the fork's headline feature,
  now official.
- Podcasts + audiobooks + the whole catalogue; **Free accounts**;
  official Connect-based auth (`--pair` stores a session) — the
  new-account login problem does not exist here by construction.
- Single-track mode (`--single-track URI`, plays one uri and exits).

## Why it is NOT the box's engine today

1. **90-day build expiry, exit code 10.** A kids appliance must never
   brick on a timer. Cabin weeks without wifi + a build crossing its
   expiry = dead Spotify until someone updates. Weekly auto-update
   mitigates only while online. Hard disqualifier until Spotify drops
   or extends this (the community hopes GA will).
2. **PipeWire/PulseAudio only — no ALSA.** vibb is bare ALSA +
   bluealsa with hand-built PCMs, zero sound server BY DESIGN (RAM and
   idle power on the Zero 2 W). Adopting Soloist means adopting
   PipeWire, which in practice also means migrating the BT path off
   bluealsa — a platform migration through the box's most
   field-hardened layer, not an engine swap.
3. Closed source: the fork's kid-tuned behaviors (fast-skip debounce,
   throttled-key circuit breaker, /context/tracks ready-polling,
   pending_track_uri optimism) cannot be ported into their binary.

## Strategic read

Official headless client + simultaneous auth tightening against
reverse-engineered clients (see the device_auth wave, install.sh
v0.2.2 note) = a funnel. The go-librespot path's risk RISES over time.
vibb's insurance already exists: the engine sits behind two seams —
vibb/spotify.py's API client and the sidecar pattern sonosd proves —
so a future `soloistd` adapter (WS -> the same internal surface) is a
bounded project, not a rewrite. PipeWire is the real migration cost.

## Decision + the one open question

STAY on the go-librespot fork (works, invested, device_auth covers the
auth class). Treat this file as the contingency plan. If the fork's
path degrades further, the bench test (NEVER the box) that decides
adapter feasibility is: **can the WS API start a playback context at
an exact position** — the replay-at-the-right-second machinery is the
box's spine. Queue access + seek exist; context-at-position parity is
unverified.
