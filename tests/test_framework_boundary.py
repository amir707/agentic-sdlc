"""ADR-0007 enforced structurally: the SDLC core never imports an agent
framework, specs declare tool needs instead of constructing them, agent
verdicts validate against schemas, and the ADK Workflow expression
stays in parity with the framework-neutral definition."""

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from sdlc.governance import schemas
ROOT = Path(__file__).resolve().parent.parent

# The framework-free core: everything under sdlc/ except the adapters
# (which exist to name frameworks) and app/ (the composition roots), plus
# the step knowledge, the coder's tools and the store.
CORE_DIRS = ("sdlc", "mcp_server")
NON_CORE = (ROOT / "sdlc" / "adapters", ROOT / "sdlc" / "app")
FRAMEWORK_PREFIXES = ("google.adk", "google.genai", "sdlc.adapters.adk")


def _core_files():
    for d in CORE_DIRS:
        for py_file in (ROOT / d).rglob("*.py"):
            if any(py_file.is_relative_to(x) for x in NON_CORE):
                continue
            yield py_file


def _imports(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(), filename=str(py_file))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_core_never_imports_a_framework():
    """sdlc/app is the composition root and sdlc/adapters exists to name
    frameworks; nothing else may (ADR-0007)."""
    for py_file in _core_files():
        for module in _imports(py_file):
            assert not module.startswith(FRAMEWORK_PREFIXES), (
                f"{py_file.relative_to(ROOT)} imports {module!r} — "
                "framework code belongs in sdlc/adapters/adk/ (ADR-0007)")


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


def test_core_depends_on_ports_not_adapters():
    """The core talks to the repo host, the store and the deploy tool
    through sdlc/ports Protocols. A module-level `sdlc.adapters` import
    outside sdlc/app and sdlc/adapters is the dependency arrow pointing
    the wrong way."""
    for py_file in _core_files():
        for module in _module_level_imports(py_file):
            assert not module.startswith("sdlc.adapters"), (
                f"{py_file.relative_to(ROOT)} imports {module!r} at module "
                "level — depend on sdlc.ports instead")


def test_the_two_clocks_stay_apart():
    """sprint/ may hand off to release/ (the trigger seam) but release/
    never imports sprint/: the release clock must be runnable, testable
    and deployable without the sprint clock (its own service, its own
    Workflow). governance/, ports/, definition, context, engine are the
    shared kernel — neither clock imports the other's internals."""
    for py_file in (ROOT / "sdlc" / "release").rglob("*.py"):
        for module in _imports(py_file):
            assert not module.startswith("sdlc.sprint"), (
                f"{py_file.relative_to(ROOT)} imports {module!r} — the "
                "release clock must not depend on the sprint clock")
    for py_file in list((ROOT / "sdlc" / "governance").rglob("*.py")) + \
            list((ROOT / "sdlc" / "ports").rglob("*.py")) + \
            list((ROOT / "sdlc" / "engine").rglob("*.py")) + \
            [ROOT / "sdlc" / "definition.py", ROOT / "sdlc" / "context.py"]:
        for module in _imports(py_file):
            assert not module.startswith(("sdlc.sprint", "sdlc.release",
                                          "sdlc.app")), (
                f"{py_file.relative_to(ROOT)} imports {module!r} — the "
                "shared kernel must not depend on a clock or the app")


def test_concrete_adapters_satisfy_the_ports():
    """Structural typing, checked mechanically: every method a port
    names exists on the adapter that is injected for it."""
    import inspect
    from sdlc.adapters import gcloud as deploy
    from sdlc.adapters.github import GitHubRepoHost
    from sdlc.adapters.store_client import DeliveryStore
    from sdlc.ports import world as ports
    def names(proto):
        return {n for n, v in vars(proto).items()
                if callable(v) and not n.startswith("_")}

    assert names(ports.RepoHost) <= set(dir(GitHubRepoHost))
    assert names(ports.Store) <= set(dir(DeliveryStore))
    assert names(ports.Deployer) <= {n for n, _ in inspect.getmembers(deploy)}


def test_workflow_renders_the_definition_graph_exactly():
    """The executing ADK Workflow is built FROM sdlc/definition.py's
    PER_ITEM_EDGES — it renders the definition, it does not redefine it.
    (Graph-internal consistency is pinned in test_definition.py.)"""
    from sdlc.adapters.adk.item_workflow import EDGE_TABLE
    from sdlc.definition import PER_ITEM_EDGES
    assert list(EDGE_TABLE) == list(PER_ITEM_EDGES)


def test_workflow_constructs_and_validates():
    """ADK's own graph validation (edge types, cycle rules) accepts the
    per-item workflow — construction only, no execution/model calls."""
    from unittest.mock import MagicMock

    from sdlc.adapters.adk.item_workflow import build_item_workflow

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
