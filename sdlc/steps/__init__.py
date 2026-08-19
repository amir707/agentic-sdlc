"""One folder per SDLC worker — everything a worker IS, in one place:
its knowledge (prompts.md, policy.yaml) and its code (spec.py for a
reasoning agent; __init__.py for a deterministic step). Root
policy.yaml holds keys shared across steps.

  risk_assessor*  sprint_packer   coder*  code_reviewer*  verify
  preprod_ci      approver*       release_manager*  incident_resolver
  monitor         orchestrator (flow-control policy only)      * = agent

Project bundles overlay this tree at projects-config/<name>/steps/<step>/
(customised-prompt.md extends, policy.yaml overrides) — the engine
composes them (sdlc.engine.config). Steps are named by sdlc.definition;
this package is the knowledge behind those names.
"""
