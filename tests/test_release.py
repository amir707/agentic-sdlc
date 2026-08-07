"""Release reads its queue from the STORE, not memory (Workstream B).

The decoupling that matters: `release_queue` is a pure store read, and
`run_release_loop` drives itself off it — so the release loop is
resumable and independent of any sprint process (a standalone
`python -m orchestrator.release` runs the identical loop). These pin the
queue-selection and the bounded-loop behaviour without touching a live
release-manager agent.
"""

import asyncio
from types import SimpleNamespace

from orchestrator import driver


class FakeStore:
    def __init__(self, items):
        self.items = items

    async def call(self, tool, **kw):
        assert tool == "list_backlog"
        return self.items


def _ctx(items):
    return SimpleNamespace(store=FakeStore(items))


def test_release_queue_selects_only_queued_with_pr():
    items = [
        {"id": "A", "status": "queued", "pr": 1},
        {"id": "B", "status": "released", "pr": 2},   # already done
        {"id": "C", "status": "escalated", "pr": 3},  # not queued
        {"id": "D", "status": "queued", "pr": None},  # queued but no PR yet
        {"id": "E", "status": "queued", "pr": 5},
    ]
    queue = asyncio.run(driver.release_queue(_ctx(items)))
    assert [i["id"] for i in queue] == ["A", "E"]


def test_release_queue_empty_when_nothing_queued():
    items = [{"id": "A", "status": "released", "pr": 1}]
    assert asyncio.run(driver.release_queue(_ctx(items))) == []


def test_no_in_process_recheck_loop_exists():
    """Release is event-driven: ONE pass per invocation, no in-process
    wait loop (a held PR waits for the next event, not an asyncio.sleep).
    Guard against the loop creeping back in."""
    assert not hasattr(driver, "run_release_loop")
    import inspect
    assert "asyncio.sleep" not in inspect.getsource(driver.run_release_pass)


def test_release_pass_delegates_to_the_release_executor():
    """The pass runs on the ReleaseExecutor port (its own ADK Workflow,
    ADR-0007) — the driver holds the lock and delegates, nothing more."""
    calls = []

    class FakeExecutor:
        async def run_pass(self, ctx):
            calls.append("pass")

    ctx = SimpleNamespace(release_lock=asyncio.Lock(),
                          release_executor=FakeExecutor())
    asyncio.run(driver.run_release_pass(ctx))
    assert calls == ["pass"]
