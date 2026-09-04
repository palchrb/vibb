#!/usr/bin/env bash
#
# Vibb test rig — install script for Raspberry Pi Zero 2 W (Raspberry Pi OS, Trixie/Bookworm)
#
# Installs:
#   - go-librespot : Spotify Connect daemon with a local HTTP API (this is our
#                    "librespot" — same job, but controllable via curl, which is
#                    what lets play.sh start a track from a share link)
#   - BlueZ + bluez-alsa : Bluetooth stack + ALSA bridge for A2DP headphones
#   - mpv + yt-dlp : local files, NRK and internet radio playback
#   - PN532 RFID support (vibb-rfid daemon) + vibb-card / vibb-power tools
#
# Then walks you through Spotify login (zeroconf): you pick the device in the
# Spotify app on your phone, credentials are persisted on the Pi.
#
# The script is idempotent: re-running skips everything already done, so it
# doubles as an updater after git pull. Use --update to force re-downloading
# go-librespot and upgrading the python libs.
#
# Usage:  sudo ./install.sh [--update] [--bluealsa|--pipewire] [--librespot|--soloist]
# After:  sudo ./play.sh connect
#         sudo ./play.sh "https://open.spotify.com/track/..."

set -euo pipefail

API_PORT=3678
UPDATE=0
usage() {
  cat <<'USAGE'
usage: install.sh [--update] [--bluealsa|--pipewire] [--librespot|--soloist]

  --update      re-install the python libs too (the slow path)
  --bluealsa    audio stack: bare ALSA + bluealsa (default, today's box)
  --pipewire    audio stack: PipeWire + WirePlumber, bluealsa masked
                (rollback: re-run with --bluealsa)
  --librespot   Spotify engine: the go-librespot fork (default)
  --soloist     Spotify engine: Spotify's official headless client
                (needs --pipewire; the soloistd sidecar is not built yet)

Both toggles are also settable as VIBB_AUDIO_STACK / VIBB_SPOTIFY_ENGINE,
and both are remembered in /etc/vibb/ so a bare re-run keeps your choice.
USAGE
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --update)    UPDATE=1 ;;
    --bluealsa)  VIBB_AUDIO_STACK=bluealsa ;;
    --pipewire)  VIBB_AUDIO_STACK=pipewire ;;
    --librespot) VIBB_SPOTIFY_ENGINE=golibrespot ;;
    --soloist)   VIBB_SPOTIFY_ENGINE=soloist ;;
    -h|--help)   usage; exit 0 ;;
    *) echo "install.sh: unknown option '$1'" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
export VIBB_AUDIO_STACK="${VIBB_AUDIO_STACK:-}" VIBB_SPOTIFY_ENGINE="${VIBB_SPOTIFY_ENGINE:-}"

# The Spotify engine is an INSTALL-TIME toggle like the audio stack
# (PLAN-soloistd.md): one engine is provisioned, and the daemon's ~30
# REST call sites are engine-blind through VIBB_GO_API while every
# systemctl call goes through paths.go_unit_cmd()/VIBB_GO_UNIT. Resolved
# FIRST so an engine we cannot provision refuses before anything is
# touched, not halfway through.
SPOTIFY_ENGINE="$VIBB_SPOTIFY_ENGINE"
if [[ -z $SPOTIFY_ENGINE && -r /etc/vibb/spotify-engine ]]; then
  SPOTIFY_ENGINE="$(tr -d '[:space:]' < /etc/vibb/spotify-engine)"
fi
SPOTIFY_ENGINE="${SPOTIFY_ENGINE:-golibrespot}"
case "$SPOTIFY_ENGINE" in
  golibrespot) ;;
  soloist)
    echo "install.sh: --soloist is not available yet." >&2
    echo "  The soloistd sidecar (docs/PLAN-soloistd.md P1) is designed but" >&2
    echo "  NOT built: nothing would provision or supervise the engine." >&2
    echo "  It also requires --pipewire (Soloist has no ALSA backend) and a" >&2
    echo "  personal Soloist API key from a Premium account." >&2
    exit 2 ;;
  *)
    echo "install.sh: VIBB_SPOTIFY_ENGINE must be golibrespot or soloist" >&2
    exit 2 ;;
esac

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo $0" >&2
  exit 1
fi

# Box name -> <name>.local (mDNS) and the Spotify device name. Default is
# "vibb"; override with VIBB_NAME=<name>, or answer the one-time prompt
# on the first install. Re-runs keep whatever avahi already advertises.
BOX_NAME="${VIBB_NAME:-}"
if [[ -z $BOX_NAME ]]; then
  if [[ -f /etc/avahi/avahi-daemon.conf ]] \
      && grep -q '^host-name=' /etc/avahi/avahi-daemon.conf; then
    BOX_NAME="$(sed -n 's/^host-name=//p' /etc/avahi/avahi-daemon.conf | head -n1)"
  elif [[ -t 0 ]]; then
    read -r -p "Name for this box (=> <name>.local) [vibb]: " BOX_NAME || true
  fi
fi
BOX_NAME="$(echo "${BOX_NAME:-vibb}" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9-')"
[[ -n $BOX_NAME ]] || BOX_NAME=vibb
if [[ $BOX_NAME == vibb ]]; then
  DEVICE_NAME="Vibb Test"
else
  DEVICE_NAME="Vibb ($BOX_NAME)"
fi

RUN_USER="${SUDO_USER:-pi}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
CONF_DIR="$RUN_HOME/.config/go-librespot"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- helpers -----------------------------------------------------------------

have_pkg() { dpkg -s "$1" >/dev/null 2>&1; }

# Replace <dest> with stdin only if content differs. Returns 0 when changed.
write_if_changed() {
  local dest="$1" tmp
  tmp="$(mktemp)"
  cat > "$tmp"
  if cmp -s "$tmp" "$dest" 2>/dev/null; then
    rm -f "$tmp"
    return 1
  fi
  mkdir -p "$(dirname "$dest")"
  # mktemp creates 600 and mv keeps it — systemd then warns that unit
  # files are 'world-inaccessible'. World-readable is right for all of
  # these (units, ALSA config, helper scripts hold no secrets).
  chmod 644 "$tmp"
  mv "$tmp" "$dest"
  return 0
}

# Show the box token + the link a phone can open. Printed at the end of
# every run: it is the recovery path when a screen breaks, and the only
# copy anyone gets on a box whose screen isn't enabled.
# Point at the token, never print it. The secret only appears when
# someone explicitly runs `vibb-token` — an install log (or a
# scrollback shared while debugging) shouldn't carry it.
print_token() {
  echo
  echo "    Pair a phone:   on the box, Settings -> Link phone (scan the QR)"
  echo "    Or over ssh:    sudo vibb-token"
}

# install(1) only if content differs. Returns 0 when changed.
install_if_changed() {  # <mode> <src> <dest>
  if cmp -s "$2" "$3" 2>/dev/null; then
    return 1
  fi
  install -m "$1" "$2" "$3"
  return 0
}

# --- 1. packages -------------------------------------------------------------

PKGS=(bluez bluez-alsa-utils libasound2-plugin-bluez alsa-utils curl jq
      mpv yt-dlp python3-venv python3-dev i2c-tools fonts-dejavu-core
      fonts-noto-color-emoji               # CBDT bitmap strikes: ui.py
                                           # renders real emoji sprites
      avahi-daemon ffmpeg netcat-openbsd   # ffmpeg: HLS->m4a episode cache;
                                           # nc: power.sh battery logger
      openssl                              # storytel.py AES-encrypts the
                                           # login password (system python
                                           # has no AES); normally present
      restic rclone                        # backup.py: restic snapshots the
                                           # box's irreplaceable state
                                           # (authenticated encryption, dedup,
                                           # retention), rclone is the bridge
                                           # to whatever storage the owner
                                           # picked. From apt, not a vendored
                                           # binary, so both get security
                                           # updates with everything else
      python3-dbus python3-gi)             # BlueZ D-Bus backend (PLAN-bt-dbus.md)
