#!/usr/bin/env python3
"""I13: the VIBB_AUDIO_STACK toggle (pi/audio-stack.sh) in both
directions, against a fake systemctl and a temp filesystem root.

  1. resolve: env > file > bluealsa; garbage refused; the file is written
  2. pipewire: the three system units carry the bench-proven shape — no
     RuntimeDirectory on the socket-activated service (AM-1), wireplumber
     BindsTo + WantedBy=pipewire.service (AM-2), Nice=0 (AM-4), no
     PIPEWIRE_CONFIG_DIR (AM-21), `-p main-embedded` (AM-22); the
     WirePlumber fragment disables find-default/find-best (AM-23),
     pins sbc / no XQ / no mSBC / hw-volume per WP_HW_VOLUME / roles per
     WP_ROLES / suspend 120 on bluez nodes; the client fragment carries
     dont-reconnect + dont-fallback; bluealsa MASKED (never removed);
     enable order socket -> service -> wireplumber
  3. bluealsa with pipewire units present = ROLLBACK: disable+mask in the
     order wireplumber -> pipewire.service -> pipewire.socket, Debian's
     99-pipewire-default.conf removed, bluealsa unmasked + enabled
  4. bluealsa on a never-migrated box: no pipewire artefact, no mask call
  5. unit env lines per stack; the endpoint owner for After=
"""
import os
import re
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SH = os.path.join(REPO, "pi", "audio-stack.sh")


def run(stack_env, root, extra="", fake_pw_starts="1"):
    """Source the toggle with fakes on PATH, apply, dump the systemctl log."""
    bindir = os.path.join(root, "bin")
    os.makedirs(bindir, exist_ok=True)
    log = os.path.join(root, "systemctl.log")
    for tool in ("systemctl", "useradd", "groupadd", "chown"):
        with open(os.path.join(bindir, tool), "w") as f:
            # `enable --now pipewire...` creates the socket, like the real
            # thing would; is-active answers per FAKE_UNIT_ACTIVE
            # `enable --now pipewire...` binds a REAL unix socket where the
            # service would, so audio-stack's `[[ -S ... ]]` readiness gate
            # is exercised for real; is-active and the socket both follow
            # FAKE_PW_STARTS so the failure path is testable too.
            bind = (f'{sys.executable} -c "import socket,os;'
                    f"os.makedirs('{root}/run/pipewire',exist_ok=True);"
                    f"s=socket.socket(socket.AF_UNIX);"
                    f"s.bind('{root}/run/pipewire/pipewire-0')\"")
            f.write(f'#!/bin/sh\necho "{tool} $@" >> {log}\n'
                    f'case "$1 $2" in\n'
                    f'  "enable --now") case "$*" in *pipewire.socket*)'
                    f' [ "${{FAKE_PW_STARTS:-1}}" = 1 ] && {bind} ;; esac ;;\n'
                    f'  "is-active --quiet") [ "${{FAKE_PW_STARTS:-1}}" = 1 ] || exit 3 ;;\n'
                    f'esac\nexit 0\n')
        os.chmod(os.path.join(bindir, tool), 0o755)
    with open(os.path.join(bindir, "getent"), "w") as f:
        f.write('#!/bin/sh\nexit 0\n')   # groups exist
    os.chmod(os.path.join(bindir, "getent"), 0o755)
    with open(os.path.join(bindir, "id"), "w") as f:
        f.write('#!/bin/sh\nexit 0\n')   # user exists
    os.chmod(os.path.join(bindir, "id"), 0o755)
    script = f"""
set -e
write_if_changed() {{ local d="$1"; mkdir -p "$(dirname "$d")"; cat > "$d.new"; if cmp -s "$d.new" "$d"; then rm "$d.new"; return 1; fi; mv "$d.new" "$d"; }}
. {SH}
{extra}
audio_stack_resolve
audio_stack_packages
audio_stack_apply
echo "ENV:"; audio_stack_unit_env
echo "AFTER: $(audio_stack_endpoint_units)"
"""
    env = dict(os.environ, PATH=bindir + ":" + os.environ["PATH"],
               VIBB_FS_ROOT=root, FAKE_PW_STARTS=fake_pw_starts)
    if stack_env is None:
        env.pop("VIBB_AUDIO_STACK", None)
    else:
        env["VIBB_AUDIO_STACK"] = stack_env
    r = subprocess.run(["bash", "-c", script], env=env, capture_output=True,
                       text=True, timeout=90)
    calls = open(log).read().splitlines() if os.path.exists(log) else []
    return r, calls


