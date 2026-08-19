"""The per-project pipeline SHAPE (proposal 0001 §7): safe axes are
configurable, guarantees are not — and the line is drawn in code."""

import pytest

from sdlc.definition import (DEFAULT_SHAPE, PER_ITEM_EDGES, PipelineShape,
                             Route, per_item_edges, per_item_nodes)


def test_default_shape_is_the_documented_graph():
    assert per_item_edges(DEFAULT_SHAPE) == PER_ITEM_EDGES
    assert "approval_gate" in per_item_nodes()


def test_no_human_gate_wires_approver_straight_to_queued():
    edges = per_item_edges(PipelineShape(human_gate=False))
    assert ("approver", "queued", None) in edges
    assert "approval_gate" not in per_item_nodes(edges)
    # every other guarantee is still wired: review loop, verify, preprod
    for src in ("coder", "code_reviewer", "verify", "preprod_ci", "approver"):
        assert any(s == src for s, _, _ in edges), src
    assert ("verify", "preprod_ci", Route.LABELED) in edges


def test_unknown_keys_are_rejected_so_guarantees_cannot_be_configured_away():
    with pytest.raises(ValueError, match="engine guarantee"):
        PipelineShape.from_mapping({"human_gate": True, "verify": False})
    with pytest.raises(ValueError, match="engine guarantee"):
        PipelineShape.from_mapping({"preprod_ci": False})
    with pytest.raises(ValueError, match="true|false"):
        PipelineShape.from_mapping({"human_gate": "no"})
    assert PipelineShape.from_mapping(None) == DEFAULT_SHAPE
    assert PipelineShape.from_mapping({"human_gate": False}).human_gate is False


def test_pipeline_yaml_is_loaded_into_the_project_config(tmp_path, monkeypatch):
    from sdlc.engine import config
    bundle = tmp_path / "proj"
    bundle.mkdir()
    (bundle / "project.yaml").write_text(
        "repo: o/r\nareas: {payments: [app/payments]}\ndefault_area: payments\n")
    (bundle / "pipeline.yaml").write_text("human_gate: false\n")
    monkeypatch.setattr(config, "PROJECTS", tmp_path)
    # no approvers configured — fine WITHOUT a human gate...
    assert config.load_project("proj").shape == PipelineShape(human_gate=False)
    # ...but a gated shape needs humans to hold it
    (bundle / "pipeline.yaml").write_text("human_gate: true\n")
    with pytest.raises(config.ConfigError, match="no approvers"):
        config.load_project("proj")
    (bundle / "pipeline.yaml").write_text("human_gate: false\nverify: false\n")
    with pytest.raises(config.ConfigError, match="engine guarantee"):
        config.load_project("proj")
