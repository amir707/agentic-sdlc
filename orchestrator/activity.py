"""Activity board: live "who is doing what, since when" telemetry.

Ephemeral by design — this is liveness information, not governed state,
so it lives in a gitignored scratch file (.activity.json) beside a
JSONL history of completed steps with durations (.activity.log.jsonl),
NOT in the delivery store: the store records decisions (audit-worthy),
the board records busyness (interesting for ~seconds). The production
successor is distributed tracing (OTel/Cloud Trace via ADK's
observability hooks), not more audit rows.

Rendered by scripts/store_status.py (make status / make watch).
Async-safe within the single-process orchestrator (one event loop);
parallel items each own one key.
"""

import json
import time
from pathlib import Path


class ActivityBoard:
    def __init__(self, path: str | Path = ".activity.json"):
        self.path = Path(path)
        self.log_path = self.path.with_suffix(".log.jsonl")
        self._current: dict[str, dict] = {}
        # A new run starts a fresh board (history log accumulates).
        self._flush()

    def begin(self, item: str, step: str, detail: str = "") -> None:
        """Item enters a step; a previous step on the same item is
        implicitly completed and logged with its duration."""
        self._complete(item, outcome="done")
        self._current[item] = {"step": step, "detail": detail,
                               "since": time.time()}
        self._flush()

    def note(self, item: str, detail: str) -> None:
        """Update the detail line without restarting the clock."""
        if item in self._current:
            self._current[item]["detail"] = detail
            self._flush()

    def finish(self, item: str, outcome: str) -> None:
        """Item leaves the pipeline (queued / rejected / failed / ...)."""
        self._complete(item, outcome)
        self._flush()

    def _complete(self, item: str, outcome: str) -> None:
        entry = self._current.pop(item, None)
        if entry is None:
            return
        record = {"item": item, "step": entry["step"],
                  "detail": entry["detail"], "outcome": outcome,
                  "seconds": round(time.time() - entry["since"], 1),
                  "ended": time.time()}
        with self.log_path.open("a") as f:
            f.write(json.dumps(record) + "\n")

    def _flush(self) -> None:
        self.path.write_text(json.dumps(
            {"updated": time.time(), "current": self._current}, indent=2))


def read_board(path: str | Path = ".activity.json") -> dict | None:
    path = Path(path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None  # mid-write; the next refresh will catch it


def read_recent_history(path: str | Path = ".activity.json",
                        limit: int = 8) -> list[dict]:
    log_path = Path(path).with_suffix(".log.jsonl")
    if not log_path.exists():
        return []
    lines = log_path.read_text().splitlines()[-limit:]
    return [json.loads(line) for line in lines if line.strip()]
