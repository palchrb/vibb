#!/bin/bash
# The audio-stack toggle for install.sh (PLAN-pipewire-soloist.md §H).
# Sourced, never run: install.sh calls the functions below in order.
#
#   audio_stack_resolve    VIBB_AUDIO_STACK env > /etc/vibb/audio-stack > bluealsa
#   audio_stack_packages   the apt packages the chosen stack adds
#   audio_stack_apply      pipewire: user, dirs, the three system units, the
#                          config fragments, bluealsa MASKED (never removed),
#                          socket -> service -> wireplumber enabled
#                          bluealsa: today's daemons; if pipewire units exist
#                          this is the ROLLBACK: wireplumber -> service ->
#                          socket disabled+masked, Debian's ALSA default
#                          override removed, bluealsa unmasked + enabled
#   audio_stack_route      asound.conf for the stack via `bt.py route`
#   audio_stack_unit_env   the Environment= lines every audio client unit gets
#
# Everything the units and fragments contain is what passed on the bench
# (bench/pipewire_platform_rig.sh, 2026-09-03): no RuntimeDirectory on the
# socket-activated service (AM-1), wireplumber BindsTo/WantedBy pipewire
# (AM-2), Nice=0 (AM-4), no PIPEWIRE_CONFIG_DIR (AM-21), the shipped
# main-embedded profile (AM-22), find-default/find-best disabled (AM-23).
# hw-volume is DEFAULTED, not set — see the comment at the fragment
# (AM-35, settled from the 1.4.2 source, not the bench).
# VIBB_FS_ROOT prefixes every path (tests).

AUDIO_STACK="${AUDIO_STACK:-}"
_AS_ROOT="${VIBB_FS_ROOT:-}"
_AS_ETC="$_AS_ROOT/etc"
_AS_STATE="$_AS_ROOT/var/lib/vibb"
_AS_STACK_FILE="$_AS_ETC/vibb/audio-stack"
_AS_SOCK=/run/pipewire/pipewire-0
PW_USER=pipewire
# bluez5.roles names the REMOTE device's role — settled from the PipeWire
# 1.4.2 source, not guessed (AM-35): bluez5-device.c emit_nodes() answers
# SPA_BT_PROFILE_A2DP_SINK with emit_node(DEVICE_ID_SINK, ...), i.e. a
# PLAYBACK node (bluez_output.*), and bluez5-dbus.c's
# media_endpoint_to_profile() maps our locally registered
# A2DP_SOURCE_ENDPOINT (UUID 0000110a) to that same profile — the very
# UUID btbus's transport gate matches. So a2dp_sink = "the headphone is
# the sink, we feed it". Listing ONLY it also means the box never
# registers the 0000110b sink endpoint, so a phone cannot stream INTO
# the box (MODERATE-3 closed by construction, not by policy).
WP_ROLES="${WP_ROLES:-a2dp_sink}"
WP_PROFILE="${WP_PROFILE:-main-embedded}"

_as_say() { echo "    audio stack: $*"; }

audio_stack_resolve() {
  local want="${VIBB_AUDIO_STACK:-}"
  if [[ -z $want && -r $_AS_STACK_FILE ]]; then
    want="$(tr -d '[:space:]' < "$_AS_STACK_FILE")"
  fi
  case "$want" in
    pipewire) AUDIO_STACK=pipewire ;;
    ""|bluealsa) AUDIO_STACK=bluealsa ;;
    *) echo "VIBB_AUDIO_STACK must be bluealsa or pipewire (got '$want')" >&2; return 1 ;;
  esac
  mkdir -p "$(dirname "$_AS_STACK_FILE")"
  printf '%s\n' "$AUDIO_STACK" > "$_AS_STACK_FILE"
  _as_say "$AUDIO_STACK ($_AS_STACK_FILE)"
}

audio_stack_packages() {
  # bluealsa's packages stay in install.sh's PKGS on BOTH stacks: masked,
  # never removed, so an offline rollback is always possible (NEW-6)
  [[ $AUDIO_STACK == pipewire ]] \
    && echo "pipewire pipewire-bin pipewire-alsa wireplumber libspa-0.2-bluetooth"
  return 0
}

audio_stack_unit_env() {
  # PIPEWIRE_PROPS is the belt under client.conf.d's stream.properties (AM-5);
  # VIBB_BT_GATE=transport is belt under btbus's stack check (finding j)
  if [[ $AUDIO_STACK == pipewire ]]; then
    cat <<EOF
Environment=VIBB_AUDIO_STACK=pipewire
Environment=PIPEWIRE_RUNTIME_DIR=/run/pipewire
Environment=PIPEWIRE_PROPS={ node.dont-reconnect=true node.dont-fallback=true }
Environment=VIBB_BT_GATE=transport
EOF
  else
    echo "Environment=VIBB_AUDIO_STACK=bluealsa"
  fi
}

# the unit ordering vibb-bt-reconnect needs: the A2DP endpoint owner
audio_stack_endpoint_units() {
  [[ $AUDIO_STACK == pipewire ]] && echo "wireplumber.service" \
    || echo "bluealsa.service bluealsad.service"
}

