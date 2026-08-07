"""Entry point: python -m orchestrator.release_service --project <name>

The RESIDENT release manager (make release-service): a long-lived ADK
api server that stays awake listening and runs ONE release pass per
incoming event. "Awake" means parked on network I/O like any web server
— never a sleep/poll loop.

Wake-up sources, all hitting the same endpoint:
- Cloud (runbook §13): Cloud Scheduler tick and store/webhook events →
  Pub/Sub push subscription → POST /apps/release/trigger/pubsub.
- Local / manual: the same POST with a Pub/Sub-shaped body, e.g.
    curl -s -X POST localhost:8788/apps/release/trigger/pubsub \
      -H 'Content-Type: application/json' \
      -d '{"message": {"data": "cmVsZWFzZQ==", "messageId": "m1"}, \
           "subscription": "manual"}'

The pass is stateless over the store (status=queued IS the queue), so
at-least-once delivery, duplicates, and spurious wake-ups are all safe:
worst case is one cheap store read and "[release] queue empty".

Third composition root (with __main__ and release.py): it selects the
project, loads env, and lets ADK's server own the process lifetime.
"""

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the resident release manager (event-driven).")
    parser.add_argument("--project", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--heartbeat-minutes", type=float, default=5.0,
                        help="self-wake interval until Scheduler/webhook "
                             "are wired (0 disables)")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / "projects-config" / args.project / ".env")
    # The agent module (adapters/adk/release_app/release/agent.py) reads
    # PROJECT at load time — one service instance governs one project.
    os.environ["PROJECT"] = args.project
    # Single-flight: releases are one-decision-one-deploy-at-a-time; two
    # concurrent trigger events must queue, not race two passes.
    os.environ.setdefault("ADK_TRIGGER_MAX_CONCURRENT", "1")

    from google.adk.cli.fast_api import get_fast_api_app

    app = get_fast_api_app(
        agents_dir=str(ROOT / "adapters" / "adk" / "release_app"),
        web=False,
        trigger_sources=["pubsub"],
    )
    print(f"[release-service] {args.project}: awake on "
          f"http://{args.host}:{args.port} — one release pass per event "
          "(POST /apps/release/trigger/pubsub)", flush=True)
    from orchestrator.heartbeat import serve_with_heartbeat
    serve_with_heartbeat(app, args.host, args.port,
                         "/apps/release/trigger/pubsub",
                         args.heartbeat_minutes, "release")


if __name__ == "__main__":
    main()
