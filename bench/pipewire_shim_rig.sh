#!/bin/bash
# PipeWire shim rig — the LAST untested integration gate for soloistd.
#
# The question: can a MINIMAL, scoped PipeWire instance act as a shim
# that lets Soloist (PipeWire/Pulse-only) play into vibb's hand-built
# ALSA pcms (vibb_bt over bluealsa, vibb_local over the I2S HAT) —
# WITHOUT adopting PipeWire as the platform, and WITHOUT taking the BT
# path away from bluealsa?
#
# Run on a BENCH Pi that mimics the box's audio setup. NEVER the box.
# The box keeps bare ALSA + bluealsa; this rig decides whether the
# soloistd plan's primary audio answer survives contact.
#
# Usage:
#   ./pipewire_shim_rig.sh check      # what's installed / what plays
#   ./pipewire_shim_rig.sh setup      # write the scoped shim config
#   ./pipewire_shim_rig.sh start      # run the shim (foreground)
#   ./pipewire_shim_rig.sh test       # the five verdicts
#   ./pipewire_shim_rig.sh clean
#
# The five things it must prove (in kill order):
#   S1 the shim opens a vibb pcm at all (plug->bluealsa / plug->hw)
#   S2 Soloist finds and uses the shim's sink (not the system default)
#   S3 the shim RELEASES the exclusive bluealsa device when idle, so a
#      subsequent mpv/aplay can open it — the box alternates engines
#   S4 memory cost on a 430MB-class box
#   S5 output switch: rewrite target pcm + restart shim, audio follows
set -u

PCM="${VIBB_PCM:-vibb_bt}"           # or vibb_local
RIG="${RIG_DIR:-$HOME/pw-shim-rig}"
CONF="$RIG/pipewire-shim.conf"
export PIPEWIRE_RUNTIME_DIR="$RIG/run"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()  { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
bad() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; }
note(){ printf '       %s\n' "$*"; }

cmd_check() {
  say "environment"
  for b in pipewire pw-cli pw-cat wpctl aplay bluealsa-aplay soloist; do
    if command -v "$b" >/dev/null 2>&1; then ok "$b"; else note "missing: $b"; fi
  done
  say "vibb pcms visible to ALSA?"
  if aplay -L 2>/dev/null | grep -q "^${PCM}$"; then
    ok "$PCM is defined in asound.conf"
  else
    bad "$PCM not in 'aplay -L' — copy the box's /etc/asound.conf here"
    note "the box defines: vibb_bt (plug->bluealsa) and vibb_local"
    note "(plug->hw:sndrpihifiberry). For a bench without the HAT,"
    note "point vibb_local at plug->default to exercise the mechanics."
  fi
  say "is $PCM a REAL device or the null placeholder? (false-pass trap)"
  # install.sh ships vibb_bt as plug->null until play.sh replaces it
  # with a real bluealsa device on first BT connect. A null slave
  # accepts every open, never blocks, and always "plays" — so S3 (the
  # exclusive-device release, the whole point) would PASS falsely.
  if grep -A3 "pcm\.$PCM" /etc/asound.conf 2>/dev/null | grep -q '"null"'; then
    bad "$PCM is the PLACEHOLDER (slave.pcm null)"
    note "S1b and S3 will pass MEANINGLESSLY against a null sink."
    note "Fix: pair+connect a BT speaker so play.sh writes the real"
    note "bluealsa pcm, or point $PCM at a real hw device by hand."
  elif grep -A5 "pcm\.$PCM" /etc/asound.conf 2>/dev/null | grep -qi "bluealsa"; then
    ok "$PCM is a real bluealsa device — S3 will test the real thing"
  else
    note "$PCM is neither null nor bluealsa; S3 tests THAT device's"
    note "open semantics, which may differ from bluealsa's exclusivity."
  fi

  say "does the pcm play RIGHT NOW (baseline, no pipewire)?"
  note "running: speaker-test -D $PCM -c2 -t sine -l1"
  timeout 6 speaker-test -D "$PCM" -c2 -t sine -l1 >/dev/null 2>&1 \
    && ok "baseline audio through $PCM works" \
    || bad "baseline failed — fix the pcm before testing any shim"
}

