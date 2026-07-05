"""The SDLC definition stays consistent with the folders and policies
that implement it — a structural test, so a rename or a missing policy
key breaks loudly here instead of mid-demo."""

from pathlib import Path

from orchestrator.config import load_project
from orchestrator import driver
from orchestrator.definition import GATE, REASONING, SDLC

ROOT = Path(__file__).resolve().parent.parent


def test_every_step_has_a_handler():
    assert {s.name for s in SDLC.all_steps()} <= set(driver.HANDLERS)


def test_every_step_has_a_knowledge_folder():
    for step in SDLC.all_steps():
        if step.kind == GATE:
            continue  # the gate's mechanics live in orchestrator/gate.py
        assert (ROOT / "sdlc_steps" / step.name).is_dir(), step.name


def test_reasoning_steps_have_prompts_and_specs():
    for step in SDLC.all_steps():
        if step.kind != REASONING:
            continue
        folder = ROOT / "sdlc_steps" / step.name
        assert (folder / "prompts.md").exists(), step.name
        assert (folder / "spec.py").exists(), step.name


def test_back_edge_bounds_resolve_in_policy():
    project = load_project("candidate-app")
    flow = project.policy("orchestrator")
    for step in SDLC.all_steps():
        if step.back_edge:
            assert step.back_edge.max_iterations_policy in flow, step.name


def test_gate_sits_between_approver_and_release():
    names = [s.name for s in SDLC.per_item]
    assert names.index("approver") < names.index("approval_gate")
    assert "release_manager" in {s.name for s in SDLC.release}


def test_assessor_resumes_instead_of_reassessing():
    """A crashed/rate-limited run reruns without wasting quota: items
    that already have an assessment are skipped (state lives in the
    store, so resume is free)."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from orchestrator.driver import RunContext, run_risk_assessor

    items = [{"id": "PAY-101", "title": "a"}, {"id": "CAT-201", "title": "b"}]
    existing = [{"item_id": "PAY-101", "risk": "high", "effort": "M",
                 "token_estimate": 60000, "recommend_split": 0}]

    async def store_call(tool, **kw):
        if tool == "list_backlog":
            return items
        if tool == "list_assessments":
            # After the (mocked) invocation, CAT-201 appears too.
            if store.calls:
                return existing + [{"item_id": "CAT-201", "risk": "low"}]
            return existing
        raise AssertionError(tool)

    store = MagicMock()
    store.calls = []
    store.call = AsyncMock(side_effect=store_call)
    invoker = MagicMock()

    async def fake_invoke(spec, message):
        store.calls.append(spec.name)
        from orchestrator.invoker import Invocation
        return Invocation(text="", input_tokens=1, output_tokens=1)

    ctx = RunContext(project=MagicMock(), store=store, repo_host=MagicMock(),
                     invoker=MagicMock(invoke=AsyncMock(side_effect=fake_invoke)),
                     workspace=MagicMock(dir="."))
    # token metering goes through ctx.invoke -> store.call; stub it out
    ctx.invoke = fake_invoke

    asyncio.run(run_risk_assessor(ctx))
    # Only the unassessed item triggered an invocation.
    assert store.calls == ["risk_assessor"]
