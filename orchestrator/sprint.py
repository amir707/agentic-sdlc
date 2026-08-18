"""SPRINT flow: resume dispatch and the run loop.

The STORE decides where each item is in its life (never GitHub — the
PR is only the artifact): this module reads that status and dispatches
— skip what is finished, honor a human's /approve or /reject on an
escalated item, escalate a merge conflict, hand a fresh or in-flight
item to the per-item ADK pipeline (PipelineExecutor port), and hand a
queued one to release. run_pipeline is the whole sprint: planning once
per store lifetime, then every selected item, sequential or fanned out
over per-item worktrees.
"""

import asyncio
import sys
from dataclasses import replace

from mcp_server.vocab import Actor, ItemStatus
from mcp_server.vocab import Decision as AuditDecision
from orchestrator import governance
from orchestrator.context import RunContext
from orchestrator.gate import Decision, parse_command
from orchestrator.planning import run_risk_assessor, run_sprint_packer
from orchestrator.rejection import Rejection
from orchestrator.release_flow import trigger_release
from orchestrator.steps import branch_for, open_pr, run_coder
from orchestrator.workspace import WorkspaceFactory
from sdlc_steps import incident_resolver


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
            ctx, item, None, Actor.ORCHESTRATOR,
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
    status = ItemStatus(item.get("status") or ItemStatus.PENDING)
    pr = item.get("pr")

    if status == ItemStatus.RELEASED:
        print(f"[resume] {item['id']}: already released — nothing to do",
              flush=True)
        return None
    if status == ItemStatus.REJECTED:
        print(f"[resume] {item['id']}: rejected — nothing to do", flush=True)
        return None
    if status.is_parked:
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
                actor=Actor.APPROVAL_GATE)
            return None
        # /approve: the human overrules the escalation. Judgment is
        # theirs; the MACHINE checks are not — the release gate will
        # re-verify and re-deploy this head before any merge.
        await ctx.audit(Actor.APPROVAL_GATE, AuditDecision.HUMAN_OVERRIDE_ESCALATION, {
            "pr": pr, "item": item["id"], "author": override.author,
            "was_status": status})
        await ctx.set_status(item["id"], ItemStatus.QUEUED, pr)
        status = ItemStatus.QUEUED
        print(f"[resume] {item['id']}: escalation overridden by "
              f"{override.author}'s /approve — queued for release "
              "(machine gates re-run)", flush=True)

    if pr is None:
        if item["implementation"] == "human":
            if not sys.stdin.isatty():
                # Headless (Cloud Run Job): nobody can type a PR number.
                # Escalate and move on; a later run resumes the item.
                await governance.escalate(
                    ctx, item, None, Actor.ORCHESTRATOR,
                    "human-implemented item needs an operator terminal — "
                    "resume interactively", note="escalated (headless run)")
                return None
            ctx.board.begin(item["id"], "await_human_pr", "team implements")
            raw = input(f"[human item] {item['id']} is human-implemented; "
                        "enter PR number when raised: ").strip()
            pr = int(raw)
            await ctx.audit(Actor.ORCHESTRATOR, AuditDecision.HUMAN_PR,
                            {"item": item["id"], "pr": pr})
            ctx.workspace.checkout_detached(
                ctx.repo_host.get_pr(pr)["head_ref"])
        else:
            await run_coder(ctx, item, branch)
            pr = await open_pr(ctx, item, branch)
        await ctx.set_status(item["id"], ItemStatus.IN_REVIEW, pr)
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
            ctx, item, pr, Actor.RELEASE_GUARD,
            "merge conflict with main — human rebases (or make reset-item "
            "to replay); agents never resolve conflicts",
            note="merge conflict — human")
        return None

    if status == ItemStatus.QUEUED:
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
    if outcome.kind == ItemStatus.QUEUED:
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
    await incident_resolver.run(ctx.project, ctx.resolver_store)

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
        if selected and all(ItemStatus(i.get("status") or "pending").is_terminal
                            for i in selected):
            leftover = [i["id"] for i in backlog.values()
                        if (i.get("status") or ItemStatus.PENDING) == ItemStatus.PENDING]
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
