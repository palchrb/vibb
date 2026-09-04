#!/bin/bash
# The Spotify ENGINE toggle for install.sh (PLAN-pipewire-soloist.md
# Phase 3, AM-53). Sourced, never run. The twin of audio-stack.sh.
#
#   spotify_engine_resolve   VIBB_SPOTIFY_ENGINE env > /etc/vibb/spotify-engine
#                            > golibrespot. Refuses soloist EARLY — before
#                            anything is touched — unless the audio stack
#                            resolves to pipewire (Soloist has no ALSA
#                            backend) and the sidecar exists in the tree.
#                            Writes NOTHING.
#   spotify_engine_unit_env  the Environment= lines every unit that talks
#                            to the engine gets: VIBB_GO_API + VIBB_GO_UNIT
#                            (paths.go_unit_cmd / spotify.API read them)
#   spotify_engine_go_config_env
#                            VIBB_GO_CONFIG for the daemon under golibrespot
#                            ONLY: under soloist the supervisor's zeroconf
#                            lock would otherwise rewrite go-librespot's
#                            config.yml and restart the soloist unit every
#                            tick (AM-53)
#   spotify_engine_apply     soloist: install the sidecar, write
#                            vibb-soloistd.service, MASK go-librespot (never
#                            remove), enable the sidecar; golibrespot: the
#                            rollback — disable+mask the sidecar unit if it
#                            exists, unmask go-librespot; config.yml is never
#                            touched either way. Records the choice LAST, so a
#                            failed apply never remembers an engine that is
#                            not serving.
#
# VIBB_FS_ROOT prefixes every path (tests). Needs write_if_changed and
# install_if_changed from install.sh, RUN_USER, SCRIPT_DIR, and
# audio_stack_peek / audio_stack_unit_env from audio-stack.sh.

SPOTIFY_ENGINE="${SPOTIFY_ENGINE:-}"
_SE_ROOT="${VIBB_FS_ROOT:-}"
_SE_ETC="$_SE_ROOT/etc"
_SE_FILE="$_SE_ETC/vibb/spotify-engine"
_SE_ENV="/etc/vibb/soloist.env"       # the API key: KEY=VALUE, 0600, PWA-written
SOLOISTD_PORT="${SOLOISTD_PORT:-3688}"  # the sidecar's go-librespot-dialect HTTP

_se_say() { echo "    spotify engine: $*"; }

spotify_engine_resolve() {
  local want="${VIBB_SPOTIFY_ENGINE:-}"
  if [[ -z $want && -r $_SE_FILE ]]; then
    want="$(tr -d '[:space:]' < "$_SE_FILE")"
  fi
  case "$want" in
    ""|golibrespot) SPOTIFY_ENGINE=golibrespot ;;
    soloist)
      if [[ $(audio_stack_peek) != pipewire ]]; then
        echo "install.sh: --soloist needs the PipeWire audio stack (Soloist has no ALSA" >&2
        echo "  backend). Re-run with --pipewire --soloist, or --pipewire first." >&2
        return 2
      fi
      if [[ ! -f $SCRIPT_DIR/soloistd.py ]]; then
        echo "install.sh: --soloist is not available yet: the soloistd sidecar" >&2
        echo "  (docs/PLAN-soloistd.md P1, docs/PLAN-pipewire-soloist.md step 4) is designed" >&2
        echo "  but not built — nothing would provision or supervise the engine." >&2
        return 2
      fi
      SPOTIFY_ENGINE=soloist ;;
    *) echo "install.sh: VIBB_SPOTIFY_ENGINE must be golibrespot or soloist (got '$want')" >&2
       return 2 ;;
  esac
  _se_say "$SPOTIFY_ENGINE"
}

spotify_engine_unit_env() {
  if [[ $SPOTIFY_ENGINE == soloist ]]; then
    cat <<EOF
Environment=VIBB_GO_API=http://127.0.0.1:$SOLOISTD_PORT
Environment=VIBB_GO_UNIT=vibb-soloistd
EOF
  else
    cat <<'EOF'
Environment=VIBB_GO_API=http://127.0.0.1:3678
Environment=VIBB_GO_UNIT=go-librespot
EOF
  fi
}

spotify_engine_go_config_env() {  # <config.yml path>
  [[ $SPOTIFY_ENGINE == soloist ]] || echo "Environment=VIBB_GO_CONFIG=$1"
}

# The unit the engine runs as. RESTORE sets in extra.sh read the same
# file this writes, so they never name the wrong engine.
spotify_engine_unit() {
  [[ $SPOTIFY_ENGINE == soloist ]] && echo vibb-soloistd || echo go-librespot
}

_se_write_soloistd_unit() {
  write_if_changed "$_SE_ETC/systemd/system/vibb-soloistd.service" <<EOF || true
[Unit]
Description=Vibb Spotify engine: soloistd (Spotify Soloist behind the go-librespot dialect)
# The sidecar supervises the soloist child itself (exit 10 = build expired
# LATCHES, persisted — a unit Restart= would brick-loop it). Ordered after
# the audio server it must bind to and after the clock the CDN/TLS needs.
After=network-online.target wireplumber.service vibb-rtc.service
Wants=network-online.target
StartLimitIntervalSec=90
StartLimitBurst=6

[Service]
User=$RUN_USER
# the API key (KEY=VALUE), written 0600 by the PWA — '-' so a box without
# one still starts, into the sidecar's clear needs-key state
EnvironmentFile=-$_SE_ENV
# Soloist honours \$STATE_DIRECTORY (session, ws.addr/ws.port) and
# \$CACHE_DIRECTORY (the playback cache, capped with -z) natively
StateDirectory=vibb-soloist
CacheDirectory=vibb-soloist
Environment=VIBB_SOLOISTD_PORT=$SOLOISTD_PORT
$(audio_stack_unit_env)
ExecStart=/opt/vibb/venv/bin/python3 /usr/local/bin/vibb-soloistd
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
}

spotify_engine_apply() {
  # audio_stack_unit_env reads AUDIO_STACK; install.sh resolved it long before
  # this runs, but never depend on it (soloist implies pipewire anyway)
  AUDIO_STACK="${AUDIO_STACK:-$(audio_stack_peek)}"
  if [[ $SPOTIFY_ENGINE == soloist ]]; then
    install_if_changed 755 "$SCRIPT_DIR/soloistd.py" "$_SE_ROOT/usr/local/bin/vibb-soloistd" || true
    _se_write_soloistd_unit
    systemctl daemon-reload
    # two Connect devices for one box is the plan's rejected coexistence:
    # mask go-librespot, never remove it (rollback = --librespot)
    systemctl mask --now go-librespot.service >/dev/null 2>&1 || true
    systemctl enable --now vibb-soloistd.service
    _se_say "soloistd up; go-librespot masked (rollback: ./install.sh --librespot)"
  else
    if [[ -e $_SE_ETC/systemd/system/vibb-soloistd.service ]]; then
      systemctl disable --now vibb-soloistd.service >/dev/null 2>&1 || true
      systemctl mask vibb-soloistd.service >/dev/null 2>&1 || true
      _se_say "soloistd disabled + masked (rollback)"
    fi
    systemctl unmask go-librespot.service >/dev/null 2>&1 || true
    # go-librespot's config.yml is never touched by this toggle
  fi
  mkdir -p "$(dirname "$_SE_FILE")"
  printf '%s\n' "$SPOTIFY_ENGINE" > "$_SE_FILE"
  _se_say "recorded $SPOTIFY_ENGINE ($_SE_FILE)"
}
