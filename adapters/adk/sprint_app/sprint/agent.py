"""The resident sprint orchestrator's agent (ADK agents_dir convention).

Loaded once at service startup by `python -m orchestrator.sprint_service`
(which sets PROJECT / PARALLEL / GATE_WAIT_MINUTES=0 and loads the env).
Composition happens here: project bundle → RunContext with both
execution ports → the sprint Workflow as `root_agent`, run once per
incoming event. Between events the server executes nothing.

The wake-up events (webhook on /approve comments, a Scheduler tick)
carry no authority and no payload semantics — every pass re-derives
everything from the store, and gate decisions are read only from the
allowlisted GitHub comments (ADR-0005).
"""

import os

from adapters.adk.executor import ADKPipelineExecutor
from adapters.adk.invoker import ADKInvoker
from adapters.adk.release_workflow import ADKReleaseExecutor
from adapters.adk.sprint_workflow import build_sprint_workflow
from orchestrator.config import load_project
from orchestrator.driver import build_context

_project = load_project(os.environ["PROJECT"])
_ctx = build_context(_project, invoker=ADKInvoker(),
                     executor=ADKPipelineExecutor(),
                     release_executor=ADKReleaseExecutor())

root_agent = build_sprint_workflow(
    _ctx, parallel=int(os.environ.get("PARALLEL", "1")))
