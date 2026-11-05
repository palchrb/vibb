#!/usr/bin/env python3
"""AM-95 (11): the backup takes the Soloist key (SECRET), the paired session
files in Soloist's data dir (SECRET, ws.* runtime files excluded) and the
sidecar's remembered-list store (PROGRESS) — a restore without them landed
in needs-key / needs-pair and forgot every warmed list."""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
ETC = os.path.join(TMP, "etc"); os.makedirs(ETC)
DATA = os.path.join(TMP, "soloist"); os.makedirs(os.path.join(DATA, "vibb", "orders"))
STATE = os.path.join(TMP, "state"); os.makedirs(STATE)
os.environ.update({"VIBB_ETC": ETC, "VIBB_SOLOIST_DATA": DATA, "VIBB_STATE": STATE,
                   "VIBB_SOLOIST_ENV": os.path.join(ETC, "soloist.env"),
                   "VIBB_ART": os.path.join(TMP, "art")})
sys.path.insert(0, os.path.join(REPO, "pi"))
from vibb import backup  # noqa: E402

open(os.path.join(ETC, "soloist.env"), "w").write("SOLOIST_API_KEY=x\n")
for rel in ("session.dat", "ws.addr", "ws.port", "vibb/ledger.json", "vibb/meta.json", "vibb/orders/abc.json"):
    open(os.path.join(DATA, rel), "w").write("{}")
open(os.path.join(STATE, "bookmark-x.json"), "w").write("{}")

sec = backup._secret_files()
assert os.path.join(ETC, "soloist.env") in sec, sec
assert os.path.join(DATA, "session.dat") in sec, sec
assert not any(os.path.basename(p).startswith("ws.") for p in sec), sec
assert not any("/vibb/" in p for p in sec), "the store is progress, not secret"
print("1. secrets: the key file + Soloist's own session files, ws.* excluded OK")

prog = backup._progress_files()
for rel in ("vibb/ledger.json", "vibb/meta.json", "vibb/orders/abc.json"):
    assert os.path.join(DATA, rel) in prog, (rel, prog)
assert os.path.join(STATE, "bookmark-x.json") in prog
print("2. progress: the sidecar store (ledger, meta, orders) beside the state dir OK")
print("\nall backup_soloist_files checks passed")
