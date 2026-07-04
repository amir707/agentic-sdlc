"""Repository layer for the delivery store (SQLite).

Standalone by design: this package imports nothing from agents/ or the
orchestrator — dependency direction is core -> MCP client -> this
server, never the reverse. Single-file SQLite in WAL mode so the
orchestrator process, monitor, and resolver can write concurrently and
judges can inspect state with any sqlite client.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS backlog_items (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('bug', 'story')),
    implementation TEXT NOT NULL CHECK (implementation IN ('agent', 'human')),
    claimed_risk TEXT NOT NULL CHECK (claimed_risk IN ('low', 'medium', 'high')),
    claimed_impact TEXT NOT NULL,
    area_hint TEXT NOT NULL,
    priority_rank INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS assessments (
    item_id TEXT NOT NULL REFERENCES backlog_items(id),
    risk TEXT NOT NULL,
    effort TEXT NOT NULL,
    token_estimate INTEGER NOT NULL,
    rationale TEXT NOT NULL,
    recommend_split INTEGER NOT NULL DEFAULT 0,
    split_reason TEXT,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sprints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_ids_json TEXT NOT NULL,
    rationale TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    area TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'resolved')),
    opened_at TEXT NOT NULL,
    resolved_at TEXT,
    error_rate REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS health_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    area TEXT NOT NULL,
    error_rate REAL NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deploys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pr INTEGER NOT NULL,
    revision TEXT NOT NULL,
    traffic TEXT NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    ts TEXT NOT NULL
);
-- Append-only by interface: the MCP server exposes no update/delete
-- tool for this table; that absence IS the security property.
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    decision TEXT NOT NULL,
    factors_json TEXT NOT NULL
);
"""


def db_path() -> str:
    return os.environ.get("DELIVERY_STORE_DB", "delivery_store.sqlite3")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rows(cursor) -> list[dict]:
    return [dict(row) for row in cursor.fetchall()]


# --- backlog / assessments -------------------------------------------------

def list_backlog(conn) -> list[dict]:
    return _rows(conn.execute(
        "SELECT * FROM backlog_items ORDER BY priority_rank"))


def get_item(conn, item_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM backlog_items WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def record_assessment(conn, item_id, risk, effort, token_estimate, rationale,
                      recommend_split, split_reason) -> None:
    conn.execute(
        "INSERT INTO assessments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (item_id, risk, effort, token_estimate, rationale,
         int(recommend_split), split_reason, now()))
    conn.commit()


def latest_assessments(conn) -> list[dict]:
    """Most recent assessment per item (items may be re-assessed)."""
    return _rows(conn.execute("""
        SELECT a.* FROM assessments a
        JOIN (SELECT item_id, MAX(ts) AS ts FROM assessments GROUP BY item_id) m
          ON a.item_id = m.item_id AND a.ts = m.ts
    """))


# --- sprints ---------------------------------------------------------------

def create_sprint(conn, item_ids: list[str], rationale: str) -> dict:
    cur = conn.execute(
        "INSERT INTO sprints (item_ids_json, rationale, created_at) VALUES (?, ?, ?)",
        (json.dumps(item_ids), rationale, now()))
    conn.commit()
    return get_sprint(conn, cur.lastrowid)


def get_sprint(conn, sprint_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM sprints WHERE id = ?", (sprint_id,)).fetchone()
    if not row:
        return None
    sprint = dict(row)
    sprint["item_ids"] = json.loads(sprint.pop("item_ids_json"))
    return sprint


def current_sprint(conn) -> dict | None:
    row = conn.execute(
        "SELECT id FROM sprints ORDER BY id DESC LIMIT 1").fetchone()
    return get_sprint(conn, row["id"]) if row else None


# --- incidents / health ----------------------------------------------------

def open_incident(conn, area: str, error_rate: float) -> dict:
    """Idempotent: one open incident per area at a time."""
    existing = conn.execute(
        "SELECT * FROM incidents WHERE area = ? AND status = 'open'",
        (area,)).fetchone()
    if existing:
        return dict(existing)
    cur = conn.execute(
        "INSERT INTO incidents (area, status, opened_at, error_rate) "
        "VALUES (?, 'open', ?, ?)", (area, now(), error_rate))
    conn.commit()
    return dict(conn.execute(
        "SELECT * FROM incidents WHERE id = ?", (cur.lastrowid,)).fetchone())


def resolve_incident(conn, incident_id: int) -> dict | None:
    conn.execute(
        "UPDATE incidents SET status = 'resolved', resolved_at = ? "
        "WHERE id = ? AND status = 'open'", (now(), incident_id))
    conn.commit()
    row = conn.execute(
        "SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    return dict(row) if row else None


def get_incident(conn, incident_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    return dict(row) if row else None


def list_open_incidents(conn) -> list[dict]:
    return _rows(conn.execute(
        "SELECT * FROM incidents WHERE status = 'open' ORDER BY id"))


def record_health_sample(conn, area: str, error_rate: float) -> None:
    conn.execute(
        "INSERT INTO health_samples (area, error_rate, ts) VALUES (?, ?, ?)",
        (area, error_rate, now()))
    conn.commit()


def list_health_samples(conn, area: str, window_seconds: int) -> list[dict]:
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=window_seconds)).isoformat(timespec="seconds")
    return _rows(conn.execute(
        "SELECT * FROM health_samples WHERE area = ? AND ts >= ? ORDER BY ts",
        (area, cutoff)))


# --- deploys ---------------------------------------------------------------

def record_deploy(conn, pr: int, revision: str, traffic: str) -> None:
    conn.execute(
        "INSERT INTO deploys (pr, revision, traffic, ts) VALUES (?, ?, ?, ?)",
        (pr, revision, traffic, now()))
    conn.commit()


def list_recent_deploys(conn, window_minutes: int) -> list[dict]:
    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=window_minutes)).isoformat(timespec="seconds")
    return _rows(conn.execute(
        "SELECT * FROM deploys WHERE ts >= ? ORDER BY ts", (cutoff,)))


# --- token usage -----------------------------------------------------------

def record_token_usage(conn, agent, model, input_tokens, output_tokens) -> None:
    conn.execute(
        "INSERT INTO token_usage (agent, model, input_tokens, output_tokens, ts) "
        "VALUES (?, ?, ?, ?, ?)",
        (agent, model, input_tokens, output_tokens, now()))
    conn.commit()


def summarize_token_usage(conn, sprint_id: int | None = None) -> list[dict]:
    """Per agent+model totals; scoped to usage since the sprint began."""
    since = "1970"
    if sprint_id is not None:
        sprint = get_sprint(conn, sprint_id)
        if sprint:
            since = sprint["created_at"]
    return _rows(conn.execute(
        "SELECT agent, model, SUM(input_tokens) AS input_tokens, "
        "SUM(output_tokens) AS output_tokens, COUNT(*) AS calls "
        "FROM token_usage WHERE ts >= ? GROUP BY agent, model", (since,)))


# --- audit (append-only) ---------------------------------------------------

def append_audit(conn, actor: str, decision: str, factors: dict) -> dict:
    cur = conn.execute(
        "INSERT INTO audit (ts, actor, decision, factors_json) VALUES (?, ?, ?, ?)",
        (now(), actor, decision, json.dumps(factors)))
    conn.commit()
    return {"id": cur.lastrowid, "ts": now(), "actor": actor,
            "decision": decision, "factors": factors}


def list_audit(conn) -> list[dict]:
    entries = _rows(conn.execute("SELECT * FROM audit ORDER BY id"))
    for entry in entries:
        entry["factors"] = json.loads(entry.pop("factors_json"))
    return entries
