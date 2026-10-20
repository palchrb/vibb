#!/usr/bin/env python3
"""Gate soloistd.listing_stale (AM-102): a remembered order is served
without `stale` — the daemon then trusts its indices for Sonos positional
jumps and paints the card from its rows — only when the warm pass proved
it complete AND verified it within LISTING_TRUST_S. Everything less is
stale: the picker still shows it, the Sonos hand-off re-transfers per
press instead of jumping to the wrong track."""
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["VIBB_STATE"] = os.environ.get("VIBB_STATE") or "/tmp/vibb-listing-stale"
sys.path.insert(0, os.path.join(REPO, "pi"))

import soloistd  # noqa: E402

now = time.time()
fresh = {"result": "done", "verified_at": now - 60}
rec = {"complete": True, "tracks": ["a", "b"]}
assert soloistd.listing_stale(rec, fresh, now) is False
print("1. complete + verified a minute ago: fresh OK")
assert soloistd.listing_stale({"complete": False, "tracks": ["a"]}, fresh, now) is True
print("2. a start window (not complete): stale OK")
old = {"result": "done", "verified_at": now - soloistd.LISTING_TRUST_S - 1}
assert soloistd.listing_stale(rec, old, now) is True
print("3. verified longer ago than LISTING_TRUST_S: stale OK")
assert soloistd.listing_stale(rec, {"result": "aborted", "verified_at": now}, now) is True
assert soloistd.listing_stale(rec, None, now) is True
assert soloistd.listing_stale(None, fresh, now) is True
print("4. no done ledger row / no order: stale OK")
assert soloistd.LISTING_TRUST_S == 24 * 3600
print("5. default trust window is a day (covers a missed 6 h sweep) OK")
print("SOLOIST LISTING STALE OK")
