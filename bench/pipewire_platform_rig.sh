#!/bin/bash
# PipeWire PLATFORM rig — the pre-code verification for
# docs/PLAN-pipewire-soloist.md (the full PipeWire + WirePlumber
# migration, NOT the dead shim in pipewire_shim_rig.sh).
#
# The plan carries ~26 "(verify on bench)" tags: PipeWire 1.4 /
# WirePlumber 0.5 config keys and semantics the design rests on and
# that nobody could confirm from the tree. Every one of them is a
# check here, and NONE of them needs vibb code. If one fails, the
# design changes before a line is written — that is the whole point.
#
# Run on a Pi 5 (or any Trixie box) — S1/S2/S3/S5 need no BT and no
# HAT. S4 needs a paired BT speaker; the RF/RSS numbers (B4/B6/B11 in
# the plan) need a Zero 2 W and are NOT measured here.
#
# Usage (root, in this order):
#   ./pipewire_platform_rig.sh check     # tools, versions, the session
#                                        # PipeWire that must be stopped
#   ./pipewire_platform_rig.sh install   # system-mode units (plan A +
#                                        # AM-1/AM-2) + config (plan B)
#   ./pipewire_platform_rig.sh start
#   ./pipewire_platform_rig.sh test      # S1 S2 S3 S5 (+S4 with BT_MAC)
#   ./pipewire_platform_rig.sh s6        # go-librespot live reopen (guided)
#   ./pipewire_platform_rig.sh s7        # soloist binding (guided)
#   ./pipewire_platform_rig.sh clean     # remove everything, unmask the
#                                        # session PipeWire
#
# No Trixie to flash? Run the REAL 1.4/0.5 binaries in a Trixie
# container on the Bookworm Pi, borrowing the host's cards, BlueZ and
# D-Bus (host, as root):
#   ./pipewire_platform_rig.sh trixie-bootstrap   # mmdebstrap trixie -> /srv/vibb-trixie (~5 min),
#                                                 # stops+masks the host session PipeWire
#   ./pipewire_platform_rig.sh trixie-shell       # nspawn shell inside; then IN the container:
#       ./pipewire_platform_rig.sh check; ./pipewire_platform_rig.sh install
#       ./pipewire_platform_rig.sh start; ./pipewire_platform_rig.sh test
#   ./pipewire_platform_rig.sh trixie-clean       # kill the container daemons, unmask the host
#   Inside the container the rig runs in VIBB_RIG_MODE=procs: config +
#   daemons as plain processes, no units — so S3c (crash survival) is
#   SKIPPED there; AM-1/AM-2 are systemd facts, verify them on a flash.
#
# Knobs (env):
#   WP_ROLES=a2dp_sink       bluez5.roles value under test. The plan
#                            wrote a2dp_source (host-centric guess);
#                            PipeWire names roles after the PEER, so
#                            a2dp_sink is the default here. S4 says
#                            which one produces bluez_output.* nodes.
#   WP_PROFILE=main-embedded WirePlumber 0.5.8 SHIPS it: main +
#                            mixin.systemwide-session (no logind/seat/
#                            reserve-device/portal) + mixin.stateless
#                            (every *.state restore hook off). Exactly
#                            the box. Fragments extend THIS profile.
#   WP_DISABLE_HOOKS=0       1 = also disable hooks.linking.target.
#                            find-default + find-best (AM-6, real names).
#                            If WirePlumber then refuses to start, the
#                            hooks are hard-required by policy.standard
#                            — a finding: a custom profile is needed.
#   BT_MAC=AA:BB:..          a paired+connected BT speaker for S4.
#   GO_API=http://127.0.0.1:3678   for s6.
#   SOLOIST=/path/to/soloist       for s7.
set -u

MODE="${VIBB_RIG_MODE:-units}"     # units (a real Trixie) | procs (inside the container)
CT=/srv/vibb-trixie
PW_USER=pipewire
RUN=/run/pipewire
SOCK="$RUN/pipewire-0"
STATE=/var/lib/vibb
WP_ROLES="${WP_ROLES:-a2dp_sink}"
WP_DISABLE_HOOKS="${WP_DISABLE_HOOKS:-1}"   # bench 2026-09-03: REQUIRED (targetless linked to default without it) and safe
WP_PROFILE="${WP_PROFILE:-main-embedded}"
BT_MAC="${BT_MAC:-}"
export PIPEWIRE_RUNTIME_DIR="$RUN"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()  { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
bad() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; }
note(){ printf '       %s\n' "$*"; }
VERDICTS=()
verdict() { VERDICTS+=("$(printf '%12s  %s' "$1" "$2")"); printf '  -> %s: \033[1m%s\033[0m\n' "$2" "$1"; }

need_root() { [ "$(id -u)" = 0 ] || { bad "run as root (sudo)"; exit 2; }; }

# The desktop user whose SESSION PipeWire owns the cards + BT on a
# stock Pi 5 image. It must be stopped or every test measures the
# wrong instance (the shim rig's trap, pipewire_shim_rig.sh:83-89).
desk_user() { echo "${SUDO_USER:-$(loginctl list-users --no-legend 2>/dev/null | awk 'NR==1{print $2}')}"; }
user_ctl() {  # user_ctl <user> systemctl-args...
  local u="$1"; shift
  local uid; uid=$(id -u "$u" 2>/dev/null) || return 1
  sudo -u "$u" XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" systemctl --user "$@"
}

