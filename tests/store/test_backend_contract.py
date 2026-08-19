"""The store-backend contract — the Firestore door, kept mechanical.

The delivery store's guarantees (append-only audit, role scoping) live
in the MCP TOOL surface, not the database. Swapping SQLite for Firestore
(the production rung) therefore means exactly three steps:

  1. implement `mcp_server/db_firestore.py` exposing the functions
     pinned below, same names and signatures;
  2. switch the `from mcp_server import db` import in
     `mcp_server/server.py` (and the scripts that read the store
     directly: store_status, seed, verify_demo, dashboard);
  3. run tests/test_delivery_store.py against it — the real-server HTTP
     suite is the behavioral contract; green = migrated.

This test pins step 1's checklist so the seam cannot silently drift:
if a function is added to db.py, it must be added HERE (and thus to any
future backend) deliberately.
"""

import inspect

from mcp_server import db

# function name -> required positional parameters (after `conn`)
CONTRACT = {
    "connect": [],
    "db_path": [],
    "init_schema": ["conn"],
    "now": [],
    # backlog / assessments
    "list_backlog": ["conn"],
    "get_item": ["conn", "item_id"],
    "set_item_status": ["conn", "item_id", "status"],
    "record_assessment": ["conn", "item_id", "risk", "effort",
                          "token_estimate", "rationale", "recommend_split",
                          "split_reason"],
    "latest_assessments": ["conn"],
    # sprints
    "create_sprint": ["conn", "item_ids", "rationale"],
    "get_sprint": ["conn", "sprint_id"],
    "current_sprint": ["conn"],
    # incidents / health
    "open_incident": ["conn", "area", "error_rate"],
    "resolve_incident": ["conn", "incident_id"],
    "get_incident": ["conn", "incident_id"],
    "list_open_incidents": ["conn"],
    "list_incidents": ["conn"],
    "record_health_sample": ["conn", "area", "error_rate"],
    "list_health_samples": ["conn", "area", "window_seconds"],
    # deploys
    "record_deploy": ["conn", "pr", "revision", "traffic"],
    "list_deploys": ["conn"],
    "list_recent_deploys": ["conn", "window_minutes"],
    # tokens
    "record_token_usage": ["conn", "agent", "model", "input_tokens",
                           "output_tokens"],
    "summarize_token_usage": ["conn"],
    # audit (append-only: no update/delete function may EVER exist)
    "append_audit": ["conn", "actor", "decision", "factors"],
    "list_audit": ["conn"],
}

FORBIDDEN = ("update_audit", "delete_audit", "clear_audit", "purge_audit")


def test_backend_exposes_the_full_contract():
    for name, required in CONTRACT.items():
        fn = getattr(db, name, None)
        assert callable(fn), f"backend missing {name}()"
        params = list(inspect.signature(fn).parameters)
        for p in required:
            assert p in params, f"{name}() missing required param {p!r}"


def test_no_public_surface_beyond_the_contract():
    """A new public db function is a contract change: add it to CONTRACT
    (and to every future backend) deliberately, or make it private."""
    public = {n for n, v in vars(db).items()
              if callable(v) and not n.startswith("_")
              and getattr(v, "__module__", "") == "mcp_server.db"}
    assert public <= set(CONTRACT), (
        f"functions outside the pinned contract: {public - set(CONTRACT)}")


def test_audit_mutation_functions_never_exist():
    for name in FORBIDDEN:
        assert not hasattr(db, name), (
            f"{name} must never exist — the audit log is append-only "
            "BY THE ABSENCE of mutation paths")
