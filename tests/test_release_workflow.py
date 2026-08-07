"""The release pass executes as its own ADK Workflow (Workstream B).

These run the real release graph through `ADKReleaseExecutor` (App +
InMemoryRunner) with the engine handlers stubbed, so the GRAPH — the
incident-hygiene step, the queue walk as a routed cycle, strict one-at-
a-time ordering, and the empty-queue path — is exercised for real
without models or GitHub. Complements tests/test_release.py (queue
selection) and test_item_workflow.py (the per-item graph).
"""

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator import driver
from adapters.adk import release_workflow as rwf
from adapters.adk.release_workflow import ADKReleaseExecutor


class Recorder:
    def __init__(self, queue):
        self._queue = list(queue)
        self.project = SimpleNamespace(
            name="proj",
            policy=lambda step: {"deploy_confidence_minutes": 10})
        self.board = SimpleNamespace(begin=lambda *a, **k: None,
                                     finish=lambda *a, **k: None)
        self.decided: list[int] = []


@pytest.fixture
def stubs(monkeypatch):
    async def resolver_run(project, store):
        return None
    monkeypatch.setattr(rwf.incident_resolver, "run", resolver_run)
    monkeypatch.setattr(rwf.DeliveryStore, "for_resolver",
                        staticmethod(lambda: None))

    async def release_queue(ctx):
        return ctx._queue
    monkeypatch.setattr(driver, "release_queue", release_queue)

    async def decide(ctx, item, confidence):
        assert confidence == 10  # policy reaches the decision node
        ctx.decided.append(item["pr"])
        return "merged"
    monkeypatch.setattr(driver, "decide_release_pr", decide)


def _run(ctx):
    asyncio.run(ADKReleaseExecutor().run_pass(ctx))


def test_pass_walks_the_whole_queue_in_order(stubs):
    ctx = Recorder([{"id": "A", "pr": 1}, {"id": "B", "pr": 2},
                    {"id": "C", "pr": 3}])
    _run(ctx)
    # strict order, one at a time — each merge is visible to the next
    # decision (confidence window semantics).
    assert ctx.decided == [1, 2, 3]


def test_empty_queue_short_circuits(stubs):
    ctx = Recorder([])
    _run(ctx)
    assert ctx.decided == []


def test_held_and_escalated_do_not_stop_the_walk(stubs, monkeypatch):
    outcomes = iter(["held", "escalated", "merged"])

    async def decide(ctx, item, confidence):
        ctx.decided.append(item["pr"])
        return next(outcomes)
    monkeypatch.setattr(driver, "decide_release_pr", decide)
    ctx = Recorder([{"id": "A", "pr": 1}, {"id": "B", "pr": 2},
                    {"id": "C", "pr": 3}])
    _run(ctx)
    assert ctx.decided == [1, 2, 3]  # a hold/escalation skips, never aborts


def test_incident_hygiene_runs_before_any_decision(stubs, monkeypatch):
    order = []

    async def resolver_run(project, store):
        order.append("resolver")
    monkeypatch.setattr(rwf.incident_resolver, "run", resolver_run)

    async def decide(ctx, item, confidence):
        order.append(f"pr{item['pr']}")
        return "merged"
    monkeypatch.setattr(driver, "decide_release_pr", decide)
    ctx = Recorder([{"id": "A", "pr": 1}])
    _run(ctx)
    assert order == ["resolver", "pr1"]  # stale incidents never hold a merge
