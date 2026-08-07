"""Entry point: python -m orchestrator.release --project <name>

The release loop as an INDEPENDENT process (Workstream B). It reads the
store (`status=queued` PRs, open incidents, deploy history), decides and
merges, and reconsiders held PRs on its bounded recheck budget — all
without a sprint run in sight. This is what proves release is decoupled:
kill the sprint orchestrator, run this, and a PR held for an open
incident still merges once the incident clears.

Deploy note: the same `run_release_loop` is what a Cloud Scheduler tick
or a GitHub webhook (→ Pub/Sub → ADK ambient trigger) invokes in the
cloud; the store-sourced queue is what makes the in-process loop and the
event-triggered form identical. No executor is constructed — release
runs no per-item pipeline, only the release-manager agent.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the release loop over store state (no sprint).")
    parser.add_argument("--project", required=True)
    parser.add_argument("--once", action="store_true",
                        help="one pass instead of the bounded recheck loop")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / "projects-config" / args.project / ".env")

    from adapters.adk.invoker import ADKInvoker
    from orchestrator.config import load_project
    from orchestrator.driver import (build_context, run_release_loop,
                                      run_release_pass)

    project = load_project(args.project)
    ctx = build_context(project, invoker=ADKInvoker())  # no executor
    loop = run_release_pass if args.once else run_release_loop
    try:
        asyncio.run(loop(ctx))
    except KeyboardInterrupt:
        print("\n[release] interrupted — held PRs stay queued in the store; "
              "rerunning resumes", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 — top level: summarize
        if args.debug:
            raise
        from orchestrator.errors import one_line
        print(f"\n[release] FAILED: {one_line(exc)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
