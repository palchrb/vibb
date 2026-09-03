#!/usr/bin/env bash
# vibb-extra — hand the box to an owner script; guarantee the return.
#
# Launched by the screen UI (hold X+Y -> Extras -> confirm) as a
# TRANSIENT systemd unit:
#
#   systemd-run --unit=vibb-extra --collect --property=Restart=no \
#     --property='ExecStopPost=/usr/local/bin/vibb-extra --restore' \
#     /usr/local/bin/vibb-extra --run <script>
#
# ExecStopPost is the return guarantee: systemd runs it however the
# main process dies — clean exit, crash, OOM, even SIGKILL of this
# wrapper itself. A shell trap cannot promise that (QA 2026-07-28).
#
# Contract with the owner script (docs/extras.md): it owns the display,
# the buttons and the audio device until it EXITS. It may stop MORE
# vibb services itself (never disable/mask them) — the restore set
# below starts everything back deterministically, including units the
# wrapper never stopped, so a script that stopped vibb-daemon for RAM
# still returns to a whole box.
set -u

SYSTEMCTL="${VIBB_SYSTEMCTL:-systemctl}"
RFKILL="${VIBB_RFKILL:-rfkill}"
IW="${VIBB_IW:-iw}"
API="${VIBB_DAEMON:-http://127.0.0.1:3679}"

# Stopped on handoff: the display/button owner, the auto-power-off (a
# game session has neither playback nor box-button activity and must
# not be shut down under the player), and the media-key grabber (a USB
# gamepad must not be half-eaten). go-librespot holds the ALSA device.
# vibb-daemon deliberately STAYS UP: it holds no hardware, and its
# API is the remote escape hatch (battery, POST /system/shutdown from
# a linked phone) while the extra runs. Low-battery poweroff (PiSugar)
# is untouched.
HANDOFF="vibb-idle vibb-buttons vibb-ui"
# The restore set is the whole audio chain, not just what --run stopped:
# a script may stop bluetooth/bluealsa to use the radio itself, and the
# box must still come back whole. Anything OUTSIDE this set that a
# script stops is the script's own business.
RESTORE="bluetooth bluealsa go-librespot vibb-daemon vibb-mpris
         vibb-bt-reconnect vibb-buttons vibb-idle vibb-ui"
# Under the PipeWire stack the audio server owns the HAT (PLAN-pipewire-
# soloist §H): --run stops it so an extra gets the raw hw: device, and
# the restore set carries it. A masked unit's start is a harmless no-op,
# so one RESTORE list serves both stacks.
AUDIO_STACK="$(cat "${VIBB_AUDIO_STACK_FILE:-/etc/vibb/audio-stack}" 2>/dev/null || echo bluealsa)"
AUDIO_UNITS=""
[ "$AUDIO_STACK" = pipewire ] && AUDIO_UNITS="pipewire.socket pipewire wireplumber"
RESTORE="$RESTORE $AUDIO_UNITS"

CPUS="${VIBB_CPUFREQ:-/sys/devices/system/cpu}"
GOV_STATE="${VIBB_RUN:-/run}/vibb-extra-governor"
# One human line for the box screen: vibb-ui shows-and-deletes this on
# its next start (docs/extras.md). Scripts write their own reason;
# --restore fills in a generic one when the unit failed silently.
MSG_FILE="${VIBB_RUN:-/run}/vibb-extra.msg"

