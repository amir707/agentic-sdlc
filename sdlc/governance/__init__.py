"""The rules both clocks obey — framework-free, shared by sprint and release.

  outcomes.py   escalate / fail / hold / bounce: the ONE way an item leaves
                the pipeline, with uniform evidence (audit + status + board)
  gates.py      the MACHINE gates on a PR head (verify, preprod) that both
                clocks run — the release guard re-runs them before a merge
  gate.py       the human approval gate (ADR-0005): authority is the GitHub
                comment; a resume is a nudge, never a decision
  rejection.py  the unified PR rejection mechanism (reasons are data)
  markers.py    SHA-keyed idempotency markers on PR comments (G5)
  schemas.py    the contracts between reasoning agents and the engine

`from sdlc import governance; governance.escalate(...)` — the outcomes
are the package's face.
"""

from sdlc.governance.outcomes import bounce, escalate, fail, hold  # noqa: F401
from sdlc.governance.rejection import reject  # noqa: F401  (patched in tests)
