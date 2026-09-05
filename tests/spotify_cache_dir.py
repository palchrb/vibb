#!/usr/bin/env python3
"""The PWA's Spotify cache size follows the engine: go-librespot's dir by
default, soloistd's CacheDirectory under soloist (owner 2026-09-06 — the
soloist box reported no Spotify cache at all)."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
for k in ("VIBB_RUN", "VIBB_STATE", "VIBB_CACHE"):
    os.environ[k] = TMP
os.environ["VIBB_SETTINGS"] = os.path.join(TMP, "settings.json")
os.environ.pop("VIBB_GO_UNIT", None)
sys.path.insert(0, os.path.join(REPO, "pi"))
from vibb import sysinfo  # noqa: E402

assert sysinfo.spotify_cache_dir() == "/var/lib/vibb/spotify-cache"
os.environ["VIBB_GO_UNIT"] = "vibb-soloistd"
assert sysinfo.spotify_cache_dir() == "/var/cache/vibb-soloist"
cache = os.path.join(TMP, "soloist-cache", "cache", "ab"); os.makedirs(cache)
open(os.path.join(cache, "x.file"), "wb").write(b"z" * 4096)
os.environ["VIBB_SOLOIST_CACHE_DIR"] = os.path.join(TMP, "soloist-cache")
sysinfo.pisugar_get = lambda *a, **k: None
sysinfo.netmgmt.wifi_snapshot = lambda: (True, "x", "1.2.3.4", False)
st = sysinfo.system_status()
assert st["caches"].get("spotify") == 4096, st["caches"]
print("spotify cache dir follows the engine; size reported under soloist OK")
