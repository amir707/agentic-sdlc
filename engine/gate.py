"""The human approval gate (ADR-0005).

After the approver agent posts its dossier, the pipeline BLOCKS here
until a human on the project's approvers list decides on the PR itself
with a command comment:

    /approve
    /reject <reason>
    /hold

Identity is the enforcement: command comments from authors not on the
approvers list are ignored, and each ignore is audited once. Blocking
by polling is the deliberate carve-out from the no-polling rule —
waiting on a human is not agent coordination. Three outcomes, all
audited; rejection is a real back-edge (return to backlog via the
unified rejection mechanism).
"""

import asyncio
from dataclasses import dataclass

_COMMANDS = ("/approve", "/reject", "/hold")


@dataclass
class Decision:
    kind: str        # approve | reject | hold
    author: str
    reason: str      # free text after /reject (empty otherwise)


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
    for comment in comments[skip:]:
        parsed = parse_command(comment["body"])
        if not parsed:
            continue
        kind, reason = parsed
        if comment["author"] not in approvers:
            ignored.append(comment)
            continue
        return Decision(kind=kind, author=comment["author"],
                        reason=reason), ignored
    return None, ignored


async def await_decision(repo_host, store, pr: int, approvers: list[str],
                         poll_seconds: float = 10,
                         timeout_seconds: float = 3600) -> Decision:
    baseline = len(repo_host.get_review_threads(pr))
    audited_ignores: set[tuple[str, str]] = set()
    waited = 0.0

    while waited <= timeout_seconds:
        comments = repo_host.get_review_threads(pr)
        decision, ignored = scan(comments, approvers, baseline)

        for comment in ignored:
            key = (comment["author"], comment["created_at"])
            if key not in audited_ignores:
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

        await asyncio.sleep(poll_seconds)
        waited += poll_seconds

    raise TimeoutError(f"no gate decision on PR #{pr} "
                       f"within {timeout_seconds}s")