# Audio stack (PLAN-pipewire-soloist.md): bluealsa (default) or pipewire.
# The toggle lives in audio-stack.sh; bluealsa's packages stay in PKGS on
# BOTH stacks — masked, never removed, so an offline rollback always works.
. "$SCRIPT_DIR/audio-stack.sh"
audio_stack_resolve
mkdir -p /etc/vibb && printf '%s\n' "$SPOTIFY_ENGINE" > /etc/vibb/spotify-engine
echo "    spotify engine: $SPOTIFY_ENGINE (/etc/vibb/spotify-engine)"
# shellcheck disable=SC2207
PKGS+=($(audio_stack_packages))
missing=()
for p in "${PKGS[@]}"; do have_pkg "$p" || missing+=("$p"); done
if ((${#missing[@]})); then
  echo "==> [1/8] Installing packages: ${missing[*]}"
  apt-get update
  # --no-install-recommends matters on a headless box: mpv otherwise drags in
  # icon themes, GTK and assorted desktop bits it never uses.
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
else
  echo "==> [1/8] Packages already installed — skipping apt"
fi

# --- 2. go-librespot ---------------------------------------------------------

# Vibb fork: upstream + on-disk cache for the encrypted audio files, so a
# track a kid replays is downloaded once instead of every time (bandwidth on
# hotspots; NOT offline playback — the session and audio keys are still live).
GO_LIBRESPOT_REPO="palchrb/go-librespot"
GO_LIBRESPOT_VERSION="v0.2.2"  # v0.0.8 Fast skip: debounced next/prev (a
# burst of N presses costs 2 audio-key requests instead of N) + a circuit
# breaker on throttled keys (aes code 2 -> one retry + stop, never the
# 51-track walk that kept the account rate-limited); /status gains
# pending_track_uri. v0.0.9: the settle wait no longer counts as playback
# position (settled tracks started ~401ms in instead of 0:00). v0.1.0:
# metadata batch+cache — /status gains pending_track and next_track (full
# name/artists/cover), backed by a whole-context metadata sweep at load.
# v0.1.1/v0.1.2: GET /context/tracks — the cached track listing for
# playlists AND albums (the now-view song picker), no Web API involved.
# v0.1.3/v0.1.4: album listings actually carry metadata (v0.1.2 returned
# track:null for every album row -> the picker showed nothing; v0.1.4 is
# the second take on the same fix, fork #15). v0.1.5: setupPcm refreshes
# the process ALSA config (snd_config_update_free_global) before every
# PCM open, so a speaker swap's rewritten asound.conf is honored by the
# running process — the silent-box bug 2026-07-27 — on both the play and
# live-reopen paths. This is what made the daemon's swap-restart guard
# deletable. v0.1.6: skip_debounce_ms default 400 -> 800 (fork #17) —
# the first skip of a burst still loads inline, so a single press is as
# fast as ever; the wider window just catches more of a mash in one
# audio-key request. We deliberately do NOT pin the value in config.yml:
# the fork's default is the tuning knob. Also adds debug-level logging
# of each debounce decision (silent at our log level, there when
# tuning). v0.1.7 (#18-#21): /context/tracks enumerates ANY playable
# context (artists too, not just playlist/album) and never blocks on
# the network — the first call for an unknown context returns
# ready=false with an empty listing while enumeration runs behind it,
# and snapshot_id is gone (we never read it; the /cache/snapshot one is
# a different endpoint and unchanged). spotify.context_tracks polls for
# that, or the song picker opens empty. Also: only the FIRST deferral
# of a skip burst PUTs connect state upstream, which is what was
# drawing 429s during a mash — local /status still sees every move.
# v0.1.8: upstream sync (fork #23) — two SESSION fixes (a permanently
# lost AP/dealer connection tears down and rebuilds the session like a
# logout instead of leaving the zombie behind 'spotify session is
# empty' replays; session management made atomic), plus podcast resume
# (server-side, episodes only — an explicit play position still wins:
# the API seek runs AFTER the resume lookup), an MP3 decoder (what
# many Spotify podcast episodes actually ship as) and codec/bitrate
# fields in /status (additive; nothing here reads them yet). v0.1.9
# (fork #24): /context/tracks lists EPISODES too, so a show enumerates
# — the song picker and the PWA queue show a podcast's episodes with no
# vibb change; and the Liked Songs collection
# (spotify:user:<id>:collection) is accepted, matched by to_uri here.
# Audiobooks are wired upstream but untested (unavailable market).
# v0.2.0 (fork #25): upstream sync — the API server is now CODEGENED
# from api-spec.yml (verified: every field vibb reads keeps its json
# name — track/artist_names/album_cover_url/pending_track/next_track/
# ready/cached etc.), unsupported URIs are rejected early with the
# user-scoped collection form recognized explicitly, richer track
# metadata in the CONNECT state (phone display, not our API), and an
# MP3-decoder build fix. v0.2.2: upstream alignment (50 commits),
# verified non-breaking against every surface vibb touches — all 15
# config.yml keys intact (schema additions are optional:
# prefer_firewall_friendly_ports, credentials.device_auth, pipe
# wait-for-reader), /status field set unchanged, every /player verb +
# skip_to_uri + /context/tracks + pending_track_uri/pending_track/
# next_track + the disk cache all present at the tag; the one REMOVED
# endpoint (/web-api/{path}) has zero callers in vibb. Wins we care
# about: bounded dealer/AP retry loops + player-close fixes (the
# zombie-session family), skip_to_uri now finds tracks already in the
# queue, bounded end-of-track advance, truncated-audio-key and
# decryption-race fixes.
GL_VERSION_FILE=/usr/local/bin/.go-librespot.version

if [[ -x /usr/local/bin/go-librespot && $UPDATE -eq 0 \
      && "$(cat "$GL_VERSION_FILE" 2>/dev/null)" == "$GO_LIBRESPOT_REPO $GO_LIBRESPOT_VERSION" ]]; then
  echo "==> [2/8] go-librespot $GO_LIBRESPOT_VERSION already installed — skipping"
else
  echo "==> [2/8] Downloading go-librespot ($GO_LIBRESPOT_REPO $GO_LIBRESPOT_VERSION)..."
  case "$(uname -m)" in
    aarch64)        ASSET="go-librespot_linux_arm64.tar.gz" ;;
    armv6l|armv7l)  ASSET="go-librespot_linux_armv6_rpi.tar.gz" ;;
    x86_64)         ASSET="go-librespot_linux_x86_64.tar.gz" ;;
    *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
  esac
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  curl -fL --retry 3 -o "$TMP/gl.tar.gz" \
    "https://github.com/$GO_LIBRESPOT_REPO/releases/download/$GO_LIBRESPOT_VERSION/$ASSET"
  tar -xzf "$TMP/gl.tar.gz" -C "$TMP"
  install -m 755 "$(find "$TMP" -type f -name go-librespot | head -n1)" /usr/local/bin/go-librespot
  echo "$GO_LIBRESPOT_REPO $GO_LIBRESPOT_VERSION" > "$GL_VERSION_FILE"
  GO_RESTART_NEEDED=1  # new binary -> restart the service in step 6
fi

# --- 3. configs --------------------------------------------------------------

echo "==> [3/8] ALSA + go-librespot config..."
# bluealsa only: under pipewire `audio_stack_route` (after the package
# install below) writes asound.conf with both pcm names pinned to graph nodes.
if [[ $AUDIO_STACK == bluealsa ]]; then
# Placeholder ALSA device: play.sh rewrites this with your headset's MAC.
if [[ ! -e /etc/asound.conf ]] || ! grep -q "bluealsa\|vibb_bt" /etc/asound.conf; then
  cat > /etc/asound.conf <<'EOF'
# Managed by vibb pi/play.sh — replaced with a bluealsa device on first connect
pcm.vibb_bt {
    type plug
    slave.pcm "null"
}
# Built-in/HAT speaker (Pirate Audio / Amp SHIM, MAX98357A over I2S).
# Needs dtoverlay=hifiberry-dac (sudo vibb-power hat-audio-on) + reboot.
pcm.vibb_local {
    type plug
    slave.pcm "hw:sndrpihifiberry"
}
EOF
  echo "    wrote placeholder /etc/asound.conf"
fi
# Migration: older asound.conf versions lack the vibb_local pcm
if ! grep -q "pcm\.vibb_local" /etc/asound.conf 2>/dev/null; then
  cat >> /etc/asound.conf <<'EOF'
# Built-in/HAT speaker (Pirate Audio / Amp SHIM, MAX98357A over I2S).
# Needs dtoverlay=hifiberry-dac (sudo vibb-power hat-audio-on) + reboot.
pcm.vibb_local {
    type plug
    slave.pcm "hw:sndrpihifiberry"
}
EOF
  echo "    added vibb_local pcm to /etc/asound.conf"
fi
fi  # bluealsa placeholder

if [[ -f "$CONF_DIR/config.yml" ]]; then
  echo "    keeping existing $CONF_DIR/config.yml (delete it to regenerate)"
else
  mkdir -p "$CONF_DIR"
  cat > "$CONF_DIR/config.yml" <<EOF
device_name: "$DEVICE_NAME"
device_type: speaker
bitrate: 160  # 96 | 160 | 320 (kbps, Ogg Vorbis)
audio_backend: alsa
audio_device: vibb_bt
server:
  enabled: true
  address: localhost
  port: $API_PORT
zeroconf_enabled: true
disable_autoplay: true  # playlist ends -> silence, not algorithm radio
# Vibb fork feature: encrypted audio files cached on disk (repeat plays
# skip the CDN download; still requires a live session + audio key).
cache:
  enabled: true
  dir: /var/lib/vibb/spotify-cache
  size_limit: 20GB
credentials:
  type: zeroconf
  zeroconf:
    persist_credentials: true
EOF
  chown -R "$RUN_USER:" "$CONF_DIR"
fi

# Config migrations for existing installs (append-only, idempotent).
# Kids box: when a playlist/album ends we want silence, not Spotify's
# algorithmic autoplay picking "similar" tracks.
if [[ -f "$CONF_DIR/config.yml" ]] && ! grep -q '^disable_autoplay:' "$CONF_DIR/config.yml"; then
  echo 'disable_autoplay: true  # playlist ends -> silence, not algorithm radio' >> "$CONF_DIR/config.yml"
  echo "    config: added disable_autoplay: true"
  GO_RESTART_NEEDED=1
