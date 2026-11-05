#!/usr/bin/env python3
"""The daemon's view of a soloistd pass (4d): _warming() reads the sidecar's
health at most every 5 s and ignores an expired deadline (AM-63/69);
_warm_abort_wait() POSTs /cache/abort and waits for the pass to end (AM-65);
/status carries `warming` for idle.py and the backup; the library's
WARM_START hook is the wifi power-save kick (AM-67)."""
import io
import json
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
for k in ("VIBB_RUN", "VIBB_STATE", "VIBB_CACHE"):
    os.environ[k] = TMP
os.environ["VIBB_SETTINGS"] = os.path.join(TMP, "settings.json")
os.environ["VIBB_GO_UNIT"] = "vibb-soloistd"
sys.path.insert(0, os.path.join(REPO, "pi"))

import daemon  # noqa: E402

HEALTH = {"warming": {"uri": "spotify:playlist:p", "until": time.time() + 600}}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(req, timeout=5):
    url = req if isinstance(req, str) else req.full_url
    assert url.endswith("/soloist/health"), url
    return _Resp(json.dumps(HEALTH).encode())


daemon.urllib.request.urlopen = fake_urlopen
POSTS = []
daemon._sidecar_post = lambda path, body=None, timeout=5: (POSTS.append(path), HEALTH.update({"warming": None}))

daemon._WARM["at"] = 0.0
assert daemon._warming(), "a pass with a live deadline"
HEALTH["warming"]["until"] = time.time() - 1
daemon._WARM["at"] = 0.0
assert daemon._warming() is None, "an expired deadline reads as no pass (AM-63)"
HEALTH["warming"] = {"uri": "spotify:playlist:p", "until": time.time() + 600}
daemon._WARM["at"] = 0.0
assert daemon._warm_abort_wait("play", timeout=2) is True and POSTS == ["/cache/abort"], POSTS
assert daemon._warming() is None
assert daemon._warm_abort_wait("play") is False and POSTS == ["/cache/abort"], "no pass: nothing posted"
# the hook is wired where BUSY_CHECK is (at daemon start, not import): pin the source
src = open(daemon.__file__, encoding="utf-8").read()
i = src.index("_library.BUSY_CHECK = _audible_now")
assert "_library.WARM_START = _PS_KICK.set" in src[i:i + 300], "a pass starts = wifi power save off"
print("warm status: deadline, abort+wait, PS kick hook OK")
