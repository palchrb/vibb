#!/usr/bin/env python3
"""The Spotify engine seam (PLAN-soloistd.md: the engine is an
INSTALL-TIME toggle, --librespot vs --soloist).

Two halves make an engine swap possible. The REST half already existed
(VIBB_GO_API, and the sidecar speaks the go-librespot dialect so the
daemon's ~30 call sites never learn about it). The UNIT-NAME half did
not: "go-librespot" was hardcoded in 14 systemctl calls across five
files, so any swap would have restarted the wrong thing. Pinned here:

  1. paths.go_unit_cmd() builds the argv, VIBB_GO_UNIT selects the unit,
     and the default is today's go-librespot
  2. no systemctl call anywhere in pi/ names the engine unit literally
  3. the play_origin VALUE "go-librespot" (what Spotify's API reports as
     the origin of playback) is NOT a unit name and must survive — it is
     the phone-clobber guard, and renaming it would break that
  4. install.sh: --librespot / --soloist / --bluealsa / --pipewire parse,
     the choice is remembered, an unknown flag exits 2, and --soloist
     refuses EARLY and says why (the sidecar is not built)
"""
import os
import re
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pi"))

from vibb import paths  # noqa: E402

# 1. the builder
assert paths.GO_UNIT == "go-librespot", "the default engine is unchanged"
assert paths.go_unit_cmd("restart") == ["systemctl", "restart", "go-librespot"]
assert paths.go_unit_cmd("is-active", "--quiet") == \
    ["systemctl", "is-active", "--quiet", "go-librespot"]
assert paths.go_unit_cmd("--no-block", "try-restart") == \
    ["systemctl", "--no-block", "try-restart", "go-librespot"]
env = dict(os.environ, VIBB_GO_UNIT="vibb-soloistd")
out = subprocess.run(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0, %r);"
     "from vibb.paths import go_unit_cmd; print(go_unit_cmd('restart'))"
     % os.path.join(REPO, "pi")],
    env=env, capture_output=True, text=True, check=True).stdout
assert "vibb-soloistd" in out and "go-librespot" not in out, out
print("1. go_unit_cmd builds the argv; VIBB_GO_UNIT selects the unit OK")

# 2. no literal unit name in any systemctl call
LITERAL = re.compile(r'\[\s*"systemctl"[^\[\]]*"go-librespot"')
hits = []
for root, _dirs, files in os.walk(os.path.join(REPO, "pi")):
    for f in files:
        if not f.endswith((".py", ".sh")):
            continue
        path = os.path.join(root, f)
        src = open(path, encoding="utf-8").read()
        if LITERAL.search(src) or re.search(r"systemctl [a-z-]+ go-librespot", src):
            hits.append(os.path.relpath(path, REPO))
assert sorted(hits) == ["pi/install.sh", "pi/spotify-engine.sh"], \
    f"the engine unit is named outside the two files that own the unit FILES: {hits}"
print("2. only install.sh + spotify-engine.sh name the unit literally (they write it) OK")

# 3. the play_origin value survives — it is not a unit name
spot = open(os.path.join(REPO, "pi/vibb/spotify.py"), encoding="utf-8").read()
daem = open(os.path.join(REPO, "pi/daemon.py"), encoding="utf-8").read()
assert 'origin != "go-librespot"' in spot, \
    "the box-origin check is the phone-clobber guard; it is a VALUE, not a unit"
assert daem.count('"go-librespot", "", None') == 2, \
    "both play_origin tuples must survive the seam refactor"
print("3. the play_origin value 'go-librespot' survived untouched OK")

# 4. install.sh's flags
SH = os.path.join(REPO, "pi", "install.sh")


def run_flags(*args, engine_file=None):
    """Parse-only: install.sh down to the first section — the flags, both
    toggles sourced, the engine resolve — nothing that touches the system.
    SUDO_USER is the current user so RUN_HOME's getent succeeds under set -e."""
    tmp = tempfile.mkdtemp()
    src = open(SH, encoding="utf-8").read()
    head = src[:src.index("# --- 1. packages")]
    # stdin has no BASH_SOURCE: pin SCRIPT_DIR to the real pi/ dir
    head = head.replace('SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
                        f'SCRIPT_DIR={os.path.join(REPO, "pi")}')
    # EUID is read-only in bash: drop the root check, it is not under test
    head = re.sub(r"^if \[\[ \$EUID.*?^fi\n", "", head, flags=re.S | re.M)
    if engine_file:
        os.makedirs(f"{tmp}/etc/vibb", exist_ok=True)
        open(f"{tmp}/etc/vibb/spotify-engine", "w").write(engine_file + "\n")
    script = head + '\necho "ENGINE=$SPOTIFY_ENGINE STACK=${VIBB_AUDIO_STACK:-} UPDATE=$UPDATE"\n'
    return subprocess.run(["bash", "-s", "--", *args], input=script, env=dict(
        os.environ, VIBB_NAME="vibb", VIBB_AUDIO_STACK="", VIBB_SPOTIFY_ENGINE="",
        VIBB_FS_ROOT=tmp, SUDO_USER=os.environ.get("USER") or "root"),
        capture_output=True, text=True, timeout=60)


r = run_flags()
assert "ENGINE=golibrespot" in r.stdout and "UPDATE=0" in r.stdout, r.stdout + r.stderr
r = run_flags("--librespot", "--pipewire", "--update")
assert "ENGINE=golibrespot" in r.stdout and "STACK=pipewire" in r.stdout \
    and "UPDATE=1" in r.stdout, r.stdout + r.stderr
r = run_flags("--bluealsa")
assert "STACK=bluealsa" in r.stdout, r.stdout + r.stderr
print("4a. --librespot/--bluealsa/--pipewire/--update parse OK")

r = run_flags("--soloist")
assert r.returncode == 2, f"--soloist must refuse: rc={r.returncode}"
assert "PipeWire" in r.stderr and "--pipewire" in r.stderr, r.stderr
assert "ENGINE=" not in r.stdout, "it must refuse BEFORE doing anything"
r = run_flags("--pipewire", "--soloist")
assert r.returncode == 0 and "ENGINE=soloist" in r.stdout, r.stdout + r.stderr
# (the "sidecar not built" refusal is exercised by spotify_engine_toggle.py
#  against a tree without pi/soloistd.py; here the real tree has it)
print("4b. --soloist refuses without --pipewire; resolves with it OK")

r = run_flags("--nonsense")
assert r.returncode == 2 and "unknown option" in r.stderr, r.stderr
r = run_flags("--help")
assert r.returncode == 0 and "--soloist" in r.stdout and "--pipewire" in r.stdout
print("4c. unknown flag exits 2; --help documents both toggles OK")

r = run_flags(engine_file="golibrespot")
assert "ENGINE=golibrespot" in r.stdout, r.stdout + r.stderr
print("4d. a bare re-run keeps the remembered engine OK")

print("\nall spotify_engine_seam checks passed")