dump() { pw-dump 2>/dev/null || echo '[]'; }
node_id() {  # node_id <node.name>
  dump | jq -r --arg n "$1" '.[] | select(.type=="PipeWire:Interface:Node" and .info.props["node.name"]==$n) | .id' | head -1
}
stream_nodes() {  # ids of Stream/Output/Audio nodes
  dump | jq -r '.[] | select(.type=="PipeWire:Interface:Node" and .info.props["media.class"]=="Stream/Output/Audio") | .id'
}
links_from() {  # links_from <node id> -> input node names
  local d; d=$(dump)
  for in_id in $(printf '%s' "$d" | jq -r --argjson o "$1" '.[] | select(.type=="PipeWire:Interface:Link" and .info["output-node-id"]==$o) | .info["input-node-id"]' | sort -u); do
    printf '%s' "$d" | jq -r --argjson i "$in_id" '.[] | select(.type=="PipeWire:Interface:Node" and .id==$i) | .info.props["node.name"]'
  done | sort -u
}
node_prop() { dump | jq -r --argjson i "$1" --arg k "$2" '.[] | select(.type=="PipeWire:Interface:Node" and .id==$i) | .info.props[$k] // empty'; }
mk_null_sink() {  # mk_null_sink <name> -> id
  pw-cli create-node adapter "{ factory.name=support.null-audio-sink node.name=$1 media.class=Audio/Sink object.linger=true audio.position=[FL FR] monitor.channel-volumes=false }" >/dev/null 2>&1
  sleep 0.5; node_id "$1"
}
rm_node() { [ -n "${1:-}" ] && pw-cli destroy "$1" >/dev/null 2>&1; sleep 0.5; }
bench_pcm() {  # bench_pcm <node.name|-> -> a pcm name pinned to that node ("-" = no playback_node at all)
  # Trixie's stock `pipewire:` pcm has no PLAYBACK_NODE arg (first run: "Unknown
  # parameter"), so every pinned test pcm is written with the plan's own template.
  local f=/etc/alsa/conf.d/98-vibb-bench-dyn.conf
  if [ "$1" = - ]; then printf 'pcm.vibb_bench_dyn {\n    type pipewire\n    server "%s"\n}\n' "$SOCK" > "$f"
  else printf 'pcm.vibb_bench_dyn {\n    type pipewire\n    server "%s"\n    playback_node "%s"\n}\n' "$SOCK" "$1" > "$f"; fi
  echo vibb_bench_dyn
}

# ---------------------------------------------------------------------------
cmd_check() {
  say "tools"
  for b in pipewire wireplumber pw-dump pw-cli pw-cat pw-metadata wpctl aplay busctl jq; do
    if command -v "$b" >/dev/null 2>&1; then ok "$b"; else bad "missing: $b"; fi
  done
  for b in btmon bluetoothctl soloist; do command -v "$b" >/dev/null 2>&1 && ok "$b (optional)" || note "optional, missing: $b"; done
  say "versions (plan assumes PipeWire 1.4.x / WirePlumber 0.5.x = Debian trixie)"
  pipewire --version 2>/dev/null | sed 's/^/       /'; wireplumber --version 2>/dev/null | sed 's/^/       /'
  local wpv; wpv=$(wireplumber --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)
  if [ -n "$wpv" ] && [ "$(printf '%s\n' "$wpv" 0.5 | sort -V | head -1)" != 0.5 ]; then
    bad "WirePlumber $wpv is the 0.4 series (Bookworm). The whole plan §B policy is"
    note "written in the 0.5 SPA-JSON schema (wireplumber.conf.d, wireplumber.profiles,"
    note "wireplumber.settings) which 0.4 IGNORES — it reads Lua under main.lua.d."
    note "Every S1/S4 verdict here would measure 0.4 DEFAULTS, not the design."
    note "This bench must be Raspberry Pi OS TRIXIE (also the Soloist glibc floor)."
    note "  cat /etc/os-release | head -3"
    note "Refusing to continue. FORCE=1 overrides (S2/S3 mechanics only)."
    [ "${FORCE:-0}" = 1 ] || exit 3
  fi
  say "packages"
  for p in pipewire pipewire-bin pipewire-alsa wireplumber libspa-0.2-bluetooth; do
    dpkg -s "$p" >/dev/null 2>&1 && ok "$p" || bad "$p not installed — apt install --no-install-recommends $p"
  done
  if dpkg -s pipewire-pulse >/dev/null 2>&1; then
    note "pipewire-pulse IS installed here. The plan keeps it ABSENT on the"
    note "box (Q12: Soloist must fail closed, B9). Fine on the bench, but"
    note "s7's fail-closed verdict is only real with it stopped/absent."
  fi
  say "the session PipeWire (must be OFF for every test below)"
  local u; u=$(desk_user); [ "$MODE" = procs ] && { u=""; note "container mode — the HOST's session PipeWire was masked by trixie-bootstrap"; }
  if [ -n "$u" ] && user_ctl "$u" is-active pipewire.service >/dev/null 2>&1; then
    bad "user '$u' runs a session PipeWire — it owns the cards and BT"
    note "'install' stops+masks it for you; 'clean' unmasks."
  else ok "no session PipeWire for '${u:-?}'"; fi
  say "system-mode state"
  systemctl is-active pipewire.service >/dev/null 2>&1 && ok "system pipewire.service active" || note "system pipewire.service not running (run install + start)"
  [ -S "$SOCK" ] && ok "$SOCK exists" || note "$SOCK absent"
  say "WirePlumber's real component/feature names (S1b — compare with plan §B)"
  local conf=/usr/share/wireplumber/wireplumber.conf
  if [ -r "$conf" ]; then
    note "the distro's profiles (we extend '$WP_PROFILE'):"
    sed -n '/^wireplumber.profiles/,/^}/p' "$conf" | sed 's/^/         /'
    note "every feature the distro provides (the ONLY names a profile may toggle):"
    grep -oE 'provides = [a-z][a-z0-9.-]*' "$conf" | sed 's/provides = //' | sort -u | tr '\n' ' ' | fold -s -w 70 | sed 's/^/         /'
    echo
    grep -q "$WP_PROFILE" "$conf" && ok "profile '$WP_PROFILE' exists" || bad "profile '$WP_PROFILE' NOT in $conf — set WP_PROFILE"
  else note "$conf not found"; fi
}

