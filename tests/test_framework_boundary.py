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


# The composition roots are the entry points allowed to choose a framework
# (they instantiate the adapters and inject them into the core): the sprint
# orchestrator, the one-pass release entry, and the resident release
# service (ADK api server hosting the release Workflow).
COMPOSITION_ROOTS = {
    ROOT / "orchestrator" / "bootstrap.py",
    ROOT / "orchestrator" / "__main__.py",
    ROOT / "orchestrator" / "release.py",
    ROOT / "orchestrator" / "release_service.py",
    ROOT / "orchestrator" / "sprint_service.py",
}


def test_core_never_imports_a_framework():
    for package in CORE_PACKAGES:
        for py_file in (ROOT / package).rglob("*.py"):
            if py_file in COMPOSITION_ROOTS:
                continue
            for module in _imports(py_file):
                assert not module.startswith(FRAMEWORK_PREFIXES), (
                    f"{py_file.relative_to(ROOT)} imports {module!r} — "
                    "framework code belongs in adapters/adk/ (ADR-0007)")


def _module_level_imports(py_file: Path) -> set[str]:
    """Imports at module scope only."""
    tree = ast.parse(py_file.read_text(), filename=str(py_file))
    modules: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_orchestrator_depends_on_ports_not_adapters():
    """The core talks to the repo host, the store and the deploy tool
    through orchestrator/ports.py Protocols. A module-level `adapters`
    import in orchestrator/ (outside the composition roots) is the
    dependency arrow pointing the wrong way."""
    for py_file in (ROOT / "orchestrator").rglob("*.py"):
        if py_file in COMPOSITION_ROOTS:
            continue
        for module in _module_level_imports(py_file):
            assert not module.startswith("adapters"), (
                f"{py_file.relative_to(ROOT)} imports {module!r} at module "
                "level — depend on orchestrator.ports instead")


def test_concrete_adapters_satisfy_the_ports():
    """Structural typing, checked mechanically: every method a port
    names exists on the adapter that is injected for it."""
    import inspect
    from adapters import deploy
    from adapters.repo_host import GitHubRepoHost
    from adapters.store_client import DeliveryStore
    from orchestrator import ports

    def names(proto):
        return {n for n, v in vars(proto).items()
                if callable(v) and not n.startswith("_")}

    assert names(ports.RepoHost) <= set(dir(GitHubRepoHost))
    assert names(ports.Store) <= set(dir(DeliveryStore))
    assert names(ports.Deployer) <= {n for n, _ in inspect.getmembers(deploy)}


def test_workflow_covers_every_definition_step():
    """The executing ADK Workflow must have a node for every per-item
    definition step — the graph IS the per-item execution path now, so a
    definition step with no node would silently never run. (End-to-end
    execution of that graph is exercised in test_item_workflow.py.)"""
    from adapters.adk.workflow import EDGE_TABLE

    node_names = {src for src, _, _ in EDGE_TABLE if src != "START"} \
        | {dst for _, dst, _ in EDGE_TABLE}
    per_item_names = {step.name for step in SDLC.per_item}
    assert per_item_names <= node_names, (
        "the ADK Workflow must cover every per-item definition step")
    # Every definition back-edge is realized as a routed cycle in the graph
    # (a fix step that returns to the step that requested the fix).
    for step in SDLC.per_item:
        if step.back_edge:
            fixers = {src for src, dst, _ in EDGE_TABLE if dst == step.name
                      and src != _step_before(step.name)}
            assert fixers, f"{step.name} declares a back-edge but no cycle "\
                           "returns to it"


def _step_before(name: str) -> str | None:
    from adapters.adk.workflow import EDGE_TABLE
    for src, dst, _ in EDGE_TABLE:
        if dst == name:
            return src
    return None


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