case "${1:-}" in
  --run)
    script="${2:?usage: vibb-extra --run <script>}"
    rm -f "$MSG_FILE" 2>/dev/null || true  # no stale note from last time
    # Unpark the CPU: boot runs 'vibb-power save', which pins the
    # governor to powersave (= 600 MHz flat on the Zero 2 W) — great
    # for podcasts, hopeless for an emulator. Snapshot whatever mode
    # the box was in, lift to ondemand for the extra; --restore puts
    # the snapshot back (default powersave — the safe battery state).
    prev="$(cat "$CPUS"/cpu0/cpufreq/scaling_governor 2>/dev/null || true)"
    [ -n "$prev" ] && { echo "$prev" > "$GOV_STATE" 2>/dev/null || true; }
    for g in "$CPUS"/cpu*/cpufreq/scaling_governor; do
      echo ondemand > "$g" 2>/dev/null || true
    done
    # Stop playback first — keep:true preserves the position bookmark
    # (a plain /stop clears it: 'stop = start over' is the kid-facing
    # semantic; the handoff must not cost the audiobook position —
    # field 2026-07-29). SAFE endpoint; CSRF gate wants the JSON type.
    curl -s -m 5 -X POST -H 'Content-Type: application/json' \
         -d '{"keep":true}' "$API/stop" >/dev/null 2>&1 || true
    $SYSTEMCTL stop $HANDOFF
    $SYSTEMCTL stop go-librespot 2>/dev/null || true  # frees I2S/ALSA
    if [ "$AUDIO_STACK" = pipewire ]; then
      # the server holds the HAT; stop it (reverse order) and open ALSA
      # 'default' — closed for everyone else (AM-15) — onto the HAT for
      # the extra only
      $SYSTEMCTL stop wireplumber pipewire pipewire.socket 2>/dev/null || true
      export VIBB_ALSA_DEFAULT=hw:sndrpihifiberry
    fi
    exec "$script"
    ;;
  --restore)
    # A silent failure still deserves a word on the screen: systemd
    # hands ExecStopPost the unit's outcome — if the script died
    # without leaving its own message, write a generic one.
    if [ "${SERVICE_RESULT:-success}" != success ] && [ ! -s "$MSG_FILE" ]; then
      echo "Extra failed (${EXIT_STATUS:-?}) — see journalctl -u vibb-extra" \
        > "$MSG_FILE" 2>/dev/null || true
    fi
    # Radios FIRST — everything below may pull network-online, and
    # starting go-librespot with wifi still rfkill-blocked stalled the
    # whole synchronous start queue 60s on NM-wait-online, with
    # vibb-ui stuck BEHIND it (field 2026-07-29: ~90s of black
    # screen, box unreachable over ssh). systemd-rfkill also PERSISTS
    # a block across reboots, so this line is what saves a crashed
    # script from leaving the box offline for good. txpower likewise
    # (a script's 5dBm softening must not follow us home).
    $RFKILL unblock wifi bluetooth 2>/dev/null || true
    $IW dev wlan0 set txpower auto 2>/dev/null || true
    # Heal mask/disable ONLY where the state calls for it: the blanket
    # unmask+enable ran four daemon-reloads (~2s each on a Zero 2) to
    # heal units that needed nothing. Scripts still must never
    # disable/mask — this belt is now free when unneeded.
    # (vibb-btsnoop is deliberately outside the set: it ships
    # disabled/opt-in and must stay whatever the owner chose.)
    for u in $RESTORE; do
      case "$($SYSTEMCTL is-enabled "$u" 2>/dev/null || true)" in
        masked)
          $SYSTEMCTL unmask "$u" >/dev/null 2>&1 || true
          $SYSTEMCTL enable "$u" >/dev/null 2>&1 || true ;;
        disabled)
          $SYSTEMCTL enable "$u" >/dev/null 2>&1 || true ;;
      esac
    done
    # Start the HUMAN-facing pieces first (the screen must be back the
    # moment the extra ends), network-independent audio plumbing next,
    # and go-librespot ASYNC last: its unit pulls network-online, and
    # nobody needs to wait for a Spotify login to see the menu.
    # shellcheck disable=SC2086
    for u in vibb-ui vibb-idle vibb-buttons vibb-daemon \
             vibb-mpris vibb-bt-reconnect $AUDIO_UNITS bluetooth bluealsa; do
      $SYSTEMCTL start "$u" 2>/dev/null || true
    done
    $SYSTEMCTL start --no-block go-librespot 2>/dev/null || true
    # re-park the CPU to whatever mode --run found (battery default)
    prev="$(cat "$GOV_STATE" 2>/dev/null || echo powersave)"
    for g in "$CPUS"/cpu*/cpufreq/scaling_governor; do
      echo "$prev" > "$g" 2>/dev/null || true
    done
    rm -f "$GOV_STATE" 2>/dev/null || true
    # Clear the failed-unit remnant: after an extra exits nonzero,
    # anything that later probes the unit makes systemd re-attempt
    # loading the deleted transient file and log 'Failed to open ...'
    # (field 2026-07-29: twice a minute, forever).
    $SYSTEMCTL reset-failed vibb-extra >/dev/null 2>&1 || true
    ;;
  *)
    echo "usage: vibb-extra --run <script> | --restore" >&2
    exit 2
    ;;
esac
