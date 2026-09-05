# vibb

A small, kid-proof portable music player — think *iPod for kids*: a
240×240 colour screen, four physical buttons, a battery, and a library
the parents curate. The kid flips through a carousel of album covers
and presses play; there is no store, no ads, no algorithm, and no open
internet on the device. Content comes from subscriptions and feeds the
family already has: Spotify, NRK, any podcast RSS, local files.

## Content sources

- **Spotify** (Premium; family member accounts work): playlists,
  albums, artists and audiobooks, played through a pinned
  [go-librespot](https://github.com/devgianlu/go-librespot) fork with
  an **on-disk audio cache** — repeat plays start fast and spend
  little data.
- **Podcasts — any RSS/XML feed**, with first-class **NRK** support:
  episodes cache offline (newest N per show, your choice), each with
  its own episode art and exact per-episode resume.
- **Storytel** (your own subscription): the account's audiobooks,
  grouped into **series** so a long-running kids' series is one tile
  rather than thirty. Books are **downloaded to the box**, so they play
  offline and keep exact per-book positions; those positions are also
  mirrored back to Storytel, one way, so the phone app picks up roughly
  where the box left off. Works on Sonos too.
- **Your own files** *(early support, less battle-tested)*: DRM-free
  audiobooks and rips — upload through the PWA or copy a folder onto
  the box. Track titles and order come from the files' embedded tags;
  cover art is extracted automatically from each file's embedded
  artwork — **every track shows its own cover** (a folder of loose
  singles looks right, not like one album). The collection tile uses a
  folder-level cover: extracted from the first file, or override it by
  uploading an image **named `cover.jpg`/`.png`** alongside the audio
  files (the uploader accepts images for exactly this; there is no
  dedicated cover button).
  Streamed entries take their art from the source; the only custom
  image for those is the per-category logo.

## What the kid gets

- **A cover carousel** — big album art, one cover at a time, an
  animated flip between them. Pick by picture, not by reading.
- **Four buttons, no touchscreen**: play/pause, next, previous,
  volume. Long-presses reach the episode list and settings, but
  nothing a small kid does by accident leaves the music.
- **Now playing** with full-screen art, sliding titles (emoji
  included), and a progress bar.
- **It just resumes.** Power on → the last episode continues from
  where it stopped, even mid-episode, even after a dead battery.
- **Works offline**: cached episodes play with no internet at all.

## What the parent gets

- **A web app** (PWA) on `http://<name>.local:3679` — add it to your
  home screen. Pair your phone by scanning a QR on the box's screen.
- **Library curation**: paste a Spotify link (playlist / album /
  artist / audiobook), an NRK series, any RSS feed, or a local folder.
  Choose play order and an **offline cache** depth per entry (keep the
  newest N episodes on the SD card).
- **Outputs**: the built-in speaker (Pirate Audio HAT), any Bluetooth
  speaker or kids' headphones (auto-reconnect within seconds), or a
  **Sonos** speaker on the same network.
- **Guard rails**: volume cap, screen timeout, idle auto-off, exact
  bookmarks per episode.
- **Battery** (PiSugar 3): percent tuned to *remaining playtime*, safe
  shutdown, optional button gestures on the power button.
- **Backup**: the box copies the things you cannot download again —
  the library, settings, RFID cards, category logos, the Storytel and
  Spotify logins, and every child's place in every book — to storage
  you own. Encrypted before it leaves the box, and reachable through
  [rclone](https://rclone.org), so any of its ~70 backends works
  (S3, Backblaze B2, Nextcloud/WebDAV, Jottacloud, Drive, Dropbox,
  SFTP). It runs once a day, on the way down after an idle shutdown —
  never while anything is playing, because uploading and the music
  share one 2.4GHz radio. Downloaded audio is left out on purpose: it
  can be fetched again, and the whole backup stays under a megabyte.
  Restoring onto a replacement box brings the room back as it was
  (a new box mints its own QR code, and a Bluetooth speaker is paired
  again). Set it up in the PWA: **Settings → Backup**.

## Status

Daily-driver, field-tested. Pi Zero 2 W + Pirate Audio HAT (ST7789
screen, four buttons, speaker) + PiSugar 3, playing Spotify (via a
go-librespot fork with on-disk audio cache), NRK/RSS podcasts with
offline cache and exact resume, Bluetooth speakers/headphones and
Sonos as outputs. Historical design documents live in
[SPEC.md](./SPEC.md) and [SPEC-A-explorer.md](./SPEC-A-explorer.md).

## Hardware

| Part | Role | Required |
|---|---|---|
| Raspberry Pi Zero 2 W | the box | yes |
| Pimoroni Pirate Audio Speaker HAT | screen + buttons + speaker | recommended (headless works, controlled from the PWA) |
| PiSugar 3 | battery + RTC | optional |
| Bluetooth speaker / kids' headphones | wireless output | optional |
| Spotify **Premium** account | music source (family member accounts work) | for Spotify content |

## Installation manual

Assumed already done: Raspberry Pi OS **(Trixie-based; Bookworm also
works)** flashed with the Raspberry Pi Imager (set hostname, user,
Wi-Fi and SSH there — that's a one-time provisioning; install.sh
disables cloud-init afterwards). Remote admin over Tailscale is
possible but entirely optional (own installer + interactive auth).

### 1. Install Vibb

SSH in, clone, run the installer:

```
git clone <this repo> ~/vibb
cd ~/vibb
sudo ./pi/install.sh
```

The first run asks one question — a **name for the box** (lowercase
letters/digits/hyphens). It becomes both the mDNS hostname
(`<name>.local`) and the Spotify Connect device name
("Vibb (<name>)"). Non-interactive installs: `VIBB_NAME=<name>
sudo ./pi/install.sh`.

The script is idempotent and does everything on-box: apt packages
(bluez-alsa, mpv, ffmpeg, avahi, ...), the go-librespot fork (Spotify
Connect daemon with on-disk audio cache, pinned version), ALSA
routing, the `vibb` Python package, and all systemd services
(orchestration daemon + PWA on `:3679`, screen UI, BT auto-reconnect,
media buttons, Sonos sidecar, captive-portal DNS).

### 2. Enable the screen + built-in speaker (Pirate Audio HAT)

```
sudo vibb-power hat-audio-on    # dtoverlay=hifiberry-dac + amp enable
sudo reboot
# then: PWA -> Player -> Audio out -> Built-in
sudo systemctl enable --now vibb-ui
```

Headless (no HAT) is fine too — skip this and drive everything from
the PWA.

### 3. Log in to Spotify

The installer's last step waits for this, and you can do it any time:

1. Open the Spotify app on your phone — **same Wi-Fi as the box**.
2. Play any song, tap the devices icon.
3. Pick **"Vibb (<name>)"**.

The login is stored on the box and survives reboots. To hand the box
to another account later: PWA → Settings → Spotify → *Switch account*.

### 4. Pair the Bluetooth speaker

Open the parent PWA at `http://<name>.local:3679`. Under **Settings →
Bluetooth speaker**:

- Put the speaker in pairing mode, then use **Pair nearest**
  (one-button flow), or **Scan for new** and pick it from the list.
- The chosen speaker becomes the *configured* output: audio and
  auto-reconnect follow it. Pairing more speakers is fine — tap
  *Connect* on one to make it the active output.
- **Rename** gives a speaker your own name ("The car", "Kids' room");
  it shows in this list and on the box's screen. Blank resets to the
  factory name.

From now on, turning the speaker on is enough; the box reconnects by
itself within seconds (btwatchd listens for BlueZ D-Bus events; set
`VIBB_BT_BACKEND=cli` on the vibb-bt-reconnect service to fall
back to the old 60s poll loop). Pairing itself still runs through
bluetoothctl by default; set `VIBB_BT_PAIR=dbus` on the
vibb-daemon service to use the new Agent1 path (kept opt-in until
the rig matrix in PLAN-bt-b2-pairing.md passes).

**Pairing from a car / head unit:** cars drive the pairing themselves
and expect the *device* to be visible. PWA → Bluetooth → **Pair from
car**: the box becomes discoverable for ~2 minutes — start the pairing
from the car's Bluetooth menu, then pick the car as speaker when it
appears in the device list. The box only accepts pairings during that
window (never silently), and pairing alone never moves the audio — the
new device is just listed, one tap away from becoming the output.

### 5. Build the library

PWA → **Library** → *Add*: paste a Spotify link (playlist/album/
artist/audiobook), an NRK series/podcast link, any RSS feed, or a
local folder path. Per entry you choose the play order and an
**offline cache** depth (keep newest N episodes on the SD card —
they play without internet). Podcast/RSS episodes cache as plain
files under `/var/lib/vibb/cache`; Spotify caches encrypted audio
(faster + cheaper on repeat plays, but needs a live session).

### 6. Battery (PiSugar 3) — optional

1. Install pisugar-server with PiSugar's own installer (their curl
   script; it's interactive, which is why install.sh doesn't do it).
2. Re-run `sudo ./pi/install.sh` — it now applies the Vibb-measured
   battery curve (percent ≈ remaining playtime) automatically.
3. In the PiSugar web UI (`http://<name>.local:8421`): enable **safe
   shutdown** at ~5%.
4. Optional extras: `sudo vibb-power taps-on` (PiSugar button:
   short=play/pause, double=next, long=prev), `vibb-power log-on`
   (battery CSV logger), `vibb-power curve` (recalibrate).

### 7. Power tuning — optional

- Power save at boot (powersave governor, LEDs/HDMI off, Wi-Fi power
  save) is applied automatically — install.sh runs `vibb-power
  boot-on`. Undo per-box with `sudo vibb-power boot-off`; back to
  full speed anytime with `sudo vibb-power perf`.
- Settings in the PWA: auto-off when idle, Wi-Fi auto-off away from
  known networks, screen timeout, volume cap, and **continue after
  power-on** (always, never, or only if the box was switched off less
  than N hours ago — so an interrupted car ride carries on, while last
  week's album starts from the top).
- Squeezing the last drops: add `maxcpus=2` to
  `/boot/firmware/cmdline.txt` by hand (the RPi OS kernel has no CPU
  hotplug, so `vibb-power save` can't park cores at runtime).
  Measured on zero2: idle cores sleep deeply anyway — the saving is a
  few mA at best, so most boxes should skip this.

### 8. Moving the box to a new Wi-Fi

A box that finds no known network (and has none saved) starts its own
setup hotspot **Vibb-<name>** (password `vibb1234`); joining it
pops a captive portal straight into the PWA, where you pick the new
network. A box with saved networks: use the PWA's Wi-Fi card while
it's still on the old network, or the screen's settings.

### Updating

```
cd ~/vibb && git pull && sudo ./pi/install.sh
```

The script restarts only the services whose files changed; playback
position survives (bookmark + resume).

### Kept outside install.sh (by design)

| What | How | Why manual |
|---|---|---|
| OS basics | Raspberry Pi Imager (hostname, user, wifi, SSH) | pre-boot; cloud-init is disabled after first boot (install.sh) |
| pisugar-server | PiSugar's own installer script, then re-run install.sh | third-party interactive installer; install.sh patches its config (battery curve, RTC units, log quieting) when present |
| PiSugar safe-shutdown / taps | PiSugar web UI (:8421) or `vibb-power taps-on` | user preference |
| Pirate Audio HAT (when mounted) | `sudo vibb-power hat-audio-on` + reboot + enable `vibb-ui` | hardware-gated |
| Spotify login | pick the box under Devices in the Spotify app (same wifi) | zeroconf by design; install.sh waits for it in step 8/8 |
| BT speaker pairing | PWA settings -> Bluetooth (or screen) | per-home config |
| Boot-time shave (~1.5s) | edit `/etc/fstab`, see below | a bad fstab edit can make the box unbootable — too risky to automate on a fresh card |

**Optional boot-time tuning.** install.sh already masks five boot
services this box has no use for (binfmt, e2scrub, keyboard-setup,
sshswitch) — measured on a cold Zero 2 W, they sat on or dragged into the
critical path to the screen. One further shave is left manual because it
edits `/etc/fstab`, where a mistake bricks the boot: taking `/boot/firmware`
off `local-fs.target` so its fsck no longer runs in the busiest phase of
startup. It moved `sysinit.target` from ~7.8s to ~6.2s across three boots.
`/boot/firmware` is a FAT partition the box only touches on a kernel or
config change, and the firmware reads it directly before Linux exists — so
an automount is safe. Back up fstab first, and **verify the line before
rebooting**:

```bash
# 1. back up
sudo cp /etc/fstab /etc/fstab.bak-$(date +%F)

# 2. add automount flags to the /boot/firmware line
sudo sed -i '\|/boot/firmware|s|\(vfat[[:space:]]\+\)\([^[:space:]]\+\)|\1\2,noauto,x-systemd.automount,x-systemd.idle-timeout=2min|' /etc/fstab

# 3. VERIFY the line looks right — one line, fields intact:
grep firmware /etc/fstab
#   PARTUUID=...  /boot/firmware  vfat  defaults,noauto,x-systemd.automount,x-systemd.idle-timeout=2min  0  2

# 4. confirm fstab is still valid, then reboot
findmnt --verify --fstab && sudo reboot
```

If step 3 or 4 looks wrong, restore and stop:
`sudo cp /etc/fstab.bak-* /etc/fstab`. The box boots fine either way — the
firmware doesn't need Linux to mount that partition.

Diminishing returns past that: cold boot to READY is ~19s, of which ~3.9s
is the kernel, ~5.8s the SD card's partitions merely appearing, and ~4s the
UI process (mostly cold SD reads of Python imports). All three want a faster
A2 card or Buildroot, not another config tweak — a quick A2 card is the
lowest-effort win and touches every one of those blocks at once.

## Why this exists

Kids' audio today comes in two flavours: streaming apps on a screen
the parents would rather not hand over, or closed hardware boxes
(Tonies, Yoto) that sell cheap and recoup the margin through a
proprietary content store — paying again for stories and music the
family already has through Spotify or public broadcasting.

vibb is the third option: a dedicated, durable little player the
kid fully owns and operates, fed by the subscriptions and free feeds
the family already pays for, curated by the parents, working offline,
with no accounts, ads or algorithms anywhere near the child. Seventeen
hand-picked albums beat a hundred million songs.

## Future directions

**RFID cards (the original concept).** vibb started as an
RFID-controlled speaker — tap a physical card to play its album, an
open-hardware answer to the Toniebox. The screen-and-buttons player
turned out to be the better product for our field testers, so the
card flow is parked, but the code still ships: a PN532 reader (I2C)
is supported end-to-end, `install.sh` prepares for it, and the
service is installed disabled. To experiment:

```
sudo systemctl enable --now vibb-rfid
# slot-mode switch and options: /etc/vibb/rfid.conf
# map a card: sudo vibb-card map <link>   then tap/insert the card
```

The card-player design lives in
[SPEC-B-card-player.md](./SPEC-B-card-player.md).

## License

TBD. Not yet committed.
