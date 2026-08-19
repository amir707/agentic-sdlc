"""Entry point: python -m sdlc.app.sprint --project <name>"""

from sdlc.app import bootstrap
def main() -> None:
    p = bootstrap.parser("Run the governed SDLC.")
    p.add_argument(
        "--parallel", type=int, default=1, metavar="N",
        help="run up to N coders concurrently, each in its own git "
             "worktree (default 1: sequential, per ADR-0003)")
    args = p.parse_args()
    bootstrap.load_env(args.project)
    bootstrap.announce_models()

    from sdlc.sprint.flow import run_pipeline
    ctx = bootstrap.sprint_context(args.project)
    bootstrap.run_cli(
        run_pipeline(ctx, parallel=args.parallel), label="orchestrator",
        interrupted="progress is in the store; rerunning resumes",
        failed_hint="progress is checkpointed in the store — rerunning "
                    "resumes; --debug for the full traceback",
        debug=args.debug)


if __name__ == "__main__":
    main()
