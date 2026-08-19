"""Item lifecycle transitions — the ONE place a governance outcome is
recorded.

Every consequential exit from the pipeline (hand to a human, bounce a
PR, block a merge) must leave the same evidence: an audit entry with
its rule, the item's store status, the activity board, and the console
line. Before this module each call site assembled that trio by hand and
they drifted. Now a node or handler names the OUTCOME; the evidence is
uniform by construction.

Plain forward progress (in_review -> verified -> ...) stays a bare
`ctx.set_status` — there is nothing to record beyond the status itself.
"""

from mcp_server.vocab import Actor, Decision, ItemStatus
from sdlc.governance.rejection import Rejection, reject
from sdlc.engine.narrate import say


async def escalate(ctx, item: dict, pr: int | None, actor: str, rule: str,
                   *, error: str | None = None, note: str | None = None,
                   **extra) -> None:
    """Hand the item to a human: audit the rule, mark it escalated, free
    the board. `extra` carries evidence specific to the path (author,
    head_sha, ...); `error` is a redacted, truncated cause."""
    factors = {"item": item["id"], "pr": pr, "rule": rule, **extra}
    if error is not None:
        factors["error"] = error
    await ctx.audit(actor, Decision.ESCALATE_TO_HUMAN, factors)
    await ctx.set_status(item["id"], ItemStatus.ESCALATED, pr)
    ctx.board.finish(item["id"], note or "escalated")
    say(item["id"], f"escalated to human: {rule}", level="warn", pr=pr)


async def bounce(ctx, item: dict, rejection: Rejection, actor: str) -> None:
    """Reject a PR through the unified mechanism (rejection.py) AND keep
    the store consistent: a return to the coder continues on the same
    PR (status unchanged); a return to the author or the backlog ends
    the item (status=rejected)."""
    await reject(ctx.store, ctx.repo_host, rejection, actor=actor)
    if rejection.return_to != "coder":
        await ctx.set_status(item["id"], ItemStatus.REJECTED, rejection.pr)


async def fail(ctx, item: dict, pr: int, actor: str, rule: str,
               *, head_sha: str | None = None,
               error: str | None = None) -> None:
    """A machine gate failed for the item's current head: audit the
    hold and mark the item failed (a later run may retry it)."""
    factors = {"item": item["id"], "pr": pr, "rule": rule}
    if head_sha:
        factors["head_sha"] = head_sha
    if error is not None:
        factors["error"] = error
    await ctx.audit(actor, Decision.HOLD_MERGE, factors)
    await ctx.set_status(item["id"], ItemStatus.FAILED, pr)
    say(item["id"], f"BLOCKED PR #{pr}: {rule}", level="warn", pr=pr)


async def hold(ctx, item: dict, pr: int, actor: str, rule: str, *,
               head_sha: str | None = None,
               error: str | None = None) -> None:
    """A guard blocks the merge WITHOUT changing the item's status: it
    stays queued and is reconsidered at the next release event (e.g.
    the branch is not mergeable — a human rebases). The release
    manager's own merge/hold JUDGMENT is not a guard and is audited
    where it is made."""
    factors = {"item": item["id"], "pr": pr, "rule": rule}
    if head_sha:
        factors["head_sha"] = head_sha
    if error is not None:
        factors["error"] = error
    await ctx.audit(actor, Decision.HOLD_MERGE, factors)
    say("release", f"BLOCKED PR #{pr}: {rule}")
