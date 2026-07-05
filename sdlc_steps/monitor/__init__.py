#!/usr/bin/env python3
"""Synthetic monitor (deterministic prober, NOT an agent).

Probes the LIVE service's area endpoints the way a user would — nothing
notifies it; it discovers degradation by looking. Every window it
writes one health sample per area to the store; when an area's error
rate crosses the threshold for a full window it opens an incident.

The monitor NEVER resolves incidents: closure belongs to the resolver
(separate concern, separate role token). The chaos flag and this
monitor never communicate directly; the live service is the medium.

Run: python -m sdlc_steps.monitor --url <live-url> [--project candidate-app]
"""

import argparse
import asyncio
import time
from collections import deque
from pathlib import Path

import httpx

from orchestrator.config import load_project          # noqa: E402
from adapters.store_client import DeliveryStore   # noqa: E402


def window_error_rate(samples: deque, window_seconds: float,
                      now: float) -> float | None:
    """Error rate over the trailing window; None with no samples.
    Pure function so the threshold behavior is unit-testable."""
    relevant = [ok for ts, ok in samples if ts >= now - window_seconds]
    if not relevant:
        return None
    return 1 - (sum(relevant) / len(relevant))


async def probe_loop(url: str, project, store: DeliveryStore) -> None:
    policy = project.policy("monitor")
    interval = float(policy["probe_interval_seconds"])
    window = float(policy["window_seconds"])
    threshold = float(policy["error_threshold"])

    samples: dict[str, deque] = {a: deque(maxlen=1000)
                                 for a in project.smoke_endpoints}
    last_window_close = time.monotonic()

    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            now = time.monotonic()
            for area, endpoint in project.smoke_endpoints.items():
                try:
                    resp = await client.get(url + endpoint)
                    ok = resp.status_code == 200
                except httpx.HTTPError:
                    ok = False
                samples[area].append((now, ok))

            if now - last_window_close >= window:
                last_window_close = now
                for area in project.smoke_endpoints:
                    rate = window_error_rate(samples[area], window, now)
                    if rate is None:
                        continue
                    await store.call("record_health_sample",
                                     area=area, error_rate=round(rate, 3))
                    marker = "DEGRADED" if rate > threshold else "ok"
                    print(f"[monitor] {area}: error_rate={rate:.2f} {marker}",
                          flush=True)
                    if rate > threshold:
                        incident = await store.call(
                            "open_incident", area=area,
                            error_rate=round(rate, 3))
                        print(f"[monitor] incident #{incident['id']} open "
                              f"for {area}", flush=True)

            await asyncio.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="live service base URL")
    parser.add_argument("--project", default="candidate-app")
    args = parser.parse_args()

    project = load_project(args.project)
    store = DeliveryStore.for_monitor()
    print(f"[monitor] probing {args.url} for areas "
          f"{sorted(project.smoke_endpoints)}", flush=True)
    asyncio.run(probe_loop(args.url.rstrip("/"), project, store))


if __name__ == "__main__":
    main()
