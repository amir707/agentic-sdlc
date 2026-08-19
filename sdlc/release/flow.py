"""RELEASE flow: one pass over the store's queue, one decision per PR.

Release has a different clock and ecosystem than the sprint (approvals,
incidents, confidence windows), so it is its own first-class ADK
Workflow behind the ReleaseExecutor port (sdlc/adapters/adk/release_workflow.py)
and its own resident service. This module holds what that Workflow's
nodes call: the queue read, the per-PR decision, and the trigger seam
the sprint uses to hand release off.

Stateless by design: nothing is carried from the sprint run — every
decision re-verifies the CURRENT head (the deterministic merge gate),
ensures a passing preprod for it, then asks the release manager.
"""

import json
import os

from mcp_server.vocab import Actor, Decision, ItemStatus
from sdlc import governance
from sdlc.governance import schemas
from sdlc.context import RunContext
from sdlc.engine.dependency_graph import UnparseableSource
from sdlc.engine.json_util import extract_json
from sdlc.ports.world import DeployError, RepoHostError
from sdlc.governance.gates import preprod_passed_for_head, run_preprod_ci, verify_once
from sdlc_steps.release_manager import spec as rm_spec


async def release_queue(ctx: RunContext) -> list[dict]:
    """PRs awaiting release: the backlog items the STORE marks `queued`
    (a project's store is that one project's world, so this is inherently
    per-project). The release loop reads THIS, never an in-memory list —
    so it is resumable and independent of the sprint process that queued
    them. Workstream B: release is a peer loop over store state."""
    items = await ctx.store.call("list_backlog")
    return [i for i in items
            if i.get("status") == ItemStatus.QUEUED and i.get("pr")]


async def run_release_pass(ctx: RunContext) -> None:
    """One release pass — run as its own ADK Workflow (Workstream B).
    Release has a different clock and ecosystem than the sprint, so it is
    its own first-class ADK graph behind the ReleaseExecutor port
    (sdlc/adapters/adk/release_workflow.py); the lock serializes trickle calls
    within one process."""
    async with ctx.release_lock:
        await ctx.release_executor.run_pass(ctx)


async def trigger_release(ctx: RunContext) -> None:
    """Queued items get a release decision — but by WHOM depends on the
    deployment shape. With RELEASE_TRIGGER_URL set (the resident release
    service is running), the sprint side DELEGATES: it fires one event at
    the release service, whose log then owns the entire release
    narration — the sprint's job ends at status=queued (Workstream B's
    full separation). Without it (one-shot `make orchestrate`, no service
    running), the pass runs in-process as before."""
    url = os.environ.get("RELEASE_TRIGGER_URL")
    if not url:
        await run_release_pass(ctx)
        return
    from sdlc.engine.heartbeat import post_event
    print(f"[release] delegating to the release service ({url})",
          flush=True)
    await post_event(url, "sprint-delegate")


async def decide_release_pr(ctx: RunContext, item: dict,
                            confidence: float) -> str:
    """Decide and act on ONE queued PR: re-verify the head (the
    deterministic merge gate), ensure a passing preprod deploy, ask the
    release manager, then merge or hold. Returns the outcome
    ("merged" | "held" | "escalated" | "failed"). Called per PR by the
    release Workflow's node — the release-manager agent stays behind the
    AgentInvoker port (ADR-0007), exactly as the coder/reviewer do.

    Degrade, don't die: a repo-host failure on THIS PR (e.g. the store
    says queued but the PR does not exist — store/GitHub disagree after
    a repo reset) escalates this one item; it never aborts the pass, so
    the rest of the queue still gets its decisions."""
    try:
        return await _decide_release_pr(ctx, item, confidence)
    except RepoHostError as exc:
        await governance.escalate(
            ctx, item, item["pr"], Actor.RELEASE_GUARD,
            "repo host error while releasing this PR — the store and the "
            "repo may disagree (reset-item to replay, or reseed if the "
            "repo was recreated)", error=str(exc)[:200])
        return "escalated"


