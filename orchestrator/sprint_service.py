"""Entry point: python -m orchestrator.sprint_service --project <name>

The RESIDENT sprint orchestrator (make orchestrate-service): a
long-lived ADK api server that stays awake listening and runs ONE sprint
resume pass per incoming event — the sprint-side twin of
orchestrator/release_service.py. "Awake" means parked on network I/O;
between events it executes nothing (locally: negligible; on Cloud Run:
scale-to-zero).

Event semantics, tuned for cost:
- GATE_WAIT_MINUTES=0 — an event-triggered pass gives every awaiting
  gate exactly ONE authenticated look and never sits waiting; a pass
  with nothing to consume makes no model calls at all (resume markers
  skip every agent stage).
- The wake-ups: a GitHub webhook on PR comments (the /approve event)
  and a Cloud Scheduler heartbeat, both → Pub/Sub push →
  POST /apps/sprint/trigger/pubsub. Locally, the same POST by hand:
    curl -s -X POST localhost:8789/apps/sprint/trigger/pubsub \
      -H 'Content-Type: application/json' \
      -d '{"message": {"data": "c3ByaW50", "messageId": "m1"}, \
           "subscription": "manual"}'

Fourth composition root (with __main__, release.py, release_service.py).
"""

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the resident sprint orchestrator (event-driven).")
    parser.add_argument("--project", required=True)
    parser.add_argument("--parallel", type=int, default=1)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8789)
    parser.add_argument("--heartbeat-minutes", type=float, default=5.0,
                        help="self-wake interval until Scheduler/webhook "
                             "are wired (0 disables)")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / "projects-config" / args.project / ".env")
    os.environ["PROJECT"] = args.project
    os.environ["PARALLEL"] = str(args.parallel)
    # Event semantics: one gate look per pass (see module docstring).
    os.environ.setdefault("GATE_WAIT_MINUTES", "0")
    # Single-flight: two concurrent events must queue, not race two
    # passes over the same items.
    os.environ.setdefault("ADK_TRIGGER_MAX_CONCURRENT", "1")

    from google.adk.cli.fast_api import get_fast_api_app

    app = get_fast_api_app(
        agents_dir=str(ROOT / "adapters" / "adk" / "sprint_app"),
        web=False,
        trigger_sources=["pubsub"],
    )
    print(f"[sprint-service] {args.project}: awake on "
          f"http://{args.host}:{args.port} — one resume pass per event "
          "(POST /apps/sprint/trigger/pubsub)", flush=True)
    from orchestrator.heartbeat import serve_with_heartbeat
    serve_with_heartbeat(app, args.host, args.port,
                         "/apps/sprint/trigger/pubsub",
                         args.heartbeat_minutes, "sprint")


if __name__ == "__main__":
    main()
