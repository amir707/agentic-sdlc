"""The resident release manager's agent (ADK agents_dir convention).

Loaded once at service startup by `python -m orchestrator.release_service`
(which sets PROJECT and loads the env). Composition happens here — this
module wires the project bundle, the RunContext, and the release
Workflow into `root_agent`, which ADK's trigger endpoint runs once per
incoming event. Between events the server sits parked on network I/O:
awake, listening, executing nothing.

One workflow instance serves every event; the workflow resets its walk
cursor on each run and re-reads the store queue, so repeated and even
spurious triggers are harmless (idempotent pass over durable state).
"""

import os

from adapters.adk.release_workflow import build_release_workflow
from orchestrator import bootstrap

# Provisions the working checkout at startup (clone + venv) — a resident
# service pays this once, then every event reuses the warm checkout.
_ctx = bootstrap.release_context(os.environ["PROJECT"])
root_agent = build_release_workflow(_ctx)
