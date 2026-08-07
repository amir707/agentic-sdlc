"""The driver: runs the SDLC definition (ADR-0003, ADR-0007).

definition.py says WHAT the process is; this file carries out the
PLANNING and RELEASE phases and the per-item governance around the edges
(resume dispatch from store status, escalation overrides, conflict
checks). The per-item PIPELINE itself — coder → review → verify →
preprod → approver → gate, with its fix/flag loops and human gate — runs
on ADK's graph engine through the PipelineExecutor port
(orchestrator/executor.py; ADK impl in adapters/adk/): one graph, no
imperative shadow. The single-shot handlers here (run_coder, review_once,
verify_once, run_preprod_ci, run_approver) are what that graph's nodes
call; the HANDLERS registry binds every definition step name to its
implementation.

Coordination is through the store and artifacts, never in memory: the
PR is the artifact between coder and reviewer; the store is the artifact
between everything else and the single source of lifecycle truth (G3).
"""

import asyncio
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

from orchestrator.config import ProjectConfig
from orchestrator.dependency_graph import (UnparseableSource, blast_radius,
                                           build_import_graph)
from tools.diff_analysis import files_touched
from orchestrator.activity import ActivityBoard
from orchestrator.gate import Decision, check_decision, parse_command
from orchestrator.invoker import AgentInvoker, Invocation
from orchestrator.executor import PipelineExecutor, ReleaseExecutor
from orchestrator.json_util import extract_json
from orchestrator import schemas
from orchestrator.rejection import Rejection, reject
from adapters.repo_host import GitHubRepoHost, RepoHostError
from adapters.store_client import DeliveryStore
from orchestrator.workspace import Workspace, WorkspaceFactory
from adapters import deploy
from sdlc_steps import incident_resolver, preprod_ci, sprint_packer, verify as verify_step
from sdlc_steps.approver import spec as approver_spec
from sdlc_steps.code_reviewer import spec as reviewer_spec
from sdlc_steps.coder import spec as coder_spec
from sdlc_steps.release_manager import spec as rm_spec
from sdlc_steps.risk_assessor import spec as assessor_spec


@dataclass
class RunContext:
    project: ProjectConfig
    store: DeliveryStore
    repo_host: GitHubRepoHost
    invoker: AgentInvoker
    workspace: Workspace
    # The two ADR-0007 execution ports, injected by the composition root
    # (None only in unit tests that never run them): the per-item pipeline
    # and the release pass — separate Workflows, separate clocks.
    executor: PipelineExecutor | None = None
    release_executor: ReleaseExecutor | None = None
    # Concurrent preprod deploys against ONE Cloud Run service would
    # fight over revision creation; CI is the one per-item stage that
    # must queue even when coders run in parallel.
    ci_lock: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))
    # Live "who is doing what, since when" (rendered by make watch).
    board: ActivityBoard = field(default_factory=ActivityBoard)
    # Release passes are serialized: with --parallel, two gate approvals
    # must not run two release managers over the same queue at once.
    release_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def invoke(self, spec, message: str) -> Invocation:
        """Every invocation is metered: token spend is sprint capacity."""
        result = await self.invoker.invoke(spec, message)
        await self.store.call(
            "record_token_usage", agent=spec.name, model=spec.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens)
        return result

    async def audit(self, actor: str, decision: str, factors: dict) -> None:
        await self.store.call("append_audit", actor=actor,
                              decision=decision, factors=factors)

    async def set_status(self, item_id: str, status: str,
                         pr: int | None = None) -> None:
        """Item lifecycle lives in the STORE; the orchestrator resumes
        from this, never from GitHub (the PR is only the artifact)."""
        await self.store.call("set_item_status", item_id=item_id,
                              status=status, pr=pr)


