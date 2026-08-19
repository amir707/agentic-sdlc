"""pipeline.code_reviewer / coder_fix: the bounded review loop's policy,
tested WITHOUT ADK — a fake ctx and patched single-shot steps are all a
node decision needs. (test_item_workflow still runs the real graph.)"""

import asyncio
from types import SimpleNamespace

import pytest

from sdlc import governance
from sdlc.governance import outcomes

from sdlc.sprint import pipeline

from sdlc.sprint import actions as steps
from sdlc.engine.dependency_graph import UnparseableSource
from sdlc.sprint.pipeline import PipelineState, Route


class Ctx:
    def __init__(self, max_fix=2):
        self.audits, self.statuses, self.comments = [], [], []
        self.project = SimpleNamespace(
            policy=lambda name: {"orchestrator": {"max_fix_iterations": max_fix}}[name])
        self.repo_host = SimpleNamespace(
            post_comment=lambda pr, body: self.comments.append(body))
        self.board = SimpleNamespace(finish=lambda *a: None)
        self.store = None

    async def audit(self, actor, decision, factors):
        self.audits.append((actor, decision))

    async def set_status(self, item_id, status, pr=None):
        self.statuses.append((status, pr))


ITEM = {"id": "PAY-101", "title": "x"}


def _verdict(kind):
    return SimpleNamespace(verdict=kind, reasoning="r",
                           model_dump_json=lambda: '{"v": "%s"}' % kind)


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(steps, "review_already_approved", lambda ctx, pr: False)

    async def no_reject(*a, **k):
        pass
    monkeypatch.setattr(outcomes, "reject", no_reject)


def _review(kind, monkeypatch):
    async def once(ctx, item, pr, iteration):
        if kind == "unparseable":
            raise UnparseableSource("app/x.py", "bad syntax")
        return _verdict(kind)
    monkeypatch.setattr(steps, "review_once", once)


def test_approve_routes_approved_and_audits(quiet, monkeypatch):
    _review("approve", monkeypatch)
    ctx, state = Ctx(), PipelineState(pr=7)
    r = asyncio.run(pipeline.code_reviewer(ctx, ITEM, state))
    assert r.route == Route.APPROVED
    assert ("code_reviewer", "approve_review") in ctx.audits


def test_changes_requested_consumes_budget_then_escalates(quiet, monkeypatch):
    _review("request_changes", monkeypatch)
    ctx, state = Ctx(max_fix=2), PipelineState(pr=7)
    assert asyncio.run(pipeline.code_reviewer(ctx, ITEM, state)).route == Route.CHANGES_REQUESTED
    assert asyncio.run(pipeline.code_reviewer(ctx, ITEM, state)).route == Route.CHANGES_REQUESTED
    assert state.review_rounds == 2
    r = asyncio.run(pipeline.code_reviewer(ctx, ITEM, state))
    assert r.route == Route.ESCALATE
    assert ("escalated", 7) in ctx.statuses


def test_unparseable_is_a_bounded_fix_round_not_a_crash(quiet, monkeypatch):
    _review("unparseable", monkeypatch)
    ctx, state = Ctx(max_fix=1), PipelineState(pr=7)
    r = asyncio.run(pipeline.code_reviewer(ctx, ITEM, state))
    assert r.route == Route.CHANGES_REQUESTED and "does not parse" in r.output
    assert asyncio.run(pipeline.code_reviewer(ctx, ITEM, state)).route == Route.ESCALATE


def test_out_of_scope_bounces_to_author(quiet, monkeypatch):
    _review("out_of_scope", monkeypatch)
    ctx, state = Ctx(), PipelineState(pr=7)
    assert asyncio.run(pipeline.code_reviewer(ctx, ITEM, state)).route == Route.OUT_OF_SCOPE
    assert ("rejected", 7) in ctx.statuses


def test_already_approved_head_short_circuits(monkeypatch):
    monkeypatch.setattr(steps, "review_already_approved", lambda ctx, pr: True)
    r = asyncio.run(pipeline.code_reviewer(Ctx(), ITEM, PipelineState(pr=7)))
    assert r.route == Route.APPROVED


def test_no_change_fix_round_is_an_impasse_on_the_artifact(monkeypatch):
    async def declined(ctx, item, branch, feedback=None):
        return False, "I disagree"
    monkeypatch.setattr(steps, "run_coder", declined)
    ctx = Ctx()
    r = asyncio.run(pipeline.coder_fix(ctx, ITEM, "item/x", PipelineState(pr=7), "fix it"))
    assert r.route == Route.IMPASSE
    assert any("no code changes" in c for c in ctx.comments)
    assert ("escalated", 7) in ctx.statuses
