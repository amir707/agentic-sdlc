"""governance: every consequential exit leaves the SAME evidence.

Before this module each escalation site assembled audit + store status
+ board + console by hand and they drifted (some audited hold_merge
while setting escalated, some forgot the item id, some skipped the
board). These tests pin the uniform shape so a new call site cannot
drift again.
"""

import asyncio
from types import SimpleNamespace

from orchestrator import governance
from orchestrator.rejection import Rejection


class FakeCtx:
    def __init__(self):
        self.audits, self.statuses, self.board_done = [], [], []
        self.board = SimpleNamespace(
            finish=lambda item, note: self.board_done.append((item, note)))
        self.store = self.repo_host = None  # bounce is patched in its test

    async def audit(self, actor, decision, factors):
        self.audits.append((actor, decision, factors))

    async def set_status(self, item_id, status, pr=None):
        self.statuses.append((item_id, status, pr))


ITEM = {"id": "PAY-101", "title": "x"}


def test_escalate_records_audit_status_and_board():
    ctx = FakeCtx()
    asyncio.run(governance.escalate(
        ctx, ITEM, 7, "release_guard", "why", error="boom", head_sha="abc"))
    assert ctx.audits == [("release_guard", "escalate_to_human", {
        "item": "PAY-101", "pr": 7, "rule": "why",
        "head_sha": "abc", "error": "boom"})]
    assert ctx.statuses == [("PAY-101", "escalated", 7)]
    assert ctx.board_done == [("PAY-101", "escalated")]


def test_fail_marks_failed_and_audits_hold_merge():
    ctx = FakeCtx()
    asyncio.run(governance.fail(ctx, ITEM, 7, "release_guard", "ci red",
                                head_sha="abc"))
    assert ctx.audits[0][1] == "hold_merge"
    assert ctx.audits[0][2]["head_sha"] == "abc"
    assert ctx.statuses == [("PAY-101", "failed", 7)]


def test_hold_audits_but_never_changes_status():
    ctx = FakeCtx()
    asyncio.run(governance.hold(ctx, ITEM, 7, "release_guard", "not mergeable",
                                error="405"))
    assert ctx.audits[0][1] == "hold_merge"
    assert ctx.statuses == []  # stays queued for the next release event


def test_bounce_to_coder_keeps_status_but_to_backlog_rejects(monkeypatch):
    seen = []

    async def fake_reject(store, repo_host, rejection, actor):
        seen.append((rejection.reason_code, actor))
    monkeypatch.setattr(governance, "reject", fake_reject)

    ctx = FakeCtx()
    asyncio.run(governance.bounce(
        ctx, ITEM, Rejection(7, "policy_flag_required", "coder", "flag"),
        actor="verify"))
    assert ctx.statuses == []  # same PR, coder continues
    asyncio.run(governance.bounce(
        ctx, ITEM, Rejection(7, "human_declined", "backlog", "no"),
        actor="approval_gate"))
    assert ctx.statuses == [("PAY-101", "rejected", 7)]
    assert [a for _, a in seen] == ["verify", "approval_gate"]