fi
# Audio cache (Vibb fork feature) — see the config template above.
if [[ -f "$CONF_DIR/config.yml" ]] && ! grep -q '^cache:' "$CONF_DIR/config.yml"; then
  cat >> "$CONF_DIR/config.yml" <<'EOF'
cache:
  enabled: true
  dir: /var/lib/vibb/spotify-cache
  size_limit: 20GB
EOF
  echo "    config: enabled the audio cache (20GB, /var/lib/vibb/spotify-cache)"
  GO_RESTART_NEEDED=1
fi
# The cache dir must be writable by the service user (go-librespot runs
# as $RUN_USER, unlike the root-owned vibb daemons).
mkdir -p /var/lib/vibb/spotify-cache
chown "$RUN_USER:" /var/lib/vibb/spotify-cache

# Persistent journal: a wedged box always gets a power cycle, and the
# default volatile journal loses exactly the evidence we need afterwards
# (BT firmware crashes, watchdog decisions). bt.py's crash detection
# reads the kernel journal too. 64M (was 200M): with the noise cuts
# below (bluealsa at warning, NetworkManager at WARN) that still holds
# days of history, prunes oldest-first, and wears the SD card less.
# Small files = finer-grained rotation, so pruning frees space sooner.
if write_if_changed /etc/systemd/journald.conf.d/vibb.conf <<'EOF'
[Journal]
Storage=persistent
SystemMaxUse=64M
SystemMaxFileSize=8M
EOF
then
  systemctl restart systemd-journald 2>/dev/null || true
  journalctl --flush 2>/dev/null || true  # move the boot's volatile logs to disk
  journalctl --vacuum-size=64M >/dev/null 2>&1 || true  # shrink NOW, not at rotation
  echo "    journald: persistent storage, capped at 64M"
fi

# NetworkManager's <info> stream (state machine per interface, DHCP
# transactions, plugin loads) is one of the biggest journal writers on
# the box. The field evidence we actually diagnose with — association,
# 4-way handshake, deauth reason codes — comes from wpa_supplicant,
# which keeps logging. NM warnings/errors still land.
if write_if_changed /etc/NetworkManager/conf.d/90-vibb-logging.conf <<'EOF'
[logging]
level=WARN
EOF
then
  systemctl try-reload-or-restart NetworkManager 2>/dev/null || true
  echo "    NetworkManager log level WARN (wpa_supplicant evidence unaffected)"
fi

# A dead ssh peer must not pin the box awake: idle.py holds auto-off
# while a session is ESTABLISHED, and a laptop that SUSPENDS mid-session
# leaves its socket established until the kernel keepalive reaps it —
# ~2h15m of full idle draw every time a lid closes on a debug session
# (power audit 2026-08-10 #1). ClientAlive makes sshd probe the client
# so a dead peer is closed within ~4 min; a live shell is never touched.
# Drop-ins are Include'd at the TOP of sshd_config on Debian and sshd is
# first-value-wins, so this beats the stock file.
if write_if_changed /etc/ssh/sshd_config.d/vibb-keepalive.conf <<'EOF'
ClientAliveInterval 60
ClientAliveCountMax 3
EOF
then
  systemctl try-reload-or-restart ssh 2>/dev/null || true
  echo "    sshd: dead sessions reaped in ~4min (was ~2h15m holding idle auto-off)"
fi

# Per-wifi-profile tuning on every saved network. Two knobs, both
# best-effort (netmgmt._tune_profile does the same for profiles created
# later via the portal/PWA):
#   1. Background scanning OFF (review R1): NM passes bgscan
#      "simple:30:-70" to wpa_supplicant — below -70 dBm that is a full
#      13-channel off-channel sweep (~1.5-2s of radio absence) every
#      30s, a macroscopic burst that bypasses the whole BUSY/PAGING
#      marker system and can stutter A2DP mid-stream. The box lives on
#      ONE home AP: roaming scans buy nothing.
#   2. IPv6 DISABLED (energy/boot review 2026-07-20): the box is IPv4
#      end to end, but with only a link-local fe80:: and no global
#      route, go-librespot tried Spotify over IPv6 at boot and fatal-
#      crashed 'network is unreachable' before systemd restarted it on
#      IPv4. Disabling IPv6 on the connection removes the trap; nothing
#      on the box (daemon 0.0.0.0, go-librespot/pisugar 127.0.0.1,
#      avahi over IPv4) needs it, and dual-stack networks use IPv4 too.
# Applies on the next activation. Idempotent: each knob is read first
# and only written (and logged) when it actually differs, so a re-run
# that changes nothing stays silent and doesn't rewrite the profile.
# Guarded: an older NM lacking either property just skips with a note.
BGSCAN_MISSING=0
while IFS= read -r c; do
  [[ -n $c ]] || continue
  if [[ "$(nmcli -g ipv6.method connection show "$c" 2>/dev/null)" != disabled ]]; then
    nmcli connection modify "$c" ipv6.method disabled 2>/dev/null \
      && echo "    wifi '$c': IPv6 off (IPv4-only box; no boot-time v6 stalls)"
  fi
  # bgscan: distinguish 'property unsupported on this NM' (query exits
  # nonzero -> the rig NOTE) from 'supported but already empty' (exit 0,
  # blank -> nothing to do, stay silent) from 'set -> clear it once'.
  # The query MUST sit in the `if` condition, not a bare assignment:
  # under `set -e` a `x="$(cmd-that-fails)"` aborts the whole script,
  # and on this rig the property IS unsupported (nmcli exits nonzero) —
  # which killed install after [3/8], skipping the service restarts
  # (regression from the idempotent rewrite, fixed 2026-07-20).
  if cur_bg="$(nmcli -g 802-11-wireless.bgscan connection show "$c" 2>/dev/null)"; then
    if [[ -n $cur_bg ]]; then
      nmcli connection modify "$c" 802-11-wireless.bgscan "" 2>/dev/null \
        && echo "    wifi '$c': background scanning off (no mid-stream sweeps)"
    fi
  else
    BGSCAN_MISSING=1
  fi
done < <(nmcli -t -f NAME,TYPE connection show 2>/dev/null \
           | awk -F: '$2=="802-11-wireless"{print $1}')
if [[ $BGSCAN_MISSING = 1 ]]; then
  echo "    NOTE: this NetworkManager lacks 802-11-wireless.bgscan —"
  echo "          verify on the box: wpa_cli -i wlan0 status (rig task)"
fi

# avahi: keep vibb.local, drop the extra advertisement chatter — every
# multicast answer wakes the radio after each DTIM regardless of power
# save (review P7)
if [[ -f /etc/avahi/avahi-daemon.conf ]]; then
  AVAHI_CHANGED=0
  grep -q '^publish-workstation=no' /etc/avahi/avahi-daemon.conf || {
    sed -i 's/^#\?publish-workstation=.*/publish-workstation=no/' \
        /etc/avahi/avahi-daemon.conf
    grep -q '^publish-workstation=no' /etc/avahi/avahi-daemon.conf \
      || sed -i '/^\[publish\]/a publish-workstation=no' \
             /etc/avahi/avahi-daemon.conf
    AVAHI_CHANGED=1
  }
  [[ $AVAHI_CHANGED = 1 ]] && {
    systemctl try-reload-or-restart avahi-daemon 2>/dev/null || true
    echo "    avahi: workstation advertisement off (less multicast chatter)"
  }
fi

# --- 4. bluetooth + go-librespot services ------------------------------------

echo "==> [4/8] Services (bluetooth, bluealsa, go-librespot, bt-reconnect)..."
usermod -aG audio,bluetooth "$RUN_USER" || true
rfkill unblock bluetooth 2>/dev/null || true
# Unblock the radio BEFORE bluetoothd powers the adapter — systemd-rfkill
# persists a soft-block across reboots, and until now the only boot-time
# unblock lived in btwatchd's (slow) python start: the box was DEAF to
# the headset's inbound reconnect for the first 25-40s of every boot
# (field 2026-07-18 20:16: headset on since before boot, first inbound
# window missed, connected a full retry cycle later). '-' = a missing
# rfkill binary must never block bluetoothd itself.
if write_if_changed /etc/systemd/system/bluetooth.service.d/vibb-rfkill.conf <<'EOF'
[Service]
ExecStartPre=-/usr/sbin/rfkill unblock bluetooth
EOF
then
  systemctl daemon-reload
  echo "    bluetooth: rfkill unblock runs before bluetoothd (radio listens from boot)"
fi
# bluetoothd dying WITHOUT the firmware-crash kernel signature has no
# healer: btwatchd goes passive on adapter loss by design (PLAN-bt-dbus.md
# §1) and the daemon-side recovery only fires on the crash signature — a
# plain bluetoothd segfault left the box speakerless until reboot (review
# 2026-07-18 R6). Debian ships bluetooth.service with no Restart= at all;
# on-failure covers exactly the uncovered case, while clean stops (the
# recovery's own systemctl stop, an operator's) stay stopped.
if write_if_changed /etc/systemd/system/bluetooth.service.d/vibb-restart.conf <<'EOF'
[Service]
Restart=on-failure
RestartSec=5
EOF
then
  systemctl daemon-reload
  echo "    bluetooth: bluetoothd restarts itself after a crash (on-failure)"
