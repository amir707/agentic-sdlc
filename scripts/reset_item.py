#!/usr/bin/env python3
"""Reset ONE item for replay — the surgical alternative to make reset-demo.

Clears the item's store lifecycle (status -> pending, PR unlinked),
closes its open PR, deletes its branch, and removes its worktree, so a
rerun of the orchestrator replays the item from coding onward while the
rest of the world stays put. The assessment is kept (the sprint's
composition should not change) unless --with-assessment.

MERGED items ask for confirmation first: their change already lives in
main, so the replayed coder diffs against itself and may produce an
empty PR. `make reset-demo` is the true world replay.

Usage: make reset-item ITEM=PAY-102
       (python scripts/reset_item.py --item PAY-102 [--with-assessment])
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os  # noqa: E402

from adapters.repo_host import GitHubRepoHost  # noqa: E402
from mcp_server import db                      # noqa: E402
from orchestrator import provisioning          # noqa: E402
from orchestrator.config import load_project   # noqa: E402
from orchestrator.driver import _branch        # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--item", required=True)
    parser.add_argument("--project", default="candidate-app")
    parser.add_argument("--with-assessment", action="store_true",
                        help="also delete the item's assessment")
    args = parser.parse_args()

    conn = db.connect()
    db.init_schema(conn)
    item = db.get_item(conn, args.item)
    if item is None:
        sys.exit(f"no backlog item {args.item}")

    project = load_project(args.project)
    host = GitHubRepoHost(project.repo, os.environ["GITHUB_TOKEN"])
    branch = _branch(item)

    # --- GitHub side: PR + branch -------------------------------------
    closed_pr = None
    prior = host.find_pr(branch, state="all")
    if prior and prior["merged"]:
        answer = input(
            f"PR #{prior['number']} was MERGED — its change is already in "
            "main, so the replay may diff to nothing (revert it in the "
            "repo first for a real replay). Proceed? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            sys.exit("aborted — nothing changed")
    if prior and prior["state"] == "open":
        host.close_pr(prior["number"])
        closed_pr = prior["number"]
        print(f"closed PR #{prior['number']}")
    host.delete_branch(branch)
    print(f"deleted branch {branch} (if it existed)")

    # --- store side: lifecycle (+ optionally assessment) ---------------
    conn.execute("UPDATE backlog_items SET status='pending', pr=NULL "
                 "WHERE id = ?", (args.item,))
    if args.with_assessment:
        conn.execute("DELETE FROM assessments WHERE item_id = ?",
                     (args.item,))
        print("assessment deleted (item will be re-assessed)")
    conn.commit()
    db.append_audit(conn, "operator", "reset_item", {
        "item": args.item, "closed_pr": closed_pr, "branch": branch,
        "with_assessment": args.with_assessment})
    print(f"{args.item}: status -> pending, pr -> none (audited)")

    # --- local side: the item's worktree --------------------------------
    checkout = provisioning.checkout_path(args.project)
    worktree = checkout.parent / f"{checkout.name}-worktrees" / args.item
    if worktree.exists():
        subprocess.run(["git", "-C", str(checkout), "worktree", "remove",
                        "--force", str(worktree)], capture_output=True)
        shutil.rmtree(worktree, ignore_errors=True)
        print(f"removed worktree {worktree}")

    print(f"\n{args.item} is ready to replay: rerun the orchestrator.")


if __name__ == "__main__":
    main()
