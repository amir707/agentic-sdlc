"""Who runs the release pass — and therefore whose log tells the release
story (Workstream B's full separation).

With a resident release service running (RELEASE_TRIGGER_URL set), the
sprint side must DELEGATE: fire one event and stop, so the release
agent's log owns every release narration. Without it (one-shot
`make orchestrate`), the pass still runs in-process so a lone sprint run
is self-sufficient.
"""

import asyncio
from types import SimpleNamespace

from orchestrator import release_flow


def test_delegates_to_the_release_service_when_configured(monkeypatch):
    monkeypatch.setenv("RELEASE_TRIGGER_URL",
                       "http://127.0.0.1:8788/apps/release/trigger/pubsub")
    ran_inprocess = []
    posted = []

    async def fake_pass(ctx):
        ran_inprocess.append(1)
    monkeypatch.setattr(release_flow, "run_release_pass", fake_pass)

    async def fake_post(url, name):
        posted.append((url, name))
    import orchestrator.heartbeat as hb
    monkeypatch.setattr(hb, "post_event", fake_post)

    asyncio.run(release_flow.trigger_release(SimpleNamespace()))
    assert posted and posted[0][0].endswith("/apps/release/trigger/pubsub")
    assert not ran_inprocess, "must not also run the pass in-process"


def test_runs_in_process_when_no_service_is_configured(monkeypatch):
    monkeypatch.delenv("RELEASE_TRIGGER_URL", raising=False)
    ran_inprocess = []

    async def fake_pass(ctx):
        ran_inprocess.append(1)
    monkeypatch.setattr(release_flow, "run_release_pass", fake_pass)

    asyncio.run(release_flow.trigger_release(SimpleNamespace()))
    assert ran_inprocess == [1]