fi
systemctl enable --now bluetooth.service
# The audio stack: bluealsa's daemon (today) or the PipeWire system units —
# and the rollback between them. Everything in audio-stack.sh.
audio_stack_apply

if [[ $AUDIO_STACK == bluealsa ]]; then  # the keep-alive is a bluealsa knob
# A2DP transport keep-alive: stock bluealsa tears the transport down the
# instant the last PCM client closes ('keep-alive: 0 ms' in the journal),
# so every switch to the built-in speaker and back — and every pause/play
# or episode change — forced a full AVDTP renegotiation: signalling load
# on the SHARED wifi/bt radio (the Zero 2 W firmware-crash trigger), plus
# the headset's reconnect chime each time. Holding the transport lets a
# play-within-the-window reuse it: no renegotiation, no chime. Default
# 120s covers realistic pauses/episode gaps; override VIBB_BT_KEEPALIVE
# (0 disables). A live-but-silent transport keeps the radio out of sleep,
# so this is a small standing battery cost — hence not maxed out.
BT_KEEPALIVE="${VIBB_BT_KEEPALIVE:-120}"
BA_UNIT=""
for u in bluealsa.service bluealsad.service; do
  systemctl cat "$u" >/dev/null 2>&1 && BA_UNIT="$u" && break
done
fi
if [[ -n $BA_UNIT ]]; then
  # first ExecStart= in systemctl cat is the distro unit's own line (our
  # drop-in, if present, appears after) — so re-runs read the base line
  # and write_if_changed re-applies only when the VALUE actually changes
  exec_line="$(systemctl cat "$BA_UNIT" | grep -m1 '^ExecStart=' \
                 | sed -e 's/ --keep-alive=[0-9]*//' \
                       -e 's/ --loglevel=[a-z]*//' || true)"
  if [[ -n $exec_line ]]; then
    extra=""
    [[ $BT_KEEPALIVE -gt 0 ]] && extra+=" --keep-alive=$BT_KEEPALIVE"
    # bluealsa's per-PCM chatter (a dbus.c/transport line for every open,
    # close, pause and codec step — ~40 lines per play/pause) is the
    # single biggest journal writer on the box. Warnings and errors (the
    # lines we diagnose with: socket disconnects, SBC config failures)
    # still land. Guarded: 3.x builds don't have the flag.
    ba_bin="${exec_line#ExecStart=}"; ba_bin="${ba_bin%% *}"
    if [[ -x $ba_bin ]] && "$ba_bin" --help 2>&1 | grep -q -- --loglevel; then
      extra+=" --loglevel=warning"
    fi
    if [[ -n $extra ]]; then
      if write_if_changed "/etc/systemd/system/$BA_UNIT.d/keep-alive.conf" <<KEEPALIVE
[Service]
ExecStart=
$exec_line$extra
KEEPALIVE
      then
        systemctl daemon-reload
        systemctl restart "$BA_UNIT"
        echo "    bluealsa:${extra} (transport survives pauses; journal quiet)"
      fi
    fi
  fi
fi

GO_CHANGED=0
write_if_changed /etc/systemd/system/go-librespot.service <<EOF && GO_CHANGED=1
[Unit]
Description=go-librespot Spotify Connect daemon
# After vibb-rtc so a TLS handshake to the Spotify AP never runs against
# an unset (1970) clock on the RTC-less Zero — a cert notBefore failure
# there would exit go-librespot straight into the retry backoff.
After=network-online.target bluetooth.service vibb-rtc.service
Wants=network-online.target
# A single early exit (a DNS blip, dealer/audio-key warmup, a clock/TLS
# hiccup) must NOT cost 30s of silence on the Spotify path — retry in 2s.
# The burst limit still stops a genuinely broken loop; the ExecStartPre
# DNS wait keeps the happy path a single clean start, so this only spaces
# retries after a real crash (rig 2026-07-18).
StartLimitIntervalSec=90
StartLimitBurst=6

[Service]
User=$RUN_USER
$(audio_stack_unit_env)
# network-online.target fires the instant NetworkManager 'finishes
# starting' — before wlan0 has a DHCP lease AND before systemd-resolved
# answers, so at boot go-librespot's first Spotify AP lookup hit a dead
# resolver ('apresolve.spotify.com on [::1]:53: connection refused'),
# exited, and RestartSec below pushed Spotify past when the net was
# actually up. Hold ExecStart until DNS can actually resolve the AP host,
# so go-librespot starts the moment the internet is truly up and succeeds
# first try. Bounded by timeout + '-' prefixed: offline it gives up after
# 60s and lets go-librespot fail-and-retry, instead of hanging in
# 'activating' forever.
ExecStartPre=-/usr/bin/timeout 60 /bin/sh -c 'until getent hosts apresolve.spotify.com >/dev/null 2>&1; do sleep 1; done'
ExecStart=/usr/local/bin/go-librespot --config_dir $CONF_DIR
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

RECON_CHANGED=0
write_if_changed /usr/local/bin/vibb-bt-reconnect-poll <<'EOF' && RECON_CHANGED=1
#!/usr/bin/env bash
# Reconnects the remembered BT headset (written by play.sh) whenever it is
# powered on near the box, so turning the headset on is all it takes.
# FALLBACK poll loop: btwatchd (the D-Bus event daemon, phase C of
# PLAN-bt-dbus.md) exec's this when VIBB_BT_BACKEND=cli or the dbus
# bindings are missing. Worst case 60s to notice the speaker vs the
# daemon's seconds — keep for one release, then reevaluate.
#
# Backoff while the speaker stays away: each failed attempt is radio time
# plus a bluetoothd 'Host is down' journal line, and most speakers connect
# back to us BY THEMSELVES when powered on (the box stays connectable) —
# the poll only needs to catch the stragglers.
MAC_FILE=/etc/vibb/bt-headset
rfkill unblock bluetooth 2>/dev/null || true
bluetoothctl power on >/dev/null 2>&1 || true
bluetoothctl pairable on >/dev/null 2>&1 || true
delay=20
while true; do
  mac="$(cat "$MAC_FILE" 2>/dev/null || true)"
  if [[ -z $mac ]] \
      || bluetoothctl info "$mac" 2>/dev/null | grep -q "Connected: yes"; then
    delay=60   # steady state: just confirming — no need to fork 3x/min
  elif bluetoothctl connect "$mac" >/dev/null 2>&1; then
    delay=20   # just (re)connected: watch a little closer while it settles
  elif (( SECONDS < 120 )); then
    # boot window: the stack (adapter power, bluealsa A2DP endpoint) may
    # not be ready yet — a failure here means "too early", not "speaker
    # away", so retry fast instead of backing off
    bluetoothctl power on >/dev/null 2>&1 || true
    delay=5
  else
    delay=$(( delay * 2 )); (( delay > 300 )) && delay=300
  fi
  sleep "$delay"
done
EOF
chmod 755 /usr/local/bin/vibb-bt-reconnect-poll
# pre-phase-C name — remove so a stale copy can never be started by hand
rm -f /usr/local/bin/vibb-bt-reconnect

install_if_changed 755 "$SCRIPT_DIR/btwatchd.py" /usr/local/bin/vibb-btwatchd && RECON_CHANGED=1
MPRIS_CHANGED=0
install_if_changed 755 "$SCRIPT_DIR/mpris.py" /usr/local/bin/vibb-mpris && MPRIS_CHANGED=1
# AVRCP media player: answers the head unit's player polling (which
# otherwise runs an endless Invalid-Player-ID error loop DURING live
# A2DP — the known channel-ops-while-streaming crasher on this chip),
# shows title/artist on the car display, and routes the car's transport
# buttons to the daemon's idempotent endpoints.
write_if_changed /etc/systemd/system/vibb-mpris.service <<'EOF' && MPRIS_CHANGED=1
[Unit]
Description=Vibb AVRCP media player (bluez MPRIS bridge)
After=bluetooth.service vibb-daemon.service
Wants=bluetooth.service

[Service]
ExecStart=/usr/bin/python3 /usr/local/bin/vibb-mpris
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
write_if_changed /etc/systemd/system/vibb-bt-reconnect.service <<EOF && RECON_CHANGED=1
[Unit]
Description=Vibb BT reconnect daemon (event-driven, btwatchd)
# after whoever owns the A2DP endpoint (bluealsa, or WirePlumber under pipewire)
After=bluetooth.service $(audio_stack_endpoint_units)
Wants=bluetooth.service

[Service]
# Kill switch: systemctl edit vibb-bt-reconnect ->
#   [Service] Environment=VIBB_BT_BACKEND=cli   (poll-loop fallback)
$(audio_stack_unit_env)
# the transport-gate shadow compare at 1/s here: _await_pcm polls 1/s for
# <=10s per connect, so a <3s transport flicker is visible (AM-12c)
Environment=VIBB_BT_GATE_SHADOW_S=1
ExecStart=/usr/bin/python3 /usr/local/bin/vibb-btwatchd
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# --- 5. RFID + tools ---------------------------------------------------------