_as_write_units() {
  write_if_changed "$_AS_ETC/systemd/system/pipewire.socket" <<EOF || true
[Unit]
Description=Vibb PipeWire (system) socket
[Socket]
Priority=6
ListenStream=$_AS_SOCK
SocketUser=$PW_USER
SocketGroup=audio
SocketMode=0660
DirectoryMode=0750
[Install]
WantedBy=sockets.target
EOF
  write_if_changed "$_AS_ETC/systemd/system/pipewire.service" <<EOF || true
[Unit]
Description=Vibb PipeWire (system-wide, owns the I2S HAT + BT A2DP)
Requires=pipewire.socket
After=pipewire.socket
[Service]
Type=simple
User=$PW_USER
Group=audio
SupplementaryGroups=bluetooth
Environment=PIPEWIRE_RUNTIME_DIR=/run/pipewire
# NO PIPEWIRE_CONFIG_DIR: it REPLACES the search path and hides
# /usr/share/pipewire/pipewire.conf (bench 2026-09-03). NO RuntimeDirectory:
# systemd removes it on every stop while the socket unit still holds the
# listener — one crash would leave every client with ENOENT (AM-1).
Environment=HOME=/var/lib/vibb/pipewire
ExecStart=/usr/bin/pipewire
Nice=0
LimitMEMLOCK=64M
Restart=on-failure
RestartSec=2
[Install]
WantedBy=multi-user.target
Also=pipewire.socket
EOF
  write_if_changed "$_AS_ETC/systemd/system/wireplumber.service" <<EOF || true
[Unit]
Description=Vibb WirePlumber (system-wide session manager, bluez5 + alsa only)
# BindsTo + WantedBy=pipewire.service: a PipeWire crash restarts BOTH — a
# Requires= stop is a clean stop and Restart=on-failure would not fire (AM-2)
BindsTo=pipewire.service
After=pipewire.service dbus.service
[Service]
Type=simple
User=$PW_USER
Group=audio
SupplementaryGroups=bluetooth
Environment=PIPEWIRE_RUNTIME_DIR=/run/pipewire
Environment=XDG_STATE_HOME=/var/lib/vibb/wireplumber
Environment=XDG_CONFIG_HOME=/etc/vibb/wp-empty-config
Environment=HOME=/var/lib/vibb/wireplumber
# no session bus on this box: silence the spa.dbus/telephony warnings
Environment=DBUS_SESSION_BUS_ADDRESS=disabled:
ExecStart=/usr/bin/wireplumber -p $WP_PROFILE
Nice=0
Restart=on-failure
RestartSec=2
[Install]
WantedBy=pipewire.service
EOF
}

_as_write_fragments() {
  write_if_changed "$_AS_ETC/pipewire/pipewire.conf.d/10-vibb.conf" <<'EOF' || true
# vibb: 44100 everywhere (mpv pins it, SBC runs at it) = no graph resampler;
# a fat quantum (~46ms) = fewer wakeups on a box with no low-latency need.
context.properties = {
  default.clock.rate          = 44100
  default.clock.allowed-rates = [ 44100 48000 ]
  default.clock.quantum       = 2048
  default.clock.min-quantum   = 1024
  default.clock.max-quantum   = 4096
  mem.allow-mlock             = false
  log.level                   = 2
}
context.objects = [
  # the null sink soloistd warms the cache into; costs nothing suspended
  { factory = adapter
    args = { factory.name = support.null-audio-sink  node.name = "vibb_null"
             node.description = "vibb null sink (cache warming)"
             media.class = "Audio/Sink"  audio.position = [ FL FR ]
             monitor.channel-volumes = false  node.passive = true } }
]
EOF
  write_if_changed "$_AS_ETC/pipewire/client.conf.d/10-vibb.conf" <<EOF || true
# vibb: every libpipewire client on the box (pipewire-alsa's plugin, soloist,
# pw-*) — a stream whose target vanishes is DESTROYED, never re-homed; a
# stream whose target is absent at open FAILS, never lands elsewhere (I2).
context.properties = { remote.name = "$_AS_SOCK"  mem.allow-mlock = false  log.level = 1 }
stream.properties = {
  node.dont-reconnect = true
  node.dont-fallback  = true
  node.autoconnect    = true
}
EOF
  write_if_changed "$_AS_ETC/wireplumber/wireplumber.conf.d/50-vibb.conf" <<EOF || true
# vibb extends the shipped '$WP_PROFILE' profile (0.5.8: main +
# systemwide-session + stateless — no logind, no seat monitoring, no
# reserve-device, no portal, every *.state restore hook off). Only names
# from the distro's own 'provides' inventory appear here.
wireplumber.profiles = {
  $WP_PROFILE = {
    hardware.video-capture            = disabled
    monitor.alsa-midi                 = disabled
    monitor.bluez-midi                = disabled
    hooks.default-nodes.state         = disabled
    hooks.stream.state                = disabled
    hooks.device.profile.state        = disabled
    hooks.device.routes.state         = disabled
    # AM-23, bench-proven REQUIRED: without these a TARGETLESS stream links
    # to the default sink — i.e. the HAT. Both names real, WirePlumber
    # starts with them off.
    hooks.linking.target.find-default = disabled
    hooks.linking.target.find-best    = disabled
  }
}
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
monitor.bluez.properties = {
  bluez5.roles              = [ $WP_ROLES ]
  bluez5.codecs             = [ sbc ]
  bluez5.enable-sbc-xq      = false
  bluez5.enable-msbc        = false
  # bluez5.enable-hw-volume is deliberately ABSENT (AM-35). It is not an
  # on/off switch: the docs call it "override device quirk list and enable
  # hardware volume for devices for which it is disabled", so true would
  # FORCE absolute volume onto headsets blacklisted for handling it badly,
  # and false would merely decline to override. The knob that matters is
  # the per-profile default, and 1.4.2's DEFAULT_HW_VOLUME_PROFILES
  # already contains SPA_BT_PROFILE_A2DP_SINK — so the kid's headset
  # keeps its own volume buttons, quirky devices stay protected, and vibb
  # still never writes a node volume itself (I8).
  bluez5.dummy-avrcp-player = false
  bluez5.default.rate       = 44100
}
monitor.bluez.rules = [
  { matches = [ { node.name = "~bluez_output.*" } ]
    actions = { update-props = { session.suspend-timeout-seconds = 120  node.pause-on-idle = true } } }
  { matches = [ { device.name = "~bluez_card.*" } ]
    actions = { update-props = { bluez5.autoswitch-profile = false } } }
]
monitor.alsa.rules = [
  { matches = [ { node.name = "~alsa_output.*" } ]
    actions = { update-props = { session.suspend-timeout-seconds = 5  api.alsa.soft-mixer = true  node.pause-on-idle = true } } }
]
EOF
}

