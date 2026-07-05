#!/usr/bin/env python3
"""Reset the delivery store and load the seeded backlog.

Destructive on purpose: `make seed` gives every demo run an identical
starting state (the deterministic eval in verify_demo.py depends on it).
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_server import db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="candidate-app",
                        help="project name under config/")
    args = parser.parse_args()
    seed_file = ROOT / "projects-config" / args.project / "backlog.json"
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
        "INSERT INTO backlog_items VALUES "
        "(:id, :title, :description, :type, :implementation, "
        " :claimed_risk, :claimed_impact, :area_hint, :priority_rank)",
        items)
    conn.commit()
    print(f"seeded {len(items)} backlog items into {path}")


if __name__ == "__main__":
    main()
