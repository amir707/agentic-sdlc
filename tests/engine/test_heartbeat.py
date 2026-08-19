"""The internal heartbeat: a self-produced event on an interval — the
local stand-in for Cloud Scheduler until real wake-ups are wired."""

import asyncio

from sdlc.engine import heartbeat


def test_payload_is_pubsub_shaped_and_numbered():
    body = heartbeat._payload("sprint", 3)
    assert body["message"]["messageId"] == "sprint-hb-3"
    assert body["subscription"] == "internal-heartbeat/sprint"
    assert body["message"]["data"]  # opaque nudge, non-empty


def test_beats_post_pubsub_shaped_events(monkeypatch):
    posted = []

    class FakeClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            posted.append((url, json))
    monkeypatch.setattr(heartbeat.httpx, "AsyncClient", FakeClient)

    async def two_beats():
        task = asyncio.get_running_loop().create_task(
            heartbeat._beat_forever("http://svc/trigger", 0.0001, "sprint"))
        while len(posted) < 2:
            await asyncio.sleep(0.005)
        task.cancel()
    asyncio.run(two_beats())

    url, body = posted[0]
    assert url == "http://svc/trigger"
    assert body["message"]["messageId"] == "sprint-hb-1"
    assert posted[1][1]["message"]["messageId"] == "sprint-hb-2"
    assert body["subscription"].startswith("internal-heartbeat")
