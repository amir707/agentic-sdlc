"""The per-item pipeline executes on ADK's engine (Workstream A).

These run the real `Workflow` through `run_item_workflow` (App +
InMemoryRunner) with every model/GitHub-touching handler stubbed, so the
GRAPH — routes, bounded cycles, the gate suspend/resume, terminal
mapping — is exercised for real without any live service. They are the
behavioral parity net for the executor: each route must land on the
right terminal and leave the right store status.
"""

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator import driver
from orchestrator.dependency_graph import UnparseableSource
from adapters.adk import workflow as wf
from adapters.adk.executor import run_item_workflow

ITEM = {"id": "PAY-1", "title": "add fee", "implementation": "agent"}


class Recorder:
    """A fake RunContext: records status/audit/queue, no I/O."""

    def __init__(self, gate="poll"):
        self._policies = {
            "orchestrator": {"max_fix_iterations": 2,
                             "max_flag_fix_iterations": 1},
            "approver": {"approvers": ["amir707"], "gate_mode": gate,
                         "gate_wait_minutes": 1, "gate_poll_seconds": 0},
        }
        self.project = SimpleNamespace(policy=self._policies.get)
        self.store = None
        self.repo_host = SimpleNamespace(post_comment=lambda *a, **k: None)
        self.approved = []
        self.ci_lock = asyncio.Lock()
        self.board = SimpleNamespace(begin=lambda *a, **k: None,
                                     finish=lambda *a, **k: None)
        self.statuses: list[tuple] = []
        self.audits: list[tuple] = []

    async def set_status(self, item_id, status, pr=None):
        self.statuses.append((item_id, status, pr))

    async def audit(self, actor, decision, factors):
        self.audits.append((actor, decision, factors))


class Verified:
    def __init__(self, needs_flag=False):
        self.needs_flag = needs_flag
        self.title_prefix = "[area:payments][risk:low][flag:no]"
        self.verified_risk = "medium"


def _verdict(kind, reasoning="ok"):
    return SimpleNamespace(verdict=kind, reasoning=reasoning,
                           model_dump_json=lambda: '{"verdict": "%s"}' % kind)


@pytest.fixture
def stubs(monkeypatch):
    """Default happy-path stubs; individual tests override pieces."""
    calls = {"coder": 0, "fix": 0}

    async def run_coder(ctx, item, branch, feedback=None):
        if feedback is not None:
            calls["fix"] += 1
            return True, "fixed"
        calls["coder"] += 1
        return True, "implemented"

    async def open_pr(ctx, item, branch):
        return 42

    monkeypatch.setattr(driver, "run_coder", run_coder)
    monkeypatch.setattr(driver, "open_pr", open_pr)
    monkeypatch.setattr(driver, "review_already_approved", lambda ctx, pr: False)
    monkeypatch.setattr(driver, "review_once",
                        _async_return(_verdict("approve")))
    monkeypatch.setattr(driver, "verify_once", _async_return(Verified()))
    monkeypatch.setattr(driver, "run_preprod_ci", _async_return(True))
    monkeypatch.setattr(driver, "run_approver", _async_return(0))
    monkeypatch.setattr(wf, "check_decision",
                        _async_return(SimpleNamespace(
                            kind="approve", author="amir707", reason="",
                            comment_index=0)))
    # reject is imported inside nodes from orchestrator.rejection
    import orchestrator.rejection as rej
    monkeypatch.setattr(rej, "reject", _async_noop())
    return calls


def _async_return(value):
    async def fn(*a, **k):
        return value
    return fn


def _async_noop():
    async def fn(*a, **k):
        return None
    return fn


def _run(ctx):
    return asyncio.run(run_item_workflow(ctx, ITEM, "item/PAY-1"))


# --- terminals ---------------------------------------------------------------

def test_happy_path_reaches_queued(stubs):
    ctx = Recorder()
    outcome = _run(ctx)
    assert outcome.kind == "queued" and outcome.pr == 42
    assert len(ctx.approved) == 1 and ctx.approved[0].pr == 42
    assert ("PAY-1", "queued", 42) in ctx.statuses


