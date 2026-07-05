"""Approval-gate tests: command parsing and the identity-checked scan."""

from engine.gate import parse_command, scan

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
