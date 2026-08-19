"""SHA-keyed idempotency markers on PR comments (guardrail G5).

Stages that post to a PR or deploy from it stamp their comment with an
invisible marker keyed to the head SHA. A restarted run finds the
marker and skips the stage; a new commit changes the SHA and naturally
invalidates it — so a stage repeats exactly when the code changed.
Kept as its own module because the guarantee is a security property
the ADK engine cannot provide (resume may replay a node)."""


def marker(kind: str, sha: str, extra: str = "") -> str:
    """The stamp text (an HTML comment, invisible in the GitHub UI)."""
    suffix = f":{extra}" if extra else ""
    return f"<!-- agentic-sdlc:{kind}:{sha}{suffix} -->"


def find_marker(comments: list[dict], stamp: str) -> int | None:
    """Index of the first comment carrying `stamp`, else None."""
    for index, comment in enumerate(comments):
        if stamp in comment["body"]:
            return index
    return None
