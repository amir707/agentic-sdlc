"""Approval-gate tests: command parsing and the identity-checked scan."""

from orchestrator.gate import parse_command, scan

APPROVERS = ["amir707"]


def _c(author, body, ts="2026-07-05T10:00:00Z"):
    return {"author": author, "body": body, "created_at": ts}


def test_parse_commands():
    assert parse_command("/approve") == ("approve", "")
    assert parse_command("  /reject scope is wrong  ") == ("reject", "scope is wrong")
    assert parse_command("/hold") == ("hold", "")
    assert parse_command("/approved") is None          # not a command
    assert parse_command("please /approve this") is None  # must lead
    assert parse_command("looks good!") is None


def test_scan_finds_first_valid_decision_after_dossier():
    comments = [
        _c("agentic-sdlc", "dossier ..."),             # index 0: history
        _c("amir707", "/approve"),
    ]
    decision, ignored = scan(comments, APPROVERS, skip=1)
    assert decision.kind == "approve" and decision.author == "amir707"
    assert ignored == []


def test_scan_ignores_non_approver_commands():
    comments = [
        _c("agentic-sdlc", "dossier ..."),
        _c("stranger", "/approve"),                    # ignored + audited
        _c("amir707", "/reject not needed anymore"),
    ]
    decision, ignored = scan(comments, APPROVERS, skip=1)
    assert decision.kind == "reject"
    assert decision.reason == "not needed anymore"
    assert [c["author"] for c in ignored] == ["stranger"]


def test_scan_ignores_pre_dossier_history():
    comments = [
        _c("amir707", "/approve"),                     # stale, before dossier
        _c("agentic-sdlc", "dossier ..."),
    ]
    decision, ignored = scan(comments, APPROVERS, skip=2)
    assert decision is None and ignored == []


def test_scan_returns_none_while_waiting():
    comments = [_c("agentic-sdlc", "dossier ..."),
                _c("amir707", "thinking about it...")]
    decision, ignored = scan(comments, APPROVERS, skip=1)
    assert decision is None and ignored == []


# --- single-check + hold-advance (nudge / ADK-suspend semantics) -------------

class _StubRepoHost:
    def __init__(self, comments):
        self.comments = comments

    def get_review_threads(self, pr):
        return self.comments


class _StubStore:
    def __init__(self):
        self.audits = []

    async def call(self, tool, **args):
        assert tool == "append_audit"
        self.audits.append(args)


import asyncio

from orchestrator.gate import check_decision


def test_check_decision_is_one_authenticated_look():
    """The nudge/resume atom: one scan, no decision -> None (and the
    nudger gained nothing); decision present -> returned + audited."""
    store = _StubStore()
    host = _StubRepoHost([_c("agentic-sdlc", "dossier ...")])
    result = asyncio.run(check_decision(host, store, 7, APPROVERS,
                                        baseline=1, audited_ignores=set()))
    assert result is None and store.audits == []

    host.comments.append(_c("amir707", "/approve"))
    result = asyncio.run(check_decision(host, store, 7, APPROVERS,
                                        baseline=1, audited_ignores=set()))
    assert result.kind == "approve"
    assert store.audits[-1]["decision"] == "human_approve"


def test_check_decision_audits_ignored_command_once():
    store = _StubStore()
    host = _StubRepoHost([_c("agentic-sdlc", "dossier ..."),
                          _c("stranger", "/approve")])
    ignores: set = set()
    for _ in range(3):  # repeated nudges must not spam the audit log
        asyncio.run(check_decision(host, store, 7, APPROVERS,
                                   baseline=1, audited_ignores=ignores))
    assert len(store.audits) == 1
    assert store.audits[0]["decision"] == "ignore_unauthorized_command"


def test_hold_carries_its_index_so_the_gate_can_advance():
    """Regression: after /hold, advancing the baseline past it lets a
    later /approve be seen instead of rescanning the same hold forever."""
    comments = [_c("agentic-sdlc", "dossier ..."),
                _c("amir707", "/hold"),
                _c("amir707", "/approve")]
    decision, _ = scan(comments, APPROVERS, skip=1)
    assert decision.kind == "hold" and decision.comment_index == 1
    decision, _ = scan(comments, APPROVERS, skip=decision.comment_index + 1)
    assert decision.kind == "approve"
