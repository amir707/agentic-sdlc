"""The store's lifecycle vocabulary — item statuses, in one place.

The store is the single source of lifecycle truth (G3), so the words
for that lifecycle live HERE, on the store side, and the engine imports
them (never the reverse: mcp_server stays standalone). Before this
module the allowed set was a tuple in server.py, the display names a
dict in scripts/store_status.py, the lifecycle a comment in db.py, and
~60 call sites spelled the literals by hand.

Members are StrEnum: they compare equal to their string, JSON-encode as
it, and bind to SQLite as it — so a status flows through the MCP tool
surface unchanged and reads back as plain text.
"""

from enum import StrEnum


class ItemStatus(StrEnum):
    """Lifecycle owned by the governor. The PR is the artifact; THIS is
    the truth the orchestrator resumes from.

        pending -> in_review -> verified -> preprod_passed
                -> awaiting_approval -> queued -> released
        | rejected | escalated | failed
    """
    PENDING = "pending"
    IN_REVIEW = "in_review"
    VERIFIED = "verified"
    PREPROD_PASSED = "preprod_passed"
    AWAITING_APPROVAL = "awaiting_approval"
    QUEUED = "queued"
    RELEASED = "released"
    REJECTED = "rejected"
    ESCALATED = "escalated"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        """The item's journey is over for this sprint: released or
        rejected. (escalated/failed are parked, not over — a human can
        /approve or /reject them back in.)"""
        return self in (ItemStatus.RELEASED, ItemStatus.REJECTED)

    @property
    def is_parked(self) -> bool:
        """Waiting on a human's word to re-enter the pipeline."""
        return self in (ItemStatus.ESCALATED, ItemStatus.FAILED)


# Operator-facing wording (store_status, dashboard tooltips).
STATUS_LABELS: dict[ItemStatus, str] = {
    ItemStatus.PENDING: "not started",
    ItemStatus.IN_REVIEW: "in review",
    ItemStatus.VERIFIED: "verified (labels applied)",
    ItemStatus.PREPROD_PASSED: "preprod passed",
    ItemStatus.AWAITING_APPROVAL: "awaiting gate decision (/approve on the PR)",
    ItemStatus.QUEUED: "approved — queued for release",
    ItemStatus.RELEASED: "MERGED + released",
    ItemStatus.REJECTED: "rejected",
    ItemStatus.ESCALATED: "escalated to a human",
    ItemStatus.FAILED: "failed preprod",
}


class Actor(StrEnum):
    """Who is speaking in the audit trail — the component, not a
    person (a human's identity is a FACTOR, e.g. author=...)."""
    CODER = "coder"
    CODE_REVIEWER = "code_reviewer"
    VERIFY = "verify"
    PREPROD_CI = "preprod_ci"
    APPROVER = "approver"
    APPROVAL_GATE = "approval_gate"
    SPRINT_PACKER = "sprint_packer"
    RELEASE_MANAGER = "release_manager"   # the agent's merge/hold judgment
    RELEASE_GUARD = "release_guard"       # the deterministic gates around it
    ORCHESTRATOR = "orchestrator"
    INCIDENT_RESOLVER = "incident_resolver"
    OPERATOR = "operator"                 # a human running an ops script


class Decision(StrEnum):
    """What was decided. The audit trail is the eval oracle
    (scripts/verify_demo.py) and the dashboard's colouring key — a
    renamed decision used to break both silently; now it is a rename
    here or nowhere."""
    # planning
    REFUSE_ITEM = "refuse_item"
    CREATE_SPRINT = "create_sprint"
    # per-item pipeline
    OPEN_PR = "open_pr"
    APPROVE_REVIEW = "approve_review"
    REJECT_PR = "reject_pr"
    ESCALATE_RISK_LABEL = "escalate_risk_label"
    PREPROD_RESULT = "preprod_result"
    POST_DOSSIER = "post_dossier"
    # the human gate
    HUMAN_APPROVE = "human_approve"
    HUMAN_REJECT = "human_reject"
    HUMAN_HOLD = "human_hold"
    HUMAN_OVERRIDE_ESCALATION = "human_override_escalation"
    HUMAN_PR = "human_pr"
    IGNORE_UNAUTHORIZED_COMMAND = "ignore_unauthorized_command"
    # governance outcomes (sdlc/governance/outcomes.py)
    ESCALATE_TO_HUMAN = "escalate_to_human"
    HOLD_MERGE = "hold_merge"
    # release
    MERGE_PR = "merge_pr"
    # incidents / ops
    RESOLVE_INCIDENT = "resolve_incident"
    RESET_ITEM = "reset_item"

    @classmethod
    def human(cls, kind: str) -> "Decision":
        """The gate's audit decision for a human command kind
        (approve | reject | hold)."""
        return cls(f"human_{kind}")