# ---------------------------------------------------------------------------
cmd_install() {
  need_root
  local u; u=$(desk_user)
  if [ "$MODE" = procs ]; then u=""; note "procs mode: no session PipeWire to mask, no units to write"; fi
  if [ -n "$u" ]; then
    say "stopping + masking the session PipeWire of '$u'"
    user_ctl "$u" stop wireplumber pipewire-pulse pipewire pipewire.socket pipewire-pulse.socket 2>/dev/null
    user_ctl "$u" mask wireplumber pipewire-pulse pipewire pipewire.socket pipewire-pulse.socket 2>/dev/null && ok "masked"
  fi
  say "user + dirs"
  getent group bluetooth >/dev/null || groupadd -r bluetooth   # bluez creates it on the box; a bare container has none
  id "$PW_USER" >/dev/null 2>&1 || useradd -r -M -d "$STATE/pipewire" -s /usr/sbin/nologin -g audio -G bluetooth "$PW_USER"
  mkdir -p "$STATE/pipewire" "$STATE/wireplumber" /etc/vibb/wp-empty-config
  chown -R "$PW_USER:audio" "$STATE/pipewire" "$STATE/wireplumber"
  ok "user $PW_USER (groups: $(id -Gn "$PW_USER"))"

  if [ "$MODE" = units ]; then
  say "units (plan §A with AM-1: no RuntimeDirectory on the service; AM-2: wireplumber bound to pipewire)"
  cat > /etc/systemd/system/pipewire.socket <<EOF
[Unit]
Description=Vibb PipeWire (system) socket
[Socket]
Priority=6
ListenStream=$SOCK
SocketUser=$PW_USER
SocketGroup=audio
SocketMode=0660
DirectoryMode=0750
[Install]
WantedBy=sockets.target
EOF
  cat > /etc/systemd/system/pipewire.service <<EOF
[Unit]
Description=Vibb PipeWire (system-wide, owns the I2S HAT + BT A2DP)
Requires=pipewire.socket
After=pipewire.socket
[Service]
Type=simple
User=$PW_USER
Group=audio
SupplementaryGroups=bluetooth
Environment=PIPEWIRE_RUNTIME_DIR=$RUN
# NO PIPEWIRE_CONFIG_DIR: it REPLACES the search path and hides /usr/share/pipewire/pipewire.conf (bench finding 2026-09-03)
Environment=HOME=$STATE/pipewire
ExecStart=/usr/bin/pipewire
Nice=0
LimitMEMLOCK=64M
Restart=on-failure
RestartSec=2
[Install]
WantedBy=multi-user.target
Also=pipewire.socket
EOF
  cat > /etc/systemd/system/wireplumber.service <<EOF
[Unit]
Description=Vibb WirePlumber (system-wide session manager, bluez5 + alsa only)
BindsTo=pipewire.service
After=pipewire.service dbus.service
[Service]
Type=simple
User=$PW_USER
Group=audio
SupplementaryGroups=bluetooth
Environment=PIPEWIRE_RUNTIME_DIR=$RUN
Environment=XDG_STATE_HOME=$STATE/wireplumber
Environment=XDG_CONFIG_HOME=/etc/vibb/wp-empty-config
Environment=HOME=$STATE/wireplumber
ExecStart=/usr/bin/wireplumber -p $WP_PROFILE
Nice=0
Restart=on-failure
RestartSec=2
[Install]
WantedBy=pipewire.service
EOF
  ok "wrote pipewire.socket pipewire.service wireplumber.service"
  fi

  say "config fragments (plan §B on profile '$WP_PROFILE', AM-17 hw-volume=true, roles=$WP_ROLES, hooks-disable=$WP_DISABLE_HOOKS)"
  mkdir -p /etc/pipewire/pipewire.conf.d /etc/pipewire/client.conf.d /etc/wireplumber/wireplumber.conf.d
  cat > /etc/pipewire/pipewire.conf.d/10-vibb.conf <<'EOF'
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
  { factory = adapter
    args = { factory.name = support.null-audio-sink  node.name = "vibb_null"
             node.description = "vibb null sink (cache warming)"
             media.class = "Audio/Sink"  audio.position = [ FL FR ]
             monitor.channel-volumes = false  node.passive = true } }
]
EOF
  cat > /etc/pipewire/client.conf.d/10-vibb.conf <<EOF
context.properties = { remote.name = "$SOCK"  mem.allow-mlock = false  log.level = 1 }
stream.properties = {
  node.dont-reconnect = true
  node.dont-fallback  = true
  node.autoconnect    = true
}
EOF
  cat > /etc/wireplumber/wireplumber.conf.d/50-vibb.conf <<EOF
