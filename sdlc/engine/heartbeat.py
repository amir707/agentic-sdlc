"""Internal heartbeat: the local stand-in for Cloud Scheduler.

Until the real event sources are wired (GitHub webhook, Cloud Scheduler
→ Pub/Sub), a resident service can produce its own wake-up: an asyncio
task that POSTs a Pub/Sub-shaped message to the service's OWN trigger
endpoint every N minutes. The consumer stays event-driven and idempotent
— the heartbeat is an event PRODUCER, not orchestration logic — so
moving to real infrastructure is deleting the flag (--heartbeat-minutes
0), never rewriting the pass.

Cost model: a beat whose pass finds nothing to consume makes no model
calls (resume markers skip every agent stage) — it costs a store read
and a few repo-host lookups. Beats serialize naturally: the next sleep
starts only after the previous pass responds.
"""

import asyncio
import itertools

import httpx
from sdlc.engine.narrate import say


def _payload(name: str, n: int) -> dict:
    # Pub/Sub push envelope shape; the data is an opaque nudge ("hb").
    return {"message": {"data": "aGI=", "messageId": f"{name}-hb-{n}"},
            "subscription": f"internal-heartbeat/{name}"}


async def _beat_forever(url: str, minutes: float, name: str) -> None:
    for n in itertools.count(1):
        await asyncio.sleep(minutes * 60)
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                await client.post(url, json=_payload(name, n))
        except Exception as exc:  # noqa: BLE001 — a failed beat is only
            # a missed wake-up; the next one (or any real event) retries.
            say("heartbeat", f"{name}: beat {n} failed "
                  f"({type(exc).__name__}: {str(exc)[:80]}) — "
                  "next interval retries")


async def post_event(url: str, name: str) -> None:
    """Deliver ONE Pub/Sub-shaped event to a trigger endpoint (the local
    stand-in for a store/webhook publish). Awaits the receiver's pass so
    the caller's process never exits with the event half-delivered; a
    failure is only a missed nudge — the receiver's own heartbeat
    retries the state soon after."""
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            await client.post(url, json=_payload(name, 1))
    except Exception as exc:  # noqa: BLE001
        say("event", f"{name}: delivery to {url} failed "
              f"({type(exc).__name__}: {str(exc)[:80]}) — the receiver's "
              "heartbeat will pick the state up")


async def _serve(app, host: str, port: int, url: str, minutes: float,
                 name: str) -> None:
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port,
                                           log_level="warning"))
    beat = (asyncio.create_task(_beat_forever(url, minutes, name))
            if minutes > 0 else None)
    try:
        await server.serve()
    finally:
        if beat:
            beat.cancel()


def serve_with_heartbeat(app, host: str, port: int, trigger_path: str,
                         minutes: float, name: str) -> None:
    """Serve the app and (optionally) its internal heartbeat as sibling
    tasks on one loop. minutes<=0 disables the heartbeat — the real
    Scheduler/webhook own the wake-ups then."""
    url = f"http://{host}:{port}{trigger_path}"
    if minutes > 0:
        say("heartbeat", f"{name}: waking every {minutes:g}m -> {url} "
              "(set --heartbeat-minutes 0 once Scheduler/webhook are wired)")
    asyncio.run(_serve(app, host, port, url, minutes, name))
