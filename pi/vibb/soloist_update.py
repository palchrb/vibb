"""The Soloist binary updater (PLAN-pipewire-soloist.md D1, AM-50..52).

Spotify's builds expire after 90 days (exit code 10). The CDN gives a
fixed URL per architecture and, verified 2026-09-04, answers with an
ETag, Content-Length, `accept-ranges: bytes` and a FULL-OBJECT
`x-amz-checksum-crc32c` — and honours If-None-Match. So:

  check   one round trip, no body: 304 = nothing new
  fetch   stream to a tmp next to the binary, RESUMABLE with Range (a
          poweroff mid-download just leaves a tmp the next run continues
          — never trusted without its stored ETag + length)
  verify  CRC-32C of the whole file == the header (not a signature, but
          it catches every truncated or corrupted download), then the
          tar member `soloist` runs `--version` on its own (exec-sanity
          + the build line) before anything is replaced
  swap    os.replace: the old inode stays mapped for a running child,
          the previous binary is kept as `soloist.prev` for one cycle

The gates are the backup's: a trusted clock (TLS against the RTC-less
boot clock fails), nothing audible, and a time budget — the run is
driven from the idle-shutdown hook's second slot and a monotonic timer
(never OnCalendar: Persistent= would fire at boot on a bogus clock).
The redistribution rule holds: the box downloads for itself from
Spotify's URL; the repo never ships the archive. Restarting the child
is the sidecar's decision (AM-52): this module only notifies it.

Stdlib only. crc32c is not in the stdlib (zlib.crc32 is CRC-32/ISO-HDLC):
the 15-line table below is the Castagnoli one, check value 0xE3069283.
"""

import base64
import json
import os
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

from vibb.paths import STATE_DIR, clock_trusted

ARCH_URL = {
    "aarch64": "https://soloist-builds.spotifycdn.com/soloist_release_arm64.tar.gz",
    "armv7l": "https://soloist-builds.spotifycdn.com/soloist_release_arm32.tar.gz",
    "x86_64": "https://soloist-builds.spotifycdn.com/soloist_release_x86_64.tar.gz",
}
URL = os.environ.get("VIBB_SOLOIST_URL") or ARCH_URL.get(os.uname().machine, ARCH_URL["aarch64"])
BIN = os.environ.get("VIBB_SOLOIST_BIN", "/usr/local/bin/soloist")
STATE_FILE = os.environ.get("VIBB_SOLOIST_UPDATE_STATE",
                            os.path.join(STATE_DIR, "soloist-update.json"))
SIDECAR = os.environ.get("VIBB_GO_API", "http://127.0.0.1:3688")
DAEMON_URL = os.environ.get("VIBB_DAEMON", "http://127.0.0.1:3678")
BUDGET_S = float(os.environ.get("VIBB_SOLOIST_UPDATE_BUDGET_S", "120"))
UA = "vibb-soloist-update/1"


def log(msg):
    print(f"soloist-update: {msg}", flush=True)


# --- CRC-32C (Castagnoli), reflected polynomial 0x82F63B78 -------------------
_POLY = 0x82F63B78
_TABLE = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ _POLY if _c & 1 else _c >> 1
    _TABLE.append(_c)


def crc32c(data, crc=0):
    """CRC-32C of `data`, continuing from `crc` (a previous return value)."""
    crc ^= 0xFFFFFFFF
    for b in data:
        crc = _TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def crc32c_b64(value):
    """The header's form: base64 of the big-endian 4-byte CRC."""
    return base64.b64encode(value.to_bytes(4, "big")).decode()


def file_crc32c(path, chunk=1 << 20):
    crc = 0
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                return crc
            crc = crc32c(block, crc)


# --- state ---------------------------------------------------------------------

def state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = f"{STATE_FILE}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


# --- the CDN --------------------------------------------------------------------

