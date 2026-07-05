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
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.config import ProjectConfig
from orchestrator.dependency_graph import blast_radius, build_import_graph
from tools.diff_analysis import files_touched
from orchestrator.gate import await_decision
from orchestrator.invoker import AgentInvoker, Invocation
from orchestrator.json_util import extract_json
from orchestrator import schemas
from orchestrator.rejection import Rejection, reject
from adapters.repo_host import GitHubRepoHost
from adapters.store_client import DeliveryStore
from orchestrator.workspace import Workspace
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

    for item in items:
        print(f"[assess] {item['id']}: {item['title']}", flush=True)
        payload = {
            "task": ("Assess this backlog item and record your judgment via "
                     "record_assessment."),
            "item": item,
            "repo_import_graph": graph_lines,
        }
        await ctx.invoke(assessor_spec.build(ctx.project),
                         json.dumps(payload, indent=2))

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
        ctx.workspace.start_branch(branch)
        task = ("Implement this backlog item in the workspace. Follow your "
                "core rules and the project conventions.")
    else:
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
    body = (f"Item: {item['id']}\n\n"
            f"claimed_risk: {item['claimed_risk']} | "
            f"claimed_impact: {item['claimed_impact']} | "
            f"area_hint: {item['area_hint']}\n\n{item['description']}")
    pr = ctx.repo_host.open_pr(branch, item["title"], body)
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
    diff = ctx.repo_host.get_diff(pr)
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
    ctx.repo_host.post_comment(pr, (
        f"**Review ({verdict.verdict})** — iteration {iteration + 1}\n\n"
        f"{verdict.reasoning}\n\n{findings}"))
    return verdict


async def run_code_reviewer(ctx: RunContext, item: dict, pr: int,
                            branch: str) -> bool:
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
            return False

        if iteration >= max_iterations:
            break
        print(f"[review] PR #{pr} changes requested "
              f"(iteration {iteration + 1}); coder fixing", flush=True)
        await run_coder(ctx, item, branch,
                        feedback=verdict.model_dump_json(indent=2))

    await ctx.audit("code_reviewer", "escalate_to_human", {
        "pr": pr, "rule": f"no approval after {max_iterations} fix iterations"})
    print(f"[review] PR #{pr} escalated to human after "
          f"{max_iterations} iterations", flush=True)
    return False


async def verify_once(ctx: RunContext, item: dict,
                      pr: int) -> verify_step.VerifyResult:
    """One verify pass (single-shot: reused by the Workflow expression).
    Audits any escalation; writes verified labels into the PR title
    when the flag policy is satisfied."""
    diff = ctx.repo_host.get_diff(pr)
    result = verify_step.verify(diff, item["claimed_risk"], ctx.project,
                                str(ctx.workspace.dir))
    if result.escalated:
        await ctx.audit("verify", "escalate_risk_label", {
            "pr": pr, "claimed_risk": result.claimed_risk,
            "verified_risk": result.verified_risk,
            "reason": result.escalation_reason})
        print(f"[verify] PR #{pr} risk escalated "
              f"{result.claimed_risk} -> {result.verified_risk}", flush=True)

    if not result.needs_flag:
        title = ctx.repo_host.get_pr(pr)["title"]
        bare = re.sub(r"^(\[[^\]]+\])+\s*", "", title)
        ctx.repo_host.update_title(pr, f"{result.title_prefix} {bare}")
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
    ci = preprod_ci.run_preprod(pr, str(ctx.workspace.dir), verified.areas,
                                ctx.project)
    ctx.repo_host.post_comment(pr, preprod_ci.format_comment(ci))
    if ci.preprod_url:
        await ctx.store.call("record_deploy", pr=pr,
                             revision=ci.revision_tag, traffic="preprod")
    await ctx.audit("preprod_ci", "preprod_result", {
        "pr": pr, "passed": ci.passed, "revision": ci.revision_tag,
        "preprod_url": ci.preprod_url, "smoke": ci.smoke})
    print(f"[ci] PR #{pr} preprod "
          f"{'passed' if ci.passed else 'FAILED'}", flush=True)
    return ci.passed


async def run_approver(ctx: RunContext, item: dict, pr: int,
                       verified) -> None:
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
    ctx.repo_host.post_comment(pr, schemas.render_dossier(dossier, approvers))
    await ctx.audit("approver", "post_dossier", {"pr": pr})


