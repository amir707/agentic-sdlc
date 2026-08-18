"""The release pass as a first-class ADK 2 `Workflow` (Workstream B).

Release is its own control loop with its own clock — approvals, incidents,
confidence windows fire on a different timeline than the sprint — so it is
its own ADK graph, not a branch of the per-item pipeline. Same execution
model as adapters/adk/workflow.py: nodes delegate to engine handlers
(release_flow.release_queue, release_flow.decide_release_pr), the release-manager
agent stays behind the AgentInvoker port, and ADK's runtime owns
execution. `ADKReleaseExecutor` is the ReleaseExecutor port's ADK impl;
it is also the natural root_agent for an ambient trigger (Cloud Scheduler
/ webhook → Pub/Sub → one run of this graph).

The graph iterates the store queue one PR at a time (deploys are strictly
serialized): a routed cycle `decide_pr → advance → decide_pr` walks the
queue, and each merge records its deploy before the next decision so the
release manager sees it (confidence window). Nothing is polled; a held PR
stays `queued` and the next event reconsiders it.
"""

from google.adk.apps import App, ResumabilityConfig
from google.adk.events.event import Event
from google.adk.runners import InMemoryRunner
from google.adk.workflow import Workflow
from google.genai import types

from orchestrator import release_flow
from sdlc_steps import incident_resolver

_APP_NAME = "agentic_sdlc_release"

# (source, target, route|None). START → incident hygiene → walk the queue
# as a routed cycle → done.
RELEASE_EDGES: list[tuple[str, str, str | None]] = [
    ("START", "incident_hygiene", None),
    ("incident_hygiene", "load_queue", None),
    ("load_queue", "decide_pr", "has_items"),
    ("load_queue", "done", "empty"),
    ("decide_pr", "advance", None),
    ("advance", "decide_pr", "next"),
    ("advance", "done", "drained"),
]


def build_release_workflow(ctx) -> Workflow:
    """The release pass as an ADK graph. Per-run cursor (the queue snapshot
    and index) lives in a closure; durable truth stays in the store, so a
    crashed pass re-invoked simply reloads the queue (merged PRs are no
    longer `queued`) and continues."""
    state: dict = {
        "queue": [], "index": 0, "outcomes": [],
        "confidence": ctx.project.policy(
            "release_manager")["deploy_confidence_minutes"],
    }

    async def incident_hygiene(node_input):
        # If a previous run left an incident open and the service has since
        # recovered, close it now (release must not read a stale hold).
        ctx.board.begin("RELEASE", "incident_resolver", "checking recovery")
        await incident_resolver.run(ctx.project, ctx.resolver_store)
        return Event(output="incidents reconciled")

    async def load_queue(node_input):
        # Reset the walk cursor HERE, not only at build time: the resident
        # release service reuses ONE workflow instance across many trigger
        # events, so every pass must start its walk from the front.
        state["index"] = 0
        state["outcomes"] = []
        state["queue"] = await release_flow.release_queue(ctx)
        if not state["queue"]:
            ctx.board.finish("RELEASE", "queue empty")
            print("[release] queue empty", flush=True)
            return Event(output={"outcome": "empty"}, route="empty")
        listing = ", ".join(f"#{i['pr']} ({i['id']})" for i in state["queue"])
        print(f"[release] queue: {len(state['queue'])} PR(s) — {listing} "
              "(one decision, one deployment at a time)", flush=True)
        return Event(output=len(state["queue"]), route="has_items")

    async def decide_pr(node_input):
        item = state["queue"][state["index"]]
        outcome = await release_flow.decide_release_pr(ctx, item,
                                                 state["confidence"])
        state["outcomes"].append((item["pr"], outcome))
        return Event(output={"pr": item["pr"], "outcome": outcome})

    def advance(node_input):
        state["index"] += 1
        if state["index"] < len(state["queue"]):
            return Event(output=state["index"], route="next")
        return Event(output={"outcome": "drained"}, route="drained")

    def done(node_input):
        ctx.board.finish("RELEASE", "pass complete")
        if state["outcomes"]:
            tally: dict[str, int] = {}
            for _, outcome in state["outcomes"]:
                tally[outcome] = tally.get(outcome, 0) + 1
            summary = ", ".join(f"{n} {kind}" for kind, n in tally.items())
            print(f"[release] pass complete: {summary}", flush=True)
        return {"outcome": "pass_complete"}

    nodes = {"incident_hygiene": incident_hygiene, "load_queue": load_queue,
             "decide_pr": decide_pr, "advance": advance, "done": done}
    for name, fn in nodes.items():
        fn.__name__ = name

    by_source: dict[str, list[tuple[str, str | None]]] = {}
    for src, dst, route in RELEASE_EDGES:
        by_source.setdefault(src, []).append((dst, route))
    edges = []
    for src, targets in by_source.items():
        src_node = "START" if src == "START" else nodes[src]
        routed = {route: nodes[dst] for dst, route in targets
                  if route is not None}
        plain = [nodes[dst] for dst, route in targets if route is None]
        if routed:
            edges.append((src_node, routed))
        for target in plain:
            edges.append((src_node, target))
    return Workflow(name=_APP_NAME, edges=edges)


class ADKReleaseExecutor:
    """Runs one release pass as a native ADK Workflow (ReleaseExecutor
    port). Stateless; the composition root injects one instance."""

    async def run_pass(self, ctx) -> None:
        flow = build_release_workflow(ctx)
        app = App(name=_APP_NAME, root_agent=flow,
                  resumability_config=ResumabilityConfig(is_resumable=True))
        runner = InMemoryRunner(app=app)
        session = await runner.session_service.create_session(
            app_name=_APP_NAME, user_id=ctx.project.name)
        message = types.Content(role="user",
                                parts=[types.Part.from_text(text="release")])
        async for _ in runner.run_async(user_id=ctx.project.name,
                                        session_id=session.id,
                                        new_message=message):
            pass
