"""The driver: executes the SDLC definition, sequentially and
inspectably (ADR-0003).

definition.py says WHAT the process is; this file says how each named
step is carried out, via the HANDLERS registry at the bottom — the
explicit binding between the definition's step names and the
sdlc_steps/ implementations. Engine mechanisms (invoker, repo host,
store client, gate, rejection) do the lifting; nothing here encodes
step knowledge.

Dispatch semantics: agents are invocations, not daemons. The driver
invoking a step IS the event; the PR is the artifact between coder and
reviewer; the store is the artifact between everything else.
"""

import asyncio
import json
import os
import re
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path

from orchestrator.config import ProjectConfig
from orchestrator.dependency_graph import blast_radius, build_import_graph
from tools.diff_analysis import files_touched
from orchestrator.activity import ActivityBoard
from orchestrator.gate import await_decision, check_decision
from orchestrator.invoker import AgentInvoker, Invocation
from orchestrator.json_util import extract_json
from orchestrator import schemas
from orchestrator.rejection import Rejection, reject
from adapters.repo_host import GitHubRepoHost
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
class ApprovedPR:
    pr: int
    item: dict
    verified: verify_step.VerifyResult
    merged: bool = False


@dataclass
class RunContext:
    project: ProjectConfig
    store: DeliveryStore
    repo_host: GitHubRepoHost
    invoker: AgentInvoker
    workspace: Workspace
    approved: list[ApprovedPR] = field(default_factory=list)
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
    await ctx.invoke(coder_spec.build(ctx.project, str(ctx.workspace.dir)),
                     json.dumps(payload, indent=2))

    if not ctx.workspace.has_changes():
        if feedback is None:
            raise RuntimeError(f"coder produced no changes for {item['id']}")
        return  # a fix round may legitimately end with reasoning only
    ctx.workspace.commit_all(f"{item['id']}: {item['title']}")
    ctx.workspace.push(branch, ctx.repo_host.authenticated_remote())


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
        f"**Review ({verdict.verdict})** — iteration {iteration + 1}\n\n"
        f"{verdict.reasoning}\n\n{findings}\n\n"
        f"{_marker('review', sha, verdict.verdict)}"))
    return verdict


async def run_code_reviewer(ctx: RunContext, item: dict, pr: int,
                            branch: str) -> bool:
    # Resume idempotency: this head commit may already carry an approval.
    sha = ctx.repo_host.get_pr(pr)["head_sha"]
    comments = ctx.repo_host.get_review_threads(pr)
    if _find_marker(comments, _marker("review", sha, "approve")) is not None:
        print(f"[resume] PR #{pr}: review already approved {sha[:7]} — "
              "skipping", flush=True)
        return True

    max_iterations = int(
        ctx.project.policy("orchestrator")["max_fix_iterations"])

    for iteration in range(max_iterations + 1):
        verdict = await review_once(ctx, item, pr, iteration)

        if verdict.verdict == "approve":
            await ctx.audit("code_reviewer", "approve_review",
                            {"pr": pr, "iterations": iteration + 1})
            print(f"[review] PR #{pr} approved "
                  f"(iteration {iteration + 1})", flush=True)
            return True

        if verdict.verdict == "out_of_scope":
            await reject(ctx.store, ctx.repo_host,
                         Rejection(pr, "out_of_scope", "author",
                                   verdict.reasoning),
                         actor="code_reviewer")
            await ctx.set_status(item["id"], "rejected")
            return False

        if iteration >= max_iterations:
            break
        print(f"[review] PR #{pr} changes requested "
              f"(iteration {iteration + 1}); coder fixing", flush=True)
        await run_coder(ctx, item, branch,
                        feedback=verdict.model_dump_json(indent=2))

    await ctx.audit("code_reviewer", "escalate_to_human", {
        "pr": pr, "rule": f"no approval after {max_iterations} fix iterations"})
    await ctx.set_status(item["id"], "escalated")
    print(f"[review] PR #{pr} escalated to human after "
          f"{max_iterations} iterations", flush=True)
    return False


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


async def run_verify(ctx: RunContext, item: dict, pr: int,
                     branch: str) -> verify_step.VerifyResult | None:
    max_flag_fixes = int(
        ctx.project.policy("orchestrator")["max_flag_fix_iterations"])

    for attempt in range(max_flag_fixes + 1):
        result = await verify_once(ctx, item, pr)
        if not result.needs_flag:
            return result

        if attempt >= max_flag_fixes:
            break
        await reject(ctx.store, ctx.repo_host,
                     Rejection(pr, "policy_flag_required", "coder",
                               f"verified risk {result.verified_risk} requires "
                               f"a feature flag; none gates the new behavior"),
                     actor="verify")
        await run_coder(ctx, item, branch, feedback=(
            "Policy violation: this change's verified risk is "
            f"{result.verified_risk}, which requires the NEW behavior to be "
            "gated behind a feature flag (default off) in flags.json. Wrap "
            "it and keep tests covering both flag states."))

    await ctx.audit("verify", "escalate_to_human", {
        "pr": pr, "rule": f"flag still missing after {max_flag_fixes} fix"})
    return None


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
    ci = preprod_ci.run_preprod(pr, str(ctx.workspace.dir), verified.areas,
                                ctx.project)
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


