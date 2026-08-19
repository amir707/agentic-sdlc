"""Deterministic mechanics with no domain rules — the leaf every layer may use.

  config.py            project bundles: prompts + policies via the overlay pattern
  workspace.py         git workspace / worktrees for agent work
  provisioning.py      the engine clones and heals its own checkout
  dependency_graph.py  import graph + blast radius (a tool, not an agent)
  activity.py          the live "who is doing what, since when" board
  heartbeat.py         internal self-wake for the resident services
  redact.py            secret redaction for anything human-facing
  errors.py            one-line operator-facing failure summaries
  json_util.py         lenient JSON extraction from model output
  agent_support.py     shared helpers for the reasoning steps' spec.py files
"""