# Extends the distro's '$WP_PROFILE' profile (0.5.8 ships main-embedded =
# main + systemwide-session + stateless: no logind, no seat monitoring, no
# reserve-device, no portal, every *.state restore hook off). Only names
# from check's "provides" inventory are used here; the *.state lines are
# belt in case WP_PROFILE is main-systemwide.
wireplumber.profiles = {
  $WP_PROFILE = {
    hardware.video-capture         = disabled
    monitor.alsa-midi              = disabled
    monitor.bluez-midi             = disabled
    hooks.default-nodes.state      = disabled
    hooks.stream.state             = disabled
    hooks.device.profile.state     = disabled
    hooks.device.routes.state      = disabled
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
  bluez5.enable-hw-volume   = true
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
  rm -f /etc/wireplumber/wireplumber.conf.d/51-vibb-hooks.conf
  if [ "$WP_DISABLE_HOOKS" = 1 ]; then
    cat > /etc/wireplumber/wireplumber.conf.d/51-vibb-hooks.conf <<EOF
# AM-6 with the REAL 0.5.8 names. If WirePlumber refuses to start with this
# file, the hooks are hard-required by policy.standard and the design must
# define its own profile listing the linking hooks explicitly.
wireplumber.profiles = {
  $WP_PROFILE = {
    hooks.linking.target.find-default = disabled
    hooks.linking.target.find-best    = disabled
  }
}
EOF
    ok "wrote 51-vibb-hooks.conf (WP_DISABLE_HOOKS=1)"
  fi
  # the plan's pcm template (C): named pcm with server + playback_node
  mkdir -p /etc/alsa/conf.d
  cat > /etc/alsa/conf.d/99-vibb-bench.conf <<EOF
# bench-only: the plan's vibb_bt/vibb_local template shape, aimed at a null sink
pcm.vibb_bench {
    type pipewire
    server "$SOCK"
    playback_node "vibb_selftest_sink"
    hint.description "vibb bench: pinned pipewire pcm"
}
EOF
  ok "wrote pipewire/client/wireplumber fragments + /etc/alsa/conf.d/99-vibb-bench.conf"
  [ "$MODE" = units ] && systemctl daemon-reload
  note "next: $0 start"
}

cmd_start() {
  need_root
  if [ "$MODE" = procs ]; then
    say "starting pipewire + wireplumber as processes (container mode, root, same env as the units)"
    pkill -x wireplumber 2>/dev/null; pkill -x pipewire 2>/dev/null; sleep 1
    mkdir -p "$RUN" && chown "$PW_USER:audio" "$RUN" && chmod 0750 "$RUN"
    HOME="$STATE/pipewire" \
      setsid pipewire >/tmp/pipewire.log 2>&1 < /dev/null &
    for _ in $(seq 1 20); do [ -S "$SOCK" ] && break; sleep 0.5; done
    [ -S "$SOCK" ] && ok "$SOCK up" || { bad "$SOCK never appeared:"; tail -20 /tmp/pipewire.log | sed 's/^/       /'; return 1; }
    chmod 0660 "$SOCK"; chown "$PW_USER:audio" "$SOCK"
    XDG_STATE_HOME="$STATE/wireplumber" XDG_CONFIG_HOME=/etc/vibb/wp-empty-config HOME="$STATE/wireplumber" \
      setsid wireplumber -p "$WP_PROFILE" >/tmp/wireplumber.log 2>&1 < /dev/null &
    sleep 3
    pgrep -x wireplumber >/dev/null && ok "wireplumber running (log: /tmp/wireplumber.log)" || { bad "wireplumber died:"; tail -30 /tmp/wireplumber.log | sed 's/^/       /'; }
    grep -iE "error|fail" /tmp/wireplumber.log | head -5 | sed 's/^/       wp: /'
  else
  say "starting system-mode PipeWire"
  systemctl enable --now pipewire.socket pipewire.service wireplumber.service 2>&1 | sed 's/^/       /'
  for _ in $(seq 1 20); do [ -S "$SOCK" ] && break; sleep 0.5; done
  [ -S "$SOCK" ] && ok "$SOCK up" || { bad "$SOCK never appeared"; journalctl -u pipewire -n 20 --no-pager | sed 's/^/       /'; return 1; }
  sleep 2
  systemctl is-active wireplumber.service >/dev/null && ok "wireplumber active" || { bad "wireplumber not active:"; journalctl -u wireplumber -n 30 --no-pager | sed 's/^/       /'; }
  fi
  say "graph"
  # wpctl can block forever when the session manager never publishes its
  # metadata (seen on a 0.4 bench) — never let a status print hang the rig
  timeout 10 wpctl status 2>/dev/null | sed 's/^/       /' | head -50 || note "wpctl status timed out (10 s) — WirePlumber is up but not publishing; check journalctl -u wireplumber"
}

# ---------------------------------------------------------------------------
cmd_test() {
  need_root
  [ -S "$SOCK" ] || { bad "$SOCK absent — run install + start"; return 1; }
  local u; u=$(desk_user)
  local id_a id_b sid links rc err pid

  # ===================== S3: topology / permissions =====================
  say "S3a: who reaches the socket (root, the audio-group user, the pipewire user)"
  pw-dump >/dev/null 2>&1 && ok "root: pw-dump works" || bad "root: pw-dump FAILED"
  if [ -n "$u" ]; then
    if sudo -u "$u" env PIPEWIRE_RUNTIME_DIR="$RUN" pw-dump >/dev/null 2>&1; then ok "$u (audio group): pw-dump works"
    else bad "$u: pw-dump failed — socket perms ($(stat -c '%U:%G %a' "$SOCK"))"; fi
    if sudo -u "$u" env -u PIPEWIRE_RUNTIME_DIR pw-dump >/dev/null 2>&1; then ok "$u WITHOUT env: client.conf.d remote.name=$SOCK is honoured"
    else note "$u without env: needs PIPEWIRE_RUNTIME_DIR (the AM-5 env belt is REQUIRED, not belt)"; fi
  fi
  if sudo -u "$PW_USER" busctl --system call org.bluez / org.freedesktop.DBus.ObjectManager GetManagedObjects >/dev/null 2>&1; then
    ok "$PW_USER can call org.bluez (bluez5 monitor will register endpoints)"; verdict PASS s3a_bus
  else bad "$PW_USER cannot call org.bluez — D-Bus policy blocks the bluez5 monitor"; verdict KILL s3a_bus; fi

  say "S3b: stream.properties from client.conf.d reach a pipewire-alsa stream? (Q3)"
  id_a=$(mk_null_sink vibb_selftest_sink)
  [ -n "$id_a" ] && ok "null sink vibb_selftest_sink id=$id_a" || { bad "could not create a null sink (pw-cli create-node)"; verdict KILL s3b_sink; }
  timeout 8 aplay -q -D vibb_bench -f cd -t raw /dev/zero 2>/dev/null & pid=$!
  sleep 1.5
  sid=$(stream_nodes | head -1)
  if [ -z "$sid" ]; then bad "no stream node appeared for aplay -D vibb_bench"; verdict KILL s2a_named_pcm
  else
    ok "stream node id=$sid app=$(node_prop "$sid" application.name) target.object=$(node_prop "$sid" target.object)"
    if [ "$(node_prop "$sid" node.dont-reconnect)" = true ] && [ "$(node_prop "$sid" node.dont-fallback)" = true ]; then
      ok "node.dont-reconnect + node.dont-fallback present on the stream"; verdict PASS s3b_client_props
    else bad "stream lacks dont-reconnect/dont-fallback — client.conf.d not applied to the ALSA plugin; use PIPEWIRE_PROPS env (AM-5) as the primary"; verdict FAIL s3b_client_props; fi
    # ===================== S2a: named pcm -> pinned node =====================
    links=$(links_from "$sid")
    if [ "$links" = "vibb_selftest_sink" ]; then ok "linked to vibb_selftest_sink via playback_node + server keys"; verdict PASS s2a_named_pcm
    else bad "linked to: '${links:-nothing}' (expected vibb_selftest_sink)"; verdict KILL s2a_named_pcm; fi
  fi
  kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; sleep 0.5

  # ===================== S1c: target vanishes -> no relink =====================
  say "S1c: destroy the target mid-stream — the stream must NOT land anywhere (BLOCKER-1)"
  err=$(mktemp)
  timeout 12 aplay -q -D vibb_bench -f cd -t raw /dev/zero 2>"$err" & pid=$!
  sleep 1.5
  sid=$(stream_nodes | head -1)
  rm_node "$id_a"; id_a=""
  sleep 2
  local other; other=$(dump | jq -r '.[] | select(.type=="PipeWire:Interface:Link") | .info["input-node-id"]' | sort -u | while read -r i; do node_prop "$i" node.name; done | grep -vE '^(vibb_selftest_sink)?$' || true)
  if [ -n "$other" ]; then bad "a link to '$other' exists after the target vanished — RESCUE HAPPENED"; verdict KILL s1c_no_rescue
  else ok "no link to any other sink after the target vanished"; verdict PASS s1c_no_rescue; fi
  if kill -0 "$pid" 2>/dev/null; then
    note "aplay still alive 2 s after the destroy — waiting for it (blocked write?)"
    wait "$pid"; rc=$?
  else wait "$pid"; rc=$?; fi
  if [ "$rc" = 124 ]; then bad "aplay BLOCKED until timeout (rc=124): a dead target = a blocked write"; verdict BLOCKED s2c_errno
  else ok "aplay exited rc=$rc: $(tr '\n' ' ' <"$err" | cut -c1-120)"; verdict "ERROR(rc=$rc)" s2c_errno; fi
  note "(plan E: ERROR = today's advance-storm shape, BLOCKED = the 8 s SIGKILL shape; both are handled — record which)"
  rm -f "$err"

  # ===================== S2d: absent target at open =====================
  say "S2d: open against a node that does not exist — must fail closed"
  err=$(mktemp)
  timeout 5 aplay -q -D "$(bench_pcm vibb_does_not_exist)" -f cd -t raw /dev/zero 2>"$err" & pid=$!
  sleep 1.5
  sid=$(stream_nodes | head -1)
  links=""; [ -n "$sid" ] && links=$(links_from "$sid")
  kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
  if [ -n "$links" ]; then bad "absent target still linked to '$links' — dont-fallback NOT honoured"; verdict KILL s2d_absent_target
  elif [ -n "$sid" ]; then ok "stream node created but linked nowhere (fails closed at the graph)"; verdict PASS s2d_absent_target
  else ok "open refused: $(tr '\n' ' ' <"$err" | cut -c1-100)"; verdict PASS s2d_absent_target; fi
  rm -f "$err"

  # ===================== S1d: default sink is irrelevant; targetless streams =====================
  say "S1d: default-sink theft + a TARGETLESS stream (I2)"
  id_a=$(mk_null_sink vibb_selftest_sink); id_b=$(mk_null_sink vibb_selftest_B)
  pw-metadata 0 default.audio.sink "{ \"name\": \"vibb_selftest_B\" }" >/dev/null 2>&1 && note "default.audio.sink -> vibb_selftest_B" || note "no default metadata object (fine: policy.default-nodes disabled)"
  timeout 6 aplay -q -D vibb_bench -f cd -t raw /dev/zero 2>/dev/null & pid=$!
  sleep 1.5; sid=$(stream_nodes | head -1); links=""; [ -n "$sid" ] && links=$(links_from "$sid")
  kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
  if [ "$links" = "vibb_selftest_sink" ]; then ok "pinned stream ignored the default sink"; verdict PASS s1d_pinned_vs_default
  else bad "pinned stream linked to '${links:-nothing}' with default=vibb_selftest_B"; verdict KILL s1d_pinned_vs_default; fi
  timeout 6 aplay -q -D "$(bench_pcm -)" -f cd -t raw /dev/zero 2>/dev/null & pid=$!
  sleep 1.5; sid=$(stream_nodes | head -1); links=""; [ -n "$sid" ] && links=$(links_from "$sid")
  kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
  if [ -z "$links" ]; then ok "targetless stream linked NOWHERE"; verdict PASS s1d_targetless
  else bad "targetless stream linked to '$links' — find-default/find-best still active; needs AM-6 (WP_DISABLE_HOOKS=1) or a custom profile"; verdict KILL s1d_targetless; fi
  pw-metadata 0 default.audio.sink -d >/dev/null 2>&1
  rm_node "$id_a"; rm_node "$id_b"

  # ===================== S1a: settings names =====================
  say "S1a: settings names the plan uses (wpctl settings)"
  local s; s=$(wpctl settings 2>/dev/null || true)
  for k in linking.allow-moving-streams linking.follow-default-target node.stream.restore-props node.stream.restore-target device.restore-profile device.restore-routes bluetooth.autoswitch-to-headset-profile bluetooth.use-persistent-storage; do
    # 0.5.8 prints "- Name: <key>" then "  Value: <v>" on the next line
    if printf '%s' "$s" | grep -q "Name: $k"; then ok "$k = $(printf '%s' "$s" | grep -A1 "Name: $k" | grep -oE 'Value: .*' | head -1 | cut -c8-)"; else bad "$k NOT a known setting — fix the fragment name"; fi
  done
  printf '%s' "$s" | grep -A1 'Name: linking.follow-default-target' | grep -qiE 'Value: *false' && verdict PASS s1a_settings || verdict CHECK s1a_settings

  # ===================== S3c: crash survival (AM-1 / AM-2) =====================
  say "S3c: kill -9 pipewire — socket path must survive, wireplumber must come back"
  if [ "$MODE" = procs ]; then
    note "SKIPPED in container mode (no units) — AM-1/AM-2 are systemd facts; verify on a Trixie flash"; verdict SKIPPED s3c_crash
  else
  kill -9 "$(pidof pipewire)" 2>/dev/null; sleep 5
  if [ -S "$SOCK" ] && systemctl is-active pipewire.service >/dev/null && systemctl is-active wireplumber.service >/dev/null && pw-dump >/dev/null 2>&1; then
    ok "socket present, pipewire + wireplumber active, pw-dump works after the crash"; verdict PASS s3c_crash
  else bad "after kill -9: sock=$([ -S "$SOCK" ] && echo yes || echo NO) pipewire=$(systemctl is-active pipewire.service) wireplumber=$(systemctl is-active wireplumber.service)"; verdict KILL s3c_crash; fi
  sleep 1
  fi

  # ===================== S5: node names + prop keys =====================
  say "S5: Audio/Sink nodes and the prop keys the resolver will read"
  dump | jq -r '.[] | select(.type=="PipeWire:Interface:Node" and .info.props["media.class"]=="Audio/Sink") | .info.props | "  \(.["node.name"])  bluez.addr=\(.["api.bluez5.address"] // "-")  codec=\(.["api.bluez5.codec"] // "-")  alsa.card_name=\(.["alsa.card_name"] // "-")  api.alsa.card.name=\(.["api.alsa.card.name"] // "-")  suspend=\(.["session.suspend-timeout-seconds"] // "-")"'
  verdict RECORDED s5_names

  # ===================== S4: BT (optional) =====================
  if [ -n "$BT_MAC" ]; then
    say "S4: BT speaker $BT_MAC (roles=[$WP_ROLES])"
    local bn; bn=$(dump | jq -r --arg m "$BT_MAC" '.[] | select(.type=="PipeWire:Interface:Node" and (.info.props["api.bluez5.address"]|ascii_upcase)==($m|ascii_upcase)) | .info.props["node.name"]' | head -1)
    if [ -n "$bn" ]; then ok "node for $BT_MAC: $bn"
      case "$bn" in bluez_output.*) ok "bluez_output.* => roles=[$WP_ROLES] is the OUTPUT (host-source) role name"; verdict "PASS($WP_ROLES)" s4_roles;;
                    *) bad "node is '$bn', not bluez_output.* — wrong role"; verdict KILL s4_roles;; esac
      local bid; bid=$(node_id "$bn")
      note "codec=$(node_prop "$bid" api.bluez5.codec) suspend-timeout=$(node_prop "$bid" session.suspend-timeout-seconds)"
      [ "$(node_prop "$bid" api.bluez5.codec)" = sbc ] && verdict PASS s4_codec_sbc || verdict FAIL s4_codec_sbc
      [ "$(node_prop "$bid" session.suspend-timeout-seconds)" = 120 ] && verdict PASS s4_suspend_120 || verdict FAIL s4_suspend_120
    else bad "no node with api.bluez5.address=$BT_MAC — connect the speaker, or flip WP_ROLES (a2dp_sink <-> a2dp_source) and re-run install/start"; verdict KILL s4_roles; fi
    local tr; tr=$(busctl --system --json=short call org.bluez / org.freedesktop.DBus.ObjectManager GetManagedObjects 2>/dev/null | jq -r '.data[0] | to_entries[] | select(.value["org.bluez.MediaTransport1"]) | .value["org.bluez.MediaTransport1"].UUID.data' | sort | uniq -c)
    note "MediaTransport1 UUIDs on the bus (110a = we are the SOURCE, 110b = a peer streams INTO us):"; printf '%s\n' "$tr" | sed 's/^/         /'
    printf '%s' "$tr" | grep -q 0000110b && { bad "a 0000110b transport exists — the sink role is on"; verdict FAIL s4_no_sink_role; } || { ok "no 0000110b transport"; verdict PASS s4_no_sink_role; }
    dump | jq -r '.[] | select(.type=="PipeWire:Interface:Node") | .info.props["node.name"]' | grep -q '^bluez_input' && { bad "bluez_input.* node present (HFP/HSP or source role)"; verdict FAIL s4_no_input_nodes; } || { ok "no bluez_input.* nodes"; verdict PASS s4_no_input_nodes; }
    local players; players=$(busctl --system --json=short call org.bluez / org.freedesktop.DBus.ObjectManager GetManagedObjects 2>/dev/null | jq -r '.data[0] | to_entries[] | select(.value["org.bluez.MediaPlayer1"]) | .key')
    note "MediaPlayer1 objects: ${players:-none} (plan J: on the box exactly one, vibb-mpris; here expect none => dummy-avrcp-player=false honoured)"
    say "S4 (ears): play a tone to $bn, press the HEADSET's own volume buttons (AM-17)"
    timeout 12 aplay -q -D "$(bench_pcm "$bn")" -f cd -t raw /dev/urandom 2>/dev/null & pid=$!
    read -r -p "  [operator] Did the headset's volume buttons change the level audibly? [y/n] " a; kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
    [ "${a:-n}" = y ] && verdict PASS s4_hw_volume_buttons || verdict FAIL s4_hw_volume_buttons
    note "AVDTP Suspend count vs keep-alive=120 (B4) and the PDU/RSS numbers (B6/B11) need btmon on a Zero 2 W — not measured here."
  else
    note "S4 skipped: set BT_MAC=<paired+connected speaker> to run it"
  fi

  say "VERDICT MAP (against docs/PLAN-pipewire-soloist.md)"
  printf '%s\n' "${VERDICTS[@]}"
  note "KILL on s1c/s1d/s2a/s2d/s3c = the design section named in the plan changes before code."
}