echo "==> [5/8] RFID reader support (PN532 over I2C) + tools..."
raspi-config nonint do_i2c 0 2>/dev/null || true
# SPI drives the Pirate Audio display (vibb-ui); harmless when unused
raspi-config nonint do_spi 0 2>/dev/null || true
if [[ ! -x /opt/vibb/venv/bin/python3 ]]; then
  python3 -m venv /opt/vibb/venv
fi
if [[ $UPDATE -eq 1 ]] || ! /opt/vibb/venv/bin/python3 -c 'import adafruit_pn532, evdev, PIL, gpiozero, lgpio, qrcode, soco' 2>/dev/null; then
  echo "    installing python libs (this can take a few minutes on a Zero)..."
  # soco: the vibb-sonos sidecar (UPnP control of Sonos speakers).
  # In the COMBINED probe above — a lib added outside it is silently
  # skipped by every future --update (architect review 2026-08-09).
  /opt/vibb/venv/bin/pip install --quiet --upgrade \
    adafruit-circuitpython-pn532 evdev pillow gpiozero qrcode soco
  # lgpio is gpiozero's pin factory on kernel 6.x — without it gpiozero
  # falls back to RPi.GPIO, whose edge detection is broken there
  # ('RuntimeError: Failed to add edge detection' from vibb-ui).
  # No prebuilt wheel for this python: pip builds from source, which
  # needs swig + the lg library headers. Never abort the install on it.
  if ! /opt/vibb/venv/bin/python3 -c 'import lgpio' 2>/dev/null; then
    apt-get install -y -qq swig liblgpio-dev >/dev/null 2>&1 || true
    /opt/vibb/venv/bin/pip install --quiet --upgrade lgpio \
      || echo "    WARNING: lgpio build failed — vibb-ui buttons need it (apt install swig liblgpio-dev, then rerun)"
  fi
  # Screen driver for the Pirate Audio HAT (harmless without the hardware)
  /opt/vibb/venv/bin/pip install --quiet --upgrade st7789 spidev 2>/dev/null \
    || echo "    (st7789 screen lib skipped — install when the HAT arrives)"
else
  echo "    python libs already installed — skipping pip"
fi

# Shared python package (vibb/): one copy under /usr/local/lib/vibb-py.
# The entry scripts bootstrap it from there (repo checkout wins when present).
PKG_CHANGED=0
mkdir -p /usr/local/lib/vibb-py/vibb
for f in "$SCRIPT_DIR"/vibb/*.py; do
  install_if_changed 644 "$f" "/usr/local/lib/vibb-py/vibb/$(basename "$f")" && PKG_CHANGED=1
done
# The pre-package layout installed nrk.py loose in /usr/local/bin — remove it
# so a stale copy can never shadow the package (we've been bitten before).
rm -f /usr/local/bin/nrk.py
# btwatchd imports vibb.bt — a package change must restart it too
[[ $PKG_CHANGED -eq 1 ]] && RECON_CHANGED=1
# asound.conf for the stack: pipewire pins both pcm names to graph nodes
# (needs the package just installed and WirePlumber, up since step 4);
# bluealsa is today's rewrite when a speaker is remembered.
audio_stack_route

RFID_CHANGED=$PKG_CHANGED
install_if_changed 755 "$SCRIPT_DIR/rfid.py"   /usr/local/bin/vibb-rfid   && RFID_CHANGED=1
install_if_changed 755 "$SCRIPT_DIR/player.py" /usr/local/bin/vibb-player && RFID_CHANGED=1
install_if_changed 755 "$SCRIPT_DIR/card.sh"  /usr/local/bin/vibb-card  || true
install_if_changed 755 "$SCRIPT_DIR/token.sh" /usr/local/bin/vibb-token || true
# Extras: owner-dropped launch scripts (docs/extras.md — RetroPie etc.).
# The dir is created once and its CONTENT is never touched by re-runs;
# scripts arrive over SSH only (root-owned, no API route, no upload
# path). The wrapper is the handoff/return machinery for the X+Y chord.
install -d -m 755 /etc/vibb/extras
install_if_changed 755 "$SCRIPT_DIR/extra.sh" /usr/local/bin/vibb-extra || true
install_if_changed 755 "$SCRIPT_DIR/lib.py"   /usr/local/bin/vibb-lib   || true
UI_CHANGED=$PKG_CHANGED
install_if_changed 755 "$SCRIPT_DIR/ui.py"    /usr/local/bin/vibb-ui    && UI_CHANGED=1
# The boot mark's artwork and its typeface. ui.py is copied INTO
# /usr/local/bin, so "beside the script" is not where these can live;
# the screen looks here next. A changed logo restarts the UI.
mkdir -p /usr/local/share/vibb/art
for _a in "$SCRIPT_DIR"/art/*; do
  [[ -e $_a ]] || continue
  install_if_changed 644 "$_a" "/usr/local/share/vibb/art/$(basename "$_a")" \
    && UI_CHANGED=1
done
install_if_changed 755 "$SCRIPT_DIR/play.sh"  /usr/local/bin/vibb-play  || true

# mDNS: advertise the box as <BOX_NAME>.local regardless of the system
# hostname (avahi's host-name option). iOS/macOS/Windows resolve .local
# natively; Android's browsers mostly do NOT — use the IP there.
if [[ -f /etc/avahi/avahi-daemon.conf ]] \
    && ! grep -q "^host-name=$BOX_NAME\$" /etc/avahi/avahi-daemon.conf; then
  sed -i "s/^#\?host-name=.*/host-name=$BOX_NAME/" /etc/avahi/avahi-daemon.conf
  grep -q '^host-name=' /etc/avahi/avahi-daemon.conf \
    || sed -i "/^\[server\]/a host-name=$BOX_NAME" /etc/avahi/avahi-daemon.conf
  systemctl enable --now avahi-daemon 2>/dev/null || true
  systemctl restart avahi-daemon 2>/dev/null || true
  echo "    mDNS: box advertised as $BOX_NAME.local"
else
  systemctl enable --now avahi-daemon 2>/dev/null || true
fi

# PiSugar battery curve: apply the calibrated Vibb curve (measured on a
# full discharge run; percent = remaining playtime) — but only when the
# config has NO curve yet, so a hand-tuned one is never overwritten.
# Re-apply/overwrite explicitly with: sudo vibb-power curve
if [[ -f /etc/pisugar-server/config.json ]] \
    && ! grep -q battery_curve /etc/pisugar-server/config.json; then
  bash "$SCRIPT_DIR/power.sh" curve && echo "    applied calibrated battery curve" \
    || echo "    battery curve not applied (see: sudo vibb-power curve)"
fi

# pisugar-server logs 2 INFO lines per TCP connect and a WARN for every
# normal client close ("Response error: Stream closed") — pure journal
# noise with anything polling the battery. Real errors still get logged.
if systemctl cat pisugar-server.service &>/dev/null; then
  if write_if_changed /etc/systemd/system/pisugar-server.service.d/quiet.conf <<'EOF'
[Service]
Environment=RUST_LOG=error
EOF
  then
    systemctl daemon-reload
    systemctl restart pisugar-server || true
    echo "    pisugar-server journal chatter silenced (RUST_LOG=error)"
  fi
fi

# --- boot & power tuning (reproduces the rig's manual tweaks) ---------------
# Everything here is idempotent and was field-verified on zero2 2026-07-18.

# boot_delay=0: drops a fixed ~1s firmware wait before the kernel loads.
# The ONLY config.txt boot knob that survived measurement: the boot
# governor is already ondemand (initial_turbo would gain ~nothing), the
# rainbow splash is HDMI-only (invisible on the SPI screen), and the
# autodetect probes are worth <1s combined.
BOOT_FW=/boot/firmware
[[ -f $BOOT_FW/config.txt ]] || BOOT_FW=/boot
if [[ -f $BOOT_FW/config.txt ]] && ! grep -q '^boot_delay=' "$BOOT_FW/config.txt"; then
  printf '\n[all]\nboot_delay=0\n' >> "$BOOT_FW/config.txt"
  echo "    config.txt: boot_delay=0 (takes effect next reboot)"
fi

# cloud-init: a first-boot provisioning tool (Imager customisation). After
# setup it re-scans its datasources on EVERY boot for ~6s while gating
# sysinit — which held the screen back. Everything it manages (user, wifi,
# hostname, ssh) is already materialised on disk; disabling is the soft,
# reversible switch (rm the file to re-enable).
if command -v cloud-init >/dev/null 2>&1 \
    && [[ ! -f /etc/cloud/cloud-init.disabled ]]; then
  touch /etc/cloud/cloud-init.disabled
  echo "    cloud-init disabled (~6s off every boot; rm /etc/cloud/cloud-init.disabled to undo)"
fi

# apt's daily timers: unattended update checks pick their own moment to
# hammer the network — on the SHARED 2.4GHz radio that moment lands mid-
# playback and starves A2DP/CDN loads. The box is an appliance: NO
# automatic security patches after this; updates happen only when someone
# runs install.sh (or apt) by hand over ssh.
for t in apt-daily.timer apt-daily-upgrade.timer; do
  if systemctl is-enabled --quiet "$t" 2>/dev/null; then
    systemctl disable --now "$t" >/dev/null 2>&1 || true
    echo "    $t disabled (no surprise network bursts; updates via manual runs)"
  fi
