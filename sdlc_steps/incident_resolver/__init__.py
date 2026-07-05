"""Incident resolver (deterministic tool, NOT an agent).

Closure is a separate concern from detection: the monitor opens, only
this resolver — holding its own role token — closes, and only after
SUSTAINED recovery: the last resolver_recovery_windows health samples
for the area must all be at or below the error threshold (hysteresis,
so a flapping service cannot close its own incident). Every resolution
is audited with the recovery evidence.

Invoked by the orchestrator before each release_manager pass, and by
the demo loop periodically.
"""

from adapters.store_client import DeliveryStore


def decide(open_incidents: list[dict], samples_by_area: dict[str, list[dict]],
           policy: dict) -> list[dict]:
    """Pure decision: which incidents have earned resolution.
    Returns [{incident, factors}] — the caller applies them."""
    needed = int(policy["resolver_recovery_windows"])
    threshold = float(policy["error_threshold"])
    resolutions = []
    for incident in open_incidents:
        samples = samples_by_area.get(incident["area"], [])
        recent = samples[-needed:]
        if len(recent) < needed:
            continue
        rates = [s["error_rate"] for s in recent]
        if all(rate <= threshold for rate in rates):
            resolutions.append({
                "incident": incident,
                "factors": {
                    "area": incident["area"],
                    "healthy_windows": needed,
                    "recent_error_rates": rates,
                    "threshold": threshold,
                    "rule": f"{needed} consecutive windows at or below threshold",
                },
            })
    return resolutions


async def run(project, store: DeliveryStore | None = None) -> list[dict]:
    """Fetch state, decide, resolve. Returns the applied resolutions."""
    store = store or DeliveryStore.for_resolver()
    policy = project.policy("monitor")
    open_incidents = await store.call("list_open_incidents")
    if not open_incidents:
        return []

    lookback = int(policy["resolver_recovery_windows"]) \
        * int(policy["window_seconds"]) * 3  # generous fetch window
    samples_by_area = {
        incident["area"]: await store.call(
            "list_health_samples", area=incident["area"],
            window_seconds=lookback)
        for incident in open_incidents
    }

    applied = []
    for resolution in decide(open_incidents, samples_by_area, policy):
        incident = resolution["incident"]
        await store.call("resolve_incident", incident_id=incident["id"],
                         factors=resolution["factors"])
        print(f"[resolver] resolved incident #{incident['id']} "
              f"({incident['area']})", flush=True)
        applied.append(resolution)
    return applied
