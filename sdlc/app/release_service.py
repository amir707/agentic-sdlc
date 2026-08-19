"""Entry point: python -m sdlc.app.release_service --project <name>

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

import os

from sdlc.app import bootstrap
def main() -> None:
    p = bootstrap.parser("Run the resident release manager (event-driven).")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8788)
    p.add_argument("--heartbeat-minutes", type=float, default=5.0,
                   help="self-wake interval until Scheduler/webhook are "
                        "wired (0 disables)")
    args = p.parse_args()
    bootstrap.load_env(args.project)

    # The ADK app module (sdlc/adapters/adk/apps/release_app/release/agent.py) reads
    # PROJECT at load time — one service instance governs one project.
    os.environ["PROJECT"] = args.project

    bootstrap.serve_resident(
        "release_app", "release", args.host, args.port,
        args.heartbeat_minutes, args.project,
        describe="one release pass per event")


if __name__ == "__main__":
    main()
