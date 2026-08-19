"""THE RELEASE CLOCK — a different timeline from the sprint (approvals,
incidents, confidence windows): one event = one pass over the store's
queue, one decision per PR, stateless (every decision re-verifies the
current head). Runs as its own ADK Workflow behind the ReleaseExecutor
port and its own resident service (app/release_service).

  flow.py   release_queue, decide_release_pr, run_release_pass, trigger_release

Depends on governance and ports only — never on sdlc.sprint.
"""
