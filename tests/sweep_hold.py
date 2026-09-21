#!/usr/bin/env python3
"""Gate library.sweeping() — what /status.sweeping and idle.py's auto-off
hold (AM-101) stand on. Three sources, one answer:
  1. a sweep round in progress: the flag is raised when the round starts
     and dropped after the stamp (source pin: the order in _cache_sweeper)
  2. go-librespot's background pre-cache — invisible to the daemon after
     POST /cache/download returned — read off its cache dir: a shard
     directory (<dir>/audio/<2 hex>/) written within GO_CACHE_QUIET_S
  3. under soloist the cache probe is off (the sidecar reports its pass
     as /status.warming, which the daemon folds into sweeping)"""
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ["VIBB_STATE"] = TMP
os.environ["VIBB_CACHE"] = os.path.join(TMP, "cache")
os.environ["VIBB_RUN"] = TMP
os.environ["VIBB_LIBRARY"] = os.path.join(TMP, "lib.json")
os.environ["VIBB_GO_CACHE_DIR"] = os.path.join(TMP, "go-cache")
os.environ["VIBB_GO_CACHE_QUIET"] = "60"
sys.path.insert(0, os.path.join(REPO, "pi"))

from vibb import library as lib  # noqa: E402

# 1. nothing running, no cache dir: not sweeping
assert lib.sweeping() is False
print("1. idle box: not sweeping OK")

# 2. the round flag
lib._SWEEP["active"] = True
assert lib.sweeping() is True
lib._SWEEP["active"] = False
assert lib.sweeping() is False
src = open(os.path.join(REPO, "pi", "vibb", "library.py")).read()
body = src[src.index("def _cache_sweeper"):src.index("# --- expansion")]
i_on = body.index('_SWEEP["active"], _SWEEP["since"] = True')
i_prof = body.index("sync_profile_sections()")
i_stamp = body.index("_stamp_sweep()")
i_off = body.index('_SWEEP["active"] = False')
assert i_on < i_prof < i_stamp < i_off, \
    "the flag brackets the whole round: raised before the first sync, dropped after the stamp"
print("2. a round in progress reports sweeping, flag brackets the round OK")

# 3. go-librespot's background download: a shard written just now
shard = os.path.join(os.environ["VIBB_GO_CACHE_DIR"], "audio", "ab")
os.makedirs(shard)
lib._GO_CACHE["at"] = 0.0   # drop the 30 s probe cache
assert lib.sweeping() is True, "a fresh shard = the fork is fetching"
old = time.time() - 3600
os.utime(shard, (old, old))
lib._GO_CACHE["at"] = 0.0
assert lib.sweeping() is False, "an hour-old shard = nothing in flight"
print("3. go-librespot cache dir written recently -> sweeping, quiet -> not OK")

# 4. under soloist the probe is off (the sidecar's pass is /status.warming)
now = time.time()
os.utime(shard, (now, now))
lib._GO_CACHE["at"] = 0.0
lib._paths.GO_UNIT = "vibb-soloistd"
assert lib.sweeping() is False
lib._paths.GO_UNIT = "go-librespot"
print("4. soloist engine: the go-librespot cache probe is off OK")

print("SWEEP HOLD OK — sweeping() reads the round, the fork's cache dir, and nothing under soloist.")
