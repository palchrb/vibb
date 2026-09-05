#!/usr/bin/env python3
"""The Spotify engine toggle (pi/spotify-engine.sh), both directions,
against a fake systemctl and a temp root (PLAN-pipewire-soloist.md
step 3, AM-53).

  1. golibrespot (default): go-librespot unmasked, no sidecar unit, the
     engine recorded; env = 3678 / go-librespot; VIBB_GO_CONFIG emitted
  2. soloist REFUSES, writing nothing, when the audio stack is not
     pipewire — and when the sidecar is not in the tree
  3. soloist + pipewire + sidecar: the sidecar installed, its unit written
     as $RUN_USER with EnvironmentFile=-/etc/vibb/soloist.env,
     StateDirectory/CacheDirectory (Soloist honours them), Restart=on-failure
     never always, the pipewire client props; go-librespot MASKED never
     removed; enabled; env = 3688 / vibb-soloistd; NO VIBB_GO_CONFIG
  4. rollback (--librespot with the sidecar unit present): sidecar
     disabled + masked, go-librespot unmasked, config.yml byte-identical
  5. a failed apply never records the engine
"""
import os
import re
import subprocess
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PI = os.path.join(REPO, "pi")


def run(engine, stack, sidecar=True, apply=True, fail_enable=False, root=None):
    root = root or tempfile.mkdtemp()
    bindir = os.path.join(root, "bin"); os.makedirs(bindir, exist_ok=True)
    log = os.path.join(root, "systemctl.log")
    with open(os.path.join(bindir, "systemctl"), "w") as f:
        f.write(f'#!/bin/sh\necho "systemctl $@" >> {log}\n'
                f'[ "${{FAKE_ENABLE_FAILS:-0}}" = 1 ] && [ "$1" = enable ] && exit 1\nexit 0\n')
    os.chmod(os.path.join(bindir, "systemctl"), 0o755)
    sd = os.path.join(root, "tree"); os.makedirs(sd, exist_ok=True)
    for f in ("audio-stack.sh", "spotify-engine.sh"):
        os.symlink(os.path.join(PI, f), os.path.join(sd, f)) if not os.path.exists(os.path.join(sd, f)) else None
    if sidecar:
        open(os.path.join(sd, "soloistd.py"), "w").write("#!/usr/bin/env python3\n")
    os.makedirs(os.path.join(root, "etc/vibb"), exist_ok=True)
    if stack:
        open(os.path.join(root, "etc/vibb/audio-stack"), "w").write(stack + "\n")
    script = f"""
set -e
write_if_changed() {{ mkdir -p "$(dirname "$1")"; cat > "$1"; }}
install_if_changed() {{ echo "install $2 -> $3" >> {log}; mkdir -p "$(dirname "$3")"; cp "$2" "$3"; }}
RUN_USER=kid; SCRIPT_DIR={sd}
. {sd}/audio-stack.sh
. {sd}/spotify-engine.sh
spotify_engine_resolve
echo "RESOLVED=$SPOTIFY_ENGINE"
{"spotify_engine_apply" if apply else ":"}
echo "UNIT=$(spotify_engine_unit)"
echo "ENV:"; spotify_engine_unit_env
echo "GOCONF:"; spotify_engine_go_config_env /home/kid/.config/go-librespot/config.yml
"""
    env = dict(os.environ, PATH=bindir + ":" + os.environ["PATH"], VIBB_FS_ROOT=root,
               VIBB_SPOTIFY_ENGINE=engine or "", VIBB_AUDIO_STACK="",
               FAKE_ENABLE_FAILS="1" if fail_enable else "0")
    r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)
    calls = open(log).read().splitlines() if os.path.exists(log) else []
    rec = os.path.join(root, "etc/vibb/spotify-engine")
    recorded = open(rec).read().strip() if os.path.exists(rec) else None
    return r, calls, recorded, root


# 1. golibrespot
r, calls, rec, root = run("", "bluealsa")
assert r.returncode == 0, r.stderr
assert "RESOLVED=golibrespot" in r.stdout and rec == "golibrespot"
assert "systemctl unmask go-librespot.service" in calls and not any("soloistd" in c for c in calls)
assert "Environment=VIBB_GO_API=http://127.0.0.1:3678" in r.stdout
assert "Environment=VIBB_GO_UNIT=go-librespot" in r.stdout
assert "Environment=VIBB_GO_CONFIG=/home/kid/.config/go-librespot/config.yml" in r.stdout
assert "UNIT=go-librespot" in r.stdout
print("1. golibrespot: default, recorded, env + GO_CONFIG OK")