# ---------------------------------------------------------------------------
cmd_s6() {
  # go-librespot (the fork) live /player/output reopen through pipewire-alsa (plan §C, B7, AM-13).
  local api="${GO_API:-http://127.0.0.1:3678}"
  say "S6: go-librespot live reopen via pipewire-alsa (needs the fork running with audio_backend: alsa)"
  note "1. two pcms in the fork's reach: add to /etc/alsa/conf.d/99-vibb-bench.conf"
  note "     pcm.vibb_bench_a { type pipewire server \"$SOCK\" playback_node \"vibb_selftest_sink\" }"
  note "     pcm.vibb_bench_b { type pipewire server \"$SOCK\" playback_node \"vibb_selftest_B\" }"
  note "   and create both sinks: this script's mk_null_sink, or pw-cli create-node ... object.linger=true"
  note "2. config.yml: audio_backend: alsa, audio_device: vibb_bench_a; start the fork; play a track from the phone"
  note "3. then:"
  note "     curl -s $api/status | jq '{track:.track.uri,pos:.track.position,paused:.paused}'"
  note "     curl -s -X POST $api/player/output -d '{\"device\":\"vibb_bench_b\"}'"
  note "     pw-dump | jq '[.[]|select(.type==\"PipeWire:Interface:Link\")|.info[\"input-node-id\"]]'   # moved to B?"
  note "     curl -s $api/status | jq '{track:.track.uri,pos:.track.position,paused:.paused}'   # same track, position advanced?"
  note "   PASS: link moved to vibb_selftest_B, same track/session, no re-auth in the fork's log."
  note "4. AM-13: with NO track loaded, POST $api/player/volume -d '{\"volume\":10}' — accepted (2xx)?"
  if curl -s -m 2 "$api/status" >/dev/null 2>&1; then
    ok "fork API reachable at $api — running step 4 now"
    local code; code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 -X POST "$api/player/volume" -H 'Content-Type: application/json' -d '{"volume":10}')
    note "POST /player/volume -> HTTP $code"; [ "${code:0:1}" = 2 ] && verdict PASS s6_volume_no_track || verdict FAIL s6_volume_no_track
  else note "fork API not reachable at $api — steps are manual"; fi
  printf '%s\n' "${VERDICTS[@]}"
}