async def _decide_release_pr(ctx: RunContext, item: dict,
                             confidence: float) -> str:
    pr = item["pr"]
    # Recompute `verified` from the CURRENT head — no VerifyResult is
    # carried from the sprint run (stateless release). verify_once is
    # deterministic and idempotent; this IS the deterministic merge gate.
    pr_data = ctx.repo_host.get_pr(pr)
    head = pr_data["head_sha"]
    print(f"[release] deciding PR #{pr} ({item['id']}) — head {head[:7]}",
          flush=True)
    ctx.workspace.checkout_detached(pr_data["head_ref"])
    try:
        verified = await verify_once(ctx, item, pr)
    except UnparseableSource as broken:
        # No rework loop this late: post-approval commits are a human's to
        # answer for. Block the merge and escalate.
        await governance.escalate(
            ctx, item, pr, Actor.RELEASE_GUARD,
            f"post-approval head does not parse: {broken}", head_sha=head)
        return "escalated"
    if verified.needs_flag:
        await governance.escalate(
            ctx, item, pr, Actor.RELEASE_GUARD,
            "post-approval head violates the flag policy", head_sha=head)
        return "escalated"
    if not preprod_passed_for_head(ctx, pr, head):
        print(f"[release] PR #{pr}: head {head[:7]} has no passing preprod "
              "— deploying it now", flush=True)
        async with ctx.ci_lock:
            ci_ok = await run_preprod_ci(ctx, item, pr, verified)
        ctx.board.finish(item["id"], "head re-verified + preprod deployed")
        if not ci_ok:
            await governance.fail(ctx, item, pr, Actor.RELEASE_GUARD,
                                  "preprod failed for the current head",
                                  head_sha=head)
            return "failed"

    print(f"[release] PR #{pr} verified: area={verified.primary_area} "
          f"risk={verified.verified_risk} "
          f"flag={'yes' if verified.flag['covered'] else 'no'} — asking the "
          "release manager", flush=True)
    ctx.board.begin("RELEASE", "release_manager", f"deciding PR #{pr}")
    payload = {
        "task": ("Decide merge or hold for THIS ONE PR, right now. "
                 "Consult the store (open incidents, recent deploys, "
                 "health samples) and weigh your judgment rules — "
                 "especially: never merge into an area with an open "
                 "incident, and postpone when a recent PRODUCTION "
                 "deploy (traffic='100') in the same area or with an "
                 "overlapping closure has not yet shown healthy signal "
                 "within the confidence window. Deploy records with "
                 "traffic='preprod' are zero-traffic CI evidence — "
                 "ignore them; every PR has one by construction. "
                 'Reply ONLY with JSON: {"pr": ' + str(pr) +
                 ', "action": "merge|hold", "reasoning": "...", '
                 '"factors": {}}'),
        "pr": {
            "pr": pr, "item": item["id"],
            "area": verified.primary_area,
            "verified_risk": verified.verified_risk,
            "feature_flagged": verified.flag["covered"],
            "dependency_closure": sorted(verified.radius),
        },
        "deploy_confidence_minutes": confidence,
    }
    result = await ctx.invoke(rm_spec.build(ctx.project),
                              json.dumps(payload, indent=2))
    decision = schemas.ReleaseDecision.model_validate(
        extract_json(result.text))

    factors = {"pr": pr, "area": verified.primary_area,
               "verified_risk": verified.verified_risk,
               "feature_flagged": verified.flag["covered"],
               **decision.factors,
               "reasoning": decision.reasoning}
    if decision.action == "merge":
        try:
            ctx.repo_host.merge_pr(pr)
        except Exception as exc:  # noqa: BLE001 — degrade, don't die
            # Typically 405: branch not mergeable (main advanced and the
            # branch conflicts — flags.json is the usual magnet). Auto-rebase
            # is a documented successor, not built: the PR stays queued with
            # an audited reason for a human (rebase, or make reset-item).
            await governance.hold(
                ctx, item, pr, Actor.RELEASE_GUARD,
                "merge failed — branch likely conflicts with advanced "
                "main; rebase or reset-item", error=str(exc)[:200])
            return "held"
        try:
            ctx.deployer.promote(f"pr-{pr}")
        except DeployError as exc:
            # The MERGE already landed; only the traffic shift failed.
            # That is a half-released state no rerun can safely finish
            # (the branch is merged; re-verifying it is meaningless) —
            # a human completes the promote. Escalate with the facts,
            # never crash the pass.
            await governance.escalate(
                ctx, item, pr, Actor.RELEASE_GUARD,
                "PR merged but the traffic shift failed — promote tag "
                f"pr-{pr} manually (sdlc.adapters.gcloud promote) and set the "
                "item released", error=str(exc)[:300])
            return "escalated"
        await ctx.store.call("record_deploy", pr=pr,
                             revision=f"pr-{pr}", traffic="100",
                             area=verified.primary_area)
        await ctx.audit(Actor.RELEASE_MANAGER, Decision.MERGE_PR, factors)
        await ctx.set_status(item["id"], ItemStatus.RELEASED, pr)
        rule = decision.factors.get("dominating_rule", "")
        print(f"[release] MERGED PR #{pr} (traffic -> pr-{pr})"
              + (f" — rule: {rule}" if rule else "")
              + f" — {decision.reasoning[:140]}", flush=True)
        return "merged"
    # Held: the item STAYS queued in the store and is reconsidered on the
    # next release EVENT (incident cleared, confidence window passed).
    await ctx.audit(Actor.RELEASE_MANAGER, Decision.HOLD_MERGE, factors)
    rule = decision.factors.get("dominating_rule", "")
    print(f"[release] HELD PR #{pr}"
          + (f" — rule: {rule}" if rule else "")
          + f" — {decision.reasoning[:140]}", flush=True)
    return "held"


# NO in-process recheck loop: "when to reconsider a held PR" is answered
# by an EVENT — a Cloud Scheduler tick or a GitHub webhook (incident
# recovery / approval) → Pub/Sub → ADK's ambient-trigger endpoint
# (get_fast_api_app(trigger_sources=["pubsub"])), each firing exactly one
# run_release_pass over store state. A held PR simply stays queued until
# the next event; there is nothing to poll, so there is no asyncio.sleep.
# `python -m sdlc.app.release` (make release) is the same single pass
# run manually; see the runbook for the trigger wiring.