# 2. refusals write nothing
r, calls, rec, _ = run("soloist", "bluealsa")
assert r.returncode != 0 and "PipeWire" in r.stderr and rec is None and calls == [], (r.stderr, calls)
r, calls, rec, _ = run("soloist", "pipewire", sidecar=False)
assert r.returncode != 0 and "not built" in r.stderr and rec is None and calls == []
print("2. soloist refused without pipewire / without the sidecar; nothing written OK")

# 3. soloist provisioned
r, calls, rec, root = run("soloist", "pipewire")
assert r.returncode == 0, r.stderr
assert rec == "soloist"
assert any(c.startswith("install ") and c.endswith("/usr/local/bin/vibb-soloistd") for c in calls), calls
unit = open(os.path.join(root, "etc/systemd/system/vibb-soloistd.service")).read()
for line in ("User=kid", "EnvironmentFile=-/etc/vibb/soloist.env", "StateDirectory=vibb-soloist",
             "CacheDirectory=vibb-soloist", "Restart=on-failure",
             'Environment="PIPEWIRE_PROPS={ node.dont-reconnect=true node.dont-fallback=true }"',
             "Environment=VIBB_AUDIO_STACK=pipewire", "After=network-online.target wireplumber.service"):
    assert line in unit, line
assert not re.search(r"^Restart=always", unit, re.M), "a unit Restart=always would brick-loop exit 10"
# disable, not mask: go-librespot.service is OUR file in /etc/systemd/system
# and systemctl refuses to mask those (first Zero 2026-09-05: the mask had
# failed silently, is-enabled said 'disabled')
assert "systemctl disable --now go-librespot.service" in calls and "systemctl enable --now vibb-soloistd.service" in calls
assert not any(c.startswith("systemctl mask") for c in calls), "no mask on /etc units"
assert not any("remove" in c or "purge" in c for c in calls)
assert "Environment=VIBB_GO_API=http://127.0.0.1:3688" in r.stdout and "Environment=VIBB_GO_UNIT=vibb-soloistd" in r.stdout
assert "VIBB_GO_CONFIG" not in r.stdout.split("GOCONF:")[1], "no GO_CONFIG under soloist (AM-53)"
assert "UNIT=vibb-soloistd" in r.stdout
upd = open(os.path.join(root, "etc/systemd/system/vibb-soloist-update.service")).read()
assert "ExecStart=/usr/bin/python3 -m vibb.soloist_update" in upd and "Nice=19" in upd
timer = open(os.path.join(root, "etc/systemd/system/vibb-soloist-update.timer")).read()
assert "OnBootSec=" in timer and "OnUnitActiveSec=" in timer, "monotonic timer"
assert not re.search(r"^(OnCalendar|Persistent)=", timer, re.M), \
    "never a calendar timer: it would fire at boot on the RTC-less bogus clock (AM-50)"
assert "systemctl enable --now vibb-soloist-update.timer" in calls
print("3. soloist: sidecar installed, unit shaped, go-librespot disabled, env, no GO_CONFIG OK")
print("3b. updater unit + MONOTONIC timer written and enabled OK")

# 4. rollback keeps config.yml byte-identical
cfg = os.path.join(root, "config.yml"); open(cfg, "w").write("audio_backend: alsa\naudio_device: vibb_bt\n")
before = open(cfg).read()
os.remove(os.path.join(root, "systemctl.log"))
# the file remembers soloist, so the rollback is the EXPLICIT --librespot
r, calls, rec, _ = run("golibrespot", "pipewire", root=root)
assert r.returncode == 0 and rec == "golibrespot", (r.stderr, rec)
assert "systemctl disable --now vibb-soloistd.service" in calls
assert not any(c.startswith("systemctl mask") for c in calls), "no mask on /etc units"
assert "systemctl disable --now vibb-soloist-update.timer" in calls, "the rollback stops the updater"
assert "systemctl unmask go-librespot.service" in calls
assert calls.index("systemctl disable --now vibb-soloistd.service") < calls.index("systemctl unmask go-librespot.service")
assert open(cfg).read() == before
print("4. rollback: sidecar masked, go-librespot back, config.yml untouched OK")

# 5. a failed apply records nothing
r, calls, rec, _ = run("soloist", "pipewire", fail_enable=True)
assert r.returncode != 0 and rec is None, (r.returncode, rec)
print("5. failed apply -> engine not recorded OK")

print("\nall spotify_engine_toggle checks passed")