done

# (power save at boot is enabled further down, after vibb-power is
# installed — the unit's ExecStart must point at the installed copy)

# PiSugar RTC: the Zero has no real-time clock, so an offline boot starts
# in 1970 until NTP (if it ever) syncs — the battery logger and journal
# timestamps go haywire, and time-of-day features can't work. The PiSugar
# 3 has a battery-backed RTC; load it into the system clock at boot, and
# write the NTP-corrected time back periodically so it stays accurate.
if [[ -f /etc/pisugar-server/config.json ]]; then
  write_if_changed /etc/systemd/system/vibb-rtc.service <<'EOF' && RTC_CHANGED=1
[Unit]
Description=Vibb: load system clock from the PiSugar RTC
After=pisugar-server.service
Wants=pisugar-server.service
Before=time-sync.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/vibb-power rtc-load

[Install]
WantedBy=multi-user.target
EOF
  write_if_changed /etc/systemd/system/vibb-rtc-save.service <<'EOF' && RTC_CHANGED=1
[Unit]
Description=Vibb: write the NTP-corrected time back to the PiSugar RTC

[Service]
Type=oneshot
ExecStart=/usr/local/bin/vibb-power rtc-save
EOF
  write_if_changed /etc/systemd/system/vibb-rtc-save.timer <<'EOF' && RTC_CHANGED=1
[Unit]
Description=Vibb: periodically refresh the PiSugar RTC from the system clock

[Timer]
OnBootSec=3min
# 6h, not 30min: the RTC drifts seconds/month — refreshing it twice a
# day is plenty, and each run forks timedatectl+nc (review P7)
OnUnitActiveSec=6h

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now vibb-rtc.service >/dev/null 2>&1 || true
  systemctl enable --now vibb-rtc-save.timer >/dev/null 2>&1 || true
  [[ ${RTC_CHANGED:-0} -eq 1 ]] && echo "    PiSugar RTC clock sync installed (offline boots get a sane time)"
fi

# Captive portal DNS: while the setup hotspot runs (NetworkManager shared
# mode), resolve every hostname to the box so phone connectivity probes hit
# vibbd's :80 redirect and pop the portal. Inert outside hotspot mode.
write_if_changed /etc/NetworkManager/dnsmasq-shared.d/vibb-captive.conf <<'EOF' || true
address=/#/10.42.0.1
EOF