# 1. resolve
root = tempfile.mkdtemp()
r, _ = run("nonsense", root)
assert r.returncode != 0 and "must be bluealsa or pipewire" in r.stderr
r, _ = run(None, root)
assert r.returncode == 0, r.stderr
assert open(os.path.join(root, "etc/vibb/audio-stack")).read().strip() == "bluealsa"
with open(os.path.join(root, "etc/vibb/audio-stack"), "w") as f:
    f.write("pipewire\n")
r, _ = run(None, root)
assert "audio stack: pipewire" in r.stdout, "the file selects the stack when env is unset"
r, _ = run("bluealsa", root)
assert "audio stack: bluealsa" in r.stdout, "env beats the file"
print("1. resolve: env > file > default, garbage refused OK")

# 2. pipewire
root = tempfile.mkdtemp()
r, calls = run("pipewire", root)
assert r.returncode == 0, r.stderr
assert "pipewire pipewire-bin pipewire-alsa wireplumber libspa-0.2-bluetooth" in r.stdout
units = os.path.join(root, "etc/systemd/system")
svc = open(os.path.join(units, "pipewire.service")).read()
sock = open(os.path.join(units, "pipewire.socket")).read()
wp = open(os.path.join(units, "wireplumber.service")).read()
assert not re.search(r"^RuntimeDirectory", svc, re.M), "AM-1: the socket unit owns /run/pipewire"
assert not re.search(r"^Environment=PIPEWIRE_CONFIG_DIR", svc, re.M), "AM-21"
assert "Nice=0" in svc and "Nice=0" in wp, "AM-4"
assert "DirectoryMode=0750" in sock and "SocketGroup=audio" in sock
assert "BindsTo=pipewire.service" in wp and "WantedBy=pipewire.service" in wp, "AM-2"
assert "Requires=pipewire.service" not in wp
assert "ExecStart=/usr/bin/wireplumber -p main-embedded" in wp, "AM-22"
assert "DBUS_SESSION_BUS_ADDRESS=disabled:" in wp
frag = open(os.path.join(root, "etc/wireplumber/wireplumber.conf.d/50-vibb.conf")).read()
for key in ("hooks.linking.target.find-default = disabled",
            "hooks.linking.target.find-best    = disabled",
            "bluez5.codecs             = [ sbc ]",
            "bluez5.enable-sbc-xq      = false",
            "bluez5.enable-msbc        = false",
            "bluez5.roles              = [ a2dp_sink ]",
            "bluez5.dummy-avrcp-player = false",
            "session.suspend-timeout-seconds = 120",
            "linking.follow-default-target           = false",
            "main-embedded = {"):
    assert key in frag, f"missing in 50-vibb.conf: {key}"
assert "policy.linking.standard" not in frag and "policy.default-nodes" not in frag, \
    "only names from the distro's provides inventory"
assert not re.search(r"^\s*bluez5\.(enable-)?hw-volume\s*=", frag, re.M), \
    "AM-35: hw-volume is a quirk-list override, never set by vibb"
client = open(os.path.join(root, "etc/pipewire/client.conf.d/10-vibb.conf")).read()
assert "node.dont-reconnect = true" in client and "node.dont-fallback  = true" in client
core = open(os.path.join(root, "etc/pipewire/pipewire.conf.d/10-vibb.conf")).read()
assert 'node.name = "vibb_null"' in core and "default.clock.rate          = 44100" in core
masks = [c for c in calls if c.startswith("systemctl mask")]
assert masks == ["systemctl mask --now bluealsa.service",
                 "systemctl mask --now bluealsad.service"], masks
