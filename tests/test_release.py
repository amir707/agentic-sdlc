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


def test_repo_host_error_escalates_the_item_not_the_pass():
    """Live-found regression: the store said `queued, pr=3` but the repo
    (recreated from baseline) had no PR #3 — the 404 killed the whole
    release workflow. A repo-host failure must escalate THAT item and
    return an outcome the graph can route past."""
    from adapters.repo_host import RepoHostError

    class ExplodingRepoHost:
        def get_pr(self, pr):
            raise RepoHostError(f"GET /pulls/{pr}: 404 Not Found")

    audits, statuses = [], []

    class Ctx:
        repo_host = ExplodingRepoHost()
        board = SimpleNamespace(finish=lambda *a, **k: None)

        async def audit(self, actor, decision, factors):
            audits.append((actor, decision, factors))

        async def set_status(self, item_id, status, pr=None):
            statuses.append((item_id, status, pr))

    outcome = asyncio.run(driver.decide_release_pr(
        Ctx(), {"id": "CAT-202", "pr": 3}, confidence=10))
    assert outcome == "escalated"
    assert ("CAT-202", "escalated", 3) in statuses
    assert any(d == "escalate_to_human" and "repo host" in f["rule"]
               for _, d, f in audits)