async def _escalation_override(ctx: "RunContext", item: dict,
                               pr: int) -> Decision | None:
    """An escalated/failed item re-enters the pipeline only by HUMAN
    word: the latest allowlisted gate command on the PR that is NEWER
    than the escalation itself. /approve = overrule and queue (the
    machine gates — verify + preprod — still re-run at release);
    /reject = back to the backlog."""
    approvers = ctx.project.policy("approver")["approvers"]
    audit = await ctx.store.call("list_audit")
    escalated_at = max(
        (e["ts"] for e in audit
         if e["factors"].get("pr") == pr and "escalate" in e["decision"]),
        default=None)
    for comment in reversed(ctx.repo_host.get_review_threads(pr)):
        parsed = parse_command(comment["body"])
        if not parsed or comment["author"] not in approvers:
            continue
        if escalated_at and comment["created_at"] <= escalated_at:
            return None  # command predates the escalation: stale
        kind, reason = parsed
        return Decision(kind=kind, author=comment["author"], reason=reason)
    return None


def _marker(kind: str, sha: str, extra: str = "") -> str:
    """Idempotency stamp for bot comments (invisible in the GitHub UI).
    Keyed to the head SHA: a new commit naturally invalidates it, so a
    restarted run repeats a stage only when the code actually changed."""
    suffix = f":{extra}" if extra else ""
    return f"<!-- agentic-sdlc:{kind}:{sha}{suffix} -->"


def _find_marker(comments: list[dict], marker: str) -> int | None:
    for index, comment in enumerate(comments):
        if marker in comment["body"]:
            return index
    return None


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:32]


def _branch(item: dict) -> str:
    return f"item/{item['id']}-{_slug(item['title'])}"


# --- planning phase ----------------------------------------------------------

async def run_risk_assessor(ctx: RunContext) -> dict[str, dict]:
    items = await ctx.store.call("list_backlog")
    graph = build_import_graph(ctx.workspace.dir)
    graph_lines = [f"{module} -> {sorted(deps)}"
                   for module, deps in sorted(graph.items()) if deps]

    # Resume-friendly: state lives in the store, so a crashed or
    # rate-limited run just reruns — items already assessed are skipped
    # (no wasted quota, no duplicate work). `make seed` remains the
    # explicit way to start truly fresh.
    done = {a["item_id"] for a in await ctx.store.call("list_assessments")}

    for item in items:
        if item["id"] in done:
            print(f"[assess] {item['id']}: already assessed (skipped)",
                  flush=True)
            continue
        print(f"[assess] {item['id']}: {item['title']}", flush=True)
        ctx.board.begin(item["id"], "risk_assessor", item["title"][:40])
        payload = {
            "task": ("Assess this backlog item and record your judgment via "
                     "record_assessment."),
            "item": item,
            "repo_import_graph": graph_lines,
        }
        await ctx.invoke(assessor_spec.build(ctx.project),
                         json.dumps(payload, indent=2))
        ctx.board.finish(item["id"], "assessed")

    assessments = {a["item_id"]: a
                   for a in await ctx.store.call("list_assessments")}
    missing = [i["id"] for i in items if i["id"] not in assessments]
    if missing:
        raise RuntimeError(f"assessor recorded nothing for: {missing}")
    return assessments


async def run_sprint_packer(ctx: RunContext,
                            assessments: dict[str, dict]) -> list[dict]:
    items = await ctx.store.call("list_backlog")
    result = sprint_packer.pack(items, assessments,
                                ctx.project.policy("sprint_packer"))
    for refusal in result.refused:
        await ctx.audit("sprint_packer", "refuse_item", {
            "item": refusal.item_id, "constraint": refusal.constraint,
            "detail": refusal.detail})
        print(f"[pack] REFUSED {refusal.item_id}: {refusal.constraint} "
              f"({refusal.detail})", flush=True)
    sprint = await ctx.store.call(
        "create_sprint", item_ids=[i["id"] for i in result.selected],
        rationale=result.rationale)
    await ctx.audit("sprint_packer", "create_sprint", {
        "sprint": sprint["id"], "items": sprint["item_ids"],
        "rationale": result.rationale})
    print(f"[pack] sprint #{sprint['id']}: {sprint['item_ids']}", flush=True)
    return result.selected


