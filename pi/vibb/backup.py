"""Backup & restore of the box's critical state to a restic repo.

vibb keeps everything as small JSON/plaintext files on the SD card. When the
card dies (and a battery box yanked by toddlers kills cards), the owner loses
the whole curated library, all settings, the token secret, the Storytel and
Spotify logins, RFID mappings, the BT speaker MAC, and every child's place in
every book. None of that is downloadable again.

This module snapshots exactly that critical state — CONFIG + SECRET +
PROGRESS, never the regenerable caches or the GBs of downloaded audio — into
a **restic** repo. restic is the engine because it brings authenticated
encryption, dedup, incremental snapshots, retention and an integrity check
for free — less crypto we own, and it fails *cleanly* on a wrong password
rather than an unauthenticated cipher decrypting to garbage.

The backend is the owner's choice, not ours: restic reaches it through
**rclone**, so any of rclone's ~70 backends (S3, B2, WebDAV/Nextcloud,
Jottacloud, Drive, Dropbox, SFTP …) works. The owner sets it up entirely in
the PWA by pasting an rclone remote config block; nothing here is tied to any
one provider. The restic repo is then `rclone:<remote>:<path>`.

Kept deliberately self-contained — imports only `vibb.paths` — so it never
reintroduces the wide-import coupling that has bitten the subprocess modules.
The restic/rclone shell-outs live behind thin wrappers; the load-bearing
logic (which files, and the atomic two-phase restore) needs no binary and is
unit-tested on a tmp tree.
"""

import glob
import json
import os
import shutil
import signal
import subprocess
import time
import urllib.request

from vibb.paths import ART_DIR, STATE_DIR, clock_trusted

# --- locations (env-overridable so tests point them at a tmp tree) ----------
ETC = os.environ.get("VIBB_ETC", "/etc/vibb")

# The config/secret files each owning module can be pointed elsewhere with an
# env var. We read the SAME variables rather than hardcoding the filenames:
# a literal "storytel.json" here would silently back up nothing whenever
# VIBB_STORYTEL_CREDS is set, and a backup that quietly omits the account is
# worse than no backup (architect review 2026-08-17). Importing those modules
# instead would drag the daemon's world into a subprocess that must stay
# thin, so the env var — the actual contract — is what is shared.
LIBRARY_FILE = os.environ.get("VIBB_LIBRARY", os.path.join(ETC, "library.json"))
SETTINGS_FILE = os.environ.get("VIBB_SETTINGS", os.path.join(ETC, "settings.json"))
STORYTEL_CREDS = os.environ.get("VIBB_STORYTEL_CREDS",
                                os.path.join(ETC, "storytel.json"))
SPOTIFY_API_CREDS = os.environ.get("VIBB_SPOTIFY_API",
                                   os.path.join(ETC, "spotify-api.json"))
BT_MAC_FILE = os.environ.get("VIBB_BT_FILE", os.path.join(ETC, "bt-headset"))
RESTIC_BIN = os.environ.get("VIBB_RESTIC_BIN", "restic")
RCLONE_BIN = os.environ.get("VIBB_RCLONE_BIN", "rclone")
RCLONE_CONF = os.environ.get("VIBB_RCLONE_CONF", os.path.join(ETC, "rclone.conf"))
RESTIC_PASS_FILE = os.environ.get(
    "VIBB_RESTIC_PASS", os.path.join(ETC, "restic-pass"))
# Where the chosen restic repo string ({"repo": "rclone:remote:path"}) lives.
# Written at setup, read on every call — the backend is not known at import.
BACKUP_CONF = os.environ.get("VIBB_BACKUP_CONF", os.path.join(ETC, "backup.json"))
DEFAULT_REPO_PATH = "vibb-backup"

# go-librespot's config dir holds credentials.json + state.json. The daemon
# already knows it via VIBB_GO_CONFIG (a path to config.yml); we take its
# directory. Empty when unset -> those two files are simply skipped.
_GO_CONFIG = os.environ.get("VIBB_GO_CONFIG", "")
GO_DIR = os.path.dirname(_GO_CONFIG) if _GO_CONFIG else ""

MANIFEST_FORMAT = "vibb-backup/1"
# Bump when a restore would need code this version lacks. A backup whose
# schema is HIGHER than this is refused (it may carry fields we cannot apply);
# an equal-or-lower one is fine — each module already migrates old shapes on
# read (bookmarks.py, library.py).
SCHEMA = 1

