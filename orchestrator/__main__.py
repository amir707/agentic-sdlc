"""Entry point: python -m orchestrator [--project candidate-app]"""

import argparse
import asyncio
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the governed SDLC.")
    parser.add_argument("--project", default="candidate-app")
    parser.add_argument(
        "--parallel", type=int, default=1, metavar="N",
        help="run up to N coders concurrently, each in its own git "
             "worktree (default 1: sequential, per ADR-0003)")
    args = parser.parse_args()

    # Engine secrets first, then the project's own.
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / "projects-config" / args.project / ".env")

    from adapters.adk.invoker import ADKInvoker
    from orchestrator.config import load_project
    from orchestrator.driver import build_context, run_pipeline

    # Composition root: the ONLY place a framework is chosen (ADR-0007).
    project = load_project(args.project)
    ctx = build_context(project, invoker=ADKInvoker())
    asyncio.run(run_pipeline(ctx, parallel=args.parallel))


if __name__ == "__main__":
    main()