# --- per-item phase ----------------------------------------------------------

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


def _coverage_summary(ctx: RunContext) -> str:
    """Deterministic step: coverage numbers the reviewer judges."""
    proc = subprocess.run(
        [str(Path(ctx.workspace.dir) / ".venv" / "bin" / "python"),
         "-m", "pytest", "-q", "--cov=app", "--cov-report=term"],
        cwd=ctx.workspace.dir, capture_output=True, text=True, timeout=600)
    return (proc.stdout + proc.stderr)[-2500:]


async def review_once(ctx: RunContext, item: dict, pr: int,
                      iteration: int) -> schemas.ReviewVerdict:
    """One review round (single-shot: the Workflow expression reuses
    this as a node; the driver loops it below). Posts the verdict as a
    PR comment and returns it schema-validated."""
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
        f"{_marker('review', sha, verdict.verdict)}"))
    return verdict


async def verify_once(ctx: RunContext, item: dict,
                      pr: int) -> verify_step.VerifyResult:
    """One verify pass (single-shot: reused by the Workflow expression).
    Audits any escalation; writes verified labels into the PR title
    when the flag policy is satisfied."""
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


async def escalate_item(ctx: RunContext, item: dict, pr: int, actor: str,
                        rule: str) -> None:
    """Hand one item to a human: audit the rule + set store status.
    Shared by the ADK workflow nodes so every escalation path records
    the same evidence the sequential driver used to."""
    await ctx.audit(actor, "escalate_to_human", {"pr": pr, "rule": rule})
    await ctx.set_status(item["id"], "escalated", pr)
    print(f"[{item['id']}] escalated to human: {rule}", flush=True)


def review_already_approved(ctx: RunContext, pr: int) -> bool:
    """Resume idempotency: this PR's current head already carries a
    review approval (SHA-keyed marker), so the reviewer node skips a
    duplicate review on a re-run (G5)."""
    sha = ctx.repo_host.get_pr(pr)["head_sha"]
    comments = ctx.repo_host.get_review_threads(pr)
    return _find_marker(comments, _marker("review", sha, "approve")) is not None


async def run_preprod_ci(ctx: RunContext, item: dict, pr: int,
                         verified) -> bool:
    # Resume idempotency: this head commit may already be deployed+smoked.
    sha = ctx.repo_host.get_pr(pr)["head_sha"]
    comments = ctx.repo_host.get_review_threads(pr)
    if _find_marker(comments, _marker("ci", sha, "passed")) is not None:
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
        + _marker("ci", sha, "passed" if ci.passed else "failed")))
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


async def run_approver(ctx: RunContext, item: dict, pr: int,
                       verified) -> int:
    # Resume idempotency: if this head commit already has its dossier,
    # reuse it — and the gate baseline starts right after it, so a
    # decision the human made before the restart is still honored.
    sha = ctx.repo_host.get_pr(pr)["head_sha"]
    comments = ctx.repo_host.get_review_threads(pr)
    existing = _find_marker(comments, _marker("dossier", sha))
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
        + "\n\n" + _marker("dossier", sha)))
    await ctx.audit("approver", "post_dossier", {"pr": pr})
    # The gate baseline is captured HERE, at dossier-post time: a human
    # who decides on GitHub before the gate first looks must be seen.
    return len(ctx.repo_host.get_review_threads(pr))


# --- release phase -----------------------------------------------------------

async def release_queue(ctx: RunContext) -> list[dict]:
    """PRs awaiting release: the backlog items the STORE marks `queued`
    (a project's store is that one project's world, so this is inherently
    per-project). The release loop reads THIS, never an in-memory list —
    so it is resumable and independent of the sprint process that queued
    them. Workstream B: release is a peer loop over store state."""
    items = await ctx.store.call("list_backlog")
    return [i for i in items
            if i.get("status") == "queued" and i.get("pr")]


