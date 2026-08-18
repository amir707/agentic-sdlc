#!/usr/bin/env python3
"""Human-readable snapshot of the delivery store (read-only).

Layout: the CURRENT story first — the sprint's items with their PR and
lifecycle status (both owned by the STORE: backlog_items.status/pr, set
by the orchestrator at every transition — GitHub is only the artifact),
live workers, open incidents, and this sprint's token spend. Below the
divider: history, every line carrying local time and PR/item ids.

Usage: make status   (or: make watch for a self-refreshing view)
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_server import db                      # noqa: E402
from mcp_server.report import render           # noqa: E402
from mcp_server.vocab import Decision          # noqa: E402
from orchestrator.activity import read_board, read_recent_history  # noqa: E402

# --- presentation-only color layer -------------------------------------
# The status TEXT is generated plain everywhere (files, the store's
# /status route). Color is applied at display time only: below when
# stdout is a terminal, and by scripts/watch.py after each fetch — so
# local and cloud stores render identically. Rules key on the stable
# line formats produced by mcp_server/report.render().

_RESET = "\033[0m"

_STATUS_WORDS = [
    ("MERGED + released", "32"),
    ("escalated to a human", "1;31"),
    ("failed preprod", "1;31"),
    ("awaiting gate decision (/approve on the PR)", "1;33"),
    ("approved — queued for release", "33"),
    ("verified (labels applied)", "33"),
    ("preprod passed", "33"),
    ("in review", "33"),
    ("not started", "2"),
    ("SPLIT!", "1;31"),
    ("traffic=100", "32"),
    ("traffic=preprod", "33"),
]

_DECISION_WORDS = [
    (Decision.HUMAN_OVERRIDE_ESCALATION, "1;33"),
    (Decision.HUMAN_APPROVE, "32"),
    (Decision.MERGE_PR, "32"),
    (Decision.RESOLVE_INCIDENT, "32"),
    (Decision.HOLD_MERGE, "31"),
    (Decision.REJECT_PR, "31"),
    (Decision.ESCALATE_TO_HUMAN, "1;31"),
    (Decision.ESCALATE_RISK_LABEL, "33"),
]


def _wrap(line: str, sub: str, code: str) -> str:
    return line.replace(sub, f"\033[{code}m{sub}{_RESET}", 1)


def colorize_lines(lines: list[str]) -> list[str]:
    section = ""
    out = []
    for line in lines:
        if line.startswith("== "):
            section = line
            out.append(f"\033[1m{line}{_RESET}")
            continue
        if line.strip() in ("none", "none yet"):
            out.append(f"\033[2m{line}{_RESET}")
            continue
        if section.startswith("== open incidents") and line.strip():
            out.append(f"\033[1;31m{line}{_RESET}")
            continue
        if "NOW " in line:
            cut = line.index("NOW ")
            out.append(line[:cut] + f"\033[36m{line[cut:]}{_RESET}")
            continue
        for sub, code in _STATUS_WORDS:
            if sub in line:
                line = _wrap(line, sub, code)
        if section.startswith("== audit"):
            for sub, code in _DECISION_WORDS:
                if f" {sub} " in line:
                    line = _wrap(line, sub, code)
                    break
        out.append(line)
    return out


def colorize(text: str) -> str:
    return "\n".join(colorize_lines(text.split("\n")))


def report() -> str:
    """The full status text, with the engine's live activity board and
    recent history folded in (both are on this machine's disk)."""
    conn = db.connect()
    db.init_schema(conn)
    return render(conn, board=read_board(),
                  history=read_recent_history(limit=10))


if __name__ == "__main__":
    text = report()
    if os.environ.get("STATUS_COLOR") == "1" or (
            sys.stdout.isatty() and not os.environ.get("NO_COLOR")):
        text = colorize(text)
    sys.stdout.write(text)