cmd_s7() {
  # Soloist binding (plan §I, B9, AM-16). Needs the binary + a paired account.
  local bin="${SOLOIST:-$(command -v soloist || true)}"
  say "S7: soloist --pipewire-device (lazy vs eager stream; fail-closed on a bad target)"
  [ -n "$bin" ] || { bad "no soloist binary (SOLOIST=/path)"; return 1; }
  local id_a; id_a=$(mk_null_sink vibb_selftest_sink)
  note "1. eager or lazy? starting soloist pinned to vibb_selftest_sink, NOT playing:"
  note "     PIPEWIRE_RUNTIME_DIR=$RUN $bin --pipewire-device vibb_selftest_sink --ws 127.0.0.1:3690 -D ./data &"
  note "     sleep 5; pw-dump | jq -r '.[]|select(.type==\"PipeWire:Interface:Node\")|.info.props[\"application.name\"]' | grep -i solo"
  note "   node present before any play = EAGER (start-time bind check is authoritative); absent = LAZY (AM-16: check at first playing event)"
  note "2. play from the phone; then links_from that node must be ONLY vibb_selftest_sink"
  note "3. restart with --pipewire-device vibb_does_not_exist and play: PASS = no node linked anywhere / soloist reports an audio error;"
  note "   KILL = audio lands on some sink (that would be the Pulse fallback — is pipewire-pulse running here? it must NOT be)"
  systemctl is-active pipewire-pulse >/dev/null 2>&1 && bad "pipewire-pulse is ACTIVE — stop it or step 3 is meaningless" || ok "no pipewire-pulse"
  note "(vibb_selftest_sink id=$id_a stays until 'clean' or pw-cli destroy $id_a)"
}