async def run_release_pass(ctx: RunContext) -> None:
    """One release pass — run as its own ADK Workflow (Workstream B).
    Release has a different clock and ecosystem than the sprint, so it is
    its own first-class ADK graph behind the ReleaseExecutor port
    (adapters/adk/release_workflow.py); the lock serializes trickle calls
    within one process."""
    async with ctx.release_lock:
        await ctx.release_executor.run_pass(ctx)


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
        await ctx.audit("release_guard", "escalate_to_human", {
            "pr": item["pr"], "item": item["id"],
            "rule": "repo host error while releasing this PR — the store "
                    "and the repo may disagree (reset-item to replay, or "
                    "reseed if the repo was recreated)",
            "error": str(exc)[:200]})
        await ctx.set_status(item["id"], "escalated", item["pr"])
        print(f"[release] BLOCKED PR #{item['pr']}: repo host error — "
              f"escalated ({str(exc)[:80]})", flush=True)
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
        await ctx.audit("release_guard", "hold_merge", {
            "pr": pr, "head_sha": head,
            "rule": f"post-approval head does not parse: {broken}"})
        await ctx.set_status(item["id"], "escalated", pr)
        print(f"[release] BLOCKED PR #{pr}: head {head[:7]} does not parse "
              "— escalated", flush=True)
        return "escalated"
    if verified.needs_flag:
        await ctx.audit("release_guard", "hold_merge", {
            "pr": pr, "head_sha": head,
            "rule": "post-approval head violates the flag policy"})
        await ctx.set_status(item["id"], "escalated", pr)
        print(f"[release] BLOCKED PR #{pr}: head violates flag policy "
              "— escalated", flush=True)
        return "escalated"
    comments = ctx.repo_host.get_review_threads(pr)
    if _find_marker(comments, _marker("ci", head, "passed")) is None:
        print(f"[release] PR #{pr}: head {head[:7]} has no passing preprod "
              "— deploying it now", flush=True)
        async with ctx.ci_lock:
            ci_ok = await run_preprod_ci(ctx, item, pr, verified)
        ctx.board.finish(item["id"], "head re-verified + preprod deployed")
        if not ci_ok:
            await ctx.audit("release_guard", "hold_merge", {
                "pr": pr, "head_sha": head,
                "rule": "preprod failed for the current head"})
            await ctx.set_status(item["id"], "failed", pr)
            print(f"[release] BLOCKED PR #{pr}: preprod failed for head "
                  f"{head[:7]}", flush=True)
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
            await ctx.audit("release_guard", "hold_merge", {
                "pr": pr,
                "rule": "merge failed — branch likely conflicts with "
                        "advanced main; rebase or reset-item",
                "error": str(exc)[:200]})
            print(f"[release] BLOCKED PR #{pr}: not mergeable "
                  f"({str(exc)[:80]})", flush=True)
            return "held"
        try:
            deploy.promote(f"pr-{pr}")
        except deploy.DeployError as exc:
            # The MERGE already landed; only the traffic shift failed.
            # That is a half-released state no rerun can safely finish
            # (the branch is merged; re-verifying it is meaningless) —
            # a human completes the promote. Escalate with the facts,
            # never crash the pass.
            await ctx.audit("release_guard", "escalate_to_human", {
                "pr": pr, "item": item["id"],
                "rule": "PR merged but the traffic shift failed — promote "
                        f"tag pr-{pr} manually (adapters.deploy promote) "
                        "and set the item released",
                "error": str(exc)[:300]})
            await ctx.set_status(item["id"], "escalated", pr)
            print(f"[release] MERGED PR #{pr} but promote FAILED — "
                  "escalated for a manual traffic shift", flush=True)
            return "escalated"
        await ctx.store.call("record_deploy", pr=pr,
                             revision=f"pr-{pr}", traffic="100",
                             area=verified.primary_area)
        await ctx.audit("release_manager", "merge_pr", factors)
        await ctx.set_status(item["id"], "released", pr)
        rule = decision.factors.get("dominating_rule", "")
        print(f"[release] MERGED PR #{pr} (traffic -> pr-{pr})"
              + (f" — rule: {rule}" if rule else "")
              + f" — {decision.reasoning[:140]}", flush=True)
        return "merged"
    # Held: the item STAYS queued in the store and is reconsidered on the
    # next release EVENT (incident cleared, confidence window passed).
    await ctx.audit("release_manager", "hold_merge", factors)
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
# `python -m orchestrator.release` (make release) is the same single pass
# run manually; see the runbook for the trigger wiring.


