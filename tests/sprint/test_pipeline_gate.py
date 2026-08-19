"""pipeline.verify / preprod / gate decisions and the GateWait policy,
tested without ADK."""

import asyncio
from types import SimpleNamespace

import pytest

from sdlc import governance
from sdlc.governance import outcomes

from sdlc.sprint import pipeline

from sdlc.sprint import actions as steps
from sdlc.definition import PipelineShape
from sdlc.sprint.pipeline import GateWait, PipelineState, Route


class Ctx:
    def __init__(self, max_flag=1, approver_policy=None):
        self.audits, self.statuses = [], []
        pol = {"orchestrator": {"max_flag_fix_iterations": max_flag},
               "approver": approver_policy or {"approvers": ["amir707"]}}
        self.project = SimpleNamespace(policy=pol.__getitem__, shape=PipelineShape())
        self.repo_host = self.store = None
        self.board = SimpleNamespace(finish=lambda *a: None)
        self.ci_lock = asyncio.Lock()

    async def audit(self, actor, decision, factors):
        self.audits.append((actor, decision))

    async def set_status(self, item_id, status, pr=None):
        self.statuses.append((status, pr))


ITEM = {"id": "PAY-101", "title": "x"}


@pytest.fixture
def quiet(monkeypatch):
    async def no_reject(*a, **k):
        pass
    monkeypatch.setattr(outcomes, "reject", no_reject)


def _verified(needs_flag):
    return SimpleNamespace(needs_flag=needs_flag, title_prefix="[risk:low]",
                           verified_risk="high")


def test_verify_labels_when_flag_policy_satisfied(monkeypatch):
    async def once(ctx, item, pr):
        return _verified(False)
    monkeypatch.setattr(steps, "verify_once", once)
    ctx, state = Ctx(), PipelineState(pr=7)
    r = asyncio.run(pipeline.verify(ctx, ITEM, state))
    assert r.route == Route.LABELED and state.verified is not None
    assert ("verified", 7) in ctx.statuses


def test_verify_flag_missing_bounces_then_escalates(quiet, monkeypatch):
    async def once(ctx, item, pr):
        return _verified(True)
    monkeypatch.setattr(steps, "verify_once", once)
    ctx, state = Ctx(max_flag=1), PipelineState(pr=7)
    r = asyncio.run(pipeline.verify(ctx, ITEM, state))
    assert r.route == Route.POLICY_FLAG_REQUIRED and "feature flag" in r.output
    assert asyncio.run(pipeline.verify(ctx, ITEM, state)).route == Route.ESCALATE
    assert ("escalated", 7) in ctx.statuses


def test_preprod_result_sets_status_and_route(monkeypatch):
    async def ci(ctx, item, pr, verified):
        return pr == 7
    monkeypatch.setattr(steps, "run_preprod_ci", ci)
    ok = asyncio.run(pipeline.preprod_ci(Ctx(), ITEM, PipelineState(pr=7)))
    bad = asyncio.run(pipeline.preprod_ci(Ctx(), ITEM, PipelineState(pr=8)))
    assert (ok.route, bad.route) == (Route.PASSED, Route.FAILED)


def _gate(decision, monkeypatch):
    async def look(repo_host, store, pr, approvers, baseline, ignores):
        return decision
    monkeypatch.setattr(pipeline, "check_decision", look)


def test_gate_no_decision_means_no_route_and_a_fresh_interrupt(monkeypatch):
    _gate(None, monkeypatch)
    state = PipelineState(pr=7)
    r = asyncio.run(pipeline.approval_gate(Ctx(), ITEM, state))
    assert r.route is None and "awaits a decision" in r.output
    assert pipeline.gate_interrupt_id(state) == "gate_pr7_try1"
    assert pipeline.pr_from_gate_interrupt("gate_pr7_try1") == 7


def test_gate_hold_advances_baseline_and_keeps_waiting(monkeypatch):
    _gate(SimpleNamespace(kind="hold", author="amir707", reason="",
                          comment_index=4), monkeypatch)
    state = PipelineState(pr=7, gate_baseline=0)
    r = asyncio.run(pipeline.approval_gate(Ctx(), ITEM, state))
    assert r.route is None and state.gate_baseline == 5
    assert "on hold by amir707" in r.output


def test_gate_approve_and_reject_route(quiet, monkeypatch):
    _gate(SimpleNamespace(kind="approve", author="a", reason="", comment_index=1), monkeypatch)
    assert asyncio.run(pipeline.approval_gate(Ctx(), ITEM, PipelineState(pr=7))).route == Route.APPROVE
    _gate(SimpleNamespace(kind="reject", author="a", reason="no", comment_index=1), monkeypatch)
    ctx = Ctx()
    assert asyncio.run(pipeline.approval_gate(ctx, ITEM, PipelineState(pr=7))).route == Route.REJECT
    assert ("rejected", 7) in ctx.statuses


def test_gate_wait_policy(monkeypatch):
    monkeypatch.delenv("GATE_WAIT_MINUTES", raising=False)
    poll = GateWait.from_ctx(Ctx(approver_policy={
        "approvers": [], "gate_mode": "poll", "gate_wait_minutes": 1,
        "gate_poll_seconds": 10}))
    assert poll.next_action(0) == "poll" and poll.next_action(60) == "park"
    nudge = GateWait.from_ctx(Ctx(approver_policy={"approvers": [], "gate_mode": "nudge"}))
    assert nudge.next_action(0) == "nudge"
    monkeypatch.setenv("GATE_WAIT_MINUTES", "0")  # event-triggered service
    parked = GateWait.from_ctx(Ctx(approver_policy={"approvers": [], "gate_mode": "nudge"}))
    assert parked.next_action(0) == "park"  # env wins over mode: one look, then park