audio_stack_apply() {
  local u
  if [[ $AUDIO_STACK == pipewire ]]; then
    getent group bluetooth >/dev/null 2>&1 || groupadd -r bluetooth
    id "$PW_USER" >/dev/null 2>&1 \
      || useradd -r -M -d "/var/lib/vibb/pipewire" -s /usr/sbin/nologin -g audio -G bluetooth "$PW_USER"
    mkdir -p "$_AS_STATE/pipewire" "$_AS_STATE/wireplumber" "$_AS_ETC/vibb/wp-empty-config"
    chown -R "$PW_USER:audio" "$_AS_STATE/pipewire" "$_AS_STATE/wireplumber" 2>/dev/null || true
    _as_write_units
    _as_write_fragments
    systemctl daemon-reload
    # bluealsa and PipeWire both register A2DP endpoints with BlueZ and
    # BlueZ accepts both — undefined ownership. Mask, never remove (NEW-6).
    for u in bluealsa.service bluealsad.service; do
      systemctl mask --now "$u" >/dev/null 2>&1 || true
    done
    systemctl enable --now pipewire.socket pipewire.service wireplumber.service
    _as_say "PipeWire system units up; bluealsa masked (rollback: VIBB_AUDIO_STACK=bluealsa ./install.sh)"
  else
    if [[ -e $_AS_ETC/systemd/system/pipewire.service ]]; then
      # ROLLBACK: the socket too, or the first client revives the service
      for u in wireplumber.service pipewire.service pipewire.socket; do
        systemctl disable --now "$u" >/dev/null 2>&1 || true
        systemctl mask "$u" >/dev/null 2>&1 || true
      done
      # Debian's pipewire-alsa maps ALSA 'default' to PipeWire; an apt
      # upgrade may recreate this, which bt.py's asound.conf then overrides
      rm -f "$_AS_ETC/alsa/conf.d/99-pipewire-default.conf"
      _as_say "PipeWire units disabled + masked (rollback)"
    fi
    for u in bluealsa.service bluealsad.service; do
      systemctl unmask "$u" >/dev/null 2>&1 || true
    done
    systemctl enable --now bluealsa.service 2>/dev/null \
      || systemctl enable --now bluealsad.service
  fi
}

audio_stack_route() {
  # asound.conf for the stack. pipewire: pins vibb_local to the HAT node
  # and vibb_bt to the speaker's (or to a name that cannot exist) — needs
  # WirePlumber up. bluealsa: today's rewrite when a speaker is known;
  # install.sh's placeholder otherwise.
  local py="${VIBB_BT_PY:-/usr/bin/python3 -c 'import sys; sys.path.insert(0, \"/usr/local/lib/vibb-py\"); from vibb import bt; sys.exit(bt.main())'}"
  if [[ $AUDIO_STACK == pipewire ]]; then
    local _try
    for _try in 1 2 3 4 5 6 7 8 9 10; do
      [[ -S $_AS_ROOT$_AS_SOCK ]] && break
      sleep 1
    done
    : "$_try"
  fi
  # shellcheck disable=SC2086
  VIBB_AUDIO_STACK="$AUDIO_STACK" eval "$py" route || _as_say "bt.py route failed — asound.conf left as is"
}