# --- the run -----------------------------------------------------------------

async def process_item(ctx: RunContext, item: dict) -> None:
    """One item through the per-item phase — with a governed boundary:
    an agent blowing its step budget (runaway guard) is that ITEM's
    failure, escalated like any other; it never kills the sprint."""
    try:
        return await _process_item(ctx, item)
    except RuntimeError as exc:
        if "runaway guard" not in str(exc):
            raise
        await ctx.audit("orchestrator", "escalate_to_human", {
            "item": item["id"],
            "rule": "agent exceeded its step budget mid-item; a human "
                    "reviews the PR state (reset-item to replay)",
            "error": str(exc)[:120]})
        await ctx.set_status(item["id"], "escalated")
        ctx.board.finish(item["id"], "escalated (runaway agent)")
        print(f"[{item['id']}] ESCALATED: {exc}", flush=True)
        return None


async def _process_item(ctx: RunContext, item: dict) -> None:
    """One item's full journey (self-contained: parallel workers run
    this concurrently, each with its own workspace)."""
    branch = _branch(item)
    print(f"\n=== {item['id']}: {item['title']} ===", flush=True)

    # THE STORE decides where this item is in its life — never GitHub
    # (the PR is the artifact; the store is the truth).
    status = item.get("status") or "pending"
    pr = item.get("pr")

    if status == "released":
        print(f"[resume] {item['id']}: already released — nothing to do",
              flush=True)
        return None
    if status == "rejected":
        print(f"[resume] {item['id']}: rejected — nothing to do", flush=True)
        return None
    if status in ("escalated", "failed"):
        override = await _escalation_override(ctx, item, pr) if pr else None
        if override is None or override.kind == "hold":
            print(f"[resume] {item['id']}: status={status} — waiting on a "
                  "human (/approve or /reject on the PR to resolve); "
                  "skipping this run", flush=True)
            return None
        if override.kind == "reject":
            await reject(ctx.store, ctx.repo_host,
                         Rejection(pr, "human_declined", "backlog",
                                   override.reason or "declined after "
                                   "escalation"),
                         actor="approval_gate")
            await ctx.set_status(item["id"], "rejected")
            return None
        # /approve: the human overrules the escalation. Judgment is
        # theirs; the MACHINE checks are not — the release gate will
        # re-verify and re-deploy this head before any merge.
        await ctx.audit("approval_gate", "human_override_escalation", {
            "pr": pr, "item": item["id"], "author": override.author,
            "was_status": status})
        await ctx.set_status(item["id"], "queued", pr)
        status = "queued"
        print(f"[resume] {item['id']}: escalation overridden by "
              f"{override.author}'s /approve — queued for release "
              "(machine gates re-run)", flush=True)

    if pr is None:
        if item["implementation"] == "human":
            if not sys.stdin.isatty():
                # Headless (Cloud Run Job): nobody can type a PR number.
                # Escalate and move on; a later run resumes the item.
                await ctx.audit("orchestrator", "escalate_to_human", {
                    "item": item["id"],
                    "rule": "human-implemented item needs an operator "
                            "terminal — resume interactively"})
                await ctx.set_status(item["id"], "escalated")
                ctx.board.finish(item["id"], "escalated (headless run)")
                return None
            ctx.board.begin(item["id"], "await_human_pr", "team implements")
            raw = input(f"[human item] {item['id']} is human-implemented; "
                        "enter PR number when raised: ").strip()
            pr = int(raw)
            await ctx.audit("orchestrator", "human_pr",
                            {"item": item["id"], "pr": pr})
            ctx.workspace.checkout_detached(
                ctx.repo_host.get_pr(pr)["head_ref"])
        else:
            await run_coder(ctx, item, branch)
            pr = await open_pr(ctx, item, branch)
        await ctx.set_status(item["id"], "in_review", pr)
    else:
        print(f"[resume] {item['id']}: PR #{pr} at status={status}",
              flush=True)
        # Detached: the branch may be held by another checkout (the
        # base, after a crashed sequential run) — a branch can only be
        # checked out in ONE worktree, and nothing here needs the name
        # (commits work detached; push targets the branch by refspec).
        if item["implementation"] == "human":
            ctx.workspace.checkout_detached(
                ctx.repo_host.get_pr(pr)["head_ref"])
        else:
            ctx.workspace.checkout_detached(branch)

    # CONFLICTS ARE HUMAN WORK: the coder is never asked to reconcile
    # parallel changes from main (it once "fixed" flags.json into
    # duplicate keys trying). A conflicted PR escalates immediately.
    if ctx.repo_host.get_pr(pr).get("mergeable") is False:
        await ctx.audit("release_guard", "escalate_to_human", {
            "pr": pr, "item": item["id"],
            "rule": "merge conflict with main — human rebases (or "
                    "make reset-item to replay)"})
        await ctx.set_status(item["id"], "escalated")
        ctx.board.finish(item["id"], "merge conflict — human")
        print(f"[resume] {item['id']}: PR #{pr} conflicts with main — "
              "escalated to a human (agents never resolve conflicts)",
              flush=True)
        return None

    if status == "queued":
        # Human approval already given (previous run). The store-sourced
        # release pass re-verifies this head, re-checks the flag policy,
        # and decides — nothing to set up here beyond triggering it (the
        # gate is NOT asked twice for the same commit).
        ctx.board.finish(item["id"], "requeued for release")
        await run_release_pass(ctx)
        return None

    # The per-item pipeline runs on ADK's engine (ADR-0007, Workstream A):
    # the Workflow in adapters/adk/workflow.py IS the execution path — one
    # graph, no imperative shadow. Its nodes call the same single-shot
    # handlers (run_coder, review_once, verify_once, run_preprod_ci,
    # run_approver) and set store status at each transition; the executor
    # drives the gate's suspend/resume. `pr` is None for a fresh agent item
    # (the coder node opens it) and set on resume (the coder node skips
    # re-implementation). The STORE carries the results (status=queued),
    outcome = await ctx.executor.run_item(ctx, item, branch, existing_pr=pr)
    if outcome.kind == "queued":
        # Trickle release: an approval immediately gets a release decision —
        # the pass covers the WHOLE unmerged queue, so earlier holds are
        # reconsidered under the current situation too.
        await run_release_pass(ctx)
    return None


