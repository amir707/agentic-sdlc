"""The human approval gate (ADR-0005).

After the approver agent posts its dossier, the pipeline BLOCKS here
until a human on the project's approvers list decides on the PR itself
with a command comment:

    /approve
    /reject <reason>
    /hold

Identity is the enforcement: command comments from authors not on the
approvers list are ignored, and each ignore is audited once. AUTHORITY
LIVES ONLY IN THE GITHUB COMMENT — the mechanisms below merely decide
WHEN to look:

- check_decision(): one scan of the PR conversation. The atom every
  gate mode is built from; a chat "nudge" or a terminal Enter triggers
  exactly one of these and can never approve anything by itself.
- await_decision(): the polling loop (gate_mode: poll) — the deliberate
  carve-out from the no-polling rule; waiting on a human is not agent
  coordination.
- gate_mode: nudge (driver) and the ADK Workflow's RequestInput suspend
  both call check_decision() once per human nudge instead of polling.

The baseline (how much of the conversation is history) is captured when
the DOSSIER is posted, not when the gate starts — a human who decides
on GitHub before the gate first looks must still be seen.

Three outcomes, all audited; rejection is a real back-edge (return to
backlog via the unified rejection mechanism).
"""

import asyncio
from dataclasses import dataclass

_COMMANDS = ("/approve", "/reject", "/hold")


@dataclass
class Decision:
    kind: str        # approve | reject | hold
    author: str
    reason: str      # free text after /reject (empty otherwise)
    comment_index: int = -1  # position in the conversation; a hold
    #                          advances the baseline past itself so a
    #                          later /approve can be seen


def parse_command(body: str) -> tuple[str, str] | None:
    """(kind, reason) if the comment is a gate command, else None."""
    stripped = body.strip()
    for command in _COMMANDS:
        if stripped == command or stripped.startswith(command + " "):
            return command[1:], stripped[len(command):].strip()
    return None


def scan(comments: list[dict], approvers: list[str],
         skip: int) -> tuple[Decision | None, list[dict]]:
    """Pure scan of a PR conversation (unit-testable).

    Only comments after index `skip` count (everything up to and
    including the dossier is history). Returns the first valid decision
    plus any ignored command comments from non-approvers."""
    ignored = []
    for offset, comment in enumerate(comments[skip:]):
        parsed = parse_command(comment["body"])
        if not parsed:
            continue
        kind, reason = parsed
        if comment["author"] not in approvers:
            ignored.append(comment)
            continue
        return Decision(kind=kind, author=comment["author"], reason=reason,
                        comment_index=skip + offset), ignored
    return None, ignored


async def check_decision(repo_host, store, pr: int, approvers: list[str],
                         baseline: int,
                         audited_ignores: set | None = None
                         ) -> Decision | None:
    """ONE look at the PR: scan, audit any unauthorized commands (once
    per comment across repeated checks via audited_ignores), audit and
    return a valid decision, or return None."""
    comments = repo_host.get_review_threads(pr)
    decision, ignored = scan(comments, approvers, baseline)

    for comment in ignored:
        key = (comment["author"], comment["created_at"])
        if audited_ignores is None or key not in audited_ignores:
            if audited_ignores is not None:
                audited_ignores.add(key)
            await store.call(
                "append_audit", actor="approval_gate",
                decision="ignore_unauthorized_command",
                factors={"pr": pr, "author": comment["author"],
                         "body": comment["body"][:100],
                         "rule": "author not on approvers list"})

    if decision:
        await store.call(
            "append_audit", actor="approval_gate",
            decision=f"human_{decision.kind}",
            factors={"pr": pr, "author": decision.author,
                     "reason": decision.reason or None})
    return decision


async def await_decision(repo_host, store, pr: int, approvers: list[str],
                         baseline: int | None = None,
                         poll_seconds: float = 10,
                         timeout_seconds: float = 3600) -> Decision:
    """gate_mode: poll — loop check_decision until a human decides."""
    if baseline is None:
        baseline = len(repo_host.get_review_threads(pr))
    audited_ignores: set[tuple[str, str]] = set()
    waited = 0.0

    while waited <= timeout_seconds:
        decision = await check_decision(repo_host, store, pr, approvers,
                                        baseline, audited_ignores)
        if decision:
            return decision
        await asyncio.sleep(poll_seconds)
        waited += poll_seconds

    raise TimeoutError(f"no gate decision on PR #{pr} "
                       f"within {timeout_seconds}s")
