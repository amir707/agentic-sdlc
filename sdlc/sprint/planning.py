"""PLANNING phase: assess every backlog item, then pack a sprint.

Runs once per sprint (the store remembers: a run that finds a sprint
resumes it instead — see sprint.py). Both steps are resume-friendly by
reading their own prior output from the store.
"""

import json

from mcp_server.vocab import Actor, Decision

from sdlc.context import RunContext
from sdlc.engine.dependency_graph import build_import_graph
from sdlc_steps import sprint_packer
from sdlc_steps.risk_assessor import spec as assessor_spec


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
        await ctx.audit(Actor.SPRINT_PACKER, Decision.REFUSE_ITEM, {
            "item": refusal.item_id, "constraint": refusal.constraint,
            "detail": refusal.detail})
        print(f"[pack] REFUSED {refusal.item_id}: {refusal.constraint} "
              f"({refusal.detail})", flush=True)
    sprint = await ctx.store.call(
        "create_sprint", item_ids=[i["id"] for i in result.selected],
        rationale=result.rationale)
    await ctx.audit(Actor.SPRINT_PACKER, Decision.CREATE_SPRINT, {
        "sprint": sprint["id"], "items": sprint["item_ids"],
        "rationale": result.rationale})
    print(f"[pack] sprint #{sprint['id']}: {sprint['item_ids']}", flush=True)
    return result.selected
