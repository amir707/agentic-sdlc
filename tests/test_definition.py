"""The SDLC definition stays consistent with the folders and policies
that implement it — a structural test, so a rename or a missing policy
key breaks loudly here instead of mid-demo."""

from pathlib import Path

from engine.config import load_project
from orchestrator import driver
from orchestrator.definition import GATE, REASONING, SDLC

ROOT = Path(__file__).resolve().parent.parent


def test_every_step_has_a_handler():
    assert {s.name for s in SDLC.all_steps()} <= set(driver.HANDLERS)


def test_every_step_has_a_knowledge_folder():
    for step in SDLC.all_steps():
        if step.kind == GATE:
            continue  # the gate's mechanics live in engine/gate.py
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