async def run_approval_gate(ctx: RunContext, item: dict, pr: int,
                            baseline: int) -> bool:
    policy = ctx.project.policy("approver")
    approvers = policy["approvers"]
    mode = policy.get("gate_mode", "poll")
    ctx.board.begin(item["id"], "approval_gate",
                    f"PR #{pr} awaiting {approvers}")
    print(f"[gate] awaiting /approve, /reject <reason>, or /hold on PR #{pr} "
          f"from {approvers} (gate_mode: {mode})", flush=True)
    audited_ignores: set = set()

    while True:
        if mode == "nudge":
            # Human-nudged single check: the decision's AUTHORITY is the
            # GitHub comment; pressing Enter (or resuming the ADK
            # suspend) only triggers one look at it.
            await asyncio.to_thread(
                input, f"[gate] decide on PR #{pr} via a GitHub comment, "
                       "then press Enter to check: ")
            decision = await check_decision(ctx.repo_host, ctx.store, pr,
                                            approvers, baseline,
                                            audited_ignores)
            if decision is None:
                print("[gate] no decision from an allowlisted approver yet",
                      flush=True)
                continue
        else:
            decision = await await_decision(ctx.repo_host, ctx.store, pr,
                                            approvers, baseline=baseline)

        if decision.kind == "approve":
            return True
        if decision.kind == "reject":
            await reject(ctx.store, ctx.repo_host,
                         Rejection(pr, "human_declined", "backlog",
                                   decision.reason or "no reason given"),
                         actor="approval_gate")
            return False
        # A hold advances the baseline past itself so the NEXT command
        # (e.g. a later /approve) becomes visible.
        baseline = decision.comment_index + 1
        print(f"[gate] PR #{pr} on hold by {decision.author}; "
              "waiting for a final decision", flush=True)


# --- release phase -----------------------------------------------------------

async def run_release_pass(ctx: RunContext) -> None:
    async with ctx.release_lock:
        await _release_pass_locked(ctx)


async def _release_pass_locked(ctx: RunContext) -> None:
    ctx.board.begin("RELEASE", "incident_resolver", "checking recovery")
    await incident_resolver.run(ctx.project, DeliveryStore.for_resolver())
    queue = [a for a in ctx.approved if not a.merged]
    if not queue:
        ctx.board.finish("RELEASE", "queue empty")
        print("[release] queue empty", flush=True)
        return
    # One PR, one decision, one deployment at a time — strictly in a
    # row. Each merge records its deploy BEFORE the next decision, so
    # the release manager sees it as a fresh same-area deploy and can
    # postpone stacking per its judgment rules (confidence window).
    confidence = ctx.project.policy(
        "release_manager")["deploy_confidence_minutes"]
    for entry in queue:
        ctx.board.begin("RELEASE", "release_manager",
                        f"deciding PR #{entry.pr}")
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
                     'Reply ONLY with JSON: {"pr": ' + str(entry.pr) +
                     ', "action": "merge|hold", "reasoning": "...", '
                     '"factors": {}}'),
            "pr": {
                "pr": entry.pr, "item": entry.item["id"],
                "area": entry.verified.primary_area,
                "verified_risk": entry.verified.verified_risk,
                "feature_flagged": entry.verified.flag["covered"],
                "dependency_closure": sorted(entry.verified.radius),
            },
            "deploy_confidence_minutes": confidence,
        }
        result = await ctx.invoke(rm_spec.build(ctx.project),
                                  json.dumps(payload, indent=2))
        decision = schemas.ReleaseDecision.model_validate(
            extract_json(result.text))

        factors = {"pr": entry.pr, "area": entry.verified.primary_area,
                   "verified_risk": entry.verified.verified_risk,
                   "feature_flagged": entry.verified.flag["covered"],
                   **decision.factors,
                   "reasoning": decision.reasoning}
        if decision.action == "merge":
            ctx.repo_host.merge_pr(entry.pr)
            deploy.promote(f"pr-{entry.pr}")
            await ctx.store.call("record_deploy", pr=entry.pr,
                                 revision=f"pr-{entry.pr}", traffic="100",
                                 area=entry.verified.primary_area)
            await ctx.audit("release_manager", "merge_pr", factors)
            await ctx.set_status(entry.item["id"], "released")
            entry.merged = True
            print(f"[release] MERGED PR #{entry.pr} "
                  f"(traffic -> pr-{entry.pr})", flush=True)
        else:
            await ctx.audit("release_manager", "hold_merge", factors)
            print(f"[release] HELD PR #{entry.pr}: "
                  f"{decision.reasoning}", flush=True)

    ctx.board.finish("RELEASE", "pass complete")


# --- the run -----------------------------------------------------------------

