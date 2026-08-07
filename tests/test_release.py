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


def _ctx(items, recheck=0.0, wait_minutes=1.0):
    return SimpleNamespace(
        store=FakeStore(items),
        project=SimpleNamespace(policy=lambda k: {
            "release_recheck_seconds": recheck,
            "max_release_wait_minutes": wait_minutes}),
    )


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


def test_release_loop_stops_when_store_queue_drains(monkeypatch):
    """The loop reconsiders held PRs until the store shows none queued —
    a pass that finally merges the last one ends the loop."""
    items = [{"id": "A", "status": "queued", "pr": 1},
             {"id": "B", "status": "queued", "pr": 2}]
    passes = {"n": 0}

    async def fake_pass(ctx):
        passes["n"] += 1
        # each pass releases one still-queued item (a merge decision)
        for it in items:
            if it["status"] == "queued":
                it["status"] = "released"
                break
    monkeypatch.setattr(driver, "run_release_pass", fake_pass)

    asyncio.run(driver.run_release_loop(_ctx(items, recheck=0.0)))
    # initial pass + rechecks until both drained = 2 passes, then it sees
    # an empty queue and exits.
    assert passes["n"] == 2
    assert all(it["status"] == "released" for it in items)


def test_release_loop_respects_wait_budget_when_all_held(monkeypatch):
    """If every PR stays held, the loop stops at the wait budget instead
    of spinning forever; the held PRs remain queued for a later run."""
    items = [{"id": "A", "status": "queued", "pr": 1}]

    async def fake_pass(ctx):
        pass  # nothing merges — everything stays held/queued
    monkeypatch.setattr(driver, "run_release_pass", fake_pass)

    # wait budget 0 → the initial pass runs, the while-guard sees budget
    # exhausted immediately, loop exits with the PR still queued.
    asyncio.run(driver.run_release_loop(
        _ctx(items, recheck=0.0, wait_minutes=0.0)))
    assert items[0]["status"] == "queued"
