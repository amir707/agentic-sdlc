"""Entry points and the ONE composition root.

  bootstrap.py       load env, shared argparse, build_context, the two
                     adapter selections (sprint_context / release_context),
                     run_cli, serve_resident
  sprint.py          python -m sdlc.app.sprint          one sprint run
  release.py         python -m sdlc.app.release         one release pass
  sprint_service.py  python -m sdlc.app.sprint_service  resident, event-driven
  release_service.py python -m sdlc.app.release_service resident, event-driven
"""
