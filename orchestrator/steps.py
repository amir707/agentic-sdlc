"""PER-ITEM step handlers: what the ADK pipeline's nodes call.

Each function is single-shot — one coder round, one review, one verify,
one preprod deploy, one dossier — and idempotent for its head SHA
(pr_markers, G5). The LOOPS between them (fix rounds, flag rounds, the
human gate) are graph edges in adapters/adk/workflow.py, not Python
loops here (ADR-0007). Every step re-derives from ground truth — the
local workspace diff — never from an agent's claims (G1).
"""

import json
import re
import subprocess
from pathlib import Path

from adapters import deploy
from orchestrator import schemas
from orchestrator.context import RunContext
from orchestrator.dependency_graph import blast_radius
from orchestrator.json_util import extract_json
from orchestrator.pr_markers import find_marker, marker
from sdlc_steps import preprod_ci, verify as verify_step
from sdlc_steps.approver import spec as approver_spec
from sdlc_steps.code_reviewer import spec as reviewer_spec
from sdlc_steps.coder import spec as coder_spec
from tools.diff_analysis import files_touched


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:32]


def branch_for(item: dict) -> str:
    """The item's branch name — the identity a resumed run reattaches to."""
    return f"item/{item['id']}-{_slug(item['title'])}"


# --- coder -------------------------------------------------------------------

async def run_coder(ctx: RunContext, item: dict, branch: str,
                    feedback: str | None = None) -> None:
    """First call implements the item on a fresh branch; calls with
    feedback fix it in place (the generator half of the loop)."""
    if feedback is None:
        ctx.board.begin(item["id"], "coder", "implementing")
        ctx.workspace.start_branch(branch)
        task = ("Implement this backlog item in the workspace. Follow your "
                "core rules and the project conventions.")
    else:
        ctx.board.begin(item["id"], "coder", "fixing per feedback")
        task = ("Address the feedback below on your existing implementation "
                "in the workspace. Fix what is blocking; reply through code.")

    payload = {"task": task, "item": item, "feedback": feedback,
               "flag_policy_min_risk":
                   ctx.project.policy("verify")["flag_required_min_risk"]}
    result = await ctx.invoke(
        coder_spec.build(ctx.project, str(ctx.workspace.dir)),
        json.dumps(payload, indent=2))

    if not ctx.workspace.has_changes():
        if feedback is None:
            raise RuntimeError(f"coder produced no changes for {item['id']}")
        # A fix round with no code change is a DISAGREEMENT, not a fix —
        # the caller decides what to do with it (impasse -> human).
        return False, result.text
    ctx.workspace.commit_all(f"{item['id']}: {item['title']}")
    ctx.workspace.push(branch, ctx.repo_host.authenticated_remote())
    return True, result.text


async def open_pr(ctx: RunContext, item: dict, branch: str) -> int:
    # Resume support: a crashed run may already have opened this PR —
    # the branch is the identity, the PR is reused, review proceeds.
    existing = ctx.repo_host.find_open_pr(branch)
    if existing:
        print(f"[coder] PR #{existing} already open for {item['id']} "
              "(reusing)", flush=True)
        return existing
    body = (f"Item: {item['id']}\n\n"
            f"claimed_risk: {item['claimed_risk']} | "
            f"claimed_impact: {item['claimed_impact']} | "
            f"area_hint: {item['area_hint']}\n\n{item['description']}")
    # Title carries the item id; verify later prepends the verified
    # labels: "[area:payments][risk:high][flag:yes] PAY-101: <title>".
    pr = ctx.repo_host.open_pr(branch, f"{item['id']}: {item['title']}", body)
    # The item<->PR mapping lives in the audit trail (status views use it).
    await ctx.audit("coder", "open_pr",
                    {"item": item["id"], "pr": pr, "branch": branch})
    print(f"[coder] PR #{pr} opened for {item['id']}", flush=True)
    return pr


# --- reviewer ----------------------------------------------------------------

def _coverage_summary(ctx: RunContext) -> str:
    """Deterministic step: coverage numbers the reviewer judges."""
    proc = subprocess.run(
        [str(Path(ctx.workspace.dir) / ".venv" / "bin" / "python"),
         "-m", "pytest", "-q", "--cov=app", "--cov-report=term"],
        cwd=ctx.workspace.dir, capture_output=True, text=True, timeout=600)
    return (proc.stdout + proc.stderr)[-2500:]


async def review_once(ctx: RunContext, item: dict, pr: int,
                      iteration: int) -> schemas.ReviewVerdict:
    """One review round. Posts the verdict as a PR comment and returns
    it schema-validated."""
    ctx.board.begin(item["id"], "code_reviewer",
                    f"PR #{pr} round {iteration + 1}")
    # Diff from the LOCAL workspace, not GitHub: right after a push the
    # PR-diff endpoint can lag by seconds, and judging a stale diff once
    # made verify reject a fix that was already correct.
    diff = ctx.workspace.diff_against()
    closure = blast_radius(ctx.workspace.dir, files_touched(diff))
    payload = {
        "task": ("Review this PR. Reply ONLY with JSON: "
                 '{"verdict": "approve|request_changes|out_of_scope", '
                 '"reasoning": "...", '
                 '"comments": [{"body": "...", "blocking": true}]}'),
        "item": item,
        "diff": diff,
        "coverage_report": _coverage_summary(ctx),
        "dependency_closure": sorted(closure),
    }
    result = await ctx.invoke(
        reviewer_spec.build(ctx.project, str(ctx.workspace.dir), diff),
        json.dumps(payload, indent=2))
    verdict = schemas.ReviewVerdict.model_validate(extract_json(result.text))

    findings = "\n".join(
        f"- {'🔴 blocking' if c.blocking else '⚪ cosmetic'}: {c.body}"
        for c in verdict.comments) or "- no findings"
    sha = ctx.repo_host.get_pr(pr)["head_sha"]
    ctx.repo_host.post_comment(pr, (
        f"**🤖 AI code review — {verdict.verdict.upper()}** — "
        f"iteration {iteration + 1}\n"
        "<sub>automated reviewer agent verdict; NOT the human gate — "
        "that is the /approve decision on the dossier</sub>\n\n"
        f"{verdict.reasoning}\n\n{findings}\n\n"
        f"{marker('review', sha, verdict.verdict)}"))
    return verdict


