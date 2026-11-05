#!/usr/bin/env python3
"""Run every test in this directory, one process each.

The tests are standalone scripts by design — each one owns its tempdirs
and monkeypatches, so a shared runner must NOT import them. It spawns
them, with a per-file timeout, because a wedged test would otherwise
hang a whole build (several of these poll sockets and sleep).

    python3 tests/run_all.py            # everything
    python3 tests/run_all.py bt         # only files matching 'bt'
    python3 tests/run_all.py -v         # stream each test's own output

Exit 0 only when every test that RAN passed. Skips are not failures:
some BT tests need python3-dbus, which a dev box may not have.
(Open item since REVIEW-2026-07-18.md:192.)
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TIMEOUT_S = int(os.environ.get("VIBB_TEST_TIMEOUT", "180"))

# Not tests: imported BY tests, or run by hand against a capture file.
# (sonos_contract.py is imported by two rigs AND self-checks when run
# standalone — its docstring says so — so it stays in the run.)
FIXTURES = {"run_all.py", "fake_bluezd.py", "snoop_digest.py", "soloist_fakes.py"}

GREEN, RED, DIM, YELLOW, OFF = (
    ("\033[32m", "\033[31m", "\033[2m", "\033[33m", "\033[0m")
    if sys.stdout.isatty() else ("", "", "", "", ""))


def looks_skipped(out):
    """A test that bailed on a missing optional dependency, not a bug."""
    tail = out.strip().splitlines()[-6:]
    joined = "\n".join(tail)
    return ("ModuleNotFoundError" in joined
            and any(m in joined for m in ("dbus", "gi.repository", "soco")))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    verbose = "-v" in sys.argv[1:]
    names = sorted(f for f in os.listdir(HERE)
                   if f.endswith(".py") and f not in FIXTURES
                   and (not args or any(a in f for a in args)))
    if not names:
        print("no tests matched")
        return 1

    failed, skipped, t0 = [], [], time.monotonic()
    for name in names:
        start = time.monotonic()
        try:
            r = subprocess.run([sys.executable, os.path.join(HERE, name)],
                               capture_output=not verbose, text=True,
                               timeout=TIMEOUT_S)
            out = "" if verbose else (r.stdout or "") + (r.stderr or "")
            ok, timed_out = r.returncode == 0, False
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or "") if isinstance(e.stdout, str) else ""
            ok, timed_out = False, True
        took = time.monotonic() - start

        if ok:
            mark = f"{GREEN}ok{OFF}  "
        elif timed_out:
            mark, _ = f"{RED}TIME{OFF}", failed.append((name, "timeout"))
        elif looks_skipped(out):
            mark, _ = f"{YELLOW}skip{OFF}", skipped.append(name)
        else:
            mark, _ = f"{RED}FAIL{OFF}", failed.append((name, out))
        print(f"{mark} {name:<40} {DIM}{took:5.1f}s{OFF}")

    print(f"\n{len(names)} files, {len(failed)} failed, "
          f"{len(skipped)} skipped, {time.monotonic() - t0:.0f}s")
    for name, out in failed:
        print(f"\n{RED}=== {name}{OFF}")
        print("\n".join((out or "").strip().splitlines()[-25:]))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
