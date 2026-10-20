#!/usr/bin/env python3
"""Gate the sweep's pacing (AM-103): the 4 s 'breathe' between entries is
for the radio and the SD card after a DOWNLOAD. A child that only
re-checked a catalog earned none — with ~30 entries an all-quiet sweep
spent ~2 of its 3.5 min breathing (field 2026-10-20). _sync_one now
reports whether the child did work, read off the child's own output."""
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_STATE"] = tempfile.mkdtemp()
os.environ["VIBB_CACHE"] = tempfile.mkdtemp()
os.environ["VIBB_LIBRARY"] = os.path.join(os.environ["VIBB_STATE"], "lib.json")
os.environ["VIBB_SYNC_STAGGER"] = "1"
sys.path.insert(0, os.path.join(REPO, "pi"))

from vibb import library as lib  # noqa: E402

FAKE = os.path.join(os.environ["VIBB_STATE"], "fake_content.py")
with open(FAKE, "w") as f:
    f.write("import sys\n"
            "m = sys.argv[1]\n"
            "if m == 'work': print('cached show/e1', flush=True)\n"
            "elif m == 'book': print('x: downloading 123 (Title)...', file=sys.stderr)\n"
            "elif m == 'images': print('show: cached 2 episode image(s)', file=sys.stderr)\n"
            "elif m == 'quiet': print('show: sync (podcast) - 3 newest wanted, 3 already cached, 0 to download', file=sys.stderr)\n")
lib.content.__file__ = FAKE
lib._TASKSET = None
lib.BUSY_CHECK = lambda: False

assert lib._sync_one(["work"]) is True
assert lib._sync_one(["book"]) is True
assert lib._sync_one(["images"]) is True
print("1. a child that downloaded (podcast / storytel / images): worked OK")
assert lib._sync_one(["quiet"]) is False
print("2. a child that only re-checked ('already cached'): no work OK")

t0 = time.monotonic()
lib._breathe(False)
assert time.monotonic() - t0 < 0.2, "no download = no pause"
t0 = time.monotonic()
lib._breathe(True)
assert time.monotonic() - t0 >= 0.9, "a download still breathes SYNC_STAGGER_S"
print("3. breathe only after a download OK")

src = open(os.path.join(REPO, "pi", "vibb", "library.py")).read()
body = src[src.index("def _cache_sweeper"):src.index("warm_retry = []")]
assert body.count("_breathe(worked)") == 3, body.count("_breathe(worked)")
assert "_sync_wake.wait(SYNC_STAGGER_S)" not in body, \
    "no unconditional stagger left in the entry loop"
assert body.count("_log_outcome(") == 2, "podcast/feed and storytel entries log their outcome"
print("4. every entry path breathes through _breathe(worked), outcome logged OK")

print("SWEEP STAGGER OK — an all-quiet sweep no longer sleeps 4 s per entry.")
