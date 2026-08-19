"""The MACHINE gates on a PR head — deterministic, idempotent per SHA,
run by BOTH clocks: the sprint runs them on the way to the human gate,
the release pass re-runs them on the current head before any merge
(the release guard). That shared use is why they live in governance,
not in sprint: release must never depend on the sprint clock.

  verify_once             claimed-vs-actual risk, flag policy, PR labels
  preprod_passed_for_head has this head a passing preprod (marker, G5)?
  run_preprod_ci          deploy the head to preprod + smoke, once per SHA
"""

from mcp_server.vocab import Actor, Decision
from sdlc.context import RunContext
from sdlc.governance.markers import find_marker, marker
from sdlc.ports.world import DeployError
from sdlc.steps import preprod_ci, verify as verify_step
from sdlc.engine.narrate import say


async def verify_once(ctx: RunContext, item: dict,
                      pr: int) -> verify_step.VerifyResult:
    """One verify pass. Audits any risk escalation; writes verified
    labels into the PR title when the flag policy is satisfied."""
    ctx.board.begin(item["id"], "verify", f"PR #{pr} claimed-vs-actual")
    # Local diff for the same reason as review_once: GitHub's PR diff is
    # eventually consistent after a push; the workspace is the truth.
    diff = ctx.workspace.diff_against()
    assessments = {a["item_id"]: a
                   for a in await ctx.store.call("list_assessments")}
    assessed = assessments.get(item["id"], {}).get("risk")
    result = verify_step.verify(diff, item["claimed_risk"], ctx.project,
                                str(ctx.workspace.dir),
                                assessed_risk=assessed)
    if result.escalated:
        await ctx.audit(Actor.VERIFY, Decision.ESCALATE_RISK_LABEL, {
            "pr": pr, "claimed_risk": result.claimed_risk,
            "assessed_risk": assessed,
            "verified_risk": result.verified_risk,
            "reason": result.escalation_reason})
        say("verify", f"PR #{pr} risk escalated "
              f"{result.claimed_risk} -> {result.verified_risk}")

    if not result.needs_flag:
        # Title: <ITEM-ID>: [area:..][risk:..][flag:..] <item title>
        # (rebuilt from scratch — no parsing of whatever is there now).
        ctx.repo_host.update_title(
            pr, f"{item['id']}: {result.title_prefix} {item['title']}")
    return result


# --- preprod CI --------------------------------------------------------------

def preprod_passed_for_head(ctx: RunContext, pr: int, sha: str) -> bool:
    """Has this head commit already been deployed to preprod and smoked?"""
    comments = ctx.repo_host.get_review_threads(pr)
    return find_marker(comments, marker("ci", sha, "passed")) is not None


async def run_preprod_ci(ctx: RunContext, item: dict, pr: int,
                         verified) -> bool:
    sha = ctx.repo_host.get_pr(pr)["head_sha"]
    if preprod_passed_for_head(ctx, pr, sha):
        say("resume", f"PR #{pr}: preprod already passed for {sha[:7]} — "
              "skipping")
        return True

    ctx.board.begin(item["id"], "preprod_ci",
                    f"PR #{pr} build + tagged revision + smoke")
    try:
        ci = preprod_ci.run_preprod(pr, str(ctx.workspace.dir),
                                    verified.areas, ctx.project,
                                    ctx.deployer)
    except DeployError as exc:
        # Degrade, don't die: an infrastructure failure (build error,
        # missing baseline service, quota) fails THIS item's preprod —
        # audited with the redacted command — and the sprint walks on.
        await ctx.audit(Actor.PREPROD_CI, Decision.PREPROD_RESULT, {
            "pr": pr, "passed": False, "revision": f"pr-{pr}",
            "error": str(exc)[:300]})
        say("ci", f"PR #{pr} preprod FAILED (infrastructure): "
              f"{str(exc)[:120]}")
        return False
    ctx.repo_host.post_comment(pr, (
        preprod_ci.format_comment(ci) + "\n\n"
        + marker("ci", sha, "passed" if ci.passed else "failed")))
    if ci.preprod_url:
        await ctx.store.call("record_deploy", pr=pr,
                             revision=ci.revision_tag, traffic="preprod",
                             area=verified.primary_area)
    await ctx.audit(Actor.PREPROD_CI, Decision.PREPROD_RESULT, {
        "pr": pr, "passed": ci.passed, "revision": ci.revision_tag,
        "preprod_url": ci.preprod_url, "smoke": ci.smoke})
    say("ci", f"PR #{pr} preprod "
          f"{'passed' if ci.passed else 'FAILED'}")
    return ci.passed


# --- approver ----------------------------------------------------------------