def test_review_changes_then_approve_loops_and_queues(stubs, monkeypatch):
    seq = iter([_verdict("request_changes"), _verdict("approve")])
    monkeypatch.setattr(driver, "review_once",
                        lambda *a, **k: _wrap(next(seq)))
    ctx = Recorder()
    outcome = _run(ctx)
    assert outcome.kind == "queued"
    assert stubs["fix"] == 1  # exactly one coder_fix round ran


def test_review_exhausted_escalates(stubs, monkeypatch):
    monkeypatch.setattr(driver, "review_once",
                        lambda *a, **k: _wrap(_verdict("request_changes")))
    ctx = Recorder()
    outcome = _run(ctx)
    assert outcome.kind == "escalated"
    assert any(d == "escalate_to_human" for _, d, _ in ctx.audits)


def test_out_of_scope_rejects(stubs, monkeypatch):
    monkeypatch.setattr(driver, "review_once",
                        lambda *a, **k: _wrap(_verdict("out_of_scope")))
    ctx = Recorder()
    assert _run(ctx).kind == "rejected"


def test_impasse_escalates(stubs, monkeypatch):
    monkeypatch.setattr(driver, "review_once",
                        lambda *a, **k: _wrap(_verdict("request_changes")))

    async def no_change_fix(ctx, item, branch, feedback=None):
        return (True, "impl") if feedback is None else (False, "I disagree")
    monkeypatch.setattr(driver, "run_coder", no_change_fix)
    ctx = Recorder()
    assert _run(ctx).kind == "escalated"


def test_unparseable_review_then_fix_queues(stubs, monkeypatch):
    calls = {"n": 0}

    def review(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise UnparseableSource("app/x.py", "'{' was never closed (line 1)")
        return _wrap(_verdict("approve"))
    monkeypatch.setattr(driver, "review_once", review)
    ctx = Recorder()
    assert _run(ctx).kind == "queued"


def test_verify_flag_then_ok_queues(stubs, monkeypatch):
    seq = iter([Verified(needs_flag=True), Verified(needs_flag=False)])
    monkeypatch.setattr(driver, "verify_once", lambda *a, **k: _wrap(next(seq)))
    ctx = Recorder()
    assert _run(ctx).kind == "queued"


def test_verify_flag_exhausted_escalates(stubs, monkeypatch):
    monkeypatch.setattr(driver, "verify_once",
                        lambda *a, **k: _wrap(Verified(needs_flag=True)))
    ctx = Recorder()
    assert _run(ctx).kind == "escalated"


def test_preprod_failure_fails(stubs, monkeypatch):
    monkeypatch.setattr(driver, "run_preprod_ci", _async_return(False))
    ctx = Recorder()
    outcome = _run(ctx)
    assert outcome.kind == "failed"
    assert ("PAY-1", "failed", 42) in ctx.statuses


# --- the human gate ----------------------------------------------------------

def test_gate_reject_rejects(stubs, monkeypatch):
    monkeypatch.setattr(wf, "check_decision", _async_return(SimpleNamespace(
        kind="reject", author="amir707", reason="no", comment_index=0)))
    ctx = Recorder()
    assert _run(ctx).kind == "rejected"


def test_gate_suspends_then_resumes_to_queued(stubs, monkeypatch):
    """First look finds no decision (RequestInput suspend); the executor
    nudges; the second look approves — proving suspend/resume works."""
    looks = {"n": 0}

    async def decide(*a, **k):
        looks["n"] += 1
        if looks["n"] == 1:
            return None  # suspend
        return SimpleNamespace(kind="approve", author="amir707", reason="",
                               comment_index=0)
    monkeypatch.setattr(wf, "check_decision", decide)
    ctx = Recorder()
    outcome = _run(ctx)
    assert outcome.kind == "queued"
    assert looks["n"] >= 2  # it actually re-checked after the nudge


def test_gate_budget_exhausted_awaits(stubs, monkeypatch):
    monkeypatch.setattr(wf, "check_decision", _async_return(None))  # never decides
    ctx = Recorder()
    ctx._policies["approver"]["gate_wait_minutes"] = 0  # zero budget
    outcome = _run(ctx)
    assert outcome.kind == "awaiting" and outcome.pr == 42


def _wrap(value):
    async def fn():
        return value
    return fn()
