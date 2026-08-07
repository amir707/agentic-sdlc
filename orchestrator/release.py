"""Entry point: python -m orchestrator.release --project <name>

ONE release pass over store state, then exit (Workstream B). This is the
event-driven unit of release: a Cloud Scheduler tick or a GitHub webhook
(→ Pub/Sub → the ADK ambient-trigger endpoint, adapters/adk/release_app.py)
fires exactly one of these per event. There is no in-process wait loop —
a PR held on this pass stays `queued` in the store and the NEXT event
reconsiders it. Run repeatedly by whatever schedules it (Cloud Scheduler
in the cloud; a cron or `/loop` locally).

No executor is constructed — release runs no per-item pipeline, only the
release-manager agent and the deterministic merge gate.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one release pass over store state (no sprint).")
    parser.add_argument("--project", required=True)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / "projects-config" / args.project / ".env")

    from adapters.adk.invoker import ADKInvoker
    from adapters.adk.release_workflow import ADKReleaseExecutor
    from orchestrator.config import load_project
    from orchestrator.driver import build_context, run_release_pass

    project = load_project(args.project)
    ctx = build_context(project, invoker=ADKInvoker(),
                        release_executor=ADKReleaseExecutor())
    try:
        asyncio.run(run_release_pass(ctx))
    except KeyboardInterrupt:
        print("\n[release] interrupted — held PRs stay queued in the store; "
              "the next event reconsiders them", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 — top level: summarize
        if args.debug:
            raise
        from orchestrator.errors import one_line
        print(f"\n[release] FAILED: {one_line(exc)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
