"""
A stand-in for real work, used by the demos in this directory.

Sleeps for a given number of seconds and exits 0, or exits non-zero if asked to
fail. Nothing here is carpenter specific: it is an ordinary script, which is the
whole point, since carpenter supervises whatever command you give it.

    python worker.py 1.5
    python worker.py 1.5 --fail
"""

import sys
import time

seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 0.5
should_fail = "--fail" in sys.argv[2:]

print(f"working for {seconds:.1f}s", flush=True)
time.sleep(seconds)

if should_fail:
    print("something went wrong", file=sys.stderr, flush=True)
    raise SystemExit(1)

print("done", flush=True)
