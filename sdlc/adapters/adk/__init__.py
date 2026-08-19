"""Google ADK adapter (ADR-0007): the framework as an implementation detail.

  invoker.py           AgentInvoker on ADK LlmAgent + Runner (+ LiteLLM bridge)
  item_workflow.py     the per-item graph, RENDERED from sdlc.definition;
                       nodes are one-line wrappers over sdlc.sprint.pipeline
  executor.py          PipelineExecutor: run/resume the item graph, gate suspend
  release_workflow.py  the release pass as its own Workflow + ReleaseExecutor
  sprint_workflow.py   the resident sprint pass as a Workflow
  apps/                the ADK api-server app roots (sprint_app/, release_app/);
                       the <app>/agent.py convention is ADK's
"""
