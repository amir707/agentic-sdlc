"""Entry point: python -m orchestrator --project <name>"""

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the governed SDLC.")
    parser.add_argument("--project", required=True)
    parser.add_argument(
        "--parallel", type=int, default=1, metavar="N",
        help="run up to N coders concurrently, each in its own git "
             "worktree (default 1: sequential, per ADR-0003)")
    parser.add_argument("--debug", action="store_true",
                        help="show full tracebacks instead of one-line "
                             "failure summaries")
    args = parser.parse_args()

    # Engine secrets first, then the project's own.
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / "projects-config" / args.project / ".env")

    from adapters.adk.invoker import ADKInvoker
    from orchestrator.config import load_project
    from orchestrator.driver import build_context, run_pipeline

    # Composition root: the ONLY place a framework is chosen (ADR-0007).
    import os
    print("[orchestrator] models: "
          f"coder={os.environ.get('CODER_MODEL', 'anthropic/claude-sonnet-5')} | "
          f"reviewer={os.environ.get('REVIEWER_MODEL') or os.environ.get('GEMINI_MODEL', 'gemini-flash-latest')} | "
          f"gemini-default={os.environ.get('GEMINI_MODEL', 'gemini-flash-latest')}",
          flush=True)
    project = load_project(args.project)
    ctx = build_context(project, invoker=ADKInvoker())
    try:
        asyncio.run(run_pipeline(ctx, parallel=args.parallel))
    except KeyboardInterrupt:
        print("\n[orchestrator] interrupted — progress is in the store; "
              "rerunning resumes", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 — top level: summarize
        if args.debug:
            raise
        from orchestrator.errors import one_line
        print(f"\n[orchestrator] FAILED: {one_line(exc)}", file=sys.stderr)
        print("[orchestrator] progress is checkpointed in the store — "
              "rerunning resumes; --debug for the full traceback",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
