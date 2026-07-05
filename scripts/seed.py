#!/usr/bin/env python3
"""Reset the delivery store and load the seeded backlog.

Destructive on purpose: `make seed` gives every demo run an identical
starting state (the deterministic eval in verify_demo.py depends on it).
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_server import db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _reset_activity_board() -> None:
    """A fresh store deserves a fresh activity board — and a recently
    updated board is the tell that an orchestrator may STILL be running,
    which would write into the store we are about to wipe."""
    board = ROOT / ".activity.json"
    if board.exists():
        try:
            age = time.time() - json.loads(board.read_text())["updated"]
            if age < 120:
                print(f"WARNING: activity board updated {age:.0f}s ago — an "
                      "orchestrator may still be running; stop it first.",
                      flush=True)
        except (ValueError, KeyError):
            pass
        board.unlink()
    log = ROOT / ".activity.log.jsonl"
    if log.exists():
        log.unlink()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="candidate-app",
                        help="project name under config/")
    args = parser.parse_args()
    seed_file = ROOT / "projects-config" / args.project / "backlog.json"
    _reset_activity_board()
    path = db.db_path()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass

    items = json.loads(seed_file.read_text())
    conn = db.connect()
    db.init_schema(conn)
    conn.executemany(
        "INSERT INTO backlog_items (id, title, description, type, "
        "implementation, claimed_risk, claimed_impact, area_hint, "
        "priority_rank) VALUES (:id, :title, :description, :type, "
        ":implementation, :claimed_risk, :claimed_impact, :area_hint, "
        ":priority_rank)",
        items)
    conn.commit()
    print(f"seeded {len(items)} backlog items into {path}")


if __name__ == "__main__":
    main()