assert not any("remove" in c or "purge" in c for c in calls), "mask, never remove"
enables = [c for c in calls if c.startswith("systemctl enable")]
assert enables == ["systemctl enable --now pipewire.socket pipewire.service wireplumber.service"], enables
assert "systemctl daemon-reload" in calls
assert calls.index("systemctl daemon-reload") < calls.index(enables[0])
# PipeWire is proven up BEFORE bluealsa is masked: a failed start must
# never leave the box with no A2DP endpoint at all
assert calls.index(enables[0]) < calls.index(masks[0]), \
    "enable+verify pipewire, THEN mask bluealsa"
print("2. pipewire: bench-shaped units + fragments, bluealsa masked, enable order OK")

# 2b. PipeWire fails to come up: the install ABORTS with bluealsa still
#     serving audio, and the half-started units are disabled again. The
#     other order (mask first) left the box with no A2DP endpoint at all.
root_fail = tempfile.mkdtemp()
r, calls = run("pipewire", root_fail, extra="", fake_pw_starts="0")
assert r.returncode != 0, "a PipeWire that never starts must fail the install"
assert "did NOT come up" in r.stdout, r.stdout
assert not any(c.startswith("systemctl mask --now bluealsa") for c in calls), \
    "bluealsa must NOT be masked when PipeWire did not come up"
assert any(c.startswith("systemctl disable --now pipewire.socket") for c in calls), \
    "the half-started units are disabled again"
print("2b. failed PipeWire start: aborts, bluealsa untouched OK")

# 3. rollback
os.makedirs(os.path.join(root, "etc/alsa/conf.d"), exist_ok=True)
with open(os.path.join(root, "etc/alsa/conf.d/99-pipewire-default.conf"), "w") as f:
    f.write("pcm.!default pipewire\n")
os.remove(os.path.join(root, "systemctl.log"))
r, calls = run("bluealsa", root)
assert r.returncode == 0, r.stderr
assert "rollback" in r.stdout
disables = [c for c in calls if c.startswith("systemctl disable")]
assert disables == ["systemctl disable --now wireplumber.service",
                    "systemctl disable --now pipewire.service",
                    "systemctl disable --now pipewire.socket"], disables
masks = [c for c in calls if c.startswith("systemctl mask")]
assert masks == ["systemctl mask wireplumber.service", "systemctl mask pipewire.service",
                 "systemctl mask pipewire.socket"], masks
assert calls.index(masks[-1]) < calls.index("systemctl unmask bluealsa.service")
assert "systemctl enable --now bluealsa.service" in calls
assert not os.path.exists(os.path.join(root, "etc/alsa/conf.d/99-pipewire-default.conf"))
assert os.path.exists(os.path.join(units, "pipewire.service")), \
    "unit files stay (masked) — a later flip back needs no rewrite"
print("3. rollback: reverse order, socket too, default override gone, bluealsa back OK")

# 4. never-migrated box
root = tempfile.mkdtemp()
r, calls = run("bluealsa", root)
assert r.returncode == 0, r.stderr
assert not os.path.exists(os.path.join(root, "etc/systemd/system/pipewire.service"))
assert not any(c.startswith("systemctl mask") or c.startswith("systemctl disable") for c in calls), calls
assert "systemctl unmask bluealsa.service" in calls and "systemctl enable --now bluealsa.service" in calls
assert "pipewire" not in r.stdout.split("ENV:")[0].replace("audio stack: bluealsa", "")
print("4. bluealsa on a clean box: no pipewire artefact, no mask OK")

# 5. env lines + endpoint owner
assert "Environment=VIBB_AUDIO_STACK=bluealsa" in r.stdout and "PIPEWIRE_PROPS" not in r.stdout
assert "AFTER: bluealsa.service bluealsad.service" in r.stdout
r, _ = run("pipewire", tempfile.mkdtemp())
for line in ("Environment=VIBB_AUDIO_STACK=pipewire",
             "Environment=PIPEWIRE_RUNTIME_DIR=/run/pipewire",
             "Environment=PIPEWIRE_PROPS={ node.dont-reconnect=true node.dont-fallback=true }",
             "Environment=VIBB_BT_GATE=transport"):
    assert line in r.stdout, line
assert "AFTER: wireplumber.service" in r.stdout
print("5. unit env per stack, endpoint owner for After= OK")

print("\nall audio_stack_toggle checks passed")