cmd_setup() {
  mkdir -p "$RIG/run"
  # A DELIBERATELY MINIMAL pipewire: no session manager, no monitors,
  # no bluez5 (bluealsa keeps the BT path), one static ALSA sink whose
  # device is the vibb pcm name. This is the whole architectural bet:
  # PipeWire as a thin shim, ALSA+bluealsa as the truth.
  cat > "$CONF" <<EOF
context.properties = {
    core.daemon      = true
    core.name        = pipewire-vibbshim
    default.clock.rate = 48000
    default.clock.quantum = 2048
    mem.allow-mlock  = false
}
context.spa-libs = {
    audio.convert.* = audioconvert/libspa-audioconvert
    api.alsa.*      = alsa/libspa-alsa
    support.*       = support/libspa-support
}
context.modules = [
    { name = libpipewire-module-rt
      args = { nice.level = -11 } flags = [ ifexists nofail ] }
    { name = libpipewire-module-protocol-native }
    { name = libpipewire-module-adapter }
    { name = libpipewire-module-spa-node-factory }
]
context.objects = [
    { factory = adapter
      args = {
        factory.name           = api.alsa.pcm.sink
        node.name              = "vibb-shim"
        node.description       = "Vibb shim -> $PCM"
        media.class            = "Audio/Sink"
        api.alsa.path          = "$PCM"
        audio.format           = "S16LE"
        audio.rate             = 48000
        audio.channels         = 2
        api.alsa.period-size   = 1024
        # THE LOAD-BEARING KNOB: release the exclusive bluealsa device
        # shortly after silence, or the next mpv spawn hits EBUSY —
        # the field failure class the box already knows.
        session.suspend-timeout-seconds = 5
      }
    }
]
EOF
  ok "wrote $CONF"
  note "PIPEWIRE_RUNTIME_DIR=$RIG/run keeps this instance OFF the"
  note "system/user pipewire — nothing else on the machine is touched."
}

cmd_start() {
  say "starting the scoped shim (Ctrl-C to stop)"
  note "runtime dir: $PIPEWIRE_RUNTIME_DIR"
  note "in ANOTHER shell, run: $0 test"
  exec pipewire -c "$CONF"
}

cmd_test() {
  local fails=0
  say "S1: does the shim expose the sink, and is it bound to $PCM?"
  if PIPEWIRE_RUNTIME_DIR="$RIG/run" pw-cli ls Node 2>/dev/null \
       | grep -q "vibb-shim"; then
    ok "sink node 'vibb-shim' is up"
  else
    bad "no vibb-shim node — the shim did not start or the pcm failed"
    note "check the 'start' shell for spa/alsa errors (EBUSY = the pcm"
    note "is already open by bluealsa-aplay/mpv; stop them and retry)"
    fails=$((fails+1))
  fi

  say "S1b: does audio actually reach the speaker THROUGH the shim?"
  if command -v pw-cat >/dev/null 2>&1; then
    note "playing a 3s tone via the shim..."
    PIPEWIRE_RUNTIME_DIR="$RIG/run" timeout 8 pw-cat --playback \
      --target vibb-shim /usr/share/sounds/alsa/Front_Center.wav \
      >/dev/null 2>&1 \
      && ok "pw-cat played through the shim (LISTEN: did you hear it?)" \
      || { bad "pw-cat failed"; fails=$((fails+1)); }
  fi

  say "S3: does the shim RELEASE the pcm when idle? (the EBUSY trap)"
  note "waiting 8s for suspend-timeout (5s)..."
  sleep 8
  if timeout 6 speaker-test -D "$PCM" -c2 -t sine -l1 >/dev/null 2>&1; then
    ok "another ALSA client opened $PCM while the shim idled"
    note "=> engines can alternate (soloist <-> mpv) without a restart"
  else
    bad "$PCM still busy — the shim holds the device"
    note "=> soloistd must stop the shim between engines, or tune"
    note "   session.suspend-timeout-seconds lower"
    fails=$((fails+1))
  fi

  say "S4: memory cost"
  local rss
  rss=$(ps -o rss= -C pipewire 2>/dev/null | awk '{s+=$1} END {print s+0}')
  note "pipewire RSS total: $((rss/1024)) MB  (budget: the box has ~430MB)"
  [ "$rss" -lt 40000 ] && ok "within budget" || bad "heavier than planned"

  say "S2/S5: the two MANUAL steps"
  note "S2 (Soloist finds the shim):"
  note "  PIPEWIRE_RUNTIME_DIR=$RIG/run \\"
  note "    ./run-soloist.sh -n Shim -k \$KEY -D ./data \\"
  note "      --pipewire-device vibb-shim -w 127.0.0.1:3690"
  note "  then play from the phone -> audio must come out of $PCM."
  note "S5 (output switch): VIBB_PCM=vibb_local $0 setup && restart the"
  note "  shim + soloist; audio must follow to the other pcm."
  echo
  [ "$fails" -eq 0 ] && say "AUTOMATED CHECKS PASSED ($fails failures)" \
                     || say "FAILURES: $fails — see notes above"
}

cmd_clean() { rm -rf "$RIG"; ok "removed $RIG"; }

case "${1:-check}" in
  check) cmd_check ;;
  setup) cmd_setup ;;
  start) cmd_start ;;
  test)  cmd_test ;;
  clean) cmd_clean ;;
  *) echo "usage: $0 {check|setup|start|test|clean}"; exit 2 ;;
esac