# Parent PWA (served by vibb-daemon at http://vibb.local:3679)
mkdir -p /usr/share/vibb/web
for f in "$SCRIPT_DIR"/web/*; do
  install_if_changed 644 "$f" "/usr/share/vibb/web/$(basename "$f")" || true
done

# Boot-time services this box has no use for. Measured on a cold Zero 2 W
# (2026-08-18), not guessed: systemd-binfmt sat ON the critical chain to
# sysinit.target, and sshswitch's peek at /boot/firmware is what dragged
# the boot partition's fsck into the busiest phase of startup (it read
# 1.3s serialised, 3.0s once it ran concurrently). Together with the
# fstab automount for /boot/firmware, sysinit went 7.8s -> 6.2s across
# three boots, with a spread of 0.1s.
#   binfmt_misc  runs foreign-architecture binaries (qemu). Not here.
#   e2scrub      reaps interrupted LVM ext4 scrubs. No LVM on an SD card.
#   keyboard-setup  console keymap on a box with no keyboard.
#   sshswitch    enables ssh if /boot/firmware/ssh exists; ssh.service is
#                already enabled, so it only costs the mount.
# All reversible: systemctl unmask <name>.
for _u in systemd-binfmt.service e2scrub_reap.service e2scrub_all.timer \
          keyboard-setup.service sshswitch.service; do
  systemctl mask "$_u" >/dev/null 2>&1 || true
done

mkdir -p /var/cache/vibb-restic   # restic's index cache (see RESTIC_CACHE_DIR)
# The memory ceiling below is only real if the kernel's memory cgroup
# controller is on, and Raspberry Pi OS ships it OFF unless cmdline.txt says
# so. Without it MemoryMax/MemoryHigh/MemorySwapMax are silently ignored and
# a runaway backup could have the global OOM killer take mpv instead. We warn
# rather than edit cmdline.txt: that file is boot-critical, and a bad edit
# bricks the box far worse than an uncapped backup.
if ! grep -qw memory /sys/fs/cgroup/cgroup.controllers 2>/dev/null; then
  echo "    NOTE: the kernel memory cgroup controller is off, so the backup's"
  echo "          memory limit cannot be enforced. To enable it, append"
  echo "          'cgroup_enable=memory cgroup_memory=1' to the single line in"
  echo "          /boot/firmware/cmdline.txt (or /boot/cmdline.txt) and reboot."
fi
# Backup timer. A dedicated timer, NOT the 6h cache sweeper: a backup that
# fails must never interfere with cache pruning, and the two have nothing to
# do with each other. Harmless before the owner has connected any storage —
# the run exits 0 without touching the network.
write_if_changed /etc/systemd/system/vibb-backup.service <<'EOF' && BK_CHANGED=1
[Unit]
Description=Vibb: back up the box's config, secrets and bookmarks
# No network-online.target: at timer-fire time it is long since active and
# tells us nothing, and it cannot un-rfkill a radio that auto-off powered
# down. vibb.backup checks the link itself in ~10ms.

[Service]
Type=oneshot
# Playback always wins. nice+CPUWeight for the scheduler, CPUAffinity to
# match the content sweeper's own `taskset -c 1`, and GOMAXPROCS=1 so Go
# does not spin four Ps and four GC workers onto that one core.
Nice=19
CPUWeight=20
CPUAffinity=1
IOSchedulingClass=idle
# THE IMPORTANT ONES. ~430MB usable after the GPU split, and restic+rclone
# are two Go binaries that together want 100-170MB. Without a ceiling the
# kernel OOM killer picks by score — and that can just as easily be mpv or
# go-librespot as restic, i.e. the music dies to save the backup. MemoryMax
# makes the cgroup OOM local: the backup is what dies, and it simply retries
# at the next wake (QA 2026-08-17).
# MemorySwapMax=0 keeps the backup's OWN pages off swap regardless of what
# the OS provides. NOTE: Trixie DOES ship zram swap now (dev-zram0.swap is
# active; vibb-ui was seen with a 31M swap peak) — the earlier "no swap
# anywhere" note was already stale. That does not change these caps: the
# cgroup limit is enforced whether or not the system has swap, and pinning
# the backup to zero swap is still correct.
# 160M, not 100M: with the backup barred from swap, crossing MemoryHigh can
# only reclaim file-backed pages — the mapped text of two large Go binaries
# — and then throttles. Set inside the real working set it strangles the
# run off the SD card instead of protecting anything. MemoryMax is the
# ceiling that matters.
MemoryHigh=160M
MemoryMax=200M
MemorySwapMax=0
Environment=GOMEMLIMIT=80MiB GOGC=20 GOMAXPROCS=1
# systemd sets $HOME only with User=, and no vibb unit has one. Without it
# restic cannot resolve a cache dir, warns, and runs CACHELESS — re-fetching
# the whole index from the remote every run, forever. Kept out of CACHE_DIR,
# which is served and pruned.
Environment=RESTIC_CACHE_DIR=/var/cache/vibb-restic
Environment=PYTHONPATH=/usr/local/lib/vibb-py
ExecStart=/usr/bin/python3 -m vibb.backup
EOF
write_if_changed /etc/systemd/system/vibb-backup.timer <<'EOF' && BK_CHANGED=1
[Unit]
Description=Vibb: periodic backup of the box's irreplaceable state

[Timer]
# A SAFETY NET, not the schedule. The real backup happens on the way down,
# in vibb-idle: the box has been idle for the whole timeout, so nothing is
# playing, the radio is free, and the session's bookmarks are fresh. That is
# the moment worth backing up at, and no timer can guess it.
#
# This matters for a box that never reaches an idle shutdown (left playing
# for days, or auto-off disabled) AND for one turned off with the button
# before the idle timeout — neither hits the vibb-idle backup, so the timer
# is their only chance. It cannot wake a powered-off box (that would need an
# RTC alarm; we set none), so it costs nothing while off. When it fires it is
# a few milliseconds: vibb.backup's calendar-day gate says "already done
# today" and exits.
#
# OnBootSec=15min, not 30: a short button-ended session should still be
# caught. 6h thereafter because it is only a backstop, and a monotonic timer
# restarts at every boot — a tight interval on a toddler-power-cycled box
# just means firing on every boot, mid-session, which is the bug this
# replaced. The calendar-day gate keeps it to one backup a day regardless.
OnBootSec=15min
OnUnitActiveSec=6h
AccuracySec=15min
RandomizedDelaySec=15min

[Install]
WantedBy=timers.target
EOF
if [[ ${BK_CHANGED:-0} -eq 1 ]]; then
  systemctl daemon-reload
  echo "    backup timer installed (connect storage in the PWA: Settings -> Backup)"
fi
systemctl enable --now vibb-backup.timer >/dev/null 2>&1 || true

# Backlight off at boot. The Pirate Audio's backlight is lit the moment
# power arrives, but the ST7789 has no pixels until vibb-ui initialises
# it ~19s later — so the whole boot showed the panel's uninitialised RAM
# as bright snow, which reads as a BROKEN box rather than one starting
# up (owner 2026-08-18). Dark reads as "off", which is the truth. vibb-ui
# raises the light itself the instant it has a first frame.
#
# Costs nothing measurable: one pinctrl call, no SPI, no python, and it
# is ordered before nothing — it just needs to land early.
write_if_changed /etc/systemd/system/vibb-backlight-off.service <<'EOF' && BL_CHANGED=1
[Unit]
Description=Vibb: dark screen until the UI has something to show
DefaultDependencies=no
After=local-fs.target
Before=vibb-ui.service
Conflicts=shutdown.target

[Service]
Type=oneshot
RemainAfterExit=no
# pinctrl (raspi-utils) on Pi OS; the '-' prefixes mean a box without it
# simply keeps today's behaviour rather than failing the boot.
ExecStart=-/usr/bin/pinctrl set 13 op dl
ExecStart=-/usr/bin/raspi-gpio set 13 op dl

[Install]
WantedBy=sysinit.target
EOF
if [[ ${BL_CHANGED:-0} -eq 1 ]]; then
  systemctl daemon-reload
  systemctl enable vibb-backlight-off.service >/dev/null 2>&1 || true
  echo "    backlight now stays dark until the UI draws (no more boot snow)"
fi

# Screen daemon service (Pirate Audio HAT). Installed but NOT enabled —
# enable it when the screen is mounted:  sudo systemctl enable --now vibb-ui
write_if_changed /etc/systemd/system/vibb-ui.service <<'EOF' || true
[Unit]
Description=Vibb screen UI (Pirate Audio)
# Early start: the splash handles a not-yet-ready vibbd, and waiting
# behind the daemon (which waits behind the network) left the screen
# frozen on its last image for ~35s of every boot
DefaultDependencies=no
After=local-fs.target sysinit.target
Before=shutdown.target
Conflicts=shutdown.target

[Service]
# Name the pin factory instead of letting gpiozero PROBE for one. It
# tries its backends in turn, and the cost shows up in the measured
# backlight half of display init as pure variance: 0.2s / 0.5s / 0.8s /
# 1.1s across boots on the same box. lgpio is the one install.sh puts in
# the venv, and it is what the box uses either way — this only removes
# the search. (ui.py's own comment named this in 2026-08-13; it was never
# set. Measured cold boots 2026-08-18.)
Environment=GPIOZERO_PIN_FACTORY=lgpio
ExecStart=/opt/vibb/venv/bin/python3 /usr/local/bin/vibb-ui
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
install_if_changed 755 "$SCRIPT_DIR/power.sh" /usr/local/bin/vibb-power || true
# Charger-follow: the CPU governor tracks the power plug (ondemand on
# charger, powersave on battery — vibb-power _followloop). Standalone
# by design: it reads pisugar-server directly and must never depend on
# the OPT-IN battery logger. Meaningless without a PiSugar, hence the
# config-file condition.
write_if_changed /etc/systemd/system/vibb-chargefollow.service <<'EOF' && CHARGE_CHANGED=1
[Unit]
Description=Vibb charger-follow (CPU governor tracks the power plug)
After=pisugar-server.service
Wants=pisugar-server.service
ConditionPathExists=/etc/pisugar-server/config.json

[Service]
ExecStart=/usr/local/bin/vibb-power _followloop
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF
# btsnoop ring (OPT-IN diagnostic, installed disabled): HCI capture into a
# RAM ring so the next `hci0: hardware error 0x00` can be attributed to the
# right layer — the kernel synthesizes that exact event on a dead UART link
# (hci_reset_dev), so dmesg alone can't tell chip-fault from link-fault.
# Enable during a crash hunt: sudo systemctl enable --now vibb-btsnoop
install_if_changed 755 "$SCRIPT_DIR/btsnoop.sh" /usr/local/bin/vibb-btsnoop || true
# ...and the analysis half: per-crash control-plane digest of a captured
# ring segment (what was on the air before each Hardware Error):
#   vibb-snoop-digest ~/20260727-200749.snoop
install_if_changed 755 "$SCRIPT_DIR/snoopdigest.py" /usr/local/bin/vibb-snoop-digest || true
write_if_changed /etc/systemd/system/vibb-btsnoop.service <<'EOF' || true
[Unit]
Description=Vibb btsnoop ring (BT crash diagnosis, RAM-only)
After=bluetooth.service

[Service]
ExecStart=/usr/local/bin/vibb-btsnoop
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF
# Power save at boot: governor powersave + LEDs/HDMI off + wifi power save
# (vibb-power save) applied automatically at every boot. Runs after
# multi-user so it never slows the boot itself. Invoked via the INSTALLED
# copy so the generated unit's ExecStart points at /usr/local/bin.
# NOTE: 'vibb-power boot-off' removes the unit, but a later install.sh
# run re-adds it (power save at boot is the vibb default).
# Migration: the first version ordered this After=multi-user.target,
# which waits for network-online — a struggling wifi then kept the HDMI
# signal on and the CPU unparked for the whole wait (field 2026-08-04).
# Rewrite that unit; 'boot-off' users keep their choice (no unit file).
if [[ ! -f /etc/systemd/system/vibb-power.service ]]; then
  /usr/local/bin/vibb-power boot-on >/dev/null \
    && echo "    power save applied at every boot (vibb-power boot-on)"
elif grep -q '^After=multi-user.target' /etc/systemd/system/vibb-power.service; then
  /usr/local/bin/vibb-power boot-on >/dev/null \
    && echo "    power save at boot no longer waits for the network"
fi
# The battery CSV logger (vibb-power log-on) stays OPT-IN: it's a
# calibration tool that writes the SD card every 60s forever — enable it
# only while measuring a discharge curve.
IDLE_CHANGED=0
install_if_changed 755 "$SCRIPT_DIR/idle.py"  /usr/local/bin/vibb-idle  && IDLE_CHANGED=1
# Idle auto-shutdown: enabled by default — the PWA setting
# (idle_shutdown_min, 0 = never) is the actual on/off knob and idle.py
# re-reads it live. Previously this service was opt-in via
# 'vibb-power idle-on', which made the PWA setting a silent no-op.
write_if_changed /etc/systemd/system/vibb-idle.service <<'EOF2' && IDLE_CHANGED=1
[Unit]
Description=Vibb idle auto-shutdown
After=vibb-daemon.service

[Service]
ExecStart=/opt/vibb/venv/bin/python3 /usr/local/bin/vibb-idle
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF2

SONOS_CHANGED=$PKG_CHANGED
install_if_changed 755 "$SCRIPT_DIR/sonosd.py" /usr/local/bin/vibb-sonos && SONOS_CHANGED=1
write_if_changed /etc/systemd/system/vibb-sonos.service <<'EOF2' && SONOS_CHANGED=1
[Unit]
Description=Vibb Sonos sidecar (UPnP via SoCo, 127.0.0.1 only)
# After basic.target like vibbd, NOT network-online: soco imports
# lazily and the speaker cache addresses by stored IP, so startup needs
# no network (ordering behind the network cost ~18s of boot elsewhere).
After=basic.target

[Service]
ExecStart=/opt/vibb/venv/bin/python3 /usr/local/bin/vibb-sonos
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF2

DAEMON_CHANGED=$PKG_CHANGED
install_if_changed 755 "$SCRIPT_DIR/daemon.py" /usr/local/bin/vibb-daemon && DAEMON_CHANGED=1
# Pairing over D-Bus (B2, PLAN-bt-b2-pairing.md): opt in per box until the
# rig matrix passes —  systemctl edit vibb-daemon  ->
#   [Service] Environment=VIBB_BT_PAIR=dbus
write_if_changed /etc/systemd/system/vibb-daemon.service <<EOF && DAEMON_CHANGED=1
[Unit]
Description=Vibb orchestration daemon (playback state + API)
# Deliberately NOT ordered After=go-librespot / network-online: the daemon
# resumes cached podcasts and serves the PWA with no network, and it
# already waits INTERNALLY for whatever a target needs (go-librespot login
# for Spotify, internet for a fresh mpv stream). Ordering it behind the
# network held the screen's 'ready' state and cached-content resume ~18s
# into every boot — the daemon now starts at basic.target instead
# (systemd-analyze rig 2026-07-18: multi-user waited on network-online).

[Service]
Environment=VIBB_GO_CONFIG=$CONF_DIR/config.yml
$(audio_stack_unit_env)
ExecStart=/usr/bin/python3 /usr/local/bin/vibb-daemon
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

BTN_CHANGED=$PKG_CHANGED
install_if_changed 755 "$SCRIPT_DIR/buttons.py" /usr/local/bin/vibb-buttons && BTN_CHANGED=1
write_if_changed /etc/systemd/system/vibb-buttons.service <<'EOF' && BTN_CHANGED=1
[Unit]
Description=Vibb media button daemon (AVRCP etc.)
After=bluetooth.service

[Service]
ExecStart=/opt/vibb/venv/bin/python3 /usr/local/bin/vibb-buttons
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# RFID daemon service (PN532). Installed but NOT enabled — enable it when
# the reader is wired:  sudo systemctl enable --now vibb-rfid
write_if_changed /etc/systemd/system/vibb-rfid.service <<'EOF' && RFID_CHANGED=1
[Unit]
Description=Vibb RFID daemon
After=go-librespot.service

[Service]
EnvironmentFile=-/etc/vibb/rfid.conf
ExecStart=/opt/vibb/venv/bin/python3 /usr/local/bin/vibb-rfid
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# RFID config: poll mode by default; slot mode (card slot + detector switch)
# is enabled by editing this file. Written only if missing.
if [[ -f /etc/vibb/rfid.conf ]]; then
  echo "    keeping existing /etc/vibb/rfid.conf"
else
  mkdir -p /etc/vibb
  cat > /etc/vibb/rfid.conf <<'EOF'
# Vibb RFID daemon config (systemd EnvironmentFile).
# Default (everything commented out) = PN532 poll mode.
#
# Slot mode: a detector switch in the card slot senses the card; the PN532
# is only powered/read once per insertion. Card in = play, card out = pause.
# Apply changes with: sudo systemctl restart vibb-rfid
#
#SLOT_GPIO=17          # BCM pin of the slot switch (other pole to GND)
#SLOT_PRESENT=low      # 'low' = switch closes to GND when card is in (default)
#PN532_POWER_GPIO=     # optional MOSFET gate powering the PN532 (BCM pin)
#
# Testing without hardware:
#SLOT_GPIO=file:/tmp/card   # `touch /tmp/card` = insert, `rm` = remove
#FAKE_UID=cafebabe          # UID to pretend when no PN532 answers
EOF
  echo "    wrote /etc/vibb/rfid.conf (poll mode; edit it to enable slot mode)"
fi

# Spotify Web API credentials (optional): lets a library section follow a
# Spotify profile's PUBLIC playlists. One free app registered at
# https://developer.spotify.com/dashboard serves every box in the household
# (client credentials read public data only — no user login involved).
# Provide via VIBB_SPOTIFY_ID/VIBB_SPOTIFY_SECRET env or the one-time
# prompt. Re-runs keep the existing file; delete it to be asked again.
SPOTIFY_API_FILE=/etc/vibb/spotify-api.json
if [[ -f $SPOTIFY_API_FILE ]]; then
  echo "    keeping existing $SPOTIFY_API_FILE"
else
  SP_ID="${VIBB_SPOTIFY_ID:-}"
  SP_SECRET="${VIBB_SPOTIFY_SECRET:-}"
  if [[ -z $SP_ID && -t 0 ]]; then
    read -r -p "Spotify API client id (blank = skip profile-follow): " SP_ID || true
    [[ -n $SP_ID ]] && read -r -p "Spotify API client secret: " SP_SECRET || true
  fi
  if [[ -n $SP_ID && -n $SP_SECRET ]]; then
    mkdir -p /etc/vibb
    printf '{"client_id": "%s", "client_secret": "%s"}\n' \
      "$SP_ID" "$SP_SECRET" > "$SPOTIFY_API_FILE"
    chmod 600 "$SPOTIFY_API_FILE"
    echo "    wrote $SPOTIFY_API_FILE (root-only; feeds profile-follow sections)"
  fi
fi

# --- 5b. API token -----------------------------------------------------------

# The credential that gates the privileged endpoints (SECURITY.md). Made
# HERE, before the services restart, so the daemon never comes up without
# one. Keep-existing: re-running install.sh must NOT rotate it, or every
# linked phone in the house silently stops working. token.ensure() is the
# single generator — the daemon calls the same function at boot to heal a
# deleted file, and `vibb-token` reads it back on demand. Deliberately
# NOT printed here: the secret should only appear when someone asks.
/usr/bin/python3 -c 'import sys; sys.path.insert(0, "/usr/local/lib/vibb-py"); from vibb import token; token.ensure()' 2>/dev/null \
  || echo "    WARNING: could not create the API token — privileged endpoints will refuse requests"

echo "==> [6/8] Enabling services (restarting only what changed)..."
# Normalize modes on units written by older installs (mktemp made them 600
# and unchanged files are never rewritten) — silences systemd's
# 'world-inaccessible' warning on every daemon-reload.
chmod 644 /etc/systemd/system/vibb-*.service \
  /etc/systemd/system/go-librespot.service 2>/dev/null || true
systemctl daemon-reload
systemctl enable --now go-librespot.service vibb-bt-reconnect.service vibb-mpris.service \
  vibb-buttons.service vibb-daemon.service vibb-idle.service \
  vibb-chargefollow.service vibb-sonos.service
# One-time migration: earlier installs enabled vibb-rfid before the PN532
# existed. Switch it to the same opt-in contract as vibb-ui — but only
# once, so an enable after wiring the reader sticks across installs.
if [[ ! -f /var/lib/vibb/.rfid-opt-in ]]; then
  mkdir -p /var/lib/vibb && touch /var/lib/vibb/.rfid-opt-in
  if systemctl is-enabled --quiet vibb-rfid.service 2>/dev/null; then
    systemctl disable --now vibb-rfid.service
    echo "    vibb-rfid disabled (no reader yet) — enable when the PN532 is wired:"
    echo "      sudo systemctl enable --now vibb-rfid"
  fi
fi
[[ $GO_CHANGED -eq 1 || ${GO_RESTART_NEEDED:-0} -eq 1 ]] && { echo "    go-librespot changed — restarting"; systemctl restart go-librespot.service; }
[[ $RECON_CHANGED -eq 1 ]] && { echo "    bt-reconnect changed — restarting"; systemctl restart vibb-bt-reconnect.service; }
[[ ${MPRIS_CHANGED:-0} -eq 1 ]] && { echo "    mpris bridge changed — restarting"; systemctl restart vibb-mpris.service; }
[[ $IDLE_CHANGED  -eq 1 ]] && { echo "    idle daemon changed — restarting"; systemctl restart vibb-idle.service; }
[[ ${SONOS_CHANGED:-0} -eq 1 ]] && { echo "    sonos sidecar changed — restarting"; systemctl restart vibb-sonos.service; }
[[ ${CHARGE_CHANGED:-0} -eq 1 ]] && { echo "    charger-follow changed — restarting"; systemctl restart vibb-chargefollow.service; }
# Hardware-gated units the box had switched on before the rename: their
# new equivalents exist by now, so restore the choice the owner made.
if [[ -f /run/vibb-was-enabled ]]; then
  while read -r svc; do
    [[ $svc == ui.service || $svc == rfid.service ]] || continue
    if ! systemctl is-enabled --quiet "vibb-$svc" 2>/dev/null; then
      systemctl enable --now "vibb-$svc" >/dev/null 2>&1 \
        && echo "    vibb-${svc%.service} re-enabled (it was on before the rename)"
    fi
  done < /run/vibb-was-enabled
  rm -f /run/vibb-was-enabled
fi

[[ $RFID_CHANGED  -eq 1 ]] && systemctl is-enabled --quiet vibb-rfid.service 2>/dev/null \
  && { echo "    rfid daemon changed — restarting"; systemctl restart vibb-rfid.service; }
[[ $BTN_CHANGED   -eq 1 ]] && { echo "    button daemon changed — restarting"; systemctl restart vibb-buttons.service; }
[[ $DAEMON_CHANGED -eq 1 ]] && { echo "    orchestration daemon changed — restarting"; systemctl restart vibb-daemon.service; }
[[ ${UI_CHANGED:-0} -eq 1 ]] && systemctl is-active --quiet vibb-ui.service 2>/dev/null \
  && { echo "    screen ui changed — restarting"; systemctl restart vibb-ui.service; }

# --- 7. API + login ----------------------------------------------------------

echo "==> [7/8] Waiting for the API to come up..."
for _ in $(seq 1 20); do
  curl -sf "http://127.0.0.1:$API_PORT/status" >/dev/null && break
  sleep 1
done

if grep -q '"username"' "$CONF_DIR/state.json" 2>/dev/null; then
  echo "==> [8/8] Already logged in to Spotify — done!"
  print_token
  exit 0
fi

echo "==> [8/8] Spotify login"
echo
echo "    1. Open the Spotify app on your phone (same Wi-Fi as the Pi)"
echo "    2. Play any song, tap the devices icon (speaker/screen symbol)"
echo "    3. Select \"$DEVICE_NAME\""
echo
echo "    Waiting up to 5 minutes for you to connect..."
for _ in $(seq 1 60); do
  if grep -q '"username"' "$CONF_DIR/state.json" 2>/dev/null; then
    echo
    echo "    Logged in! Credentials are stored on the Pi and survive reboots."
    echo "    Next: sudo ./play.sh connect   (with your headset in pairing mode)"
    print_token
    exit 0
  fi
  sleep 5
done

echo
echo "    Timed out waiting, but the daemon keeps running — you can connect from"
echo "    the phone at any time. Check status with: journalctl -u go-librespot -f"
