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
import sys
from dataclasses import replace

from adapters import deploy
from adapters.repo_host import RepoHostError
from adapters.store_client import DeliveryStore
from orchestrator import governance, schemas
from orchestrator.context import RunContext, build_context  # noqa: F401 (re-exported for entry points)
from orchestrator.dependency_graph import UnparseableSource, build_import_graph
from orchestrator.gate import Decision, check_decision, parse_command
from orchestrator.json_util import extract_json
from orchestrator.rejection import Rejection
from orchestrator.steps import (  # noqa: F401 — re-exported: HANDLERS + ADK nodes + tests
    branch_for, open_pr, preprod_passed_for_head, review_already_approved,
    review_once, run_approver, run_coder, run_preprod_ci, verify_once)
from orchestrator.workspace import WorkspaceFactory
from sdlc_steps import incident_resolver, sprint_packer
from sdlc_steps.release_manager import spec as rm_spec
from sdlc_steps.risk_assessor import spec as assessor_spec


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
    from orchestrator.heartbeat import post_event
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
            ctx, item, item["pr"], "release_guard",
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
            ctx, item, pr, "release_guard",
            f"post-approval head does not parse: {broken}", head_sha=head)
        return "escalated"
    if verified.needs_flag:
        await governance.escalate(
            ctx, item, pr, "release_guard",
            "post-approval head violates the flag policy", head_sha=head)
        return "escalated"
    if not preprod_passed_for_head(ctx, pr, head):
        print(f"[release] PR #{pr}: head {head[:7]} has no passing preprod "
              "— deploying it now", flush=True)
        async with ctx.ci_lock:
            ci_ok = await run_preprod_ci(ctx, item, pr, verified)
        ctx.board.finish(item["id"], "head re-verified + preprod deployed")
        if not ci_ok:
            await governance.fail(ctx, item, pr, "release_guard",
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
                ctx, item, pr, "release_guard",
                "merge failed — branch likely conflicts with advanced "
                "main; rebase or reset-item", error=str(exc)[:200])
            return "held"
        try:
            deploy.promote(f"pr-{pr}")
        except deploy.DeployError as exc:
            # The MERGE already landed; only the traffic shift failed.
            # That is a half-released state no rerun can safely finish
            # (the branch is merged; re-verifying it is meaningless) —
            # a human completes the promote. Escalate with the facts,
            # never crash the pass.
            await governance.escalate(
                ctx, item, pr, "release_guard",
                "PR merged but the traffic shift failed — promote tag "
                f"pr-{pr} manually (adapters.deploy promote) and set the "
                "item released", error=str(exc)[:300])
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
        await governance.escalate(
            ctx, item, None, "orchestrator",
            "agent exceeded its step budget mid-item; a human reviews the "
            "PR state (reset-item to replay)",
            error=str(exc)[:120], note="escalated (runaway agent)")
        return None


async def _process_item(ctx: RunContext, item: dict) -> None:
    """One item's full journey (self-contained: parallel workers run
    this concurrently, each with its own workspace)."""
    branch = branch_for(item)
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
            await governance.bounce(
                ctx, item,
                Rejection(pr, "human_declined", "backlog",
                          override.reason or "declined after escalation"),
                actor="approval_gate")
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
                await governance.escalate(
                    ctx, item, None, "orchestrator",
                    "human-implemented item needs an operator terminal — "
                    "resume interactively", note="escalated (headless run)")
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
        await governance.escalate(
            ctx, item, pr, "release_guard",
            "merge conflict with main — human rebases (or make reset-item "
            "to replay); agents never resolve conflicts",
            note="merge conflict — human")
        return None

    if status == "queued":
        # Human approval already given (previous run). The store-sourced
        # release pass re-verifies this head, re-checks the flag policy,
        # and decides — nothing to set up here beyond triggering it (the
        # gate is NOT asked twice for the same commit).
        ctx.board.finish(item["id"], "requeued for release")
        await trigger_release(ctx)
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
        await trigger_release(ctx)
    return None


async def run_pipeline(ctx: RunContext, parallel: int = 1,
                       deprovision: bool = True) -> None:
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
    await trigger_release(ctx)

    # The engine cleans up after itself: the scratch checkout (and its
    # worktrees) are deleted on a CLEAN finish; a crashed run keeps
    # them so resume is instant. GitHub holds the truth either way.
    # The resident sprint service passes deprovision=False: it runs many
    # passes per process, and re-cloning per event would waste the warm
    # checkout (provision() heals it if anything is ever broken).
    if deprovision:
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