def check(url=URL, etag=None, timeout=15):
    """None when the stored ETag still matches (304); else the new build's
    {etag, length, crc32c} from a 1-byte ranged GET (S3 answers 206 with
    the full object's headers — HEAD works too, but a ranged GET is the
    one that also proves accept-ranges for the resume path)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-0",
                                               **({"If-None-Match": etag} if etag else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            h = r.headers
            total = h.get("Content-Range", "").rpartition("/")[2] or h.get("Content-Length")
            return {"etag": (h.get("ETag") or "").strip(),
                    "length": int(total or 0),
                    "crc32c": (h.get("x-amz-checksum-crc32c") or "").strip() or None,
                    "modified": h.get("Last-Modified")}
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return None
        raise


def fetch(url, dest_tmp, expected, deadline, timeout=30):
    """Stream the archive to dest_tmp, resuming a partial tmp ONLY when it
    belongs to the same ETag+length (recorded beside it). Returns True when
    the file is complete and its CRC matches; False on a budget stop (the
    partial stays for the next run); raises on a checksum mismatch."""
    meta = dest_tmp + ".meta"
    have = 0
    try:
        with open(meta) as f:
            m = json.load(f)
        if m.get("etag") == expected["etag"] and m.get("length") == expected["length"]:
            have = os.path.getsize(dest_tmp)
    except (OSError, ValueError):
        have = 0
    if have >= expected["length"] and have:
        pass                                   # complete from a previous run
    else:
        if have == 0:
            for f in (dest_tmp, meta):
                try:
                    os.remove(f)
                except OSError:
                    pass
        with open(meta, "w") as f:
            json.dump({"etag": expected["etag"], "length": expected["length"]}, f)
        headers = {"User-Agent": UA}
        if have:
            headers["Range"] = f"bytes={have}-"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r, \
                open(dest_tmp, "ab" if have else "wb") as out:
            if have and r.status != 206:
                out.seek(0); out.truncate(); have = 0     # server ignored Range
            while True:
                block = r.read(1 << 16)
                if not block:
                    break
                out.write(block)
                # progress before the budget check: every run lands at least
                # one block, so a chain of short runs still converges
                if time.monotonic() > deadline:
                    out.flush()
                    log(f"budget reached at {out.tell()}/{expected['length']} bytes — resuming next run")
                    return False
    size = os.path.getsize(dest_tmp)
    if size != expected["length"]:
        raise RuntimeError(f"length mismatch: {size} != {expected['length']}")
    if expected.get("crc32c"):
        got = crc32c_b64(file_crc32c(dest_tmp))
        if got != expected["crc32c"]:
            for f in (dest_tmp, meta):
                try:
                    os.remove(f)
                except OSError:
                    pass
            raise RuntimeError(f"crc32c mismatch: {got} != {expected['crc32c']}")
    return True


def install(archive, dest=BIN):
    """Extract `soloist`, exec-sanity it with --version, swap atomically,
    keep the previous binary as .prev. Returns the --version line."""
    new = dest + ".new"
    with tarfile.open(archive) as tf:
        member = next((m for m in tf.getmembers() if os.path.basename(m.name) == "soloist"), None)
        if member is None:
            raise RuntimeError("archive has no 'soloist' member")
        with tf.extractfile(member) as src, open(new, "wb") as out:
            while True:
                block = src.read(1 << 20)
                if not block:
                    break
                out.write(block)
    os.chmod(new, 0o755)
    r = subprocess.run([new, "--version"], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        os.remove(new)
        raise RuntimeError(f"new binary failed --version (rc {r.returncode}): {r.stderr.strip()[:200]}")
    version = (r.stdout.strip().splitlines() or ["?"])[0]
    if os.path.exists(dest):
        os.replace(dest, dest + ".prev")
    os.replace(new, dest)
    return version


def _busy():
    try:
        from vibb.backup import _box_busy
        return _box_busy()
    except Exception:
        return False


def _notify_sidecar():
    """AM-52: the sidecar decides when to restart the child (idle only,
    never on the poweroff path) and clears the exit-10 latch — a fresh
    build is a fresh 90 days."""
    try:
        req = urllib.request.Request(SIDECAR + "/soloist/updated", data=b"{}",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except OSError:
        return None


def update(url=URL, dest=BIN, budget_s=BUDGET_S, force=False):
    """The whole ladder. Returns a dict: {"result": current|updated|deferred|
    skipped, ...}. Never raises for the timer's sake; errors land in the
    dict and the state file."""
    st = state()
    if not clock_trusted():
        log("clock not trusted yet (no RTC) — skipping this run")
        return {"result": "skipped", "why": "clock"}
    if not force and _busy():
        log("box is busy (audio or hands on it) — skipping this run")
        return {"result": "skipped", "why": "busy"}
    deadline = time.monotonic() + budget_s
    tmp = dest + ".download.tmp"
    for stale in os.listdir(os.path.dirname(dest) or "."):
        p = os.path.join(os.path.dirname(dest) or ".", stale)
        if stale.startswith(os.path.basename(dest) + ".") and stale.endswith(".tmp") \
                and p != tmp and time.time() - os.path.getmtime(p) > 86400:
            try:
                os.remove(p)
            except OSError:
                pass
    try:
        new = check(url, st.get("etag"))
    except (OSError, ValueError) as e:
        log(f"check failed: {e!r}")
        _save({**st, "last_error": repr(e), "at": time.time()})
        return {"result": "skipped", "why": "check-failed", "error": repr(e)}
    if new is None:
        log(f"current (etag {st.get('etag')})")
        _save({**st, "checked_at": time.time()})
        return {"result": "current", "etag": st.get("etag")}
    log(f"new build: etag {new['etag']} {new['length']} bytes crc32c={new['crc32c']}")
    try:
        if not fetch(url, tmp, new, deadline):
            _save({**st, "pending": new, "at": time.time()})
            return {"result": "deferred", "etag": new["etag"]}
        version = install(tmp, dest)
    except (OSError, RuntimeError, subprocess.TimeoutExpired, tarfile.TarError) as e:
        log(f"update failed: {e!r}")
        _save({**st, "last_error": repr(e), "at": time.time()})
        return {"result": "failed", "error": repr(e)}
    for f in (tmp, tmp + ".meta"):
        try:
            os.remove(f)
        except OSError:
            pass
    _save({"etag": new["etag"], "length": new["length"], "crc32c": new["crc32c"],
           "version": version, "at": time.time(), "checked_at": time.time()})
    log(f"installed {version}")
    _notify_sidecar()
    return {"result": "updated", "etag": new["etag"], "version": version}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    r = update(force="--force" in argv)
    return 0 if r["result"] in ("current", "updated", "deferred", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