# Files under STATE_DIR that are transient flow state, NOT progress worth
# keeping — rewritten every play/poll. Everything else ending in .json is
# taken (arbitrary bookmark keys included); non-.json markers like
# bt-connect-kick / bt-quiet are skipped by the .json filter.
_STATE_EXCLUDE = frozenset({
    "now-playing.json", "now-queue.json", "output.json", "renderer.json",
    "sonos.json", "last-sweep.json", "spotify-precache.json",
    "podcast-new.json", "on-battery-runtime.json",
    "storytel-shelf-raw.json", "storytel-login-refused.json",
    # A cached bearer JWT (storytel.py writes it 0600, one hour TTL). It is
    # re-minted from the credentials we already carry, so backing it up buys
    # nothing — and it would ride in the PROGRESS tier, which restores 0644,
    # turning a 0600 session token world-readable (QA 2026-08-17).
    "storytel-session.json",
    # Our own bookkeeping about backup runs — restoring a stale "last backup
    # succeeded" would misreport the health of the thing doing the restoring.
    "backup-last.json",
})

RESTORE_TMP_SUFFIX = ".vibbrestore.tmp"


# --- the whitelist ----------------------------------------------------------
def _config_files():
    out = [p for p in (LIBRARY_FILE, SETTINGS_FILE, BT_MAC_FILE,
                       os.path.join(ETC, "cards.json"),
                       os.path.join(ETC, "rfid.conf"))
           if os.path.exists(p)]
    out += sorted(glob.glob(os.path.join(ART_DIR, "section-*.jpg")))
    return out


def _secret_files():
    out = [p for p in (STORYTEL_CREDS, SPOTIFY_API_CREDS) if os.path.exists(p)]
    if GO_DIR:
        for name in ("credentials.json", "state.json"):
            p = os.path.join(GO_DIR, name)
            if os.path.exists(p):
                out.append(p)
    return out


def _progress_files():
    out = []
    try:
        names = sorted(os.listdir(STATE_DIR))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json") or name in _STATE_EXCLUDE:
            continue
        out.append(os.path.join(STATE_DIR, name))
    return out


def _iter_files():
    for p in _config_files():
        yield p, "config"
    for p in _secret_files():
        yield p, "secret"
    for p in _progress_files():
        yield p, "progress"


def _box_id():
    try:
        with open("/etc/machine-id") as f:
            return f.read().strip()
    except OSError:
        return ""