async def process_item(ctx: RunContext, item: dict) -> ApprovedPR | None:
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
    if status in ("rejected", "escalated", "failed"):
        print(f"[resume] {item['id']}: status={status} — waiting on a "
              "human; skipping this run", flush=True)
        return None

    if pr is None:
        if item["implementation"] == "human":
            ctx.board.begin(item["id"], "await_human_pr", "team implements")
            raw = input(f"[human item] {item['id']} is human-implemented; "
                        "enter PR number when raised: ").strip()
            pr = int(raw)
            await ctx.audit("orchestrator", "human_pr",
                            {"item": item["id"], "pr": pr})
            ctx.workspace.checkout(ctx.repo_host.get_pr(pr)["head_ref"])
        else:
            await run_coder(ctx, item, branch)
            pr = await open_pr(ctx, item, branch)
        await ctx.set_status(item["id"], "in_review", pr)
    else:
        print(f"[resume] {item['id']}: PR #{pr} at status={status}",
              flush=True)
        if item["implementation"] == "human":
            ctx.workspace.checkout(ctx.repo_host.get_pr(pr)["head_ref"])
        else:
            ctx.workspace.checkout(branch)

    if status == "queued":
        # Human approval already given (previous run): recompute the
        # verified labels (cheap, deterministic) and requeue directly —
        # the gate is NOT asked twice for the same commit.
        verified = await verify_once(ctx, item, pr)
        ctx.board.finish(item["id"], "requeued for release")
        approved = ApprovedPR(pr=pr, item=item, verified=verified)
        ctx.approved.append(approved)
        await run_release_pass(ctx)
        return approved

    if not await run_code_reviewer(ctx, item, pr, branch):
        ctx.board.finish(item["id"], "stopped at review")
        return None
    verified = await run_verify(ctx, item, pr, branch)
    if verified is None:
        await ctx.set_status(item["id"], "escalated")
        ctx.board.finish(item["id"], "stopped at verify (flag)")
        return None
    await ctx.set_status(item["id"], "verified")
    async with ctx.ci_lock:
        ci_ok = await run_preprod_ci(ctx, item, pr, verified)
    if not ci_ok:
        await ctx.set_status(item["id"], "failed")
        ctx.board.finish(item["id"], "failed preprod")
        return None
    await ctx.set_status(item["id"], "preprod_passed")
    baseline = await run_approver(ctx, item, pr, verified)
    await ctx.set_status(item["id"], "awaiting_approval")
    if not await run_approval_gate(ctx, item, pr, baseline):
        await ctx.set_status(item["id"], "rejected")
        ctx.board.finish(item["id"], "rejected at gate")
        return None
    await ctx.set_status(item["id"], "queued")
    ctx.board.finish(item["id"], "queued for release")
    approved = ApprovedPR(pr=pr, item=item, verified=verified)
    ctx.approved.append(approved)
    # Trickle release: an approval immediately gets a release decision —
    # the pass covers the WHOLE unmerged queue, so earlier holds are
    # reconsidered under the current situation too.
    await run_release_pass(ctx)
    return approved


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
    else:
        assessments = await run_risk_assessor(ctx)
        selected = await run_sprint_packer(ctx, assessments)

    if parallel > 1:
        # Agent items fan out, each in its own git worktree (a checkout
        # is a cache of GitHub state — nothing needs to share one).
        # Human items stay sequential: they block on terminal input.
        agent_items = [i for i in selected if i["implementation"] == "agent"]
        human_items = [i for i in selected if i["implementation"] == "human"]
        factory = WorkspaceFactory(ctx.workspace.dir)
        limit = asyncio.Semaphore(parallel)

        async def worker(item: dict) -> ApprovedPR | None:
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

    # Trickle passes already ran per approval; this final pass gives any
    # remaining holds one more look now that the sprint is complete.
    await run_release_pass(ctx)
    while any(not a.merged for a in ctx.approved):
        answer = input("\n[release] held PRs remain; run another release "
                       "pass? [Y/n] ").strip().lower()
        if answer == "n":
            break
        await run_release_pass(ctx)


# The explicit binding: definition step name -> handler.
HANDLERS = {
    "risk_assessor": run_risk_assessor,
    "sprint_packer": run_sprint_packer,
    "coder": run_coder,
    "code_reviewer": run_code_reviewer,
    "verify": run_verify,
    "preprod_ci": run_preprod_ci,
    "approver": run_approver,
    "approval_gate": run_approval_gate,
    "incident_resolver": incident_resolver.run,
    "release_manager": run_release_pass,
}


def build_context(project: ProjectConfig,
                  invoker: AgentInvoker) -> RunContext:
    """The invoker arrives from the composition root (__main__), which
    is the only place that chooses a framework (ADR-0007)."""
    return RunContext(
        project=project,
        store=DeliveryStore.for_agents(),
        repo_host=GitHubRepoHost(project.repo, os.environ["GITHUB_TOKEN"]),
        invoker=invoker,
        workspace=Workspace(os.environ["CANDIDATE_APP_DIR"]),
    )