cmd_clean() {
  need_root
  say "removing the system-mode PipeWire and restoring the session one"
  if [ "$MODE" = procs ]; then pkill -x wireplumber 2>/dev/null; pkill -x pipewire 2>/dev/null; rm -rf "$RUN"
  else
  systemctl disable --now wireplumber.service pipewire.service pipewire.socket 2>/dev/null
  rm -f /etc/systemd/system/pipewire.socket /etc/systemd/system/pipewire.service /etc/systemd/system/wireplumber.service
  fi
  rm -f /etc/pipewire/pipewire.conf.d/10-vibb.conf /etc/pipewire/client.conf.d/10-vibb.conf
  rm -f /etc/wireplumber/wireplumber.conf.d/50-vibb.conf /etc/wireplumber/wireplumber.conf.d/51-vibb-hooks.conf
  rm -f /etc/alsa/conf.d/99-vibb-bench.conf /etc/alsa/conf.d/98-vibb-bench-dyn.conf
  [ "$MODE" = units ] && systemctl daemon-reload
  local u; u=$(desk_user); [ "$MODE" = procs ] && u=""
  [ -n "$u" ] && user_ctl "$u" unmask wireplumber pipewire-pulse pipewire pipewire.socket pipewire-pulse.socket 2>/dev/null && note "session PipeWire of '$u' unmasked (start it: systemctl --user start pipewire wireplumber)"
  note "left in place: user '$PW_USER', $STATE/{pipewire,wireplumber} (harmless)"
  ok "clean"
}

