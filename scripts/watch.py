#!/usr/bin/env python3
"""Self-refreshing store status with change highlighting (display-only).

Each cycle fetches the same plain text `make status` produces (local
SQLite or the cloud store's /status route — the Makefile decides),
colorizes it, and marks every line that CHANGED since the previous
refresh with a yellow bar in the left margin, so a flip like
"escalated -> MERGED + released" pulls the eye the moment it lands.

Volatile fragments — elapsed counters, "(2m ago)" ages — are stripped
before comparing, so ticking clocks never light up; only real state
changes do. Lines are keyed by (section, normalized text): the same
"none" appearing in a new section still counts as a change. Nothing is
persisted and the store is never written — previous snapshot lives in
memory only.

Usage: make watch   (or: scripts/watch.py [interval-seconds])
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.store_status import colorize_lines  # noqa: E402

# elapsed counters "(3m02s)" / "(42s)", bare board durations " 42s ",
# and relative ages "(2m ago)" — presentation noise, not state.
_VOLATILE = re.compile(
    r"\((?:\d+m\d{2}s|\d+(?:\.\d+)?s|[^()]*\bago)\)|\b\d+m\d{2}s\b|\b\d+s\b")

_MARK = "\033[1;33m▎\033[0m "
_CLEAR = "\033[H\033[2J\033[3J"


def _fetch() -> str:
    proc = subprocess.run(["make", "-s", "status"],
                          capture_output=True, text=True,
                          cwd=Path(__file__).resolve().parent.parent)
    return proc.stdout if proc.returncode == 0 else proc.stdout + proc.stderr


def _keys(lines: list[str]) -> list[tuple[str, str]]:
    section = ""
    keys = []
    for line in lines:
        if line.startswith("== "):
            section = line
        keys.append((section, _VOLATILE.sub("~", line.rstrip())))
    return keys


def main() -> None:
    interval = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    url = os.environ.get("DELIVERY_STORE_URL")
    target = url.split("//")[-1].split("/")[0] if url else "local sqlite"
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    prev: set | None = None

    while True:
        text = _fetch()
        lines = text.splitlines()
        keys = _keys(lines)
        shown = colorize_lines(lines) if color else lines

        out = []
        for key, plain, line in zip(keys, lines, shown):
            changed = (prev is not None and plain.strip()
                       and key not in prev)
            out.append((_MARK if changed and color else "  ") + line)

        header = (f"delivery store · {target} · refresh {interval:.0f}s · "
                  f"snapshot {time.strftime('%H:%M:%S')}")
        legend = f"   {_MARK.strip()}=changed since last refresh"
        if color:
            header = f"\033[1m{header}\033[0m{legend}"
        sys.stdout.write(_CLEAR + header + "\n" + "\n".join(out) + "\n")
        sys.stdout.flush()

        prev = {k for k in keys if k[1].strip()}
        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
