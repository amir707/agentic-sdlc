"""Tests for the deterministic substrate: config overlays, dependency
graph, diff analysis, sprint packer.

The packer test mirrors the seeded demo scenario end to end: given the
intended assessments for projects-config/candidate-app/backlog.json,
the packed sprint and every named refusal must come out exactly as the
demo script expects.
"""

import json
import textwrap
from pathlib import Path

import pytest

from orchestrator.config import ConfigError, load_project
from orchestrator.dependency_graph import blast_radius, build_import_graph, dependents_closure
from tools.diff_analysis import areas_touched, files_touched, flag_coverage
from sdlc_steps.sprint_packer import pack

ROOT = Path(__file__).resolve().parent.parent


# --- engine/config -----------------------------------------------------------

def test_policy_overlay_resolution():
    project = load_project("candidate-app")
    # project override wins over the engine's empty default
    assert project.policy("approver")["approvers"] == ["amir707"]
    # engine step defaults visible where the project has no override
    assert project.policy("sprint_packer")["risk_budget"] == 2
    # shared key reaches steps that consume it
    assert project.policy("code_reviewer")["flag_required_min_risk"] == "medium"
    assert project.policy("verify")["flag_required_min_risk"] == "medium"


def test_prompt_composition_order():
    project = load_project("candidate-app")
    prompt = project.prompt("code_reviewer")
    base_marker = "Core rules (system-owned"
    custom_marker = "candidate-app customised prompt"
    assert base_marker in prompt and custom_marker in prompt
    assert prompt.index(base_marker) < prompt.index(custom_marker)
    # steps without customisation still compose
    assert "Core rules" in project.prompt("approver")


def test_area_mapping():
    project = load_project("candidate-app")
    assert project.area_for("app/payments.py") == "payments"
    assert project.area_for("app/catalog.py") == "catalog"
    assert project.area_for("app/main.py") == "core"
    assert project.area_for("flags.json") == "core"


def test_unknown_project_fails_fast():
    with pytest.raises(ConfigError):
        load_project("nonexistent")


# --- tools/dependency_graph --------------------------------------------------

@pytest.fixture
def toy_repo(tmp_path):
    """app.main imports app.payments and app.catalog; both import app.flags."""
    app = tmp_path / "app"
    app.mkdir()
    (app / "__init__.py").write_text("")
    (app / "flags.py").write_text("def enabled(name): return False\n")
    (app / "payments.py").write_text("from app import flags\n")
    (app / "catalog.py").write_text("from app.flags import enabled\n")
    (app / "main.py").write_text("from app import payments\nfrom app import catalog\n")
    return tmp_path


def test_import_graph_edges(toy_repo):
    graph = build_import_graph(toy_repo)
    assert "app.flags" in graph["app.payments"]
    assert "app.flags" in graph["app.catalog"]
    assert {"app.payments", "app.catalog"} <= graph["app.main"]


def test_blast_radius_is_transitive(toy_repo):
    # Changing flags.py impacts everything that transitively imports it.
    radius = blast_radius(toy_repo, ["app/flags.py"])
    assert {"app.flags", "app.payments", "app.catalog", "app.main"} <= radius
    # Changing catalog.py reaches main but never payments.
    radius = blast_radius(toy_repo, ["app/catalog.py"])
    assert "app.main" in radius and "app.payments" not in radius
    # Non-Python files ride along untranslated.
    assert "flags.json" in blast_radius(toy_repo, ["flags.json"])


def test_closure_ignores_unknown_modules(toy_repo):
    graph = build_import_graph(toy_repo)
    assert dependents_closure(graph, {"not.a.module"}) == set()


# --- tools/diff_analysis -----------------------------------------------------

SAMPLE_DIFF = textwrap.dedent("""\
    diff --git a/app/payments.py b/app/payments.py
    --- a/app/payments.py
    +++ b/app/payments.py
    @@ -10,3 +10,6 @@
    +from app import flags
    +    if flags.enabled("payments_refund_totals"):
    +        summary["refunded_total"] = 342.10
    diff --git a/flags.json b/flags.json
    --- a/flags.json
    +++ b/flags.json
    @@ -1,3 +1,4 @@
    +  "payments_refund_totals": false,
    """)


