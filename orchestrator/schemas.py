"""Structured contracts between reasoning steps and the orchestrator.

Pydantic models validate every agent verdict at the boundary: a
malformed or hallucinated shape fails loudly here instead of silently
steering the pipeline. Agents WITH tools return JSON text (tools and
output_schema are mutually exclusive on LLM agents) which the driver
validates against these models; the tool-less approver gets its model
enforced natively via output_schema.
"""

from pydantic import BaseModel, Field


class ReviewComment(BaseModel):
    body: str
    blocking: bool = False


class ReviewVerdict(BaseModel):
    verdict: str = Field(pattern="^(approve|request_changes|out_of_scope)$")
    reasoning: str = ""
    comments: list[ReviewComment] = Field(default_factory=list)


class ReleaseDecision(BaseModel):
    pr: int
    action: str = Field(pattern="^(merge|hold)$")
    reasoning: str = ""
    factors: dict = Field(default_factory=dict)


class ReleasePlan(BaseModel):
    decisions: list[ReleaseDecision]


class Dossier(BaseModel):
    """The approver's decision-factors dossier (output_schema-enforced)."""
    preprod_summary: str
    verified_labels_summary: str
    review_triage: list[str]         # one line per thread: status + summary
    scope_match: str                 # delivered change vs originating item
    open_concerns: list[str] = Field(default_factory=list)


def render_dossier(dossier: Dossier, approvers: list[str]) -> str:
    """Render the structured dossier as the PR comment the human reads."""
    triage = "\n".join(f"- {line}" for line in dossier.review_triage) or "- none"
    concerns = "\n".join(f"- {line}" for line in dossier.open_concerns) or "- none"
    mentions = " ".join(f"@{a}" for a in approvers)
    return (
        "## Decision dossier\n\n"
        f"**Preprod:** {dossier.preprod_summary}\n\n"
        f"**Verified labels:** {dossier.verified_labels_summary}\n\n"
        f"**Review threads:**\n{triage}\n\n"
        f"**Scope:** {dossier.scope_match}\n\n"
        f"**Open concerns:**\n{concerns}\n\n"
        f"{mentions} — please decide: `/approve`, `/reject <reason>`, or `/hold`."
    )