async def run_pipeline(ctx: RunContext, parallel: int = 1) -> None:
    # Stale-incident hygiene: if a previous run left an incident open
    # and the service has since recovered, close it now (the resolver
    # also runs before every release pass).
    await incident_resolver.run(ctx.project, DeliveryStore.for_resolver())

    # ONE store lifetime = ONE sprint: if a sprint exists, resume it
    # (assessments and packing already happened); `make seed` is the
    # explicit way to start a new sprint.
    sprint = await ctx.store.call("get_current_sprint")
    if sprint:
        print(f"[pack] resuming sprint #{sprint['id']}: "
              f"{sprint['item_ids']}", flush=True)
        backlog = {i["id"]: i for i in await ctx.store.call("list_backlog")}
        selected = [backlog[i] for i in sprint["item_ids"] if i in backlog]
        # A finished sprint stays finished (the invariant above) — but
        # say so, and name the items the packer left out, so a no-op
        # resume never reads as the orchestrator forgetting them.
        if selected and all(i.get("status") in ("released", "rejected")
                            for i in selected):
            leftover = [i["id"] for i in backlog.values()
                        if (i.get("status") or "pending") == "pending"]
            note = (f"; {len(leftover)} backlog items were never packed "
                    f"({', '.join(leftover)}) — run 'make seed' to start "
                    "a new sprint" if leftover else "")
            print(f"[pack] sprint #{sprint['id']} is complete: every item "
                  f"is released or rejected{note}", flush=True)
    else:
        assessments = await run_risk_assessor(ctx)
        selected = await run_sprint_packer(ctx, assessments)

    if parallel > 1:
        # Agent items fan out, each in its own git worktree (a checkout
        # is a cache of GitHub state — nothing needs to share one).
        # Human items stay sequential: they block on terminal input.
        agent_items = [i for i in selected if i["implementation"] == "agent"]
        human_items = [i for i in selected if i["implementation"] == "human"]
        ctx.workspace.detach()  # free any branch a crashed run held
        factory = WorkspaceFactory(ctx.workspace.dir)
        limit = asyncio.Semaphore(parallel)

        async def worker(item: dict) -> None:
            async with limit:
                item_ctx = replace(
                    ctx, workspace=factory.for_item(item["id"]))
                return await process_item(item_ctx, item)

        print(f"[pipeline] running {len(agent_items)} agent items with "
              f"up to {parallel} concurrent coders", flush=True)
        await asyncio.gather(*(worker(i) for i in agent_items))
        for item in human_items:
            await process_item(ctx, item)
        factory.cleanup()
    else:
        for item in selected:
            await process_item(ctx, item)

    # Trickle passes already ran per approval; one final pass gives any
    # remaining holds a look now that the sprint is complete. It is NOT a
    # waiting loop — a PR still held here stays queued in the store, and a
    # later release EVENT (Scheduler tick / webhook → run_release_pass, or
    # `make release`) reconsiders it. Release does not depend on this
    # process staying alive (Workstream B).
    await run_release_pass(ctx)

    # The engine cleans up after itself: the scratch checkout (and its
    # worktrees) are deleted on a CLEAN finish; a crashed run keeps
    # them so resume is instant. GitHub holds the truth either way.
    from orchestrator import provisioning
    provisioning.deprovision(ctx.project.name)