def test_files_and_areas_touched():
    project = load_project("candidate-app")
    assert files_touched(SAMPLE_DIFF) == ["app/payments.py", "flags.json"]
    assert areas_touched(SAMPLE_DIFF, project) == {"payments", "core"}


def test_flag_coverage_detects_gated_change():
    coverage = flag_coverage(SAMPLE_DIFF)
    assert coverage["covered"] is True
    assert coverage["gated_flags"] == ["payments_refund_totals"]


def test_flag_coverage_requires_definition_and_use():
    # used but never defined in flags.json -> not covered
    used_only = SAMPLE_DIFF.split("diff --git a/flags.json")[0]
    assert flag_coverage(used_only)["covered"] is False
    # defined but never checked in code -> not covered
    defined_only = "diff --git a/flags.json b/flags.json\n+++ b/flags.json\n" \
                   '+  "payments_refund_totals": false,\n'
    assert flag_coverage(defined_only)["covered"] is False


# --- tools/sprint_packer -----------------------------------------------------

def _assessment(risk, effort, tokens, split=False, reason=None):
    return {"risk": risk, "effort": effort, "token_estimate": tokens,
            "recommend_split": split, "split_reason": reason}


def test_pack_matches_seeded_demo_scenario():
    """The demo's expected outcome, end to end, with intended assessments."""
    items = json.loads(
        (ROOT / "projects-config" / "candidate-app" / "backlog.json").read_text())
    assessments = {
        "PAY-101": _assessment("high", "M", 60000),
        "CAT-201": _assessment("low", "S", 30000),
        "PAY-102": _assessment("low", "S", 30000),   # the trap item: believed low until verify
        "CAT-202": _assessment("low", "S", 30000),
        "CORE-301": _assessment("medium", "L", 120000, split=True,
                                reason="L effort at medium risk"),
        "PAY-103": _assessment("high", "M", 60000),
    }
    project = load_project("candidate-app")
    result = pack(items, assessments, project.policy("sprint_packer"))

    assert [i["id"] for i in result.selected] == \
        ["PAY-101", "CAT-201", "PAY-102", "CAT-202"]

    # tokens: 60k+30k+30k+30k = the full 150k; risk: 2+0+0+0 = the cap
    by_id = {r.item_id: r.constraint for r in result.refused}
    assert by_id["CORE-301"] == "recommend_split"      # demo beat 1a
    assert by_id["PAY-103"] == "risk_budget"           # demo beat 1b (high after high)


def test_human_items_cost_no_tokens():
    items = [
        {"id": "A", "implementation": "human", "priority_rank": 1},
        {"id": "B", "implementation": "agent", "priority_rank": 2},
    ]
    assessments = {"A": _assessment("low", "S", 999999),
                   "B": _assessment("low", "S", 1000)}
    policy = {"risk_points": {"low": 0, "medium": 1, "high": 2},
              "risk_budget": 2, "token_budget": 1000,
              "reviewer_capacity": {"reviewers": 1, "prs_per_reviewer": 2}}
    result = pack(items, assessments, policy)
    assert [i["id"] for i in result.selected] == ["A", "B"]


def test_reviewer_capacity_binds():
    items = [{"id": f"I{n}", "implementation": "agent", "priority_rank": n}
             for n in range(1, 4)]
    assessments = {f"I{n}": _assessment("low", "S", 10) for n in range(1, 4)}
    policy = {"risk_points": {"low": 0, "medium": 1, "high": 2},
              "risk_budget": 2, "token_budget": 10000,
              "reviewer_capacity": {"reviewers": 1, "prs_per_reviewer": 2}}
    result = pack(items, assessments, policy)
    assert len(result.selected) == 2
    assert result.refused[0].constraint == "reviewer_capacity"
