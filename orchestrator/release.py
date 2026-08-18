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

from orchestrator import bootstrap


def main() -> None:
    args = bootstrap.parser(
        "Run one release pass over store state (no sprint).").parse_args()
    bootstrap.load_env(args.project)

    from orchestrator.release_flow import run_release_pass
    ctx = bootstrap.release_context(args.project)
    bootstrap.run_cli(
        run_release_pass(ctx), label="release",
        interrupted="held PRs stay queued in the store; the next event "
                    "reconsiders them",
        debug=args.debug)


if __name__ == "__main__":
    main()