# The explicit binding: definition step name -> handler.
# Every definition step name binds to its implementation. Planning and
# release steps are driver functions; the per-item steps are executed by
# the ADK Workflow (adapters/adk/workflow.py) and bind to the single-shot
# handlers its nodes call — the fix/flag/gate LOOPS are graph edges, not
# Python loops (ADR-0007). `test_definition` asserts this map covers the
# definition; `check_decision` is the gate's atom.
HANDLERS = {
    "risk_assessor": run_risk_assessor,
    "sprint_packer": run_sprint_packer,
    "coder": run_coder,
    "code_reviewer": review_once,
    "verify": verify_once,
    "preprod_ci": run_preprod_ci,
    "approver": run_approver,
    "approval_gate": check_decision,
    "incident_resolver": incident_resolver.run,
    "release_manager": run_release_pass,
}


def build_context(project: ProjectConfig, invoker: AgentInvoker,
                  executor: PipelineExecutor | None = None,
                  release_executor: ReleaseExecutor | None = None
                  ) -> RunContext:
    """The invoker and executors arrive from a composition root
    (__main__ or release.py), the only files that choose a framework
    (ADR-0007). Each entry point injects only what it runs: release.py
    leaves the per-item executor None. The working checkout is
    PROVISIONED by the engine itself (cloned into scratch, healed if
    missing) — no pre-existing local copy is required."""
    from orchestrator import provisioning

    repo_host = GitHubRepoHost(project.repo, os.environ["GITHUB_TOKEN"])
    workspace = provisioning.provision(
        project.name, repo_host.authenticated_remote())
    return RunContext(
        project=project,
        store=DeliveryStore.for_agents(),
        repo_host=repo_host,
        invoker=invoker,
        workspace=workspace,
        executor=executor,
        release_executor=release_executor,
    )
