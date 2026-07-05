"""ADR-0007 enforced structurally: the SDLC core never imports an agent
framework, specs declare tool needs instead of constructing them, agent
verdicts validate against schemas, and the ADK Workflow expression
stays in parity with the framework-neutral definition."""

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator import schemas
from orchestrator.definition import SDLC

ROOT = Path(__file__).resolve().parent.parent

CORE_PACKAGES = ("orchestrator", "sdlc_steps", "tools", "mcp_server")
FRAMEWORK_PREFIXES = ("google.adk", "google.genai", "adapters.adk")


def _imports(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(), filename=str(py_file))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


# The composition root is the ONE file allowed to choose a framework
# (it instantiates the adapter and injects it into the core).
COMPOSITION_ROOT = ROOT / "orchestrator" / "__main__.py"


def test_core_never_imports_a_framework():
    for package in CORE_PACKAGES:
        for py_file in (ROOT / package).rglob("*.py"):
            if py_file == COMPOSITION_ROOT:
                continue
            for module in _imports(py_file):
                assert not module.startswith(FRAMEWORK_PREFIXES), (
                    f"{py_file.relative_to(ROOT)} imports {module!r} — "
                    "framework code belongs in adapters/adk/ (ADR-0007)")


def test_workflow_expression_matches_definition():
    from adapters.adk.workflow import definition_parity

    parity = definition_parity()
    per_item_names = {step.name for step in SDLC.per_item}
    assert per_item_names <= parity["node_names"], (
        "the ADK Workflow must cover every per-item definition step")
    # Every declared back-edge is realized as a routed cycle.
    backedged = {s.name for s in SDLC.per_item if s.back_edge}
    assert set(parity["back_edges_realized"]) == backedged
    assert all(parity["back_edges_realized"].values())


def test_workflow_constructs_and_validates():
    """ADK's own graph validation (edge types, cycle rules) accepts the
    per-item workflow — construction only, no execution/model calls."""
    from unittest.mock import MagicMock

    from adapters.adk.workflow import build_item_workflow

    ctx = MagicMock()
    ctx.project.policy.return_value = {"max_fix_iterations": 2,
                                       "max_flag_fix_iterations": 1}
    workflow = build_item_workflow(ctx, {"id": "PAY-101"}, "item/PAY-101-x")
    assert workflow.name == "item_PAY_101"


def test_schemas_reject_malformed_verdicts():
    with pytest.raises(ValidationError):
        schemas.ReviewVerdict.model_validate({"verdict": "ship it"})
    with pytest.raises(ValidationError):
        schemas.ReleasePlan.model_validate(
            {"decisions": [{"pr": 7, "action": "yolo"}]})
    verdict = schemas.ReviewVerdict.model_validate(
        {"verdict": "approve", "comments": [{"body": "nice"}]})
    assert verdict.comments[0].blocking is False


def test_dossier_renders_for_humans():
    dossier = schemas.Dossier(
        preprod_summary="pr-7 healthy at tag URL",
        verified_labels_summary="[area:payments][risk:medium][flag:yes]",
        review_triage=["resolved: fee rounding"],
        scope_match="matches PAY-102",
    )
    rendered = schemas.render_dossier(dossier, ["amir707"])
    assert "@amir707" in rendered and "/approve" in rendered
    assert "pr-7 healthy" in rendered