async def run_approval_gate(ctx: RunContext, item: dict, pr: int) -> bool:
    approvers = ctx.project.policy("approver")["approvers"]
    print(f"[gate] awaiting /approve, /reject <reason>, or /hold on PR #{pr} "
          f"from {approvers}", flush=True)
    while True:
        decision = await await_decision(ctx.repo_host, ctx.store, pr,
                                        approvers)
        if decision.kind == "approve":
            return True
        if decision.kind == "reject":
            await reject(ctx.store, ctx.repo_host,
                         Rejection(pr, "human_declined", "backlog",
                                   decision.reason or "no reason given"),
                         actor="approval_gate")
            return False
        print(f"[gate] PR #{pr} on hold by {decision.author}; "
              "waiting for a final decision", flush=True)


# --- release phase -----------------------------------------------------------

async def run_release_pass(ctx: RunContext) -> None:
    await incident_resolver.run(ctx.project, DeliveryStore.for_resolver())
    queue = [a for a in ctx.approved if not a.merged]
    if not queue:
        print("[release] queue empty", flush=True)
        return

    payload = {
        "task": ("Decide merge/hold and ordering for the approved queue. "
                 "Consult the store for incidents, deploys and health. Reply "
                 'ONLY with JSON: {"decisions": [{"pr": 1, '
                 '"action": "merge|hold", "reasoning": "...", '
                 '"factors": {}}]} in your chosen order.'),
        "queue": [{
            "pr": a.pr, "item": a.item["id"], "area": a.verified.primary_area,
            "verified_risk": a.verified.verified_risk,
            "feature_flagged": a.verified.flag["covered"],
            "dependency_closure": sorted(a.verified.radius),
        } for a in queue],
        "deploy_confidence_minutes":
            ctx.project.policy("release_manager")["deploy_confidence_minutes"],
    }
    result = await ctx.invoke(rm_spec.build(ctx.project),
                              json.dumps(payload, indent=2))
    plan = schemas.ReleasePlan.model_validate(extract_json(result.text))

    by_pr = {a.pr: a for a in queue}
    for decision in plan.decisions:
        entry = by_pr.get(decision.pr)
        if entry is None:
            continue
        factors = {"pr": entry.pr, "area": entry.verified.primary_area,
                   "verified_risk": entry.verified.verified_risk,
                   "feature_flagged": entry.verified.flag["covered"],
                   **decision.factors,
                   "reasoning": decision.reasoning}
        if decision.action == "merge":
            ctx.repo_host.merge_pr(entry.pr)
            deploy.promote(f"pr-{entry.pr}")
            await ctx.store.call("record_deploy", pr=entry.pr,
                                 revision=f"pr-{entry.pr}", traffic="100")
            await ctx.audit("release_manager", "merge_pr", factors)
            entry.merged = True
            print(f"[release] MERGED PR #{entry.pr} "
                  f"(traffic -> pr-{entry.pr})", flush=True)
        else:
            await ctx.audit("release_manager", "hold_merge", factors)
            print(f"[release] HELD PR #{entry.pr}: "
                  f"{decision.reasoning}", flush=True)


# --- the run -----------------------------------------------------------------

async def run_pipeline(ctx: RunContext) -> None:
    assessments = await run_risk_assessor(ctx)
    selected = await run_sprint_packer(ctx, assessments)

    for item in selected:
        branch = _branch(item)
        print(f"\n=== {item['id']}: {item['title']} ===", flush=True)

        if item["implementation"] == "human":
            raw = input(f"[human item] {item['id']} is human-implemented; "
                        "enter PR number when raised: ").strip()
            pr = int(raw)
            ctx.workspace.checkout(ctx.repo_host.get_pr(pr)["head_ref"])
        else:
            await run_coder(ctx, item, branch)
            pr = await open_pr(ctx, item, branch)

        if not await run_code_reviewer(ctx, item, pr, branch):
            continue
        verified = await run_verify(ctx, item, pr, branch)
        if verified is None:
            continue
        if not await run_preprod_ci(ctx, item, pr, verified):
            continue
        await run_approver(ctx, item, pr, verified)
        if not await run_approval_gate(ctx, item, pr):
            continue
        ctx.approved.append(ApprovedPR(pr=pr, item=item, verified=verified))

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
