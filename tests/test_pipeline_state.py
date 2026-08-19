"""orchestrator/pipeline: the per-item graph's typed vocabulary."""

from sdlc.adapters.adk.item_workflow import EDGE_TABLE
from sdlc.sprint.pipeline import PipelineState, Route


def test_every_route_in_the_edge_table_is_a_route_member_and_vice_versa():
    used = {route for _, _, route in EDGE_TABLE if route is not None}
    assert used == set(Route), (set(Route) - used, used - set(Route))


def test_route_is_a_plain_string_for_the_engine():
    assert Route.APPROVED == "approved" and isinstance(Route.APPROVED, str)


def test_fresh_state_and_resume_state():
    fresh = PipelineState()
    assert fresh.pr is None and fresh.review_rounds == 0
    resumed = PipelineState(pr=7)
    assert resumed.pr == 7  # the coder node skips re-implementation (G5)
    assert resumed.gate_ignores == set() and resumed.gate_ignores is not fresh.gate_ignores
