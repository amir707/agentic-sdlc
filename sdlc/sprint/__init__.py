"""THE SPRINT CLOCK — one run, or one event, is one resume pass over the
selected items. Owns everything that happens to an item between backlog
and `queued`:

  planning.py   assess every item, pack a sprint (once per store lifetime)
  actions.py    the single-shot per-item handlers: run the coder, review
                once, verify once, one preprod deploy, one dossier
  pipeline.py   the per-item node decisions + PipelineState + GateWait
                (what the ADK graph's nodes call; framework-free)
  flow.py       resume dispatch from store status + run_pipeline

Hands off to the release clock through sdlc.release.flow.trigger_release
and never reaches into it otherwise.
"""
