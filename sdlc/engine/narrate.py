"""Operator narration — one voice for every "[tag] message" the engine
emits, so the format is a deployment choice instead of 55 print calls.

    say("release", f"MERGED PR #{pr}", pr=pr, item=item_id)

Locally (default) that is the familiar `[release] MERGED PR #7` line.
With LOG_FORMAT=json (set on the Cloud Run services/jobs) each line is
one JSON object with `severity`, `message`, `tag` and the extra fields —
Cloud Logging parses stdout JSON lines into structured, filterable
entries. Same call sites, same words; only the envelope changes.

Program OUTPUT (a URL a CLI prints, a report, a stderr failure summary)
is not narration and stays a plain print at its call site.
"""

import json
import os
import sys
from datetime import datetime, timezone

_LEVELS = {"info": "INFO", "warn": "WARNING", "error": "ERROR"}


def _json_mode() -> bool:
    return os.environ.get("LOG_FORMAT", "").strip().lower() == "json"


def say(tag: str, message: str, *, level: str = "info", stream=None,
        **fields) -> None:
    """Narrate one line. `tag` is the component speaking (release,
    resume, ci, ...); `fields` are structured extras kept out of the
    human line but present in the JSON one. `stream` defaults to stdout;
    a CLI whose stdout is captured by scripts narrates to sys.stderr."""
    out = stream or sys.stdout
    if _json_mode():
        entry = {"severity": _LEVELS.get(level, "INFO"),
                 "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "tag": tag, "message": message, **fields}
        out.write(json.dumps(entry, default=str) + "\n")
    else:
        out.write(f"[{tag}] {message}\n")
    out.flush()
