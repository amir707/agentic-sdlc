"""One-line summaries for the failures operators actually hit.

The orchestrator's top level catches everything, prints ONE actionable
line (what happened + what to do), and exits — full tracebacks are
opt-in via --debug. Runs are resumable from the store, so a crisp
failure message plus "rerun" is almost always the whole remedy.
"""

import re


def _leaf(exc: BaseException) -> BaseException:
    """Unwrap ExceptionGroups/causes to the most informative leaf."""
    while True:
        if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
            exc = exc.exceptions[0]
            continue
        if exc.__cause__ is not None and not str(exc):
            exc = exc.__cause__
            continue
        return exc


def one_line(exc: BaseException) -> str:
    leaf = _leaf(exc)
    text = f"{leaf}"

    if "429" in text or "RESOURCE_EXHAUSTED" in text:
        retry = re.search(r"retry in ([0-9.]+)s", text, re.IGNORECASE)
        hint = f" (provider says retry in {float(retry.group(1)):.0f}s)" \
            if retry else ""
        if "PerDay" in text:
            return ("model DAILY free-tier quota exhausted (429) — retries "
                    "cannot wait this out today: enable billing on the "
                    "key's project, or set GEMINI_MODEL/REVIEWER_MODEL to a "
                    "model with remaining quota")
        return ("model rate limit (429) persisted past all retries" + hint +
                " — slow down (sequential, not --parallel) or enable billing")

    if "ConnectError" in type(leaf).__name__ or "Connection refused" in text \
            or "connect" in text.lower() and "127.0.0.1" in text:
        return ("delivery store unreachable — is `make mcp` running? "
                "(localhost:8787)")

    if "API key not valid" in text or "API_KEY_INVALID" in text \
            or "401" in text and "anthropic" in text.lower():
        return "model API key invalid — check .env (setup.py validates them)"

    if type(leaf).__name__ == "KeyError" and any(
            k in text for k in ("GITHUB_TOKEN", "MCP_TOKEN", "CANDIDATE_APP")):
        return (f"missing environment variable {text} — fill it via "
                "scripts/setup.py")

    if type(leaf).__name__ in ("RepoHostError", "ConfigError", "StoreError"):
        return text[:300]

    first_line = text.strip().splitlines()[0] if text.strip() else ""
    return f"{type(leaf).__name__}: {first_line[:250]}"
