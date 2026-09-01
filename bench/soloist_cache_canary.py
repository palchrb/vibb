#!/usr/bin/env python3
"""Soloist cache canary — does a 90-day binary swap void the cache?

The question can only be answered OVER TIME (owner, 2026-09-01): it
needs a real build change, which happens quarterly. So instead of a
test, this is a LEDGER: run it whenever, it records the binary's build
id alongside a fingerprint of the cache, and the moment the build id
changes it tells you what happened to the cache across that swap.

Zero deps, safe to cron. Ledger lives beside the cache so it survives
everything except a cache wipe.

  python3 soloist_cache_canary.py --cache-dir ~/.cache/soloist \\
      --soloist ./run-soloist.sh            # a wrapper or the binary

Verdicts printed (and stored):
  FIRST-RUN     nothing to compare yet
  SAME-BUILD    cache grew/shrank normally; no conclusion available
  SWAP-SURVIVED the build changed and the old files are STILL THERE
                -> cache is content-keyed; no re-download after updates
  SWAP-VOIDED   the build changed and the old files vanished/were
                replaced -> every update costs a full re-warm; the
                updater must trigger cache warming (plan's mitigation)

On the box this becomes soloistd's own bookkeeping; here it is a
standalone bench/ops tool so the answer accumulates from day one.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time


def build_id(soloist):
    """The build stamp from `soloist --version`, e.g.
    'soloist 1.3.7.518 build 1788264113 (20260901) (g...)'. Returns the
    whole line — any change at all counts as a swap."""
    try:
        out = subprocess.run([soloist, "--version"], capture_output=True,
                             text=True, timeout=30)
        line = (out.stdout or out.stderr or "").strip().splitlines()
        for ln in line:
            if "build" in ln.lower() or "soloist" in ln.lower():
                return ln.strip()
        return "unknown"
    except Exception as e:
        return f"unreadable ({e.__class__.__name__})"


def fingerprint(cache_dir):
    """Cheap, stable census of the audio cache: per-file (relpath,
    size, mtime). Content hashing would be minutes of IO for no gain —
    a voided cache shows up as vanished paths, not changed bytes."""
    files = {}
    total = 0
    for root, _dirs, names in os.walk(cache_dir):
        for n in names:
            p = os.path.join(root, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            rel = os.path.relpath(p, cache_dir)
            files[rel] = [st.st_size, int(st.st_mtime)]
            total += st.st_size
    key = hashlib.sha1(
        "".join(sorted(files)).encode()).hexdigest()[:12]
    return {"files": files, "count": len(files), "bytes": total,
            "set_key": key}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", default=os.path.expanduser(
        "~/.cache/soloist"))
    ap.add_argument("--soloist", default="./run-soloist.sh")
    ap.add_argument("--ledger", help="default: <cache-dir>/../"
                                     "soloist-cache-canary.json")
    args = ap.parse_args()

    cache = os.path.abspath(os.path.expanduser(args.cache_dir))
    if not os.path.isdir(cache):
        sys.exit(f"no cache dir at {cache}")
    ledger_path = args.ledger or os.path.join(
        os.path.dirname(cache), "soloist-cache-canary.json")

    now = fingerprint(cache)
    build = build_id(args.soloist)
    entry = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "build": build,
             "count": now["count"], "bytes": now["bytes"],
             "set_key": now["set_key"]}

    try:
        with open(ledger_path) as f:
            ledger = json.load(f)
    except (OSError, ValueError):
        ledger = {"runs": [], "swaps": []}

    prev = ledger["runs"][-1] if ledger["runs"] else None
    prev_files = ledger.get("last_files") or {}

    if prev is None:
        verdict = "FIRST-RUN"
        detail = {}
    elif prev["build"] == build:
        verdict = "SAME-BUILD"
        detail = {"delta_files": now["count"] - prev["count"],
                  "delta_bytes": now["bytes"] - prev["bytes"]}
    else:
        # THE MOMENT THIS TOOL EXISTS FOR
        survived = [p for p in prev_files if p in now["files"]]
        gone = [p for p in prev_files if p not in now["files"]]
        rate = len(survived) / max(1, len(prev_files))
        verdict = "SWAP-SURVIVED" if rate >= 0.8 else "SWAP-VOIDED"
        detail = {"old_build": prev["build"], "new_build": build,
                  "files_before": len(prev_files),
                  "still_present": len(survived), "gone": len(gone),
                  "survival_rate": round(rate, 3)}
        ledger["swaps"].append({**entry, "verdict": verdict, **detail})

    entry["verdict"] = verdict
    ledger["runs"].append(entry)
    ledger["last_files"] = now["files"]
    ledger["runs"] = ledger["runs"][-200:]
    tmp = ledger_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ledger, f, indent=1)
    os.replace(tmp, ledger_path)

    print(f"build   : {build}")
    print(f"cache   : {now['count']} files, "
          f"{now['bytes'] / 1e6:.1f} MB")
    print(f"VERDICT : {verdict}")
    for k, v in detail.items():
        print(f"          {k}: {v}")
    if verdict == "SWAP-VOIDED":
        print("\n=> Every Soloist update costs a full cache re-warm.")
        print("   The plan's mitigation applies: the updater unit must")
        print("   re-warm the `cache: N` entries after a swap (charger")
        print("   + home wifi + idle). Quarterly, overnight, invisible.")
    elif verdict == "SWAP-SURVIVED":
        print("\n=> The cache is content-keyed and survives updates.")
        print("   No post-update re-warm needed; drop that piece of the")
        print("   updater design.")
    print(f"\nledger  : {ledger_path}  ({len(ledger['swaps'])} swaps seen)")


if __name__ == "__main__":
    main()
