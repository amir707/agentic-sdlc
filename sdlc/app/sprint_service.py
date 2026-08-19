"""Entry point: python -m sdlc.app.sprint_service --project <name>

The RESIDENT sprint orchestrator (make orchestrate-service): a
long-lived ADK api server that stays awake listening and runs ONE sprint
resume pass per incoming event — the sprint-side twin of
sdlc/app/release_service.py. "Awake" means parked on network I/O;
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

Wiring: sdlc/app/bootstrap.py.
"""

import os

from sdlc.app import bootstrap
def main() -> None:
    p = bootstrap.parser("Run the resident sprint orchestrator (event-driven).")
    p.add_argument("--parallel", type=int, default=1)
    # Cloud Run injects PORT and expects the process to bind 0.0.0.0;
    # locally the loopback + a fixed port. Both are the defaults, so no
    # flag is needed in either place.
    p.add_argument("--host", default="0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8789)))
    p.add_argument("--heartbeat-minutes", type=float, default=5.0,
                   help="self-wake interval until Scheduler/webhook are "
                        "wired (0 disables)")
    p.add_argument("--release-url", default="",
                   help="the resident release service's trigger endpoint; "
                        "when set, the sprint DELEGATES release (its log "
                        "owns release narration) instead of running the "
                        "pass in-process")
    args = p.parse_args()
    bootstrap.load_env(args.project)

    # The ADK app module (sdlc/adapters/adk/apps/sprint_app/sprint/agent.py) is
    # loaded by the api server and reads its configuration from the
    # environment — one service instance governs one project.
    os.environ["PROJECT"] = args.project
    os.environ["PARALLEL"] = str(args.parallel)
    # Event semantics: one gate look per pass (see module docstring).
    os.environ.setdefault("GATE_WAIT_MINUTES", "0")
    if args.release_url:
        os.environ["RELEASE_TRIGGER_URL"] = args.release_url

    bootstrap.serve_resident(
        "sprint_app", "sprint", args.host, args.port, args.heartbeat_minutes,
        args.project, describe="one resume pass per event",
        # an approval both resumes the gate here and queues an item the
        # release service should decide: the webhook nudges both
        extra_targets=[args.release_url] if args.release_url else [])


if __name__ == "__main__":
    main()
