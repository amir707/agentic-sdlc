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
