"""Audio output plumbing shared by the daemon: which ALSA pcm is active
(bt speaker vs built-in/HAT), and the go-librespot config rewrites that
audio_device / cache size changes require. Extracted verbatim from daemon.py."""

import json
import os
import re
import subprocess

from vibb.paths import STATE_DIR, go_unit_cmd

OUT_FILE = os.path.join(STATE_DIR, "output.json")
GO_CONFIG = os.environ.get("VIBB_GO_CONFIG", "")  # go-librespot config.yml
OUTPUT_PCMS = {"bt": "vibb_bt",
               "local": os.environ.get("VIBB_LOCAL_PCM", "vibb_local")}


def log(msg):
    print(f"vibbd: {msg}", flush=True)


def local_volume(stored, pcm, cap, everywhere=False):
    """The volume to actually use, given where the sound is going.

    The built-in speaker and a pair of headphones do not share a scale,
    and the box keeps ONE volume number. So the level a parent set for
    a child's headphones — often near the top, they are quiet — becomes
    a room-filling level the moment audio lands on the HAT's amplifier.
    That matters because the speaker is what the box falls back to in
    the dark, when a child has just pulled dead headphones off: the
    loudest event this box can produce was, until now, the one action
    it offers in exactly that moment.

    Cap it. Applied at USE, never written back (`_save_volume` is the
    only writer and must keep meaning "what the user chose"), so the
    headphone level is still there when the headphones come back.
    cap=0 disables the whole thing.

    everywhere=True (AM-7): the audio policy self-test found a SAFETY
    drift — a stream might reach the HAT without vibb choosing it — so
    until the next green run the cap applies on EVERY output. Nothing is
    refused (the bedtime rule); the worst case anywhere is the cap.
    """
    if not cap or (pcm != OUTPUT_PCMS["local"] and not everywhere):
        return stored
    return min(stored, cap)


def resize_spotify_cache(gb):
    """Write the size limit into go-librespot's config (startup-only there,
    like audio_device) and restart it. Eviction prunes on next start."""
    if not GO_CONFIG:
        return
    try:
        with open(GO_CONFIG) as f:
            text = f.read()
    except OSError:
        return
    new, n = re.subn(r"(?m)^(\s*size_limit:).*$", rf"\g<1> {gb}GB", text, count=1)
    if n == 0 or new == text:
        return
    with open(GO_CONFIG + ".tmp", "w") as f:
        f.write(new)
    os.replace(GO_CONFIG + ".tmp", GO_CONFIG)
    log(f"spotify cache limit -> {gb}GB (restarting go-librespot)")
    try:
        subprocess.run(go_unit_cmd("restart"), timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"go-librespot restart failed ({e!r}) — restart it manually")


def set_spotify_bitrate(kbps):
    """Write the stream bitrate into go-librespot's config (startup-only,
    like audio_device/size_limit) and restart it. 320 is best quality but
    doubles CDN airtime per track on the shared 2.4GHz radio — the disk
    cache absorbs that for repeat plays."""
    if not GO_CONFIG:
        return
    try:
        with open(GO_CONFIG) as f:
            text = f.read()
    except OSError:
        return
    line = f"bitrate: {kbps}  # 96 | 160 | 320 (kbps, Ogg Vorbis)"
    new, n = re.subn(r"(?m)^\s*bitrate:.*$", line, text, count=1)
    if n == 0:
        new = text.rstrip("\n") + "\n" + line + "\n"
    if new == text:
        return
    with open(GO_CONFIG + ".tmp", "w") as f:
        f.write(new)
    os.replace(GO_CONFIG + ".tmp", GO_CONFIG)
    log(f"spotify bitrate -> {kbps} kbps (restarting go-librespot)")
    try:
        subprocess.run(go_unit_cmd("restart"), timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"go-librespot restart failed ({e!r}) — restart it manually")


# --- audio output (bt speaker vs built-in/HAT) ----------------------------------

def _i2s_card_present():
    try:
        with open("/proc/asound/cards") as f:
            return "sndrpihifiberry" in f.read()
    except OSError:
        return False


def current_output():
    try:
        with open(OUT_FILE) as f:
            d = json.load(f)
        return {"output": d.get("output") or "bt",
                "pcm": d.get("pcm") or "vibb_bt"}
    except (OSError, ValueError):
        return {"output": "bt", "pcm": "vibb_bt"}


def _write_audio_device(pcm):
    """Persist audio_device=pcm in config.yml for the NEXT process start.
    Returns True when the file actually changed. Never restarts."""
    if not GO_CONFIG:
        return False
    try:
        with open(GO_CONFIG) as f:
            text = f.read()
    except OSError:
        return False
    new, n = re.subn(r"(?m)^audio_device:.*$", f"audio_device: {pcm}", text)
    if n == 0:
        new = text.rstrip("\n") + f"\naudio_device: {pcm}\n"
    if new == text:
        return False
    try:
        with open(GO_CONFIG + ".tmp", "w") as f:
            f.write(new)
        os.replace(GO_CONFIG + ".tmp", GO_CONFIG)
    except OSError:
        return False
    return True


def reopen_go_output(pcm):
    """v0.0.7: reopen go-librespot's audio output on `pcm` LIVE — keeping
    the Spotify session (track / position / paused / volume) intact. No
    restart, no bookmark-resume, and no session teardown that re-bursts
    the shared 2.4GHz radio. Also fixes the 'output died with the BT
    transport and stays dead' bug (the reopen rebuilds the device without
    a restart). Persists audio_device for the next process start too.
    Returns True on a live reopen; False if the endpoint is unreachable
    or too old (a pre-v0.0.7 binary 404s) — the caller falls back to the
    config-rewrite + restart path."""
    from vibb import spotify  # local: avoid an import cycle at module load
    _write_audio_device(pcm)  # persist for boot; best-effort, no restart
    try:
        spotify.go("/player/output", timeout=5, body={"device": pcm})
        return True
    except OSError:  # unreachable / 404 on an old binary (HTTPError is OSError)
        return False


def _retarget_go_librespot(pcm):
    """FALLBACK for a pre-v0.0.7 go-librespot: point audio_device at pcm
    and restart (its device is startup config there). Returns True when
    the config was changed (and a restart was issued)."""
    if not _write_audio_device(pcm):
        return False
    try:
        subprocess.run(go_unit_cmd("restart"), timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"go-librespot restart failed ({e!r}) — config updated, "
            "restart it manually")
    return True




def audio_ready():
    """Is the active output able to make sound right now? BT speakers
    drop out and reconnect; nobody should play into a void.

    Under the PipeWire stack 'able' also means the SINK NODE exists: a
    pcm pinned to an absent node fails at hw_params (bench 2026-09-03),
    the BT transport precedes its node by milliseconds, and the HAT node
    only exists once WirePlumber is up — the first tap after a reboot can
    beat it (NEW-3). Answering False there keeps the crash healer from
    burning its per-boot budget on 'server not up yet'."""
    from vibb import audio as _audio
    pipewire = _audio.stack() == "pipewire"
    if current_output()["output"] == "local":
        if not _i2s_card_present():
            return False
        return not pipewire or _audio.sink_ready("local")
    from vibb import bt as _bt, btbus
    try:
        mac = open(_bt.MAC_FILE).read().strip()
    except OSError:
        return True  # no speaker configured — nothing to wait for
    if not mac:
        return True
    if not btbus.a2dp_pcm_present(mac):
        return False
    return not pipewire or _audio.sink_ready("bt", mac)
