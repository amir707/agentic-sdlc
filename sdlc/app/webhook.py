"""GitHub webhook → trigger: the real event source the heartbeat stood in for.

A resident service (sprint or release) mounts ONE extra route,
POST /webhooks/github. GitHub delivers a signed payload; this module
verifies the HMAC, decides whether the event is one the pipeline reacts
to, and — for the ones that are — produces the SAME Pub/Sub-shaped nudge
the heartbeat produces (sdlc.engine.heartbeat.post_event) at each
configured trigger URL. The consumer stays event-driven and idempotent:
this is an event PRODUCER, not orchestration logic (the same principle
that made the heartbeat retirable — with webhooks wired, run the sprint
service with --heartbeat-minutes 0; approvals wake it).

What reacts, and why:
- issue_comment/created on a PR whose body is a gate command
  (/approve, /reject, /hold) — the gate's decision landed; give the
  awaiting gate its one authenticated look NOW instead of at the next
  heartbeat. Dispatched to every target: an approval also queues an
  item the release pass should decide.
- pull_request/synchronize|reopened — a new head; the sprint re-verifies.
- ping — GitHub's hook-creation handshake: 200, no dispatch.
Everything else is acknowledged (202) and ignored.

Authority stays where it was: the webhook only nudges; the gate still
reads the allowlisted comment itself (ADR-0005). A forged nudge costs
one idempotent pass — hence the HMAC still, so a stranger cannot even
spend that.

Delivery is fire-and-forget: GitHub times a webhook out at 10 s and a
sprint pass can take minutes, so the nudge is posted in a background
task and 202 returns immediately (a failed post is only a missed nudge —
the heartbeat/Scheduler retries the state).
"""

import asyncio
import hashlib
import hmac
import json

from sdlc.engine.heartbeat import post_event
from sdlc.engine.narrate import say
from sdlc.governance.gate import parse_command

WEBHOOK_PATH = "/webhooks/github"


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256: 'sha256=<hex hmac of the raw body>'."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


def classify(event: str, payload: dict) -> str | None:
    """The event name to dispatch, or None if the pipeline does not react.
    Pure: no I/O, so the rules are unit-testable."""
    if event == "issue_comment":
        if payload.get("action") != "created":
            return None
        issue = payload.get("issue") or {}
        if "pull_request" not in issue:       # GitHub marks PR comments by
            return None                       # presence of this key
        body = (payload.get("comment") or {}).get("body") or ""
        if parse_command(body) is None:
            return None                       # not a gate command
        return f"github:comment:{issue.get('number')}"
    if event == "pull_request":
        if payload.get("action") in ("synchronize", "reopened"):
            return f"github:pr_{payload['action']}:{payload.get('number')}"
        return None
    return None


def mount_github_webhook(app, *, secret: str, targets: list[str]) -> None:
    """Add POST /webhooks/github to a FastAPI/Starlette app. `targets`
    are trigger URLs (the service's own /apps/<name>/trigger/pubsub and,
    for the sprint, the release service's) that receive the nudge."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    async def github_webhook(request: Request):
        body = await request.body()
        if not verify_signature(secret, body,
                                request.headers.get("X-Hub-Signature-256")):
            say("webhook", "rejected a delivery with a bad or missing "
                "signature", level="warn")
            return JSONResponse({"error": "bad signature"}, status_code=401)
        event = request.headers.get("X-GitHub-Event", "")
        if event == "ping":
            return JSONResponse({"pong": True})
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return JSONResponse({"error": "not json"}, status_code=400)
        name = classify(event, payload)
        if name is None:
            return JSONResponse({"ignored": event}, status_code=202)
        say("webhook", f"{name} — nudging {len(targets)} trigger(s)",
            event=event, targets=targets)
        for url in targets:
            asyncio.create_task(post_event(url, name))
        return JSONResponse({"dispatched": name, "targets": len(targets)},
                            status_code=202)

    app.add_api_route(WEBHOOK_PATH, github_webhook, methods=["POST"])
