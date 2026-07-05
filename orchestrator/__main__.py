"""Entry point: python -m orchestrator [--project candidate-app]"""

import argparse
import asyncio
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the governed SDLC.")
    parser.add_argument("--project", default="candidate-app")
    args = parser.parse_args()

    # Engine secrets first, then the project's own.
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / "config" / "projects" / args.project / ".env")

    from engine.config import load_project
    from orchestrator.driver import build_context, run_pipeline

    project = load_project(args.project)
    ctx = build_context(project)
    asyncio.run(run_pipeline(ctx))


if __name__ == "__main__":
    main()