# --- collect: build the file set + manifest in a staging dir ----------------
def collect(staging):
    """Copy the whitelist into <staging>/files/<abspath>, write manifest.json.
    Returns the manifest dict. No restic, no network — this is the unit that
    decides WHAT is backed up, and is tested directly."""
    files_root = os.path.join(staging, "files")
    entries = []
    for src, tier in _iter_files():
        # One guard around stat AND copy: a bookmark can be pruned or a
        # session file cleared between the two (spotify.py prunes
        # spotify-bm-*, storytel logout removes its state), and a vanished
        # file must skip, never abort the whole backup (QA 2026-08-17).
        try:
            st = os.stat(src)
            dst = os.path.join(files_root, src.lstrip("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        except OSError:
            continue
        entries.append({"path": src, "tier": tier,
                        "mode": oct(st.st_mode & 0o777)})
    manifest = {
        "format": MANIFEST_FORMAT,
        "schema": SCHEMA,
        "created": int(time.time()),
        "box_id": _box_id(),
        "tiers": sorted({e["tier"] for e in entries}),
        "token_included": False,   # api-token is deliberately never in the set
        "files": entries,
    }
    with open(os.path.join(staging, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    return manifest


# --- restore: two-phase atomic apply of an already-restored tree ------------
def _check_restorable(manifest):
    fmt = manifest.get("format")
    if fmt != MANIFEST_FORMAT:
        raise ValueError(f"unknown backup format {fmt!r}")
    if int(manifest.get("schema") or 0) > SCHEMA:
        raise ValueError(
            f"backup schema {manifest.get('schema')} is newer than this box "
            f"understands ({SCHEMA}) — upgrade vibb before restoring")


def _restorable(real):
    """Is this exactly one of the things collect() would ever back up?

    Validating by DIRECTORY was not enough. /etc/vibb also holds
    api-token — restore could set the box's own credential to an
    attacker-chosen value — and extras/, whose scripts ui.py lists and runs
    AS ROOT, so a manifest could drop a root-owned executable there and the
    screen would offer to launch it. Neither is in the backup set, so
    neither may be a restore target: match the whitelist itself, not the
    folder it happens to live in (QA 2026-08-17).
    """
    if real in {os.path.realpath(p) for p in
                (LIBRARY_FILE, SETTINGS_FILE, BT_MAC_FILE,
                 STORYTEL_CREDS, SPOTIFY_API_CREDS,
                 os.path.join(ETC, "cards.json"),
                 os.path.join(ETC, "rfid.conf"))}:
        return True
    art = os.path.realpath(ART_DIR)
    name = os.path.basename(real)
    if os.path.dirname(real) == art:
        return name.startswith("section-") and name.endswith(".jpg")
    if GO_DIR and os.path.dirname(real) == os.path.realpath(GO_DIR):
        return name in ("credentials.json", "state.json")
    if os.path.dirname(real) == os.path.realpath(STATE_DIR):
        return name.endswith(".json") and name not in _STATE_EXCLUDE
    return False


def _check_dest(dest):
    """Refuse any destination outside the directories collect() draws from.

    The manifest decides where bytes land, and vibb-daemon runs as ROOT
    (install.sh gives it no User=). Without this, a manifest entry of
    "/etc/systemd/system/x.service" or "/root/.ssh/authorized_keys" turns
    restore into an arbitrary root-owned overwrite — and the repo password
    that would gate such a manifest sits on the same SD card an attacker
    would have had to reach anyway. Defence in depth on the one path that
    writes over live secrets (QA 2026-08-17).

    Symlinks are resolved first, so a planted link inside an allowed root
    cannot redirect the write outside it.
    """
    if not isinstance(dest, str) or not dest or not dest.startswith("/"):
        raise ValueError(f"backup entry has a non-absolute path: {dest!r}")
    real = os.path.realpath(dest)
    if _restorable(real):
        return real
    raise ValueError(
        f"backup entry is not something this box backs up: {dest!r}")


def _target_owner(dest):
    """(uid, gid) the restored file should end up owned by: the existing
    file's owner, else the deepest existing ancestor dir's owner (so a
    go-librespot cred restored before its dir exists still lands owned by the
    run user who owns ~/.config, not root). None when nothing exists yet."""
    if os.path.lexists(dest):
        st = os.lstat(dest)
        return (st.st_uid, st.st_gid)
    d = os.path.dirname(dest)
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    if d:
        st = os.stat(d)
        return (st.st_uid, st.st_gid)
    return None


def _makedirs_owned(path, owner):
    """makedirs, chowning any directories we create to `owner` so a fresh
    ~/.config/go-librespot the run user must read is not left root-owned."""
    if os.path.isdir(path):
        return
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        _makedirs_owned(parent, owner)
    os.mkdir(path)
    if owner:
        try:
            os.chown(path, *owner)
        except OSError:
            pass


def apply_tree(tree):
    """Apply a restored tree (<tree>/manifest.json + <tree>/files/...) to the
    live filesystem, atomically. Two phases: validate + stage every file,
    then a rename sweep — so a bundle that fails validation writes nothing,
    and a crash mid-commit still leaves each individual file whole (old or
    new, never torn). Reused by restore_snapshot; tested directly."""
    with open(os.path.join(tree, "manifest.json")) as f:
        manifest = json.load(f)
    _check_restorable(manifest)
    files_root = os.path.join(tree, "files")

    # Phase 1 — validate and stage beside each destination.
    staged = []   # (tmp, dest, owner)
    try:
        for entry in manifest["files"]:
            dest = _check_dest(entry.get("path"))
            src = os.path.join(files_root, dest.lstrip("/"))
            if not os.path.isfile(src):
                raise ValueError(f"backup is missing its file for {dest}")
            with open(src, "rb") as f:
                data = f.read()
            if dest.endswith(".json"):
                json.loads(data)   # torn/garbage json -> reject before commit
            # secrets are forced 0600 at creation, never chmod-after; other
            # files keep their recorded mode (default 0644).
            # Masked to 0o777: a manifest asking for 0o4755 would otherwise
            # have restore drop a setuid-root binary (QA 2026-08-17).
            try:
                mode = int(entry.get("mode", "0o644"), 8) & 0o777
            except (TypeError, ValueError):
                mode = 0o644
            if entry.get("tier") == "secret":
                mode = 0o600
            owner = _target_owner(dest)   # BEFORE makedirs, so it is the real
            #                               pre-existing owner, not root
            parent = os.path.dirname(dest)
            if parent:
                _makedirs_owned(parent, owner)
            tmp = dest + RESTORE_TMP_SUFFIX
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            staged.append((tmp, dest, owner))
    except BaseException:
        for tmp, _dest, _owner in staged:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise

    # Phase 2 — commit. os.replace is atomic on the same filesystem, which is
    # why the tmp sits beside its destination and not in a shared staging dir.
    for tmp, dest, owner in staged:
        if owner:
            try:
                os.chown(tmp, *owner)
            except OSError:
                pass
        os.replace(tmp, dest)
    return manifest


# --- backend config (generic: any rclone remote, pasted in the PWA) ---------
def load_repo():
    """The configured restic repo string, or "" when unset."""
    try:
        with open(BACKUP_CONF) as f:
            return json.load(f).get("repo") or ""
    except (OSError, ValueError):
        return ""


def configured():
    """True once a backend has been set up (repo pointer + repo password)."""
    return bool(load_repo()) and os.path.exists(RESTIC_PASS_FILE)


LAST_FILE = os.path.join(STATE_DIR, "backup-last.json")


def status():
    """Non-secret view for the PWA: is a backend set up, where to (the repo
    string names the remote, never a credential), and — the number that
    actually says whether this feature is working — when a backup last
    SUCCEEDED. Without it a box whose every run has failed for a month looks
    identical to a healthy one (QA 2026-08-17)."""
    out = {"configured": configured(), "repo": load_repo(),
           "last_ok": None, "last_error": None}
    try:
        with open(LAST_FILE) as f:
            last = json.load(f)
        out["last_ok"] = last.get("ok_at")
        out["last_error"] = last.get("error")
    except (OSError, ValueError):
        pass
    return out


def _note_run(ok_at=None, error=None):
    """Record the outcome of a run. Best-effort: a backup that worked must
    not be reported as failed because this bookkeeping could not be written."""
    try:
        prev = {}
        try:
            with open(LAST_FILE) as f:
                prev = json.load(f)
        except (OSError, ValueError):
            pass
        if ok_at:
            prev["ok_at"] = ok_at
            prev["error"] = None
        else:
            prev["error"] = error
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = LAST_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(prev, f)
        os.replace(tmp, LAST_FILE)
    except OSError:
        pass


def _remote_name(repo):
    """The rclone remote inside an `rclone:remote:path` repo, else "" (a
    restic-native repo string needs no rclone validation)."""
    if repo.startswith("rclone:"):
        parts = repo.split(":", 2)   # ["rclone", "remote", "path…"]
        if len(parts) >= 2:
            return parts[1]
    return ""


def _rclone_remotes(conf_text):
    """Remote names declared in a pasted rclone.conf ([name] section heads)."""
    names = []
    for line in conf_text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            names.append(line[1:-1].strip())
    return names


def configure(rclone_conf_text, repo=None, repo_password=None, path=None):
    """Set up any rclone-backed restic repo, from values pasted in the PWA.

    `rclone_conf_text` is one or more rclone remote blocks the owner generated
    on their own machine (`rclone config` — which handles OAuth backends too).
    `repo` may be given explicitly (`rclone:remote:path`); otherwise it is
    built from the first pasted remote and `path` (default 'vibb-backup').
    `repo_password` encrypts the restic repo and is stored 0600 on the box.

    Idempotent. Raises RuntimeError on any failure, having stored nothing that
    would leave a half-configured box.
    """
    if not rclone_conf_text or not rclone_conf_text.strip():
        raise RuntimeError("paste an rclone remote configuration first")
    if not repo_password:
        raise RuntimeError("a repo password is required")
    remotes = _rclone_remotes(rclone_conf_text)
    if not repo:
        if not remotes:
            raise RuntimeError("no [remote] section found in the pasted config")
        repo = f"rclone:{remotes[0]}:{path or DEFAULT_REPO_PATH}"
    remote = _remote_name(repo)
    if remote and remote not in remotes:
        raise RuntimeError(
            f"repo names remote {remote!r} but the pasted config has "
            f"{remotes or 'no remotes'}")

    # Write rclone.conf and the password FIRST — restic/rclone read them from
    # disk, so they cannot be validated without being written — but hold back
    # BACKUP_CONF, the repo pointer configured() keys on, until the backend
    # has actually answered. Committing all three up front left a box that
    # reported "Connected" and failed every 6h run against a repo that was
    # never initialised (QA 2026-08-17). On failure the two written files are
    # removed, so a retry starts clean.
    prev_conf = _read_bytes(RCLONE_CONF)
    prev_pass = _read_bytes(RESTIC_PASS_FILE)
    _write_secret_text(RCLONE_CONF, rclone_conf_text)
    _write_secret_text(RESTIC_PASS_FILE, repo_password)
    try:
        env = dict(os.environ)
        env["RCLONE_CONFIG"] = RCLONE_CONF
        if remote:
            try:
                subprocess.run(
                    [RCLONE_BIN, "lsd", f"{remote}:"],
                    env=env, capture_output=True, text=True, timeout=60,
                    check=True)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                    OSError) as e:
                detail = (getattr(e, "stderr", "") or str(e)).strip()
                raise RuntimeError(f"could not reach the backend: {detail}")
        # init the repo unless it already answers to this password
        if _restic("cat", "config", timeout=60, repo=repo).returncode != 0:
            r = _restic("init", timeout=120, repo=repo)
            if r.returncode != 0:
                raise RuntimeError(f"restic init failed: {r.stderr.strip()}")
    except BaseException:
        _restore_bytes(RCLONE_CONF, prev_conf)
        _restore_bytes(RESTIC_PASS_FILE, prev_pass)
        raise
    _write_secret_json(BACKUP_CONF, {"repo": repo})
    return status()


def _read_bytes(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _restore_bytes(path, data):
    """Put a secret file back the way it was (or remove it if there was
    none), so a failed setup leaves no half-configured backend behind."""
    try:
        if data is None:
            os.unlink(path)
        else:
            _write_secret(path, data)
    except OSError:
        pass


# --- restic / rclone shell-outs ---------------------------------------------
def _restic_env(repo=None):
    env = dict(os.environ)
    # `repo` is passed only during setup, before BACKUP_CONF is committed.
    env["RESTIC_REPOSITORY"] = repo or load_repo()
    env["RESTIC_PASSWORD_FILE"] = RESTIC_PASS_FILE
    env["RCLONE_CONFIG"] = RCLONE_CONF
    # Set HERE, not only in vibb-backup.service: the PWA's routes call this
    # module IN-PROCESS from vibb-daemon, whose unit carries none of these.
    # Relying on the unit file meant the timer/idle path was restrained and
    # the daemon path ran with Go's defaults (GOGC=100, GOMAXPROCS=4) and no
    # restic cache — measured ~124MB RSS against mpv, re-fetching the whole
    # index every time (QA 2026-08-17). Env travels with the call; a unit
    # file does not. setdefault so the unit can still tighten them.
    env.setdefault("GOMEMLIMIT", "80MiB")
    env.setdefault("GOGC", "20")
    env.setdefault("GOMAXPROCS", "1")
    env.setdefault("RESTIC_CACHE_DIR", "/var/cache/vibb-restic")
    return env


def _kill_group(proc):
    """Take down restic AND the `rclone serve restic --stdio` it spawned."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def _restic(*args, timeout=300, check=False, repo=None, watch=False):
    """Run restic, pointing it at our rclone (so it need not be on PATH) and
    our repo/password via env. The password goes by FILE, and the backend
    credentials live in rclone.conf, so neither ever reaches argv or a
    subprocess stderr we might surface in an HTTP error."""
    # start_new_session: restic spawns `rclone serve restic --stdio` as a
    # child. On a timeout subprocess kills only restic, and the stranded
    # rclone sits on tens of MB of RSS on a 512MB box — so give the pair its
    # own process group and take the group down (QA 2026-08-17).
    proc = subprocess.Popen(
        [RESTIC_BIN, "-o", f"rclone.program={RCLONE_BIN}", *args],
        env=_restic_env(repo), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True)
    try:
        if watch:
            # Checking once before we start is not enough: a run takes tens
            # of seconds, and a kid can tap play at any point inside it. The
            # content sweeper already solves this exact problem by watching
            # and terminating the child mid-download — 'the radio belongs to
            # the music'. Same rule here, and cheaper to obey: an abandoned
            # backup costs nothing, because restic dedups and the next run
            # re-uploads only what is still missing.
            deadline = time.monotonic() + timeout
            while proc.poll() is None:
                if time.monotonic() > deadline:
                    raise subprocess.TimeoutExpired(proc.args, timeout)
                if _box_busy():
                    _kill_group(proc)
                    raise Yielded("the box started playing")
                time.sleep(WATCH_POLL_S)
            out, err = proc.communicate()
        else:
            out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        proc.communicate()
        raise
    except Yielded:
        proc.communicate()
        raise
    r = subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
    if check and r.returncode != 0:
        raise subprocess.CalledProcessError(r.returncode, proc.args, out, err)
    return r


def _write_secret_text(path, text):
    """0600 from creation, atomic rename — the token._write pattern, for raw
    text (rclone.conf, the restic repo password)."""
    _write_secret(path, (text.rstrip("\n") + "\n").encode())


def _write_secret_json(path, obj):
    _write_secret(path, json.dumps(obj).encode())


def _write_secret(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def backup_now(keep=("--keep-daily", "7", "--keep-weekly", "4",
                     "--keep-monthly", "6"), watch=False):
    """Snapshot the whitelist to the repo, then prune to the retention
    policy. Builds the set in a private tmpfs dir and removes it after.
    Returns {snapshot_id, created, files}. Raises RuntimeError on failure."""
    if not configured():
        raise RuntimeError("no backup backend configured")
    staging = _mkstaging()
    try:
        manifest = collect(staging)
        # watch=True only here and on the prune below: a RESTORE is
        # user-initiated and must never be abandoned halfway.
        r = _restic("backup", "--json", os.path.join(staging, "files"),
                    os.path.join(staging, "manifest.json"), watch=watch)
        if r.returncode != 0:
            err = f"restic backup failed: {r.stderr.strip()}"
            _note_run(error=err)
            raise RuntimeError(err)
        snap_id = _parse_backup_snapshot(r.stdout)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    _note_run(ok_at=manifest["created"])
    # retention — best effort; a prune failure must not fail the backup that
    # already succeeded. The bare call did not honour that: a TimeoutExpired
    # or OSError raised straight out and turned a completed backup into a
    # 500 (QA 2026-08-17).
    try:
        _restic("forget", "--prune", *keep, timeout=600, watch=watch)
    except Exception as e:
        log_line = f"backup: retention pass failed, snapshot is safe: {e}"
        print(log_line)
    return {"snapshot_id": snap_id, "created": manifest["created"],
            "files": len(manifest["files"])}


# One real backup a day. The TIMER only wakes us — this wall-clock gate is
# what sets the cadence, and unlike a monotonic timer it survives the reboots
# a toddler causes (see main()).
# One backup per CALENDAR DAY (local time), not "24h since the last one".
# Elapsed-time gating drifted the backup later each day and skipped any day
# whose listening session ended earlier than the day before — every-other-
# day in practice for a child who listens each afternoon (owner 2026-08-19).
# Calendar day also matches restic's own retention bucketing. clock_trusted()
# is checked before this gate, so the local date is real. Set VIBB_BACKUP_DAILY=0
# to disable the gate for a forced run (the tests use it).
DAILY_GATE = os.environ.get("VIBB_BACKUP_DAILY", "1") != "0"


def _backed_up_today(last_ok):
    """True if the last successful backup falls on today's local date."""
    a, b = time.localtime(last_ok), time.localtime()
    return (a.tm_year, a.tm_yday) == (b.tm_year, b.tm_yday)
# How long to keep waiting for the music to stop before giving up until the
# next wake. Deferring is nearly free for a backup; stopping the music is not.
BUSY_WAIT_S = int(os.environ.get("VIBB_BACKUP_BUSY_WAIT", 600))
BUSY_RECHECK_S = 30
DAEMON_URL = os.environ.get("VIBB_DAEMON_URL", "http://127.0.0.1:3679")
# How recent a button press still counts as "hands on the box".
ACTIVITY_FRESH_S = 120
# How often a running backup re-checks whether the music started.
WATCH_POLL_S = 3


def _link_up():
    """Is wifi actually up? WiFi powers OFF to save battery when the box is
    away from known networks, and restic's retry ladder would then burn
    minutes before failing. A 10ms sysfs read instead — same file netmgmt
    reads, without importing it (this module stays thin by contract)."""
    try:
        with open("/sys/class/net/wlan0/operstate") as f:
            return f.read().strip() == "up"
    except OSError:
        return True   # unknown -> try anyway, never stall forever on a probe


class Yielded(Exception):
    """Raised when a backup stood down because the box got busy mid-run.
    Not a failure: nothing is broken, we simply lost the race for the radio
    and will try again at the next shutdown. Mirrors library.SweepYield."""


def _hands_on_box():
    """Someone is pressing buttons right now. Browsing is not playback, so
    /status reads idle — but a backup competing for CPU and the SD card
    while a child works the menu still shows up as a sluggish screen. The
    activity marker is the same one vibb-idle uses to hold auto-off."""
    try:
        from vibb.paths import last_activity
        age = time.time() - last_activity()
        return 0 <= age < ACTIVITY_FRESH_S
    except Exception:
        return False


def _poweroff_imminent():
    """True when vibb-idle stamped its marker moments ago: this run is
    the on-the-way-down backup, and the box has ALREADY been idle for
    its whole timeout. The marker lives in RUN_DIR (tmpfs), so it cannot
    survive a boot and leak into the next day's timer runs; the
    freshness window only has to cover the hook's own lifetime
    (vibb-idle's BACKUP_MAX_S, plus slack)."""
    from vibb.paths import RUN_DIR
    try:
        age = time.time() - os.path.getmtime(
            os.path.join(RUN_DIR, "poweroff-imminent"))
    except OSError:
        return False
    return 0 <= age < 600


def _box_busy():
    """True while the box is playing OR a Bluetooth speaker is live.

    The radio rule this box runs on: 'whoever is doing something
    time-critical owns the radio; the side that CAN wait a few seconds,
    waits' (vibb/radio.py). A backup is always the side that can wait —
    an upload shares the single 2.4GHz radio with both the Spotify stream
    and the A2DP link, and NM/TLS bursts mid-playback are the documented
    stutter-and-firmware-crash trigger (library.py, netmgmt.py).

    Read off the daemon's own /status, which is token-free by design.
    `bt_connected` is only present when the BT speaker is the ACTIVE output,
    so its presence means a live A2DP PCM right now. Counting a PAUSED
    session as busy is deliberate — a kid mid-listen resumes any second.

    EXCEPT on the way down: with poweroff imminent (vibb-idle's marker),
    a connected-but-silent link no longer counts. The box has been idle
    for its whole timeout — the "resumes any second" bet has already
    lost — but a speaker someone left switched on kept this predicate
    true, so every shutdown backup sat out its wait and was killed by
    vibb-idle's own timeout (field journal 2026-08-20: 190s then TERM,
    every time; the repo showed no new snapshots). The exception must
    live HERE, not in the wait loop: backup_now(watch=True) polls this
    same predicate mid-run, and a loop-only bypass just moved the stall
    three seconds later. The owner's invariant survives untouched —
    `playing` and fresh button presses still abort a running upload,
    so nothing wifi-heavy ever overlaps ACTIVE A2DP playback; only the
    silent link stops counting, and only while the marker is fresh.

    Fails OPEN: a broken check must never stall backups forever, the same
    rule library.py's own busy check follows.
    """
    try:
        with urllib.request.urlopen(DAEMON_URL + "/status", timeout=5) as r:
            st = json.loads(r.read() or b"{}")
    except Exception:
        return False
    return bool(st.get("playing") or _hands_on_box() or st.get("warming")
                or (st.get("bt_connected") and not _poweroff_imminent()))


def main(argv=None):
    """Entry point for the systemd timer — a real one, not a shell one-liner,
    so the clock gate and the logging live in code rather than in a quoted
    string. Exit 0 when there is nothing to do: an unconfigured box, or a
    clock we cannot trust yet.

    The clock gate matters more than it looks. The Zero has no RTC, so early
    in a boot the wall clock is roughly 'whenever the box was last switched
    off' — and restic buckets retention by CALENDAR DAY. Backing up under a
    wrong clock files snapshots in the wrong bucket, and repeated toddler
    reboots could collapse several days' keeps into one. The rest of the
    codebase already waits for `clock_trusted()`; so does this
    (architect review 2026-08-17). The next 6h tick picks it up once NTP or
    the PiSugar RTC has landed.
    """
    if not configured():
        print("backup: no backend configured — nothing to do")
        return 0
    if not clock_trusted():
        print("backup: clock not trusted yet (no RTC) — skipping this run")
        return 0
    # The cadence gate. The timer only WAKES us; the cadence is here, in
    # wall clock, so a reboot cannot reset it (a monotonic timer restarts
    # at every boot and systemd does not carry OnUnitActiveSec across one,
    # so putting the cadence in the timer meant one backup per boot, mid-
    # session — QA 2026-08-17). One backup per calendar day: the first
    # idle-shutdown of each new day backs up, whatever the hour, so an
    # afternoon listener is covered every day and the time never drifts.
    last_ok = (status() or {}).get("last_ok")
    if DAILY_GATE and last_ok and _backed_up_today(last_ok):
        # Say so. Every other gate prints its reason; a silent skip here
        # made "already covered" and "hook never ran" identical in the
        # journal (field question 2026-08-20).
        print("backup: already done today "
              f"({time.strftime('%H:%M', time.localtime(last_ok))}) "
              "— nothing to do")
        return 0
    if not _link_up():
        print("backup: no network (wifi is off to save battery) — skipping")
        return 0
    # Only one at a time: the timer and the PWA's "Back up now" would
    # otherwise collide on restic's own repo lock and surface as an error.
    lock = _take_lock()
    if lock is None:
        print("backup: another backup is already running")
        return 0
    try:
        waited = 0
        while _box_busy():
            if waited >= BUSY_WAIT_S:
                print("backup: box still busy — leaving it for the next wake")
                return 0
            time.sleep(BUSY_RECHECK_S)
            waited += BUSY_RECHECK_S
        try:
            # watch=True: stand down if the music starts WHILE we run.
            r = backup_now(watch=True)
        except Yielded as e:
            print(f"backup: stood down mid-run ({e}) — the radio belongs "
                  "to the music; retrying at the next shutdown")
            return 0                 # not a failure, and not an error to
            #                          record: nothing is broken
        except Exception as e:      # incl. subprocess timeouts, not just
            _note_run(error=str(e))  # RuntimeError — a timed-out run must
            print(f"backup: {e}")    # still reach last_error, or the PWA
            return 1                 # reports health it does not have
    finally:
        _release_lock(lock)
    print(f"backup: snapshot {r.get('snapshot_id')} ({r.get('files')} files)")
    return 0


def _take_lock():
    """A non-blocking flock, or None when someone else holds it."""
    import fcntl
    from vibb.paths import RUN_DIR
    try:
        fd = os.open(os.path.join(RUN_DIR, "vibb-backup.lock"),
                     os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _release_lock(fd):
    try:
        os.close(fd)     # closing releases the flock
    except OSError:
        pass


def snapshots():
    """List repo snapshots (newest first): [{id, time, hostname}]. Raises
    RuntimeError if the repo cannot be read."""
    if not configured():
        raise RuntimeError("no backup backend configured")
    r = _restic("snapshots", "--json", timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"restic snapshots failed: {r.stderr.strip()}")
    try:
        raw = json.loads(r.stdout or "[]")
    except ValueError:
        raise RuntimeError("restic returned unreadable snapshot list")
    out = [{"id": s.get("short_id") or s.get("id"),
            "time": s.get("time"), "hostname": s.get("hostname")}
           for s in raw]
    out.reverse()   # restic lists oldest-first; the picker wants newest-first
    return out


def restore_snapshot(snapshot="latest"):
    """Pull one snapshot from the repo and apply it atomically to the live
    box. Returns the applied manifest. Raises RuntimeError/ValueError."""
    if not configured():
        raise RuntimeError("no backup backend configured")
    staging = _mkstaging()
    try:
        r = _restic("restore", snapshot, "--target", staging, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f"restic restore failed: {r.stderr.strip()}")
        # restic restores the absolute paths we backed up: our staging/files
        # and staging/manifest.json reappear under <staging>.
        return apply_tree(staging)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _mkstaging():
    """A private 0700 staging dir on tmpfs (RUN_DIR) — never CACHE_DIR, which
    is served, pruned and pushed."""
    import tempfile
    from vibb.paths import RUN_DIR
    base = os.path.join(RUN_DIR, "vibb-backup")
    os.makedirs(base, exist_ok=True)
    os.chmod(base, 0o700)
    return tempfile.mkdtemp(dir=base)


def _parse_backup_snapshot(stdout):
    """restic backup --json emits one JSON object per line; the final
    'summary' message carries snapshot_id."""
    snap = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("message_type") == "summary" and msg.get("snapshot_id"):
            snap = msg["snapshot_id"]
    return snap


if __name__ == "__main__":
    # LAST statement on purpose. `python3 -m vibb.backup` (the systemd
    # unit — i.e. the timer AND the idle-shutdown hook, the only paths
    # that run in the field) executes this file top to bottom; a guard
    # sitting mid-file calls main() before the defs below it exist.
    # That was live from 17ae067 to 2026-08-20: every unit run that got
    # past the gates died with "name '_mkstaging' is not defined", while
    # the PWA's Back-up-now (daemon import, whole file loaded) worked —
    # which is why the field test passed and the nightly runs did not.
    import sys
    sys.exit(main())