# --------------------------- Trixie container (host side) ---------------------------
cmd_trixie_bootstrap() {
  need_root
  say "Trixie userspace at $CT via mmdebstrap (the REAL 1.4/0.5 binaries on a Bookworm host)"
  command -v mmdebstrap >/dev/null 2>&1 && command -v systemd-nspawn >/dev/null 2>&1 \
    || { note "installing mmdebstrap + systemd-container"; apt-get install -y --no-install-recommends mmdebstrap systemd-container >/dev/null || { bad "apt failed"; return 1; }; }
  if [ -x "$CT/usr/bin/wireplumber" ]; then ok "$CT already bootstrapped"
  else
    mmdebstrap --variant=apt --architectures="$(dpkg --print-architecture)" \
      --include=pipewire,pipewire-bin,pipewire-alsa,wireplumber,libspa-0.2-bluetooth,alsa-utils,jq,dbus,procps,psmisc,sudo,ca-certificates,curl \
      trixie "$CT" http://deb.debian.org/debian 2>&1 | tail -5 | sed 's/^/       /'
    [ -x "$CT/usr/bin/wireplumber" ] && ok "bootstrapped" || { bad "bootstrap failed"; return 1; }
  fi
  mkdir -p "$CT/rig" && cp "$0" "$CT/rig/" && chmod +x "$CT/rig/$(basename "$0")" && ok "rig copied to $CT/rig/"
  chroot "$CT" /usr/sbin/groupadd -r bluetooth 2>/dev/null
  chroot "$CT" /usr/sbin/useradd -r -M -s /usr/sbin/nologin -g audio -G bluetooth "$PW_USER" 2>/dev/null; true
  local u; u=$(desk_user)
  if [ -n "$u" ]; then
    say "stopping + masking the HOST session PipeWire of '$u' (it owns the cards + BT endpoint)"
    user_ctl "$u" stop wireplumber pipewire-pulse pipewire pipewire.socket pipewire-pulse.socket 2>/dev/null
    user_ctl "$u" mask wireplumber pipewire-pulse pipewire pipewire.socket pipewire-pulse.socket 2>/dev/null && ok "masked ('trixie-clean' unmasks)"
  fi
  note "next: $0 trixie-shell"
}

cmd_trixie_shell() {
  need_root
  [ -x "$CT/usr/bin/wireplumber" ] || { bad "no container at $CT — run trixie-bootstrap"; return 1; }
  # a container left behind (closed terminal, no `exit`) keeps the tree busy
  machinectl terminate vibb-trixie 2>/dev/null && { note "terminated a leftover vibb-trixie container"; sleep 2; }
  say "entering $CT (host /dev/snd, /run/udev, system D-Bus bound in; VIBB_RIG_MODE=procs)"
  note "inside:  cd /rig && ./$(basename "$0") check && ./$(basename "$0") install && ./$(basename "$0") start && ./$(basename "$0") test"
  exec systemd-nspawn -D "$CT" --machine=vibb-trixie --resolv-conf=copy-host \
    --bind=/dev/snd --bind-ro=/run/udev --bind=/run/dbus/system_bus_socket \
    --property=DeviceAllow="char-alsa rwm" --capability=CAP_SYS_NICE,CAP_SYS_RESOURCE \
    --setenv=VIBB_RIG_MODE=procs --chdir=/rig -- /bin/bash
}

cmd_trixie_clean() {
  need_root
  pkill -x wireplumber 2>/dev/null; pkill -x pipewire 2>/dev/null
  machinectl terminate vibb-trixie 2>/dev/null
  local u; u=$(desk_user)
  [ -n "$u" ] && user_ctl "$u" unmask wireplumber pipewire-pulse pipewire pipewire.socket pipewire-pulse.socket 2>/dev/null && note "host session PipeWire unmasked (systemctl --user start pipewire wireplumber)"
  ok "container daemons stopped; $CT kept (rm -rf it yourself if you want the space back)"
}

case "${1:-check}" in
  trixie-bootstrap) cmd_trixie_bootstrap ;;
  trixie-shell)     cmd_trixie_shell ;;
  trixie-clean)     cmd_trixie_clean ;;
  check)   cmd_check ;;
  install) cmd_install ;;
  start)   cmd_start ;;
  test)    cmd_test ;;
  s6)      cmd_s6 ;;
  s7)      cmd_s7 ;;
  clean)   cmd_clean ;;
  *) echo "usage: $0 {check|install|start|test|s6|s7|clean|trixie-bootstrap|trixie-shell|trixie-clean}"; exit 2 ;;
esac