def review_already_approved(ctx: RunContext, pr: int) -> bool:
    """Resume idempotency: this PR's current head already carries a
    review approval (SHA-keyed marker), so the reviewer node skips a
    duplicate review on a re-run (G5)."""
    sha = ctx.repo_host.get_pr(pr)["head_sha"]
    comments = ctx.repo_host.get_review_threads(pr)
    return find_marker(comments, marker("review", sha, "approve")) is not None


# --- verify ------------------------------------------------------------------

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
        await ctx.audit("verify", "escalate_risk_label", {
            "pr": pr, "claimed_risk": result.claimed_risk,
            "assessed_risk": assessed,
            "verified_risk": result.verified_risk,
            "reason": result.escalation_reason})
        print(f"[verify] PR #{pr} risk escalated "
              f"{result.claimed_risk} -> {result.verified_risk}", flush=True)

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
        print(f"[resume] PR #{pr}: preprod already passed for {sha[:7]} — "
              "skipping", flush=True)
        return True

    ctx.board.begin(item["id"], "preprod_ci",
                    f"PR #{pr} build + tagged revision + smoke")
    try:
        ci = preprod_ci.run_preprod(pr, str(ctx.workspace.dir),
                                    verified.areas, ctx.project)
    except deploy.DeployError as exc:
        # Degrade, don't die: an infrastructure failure (build error,
        # missing baseline service, quota) fails THIS item's preprod —
        # audited with the redacted command — and the sprint walks on.
        await ctx.audit("preprod_ci", "preprod_result", {
            "pr": pr, "passed": False, "revision": f"pr-{pr}",
            "error": str(exc)[:300]})
        print(f"[ci] PR #{pr} preprod FAILED (infrastructure): "
              f"{str(exc)[:120]}", flush=True)
        return False
    ctx.repo_host.post_comment(pr, (
        preprod_ci.format_comment(ci) + "\n\n"
        + marker("ci", sha, "passed" if ci.passed else "failed")))
    if ci.preprod_url:
        await ctx.store.call("record_deploy", pr=pr,
                             revision=ci.revision_tag, traffic="preprod",
                             area=verified.primary_area)
    await ctx.audit("preprod_ci", "preprod_result", {
        "pr": pr, "passed": ci.passed, "revision": ci.revision_tag,
        "preprod_url": ci.preprod_url, "smoke": ci.smoke})
    print(f"[ci] PR #{pr} preprod "
          f"{'passed' if ci.passed else 'FAILED'}", flush=True)
    return ci.passed


# --- approver ----------------------------------------------------------------

async def run_approver(ctx: RunContext, item: dict, pr: int,
                       verified) -> int:
    """Post the decision dossier; return the gate baseline (the comment
    index right after it, so a decision made before the gate first
    looks is still seen)."""
    # Resume idempotency: if this head commit already has its dossier,
    # reuse it — and the gate baseline starts right after it, so a
    # decision the human made before the restart is still honored.
    sha = ctx.repo_host.get_pr(pr)["head_sha"]
    comments = ctx.repo_host.get_review_threads(pr)
    existing = find_marker(comments, marker("dossier", sha))
    if existing is not None:
        print(f"[resume] PR #{pr}: dossier already posted for {sha[:7]} — "
              "reusing", flush=True)
        return existing + 1

    ctx.board.begin(item["id"], "approver", f"PR #{pr} assembling dossier")
    payload = {
        "task": "Assemble the decision dossier for this PR as one comment.",
        "item": item,
        "verified_labels": {"area": verified.primary_area,
                            "risk": verified.verified_risk,
                            "escalated": verified.escalated,
                            "flag_covered": verified.flag["covered"]},
        "review_threads": ctx.repo_host.get_review_threads(pr),
    }
    result = await ctx.invoke(approver_spec.build(ctx.project),
                              json.dumps(payload, indent=2))
    # The approver is tool-less, so its Dossier schema is enforced
    # natively (output_schema); the orchestrator renders it for humans.
    dossier = schemas.Dossier.model_validate(extract_json(result.text))
    approvers = ctx.project.policy("approver")["approvers"]
    ctx.repo_host.post_comment(pr, (
        schemas.render_dossier(dossier, approvers)
        + "\n\n" + marker("dossier", sha)))
    await ctx.audit("approver", "post_dossier", {"pr": pr})
    # The gate baseline is captured HERE, at dossier-post time: a human
    # who decides on GitHub before the gate first looks must be seen.
    return len(ctx.repo_host.get_review_threads(pr))
