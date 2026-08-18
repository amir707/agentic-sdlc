"""The resident sprint orchestrator: one resume pass per event.

Runs the sprint Workflow on ADK's engine with run_pipeline stubbed —
each trigger event must execute exactly one pass, and one workflow
instance must serve many events (the service reuses it)."""

import asyncio
from types import SimpleNamespace

from orchestrator import sprint
from adapters.adk.sprint_workflow import build_sprint_workflow


def test_one_pass_per_event_and_instance_reuse(monkeypatch):
    passes = []

    async def fake_pipeline(ctx, parallel=1, deprovision=True):
        passes.append((parallel, deprovision))
    monkeypatch.setattr(sprint, "run_pipeline", fake_pipeline)

    from google.adk.apps import App, ResumabilityConfig
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    flow = build_sprint_workflow(SimpleNamespace(), parallel=2)  # built ONCE
    app = App(name="svc", root_agent=flow,
              resumability_config=ResumabilityConfig(is_resumable=True))
    runner = InMemoryRunner(app=app)

    async def one_event(n):
        session = await runner.session_service.create_session(
            app_name="svc", user_id=f"event{n}")
        message = types.Content(role="user",
                                parts=[types.Part.from_text(text="go")])
        async for _ in runner.run_async(user_id=f"event{n}",
                                        session_id=session.id,
                                        new_message=message):
            pass

    asyncio.run(one_event(1))
    asyncio.run(one_event(2))
    # two events -> two passes; parallel threads through; the resident
    # service keeps the warm checkout (deprovision=False).
    assert passes == [(2, False), (2, False)]
