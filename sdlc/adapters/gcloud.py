#!/usr/bin/env python3
"""Cloud Run deploy wrapper (deterministic tool, not an agent).

The ONLY component that touches cloud credentials: agents never hold
them, they ask the orchestrator to invoke this tool. Uses
`gcloud run deploy --source` so container builds run in Cloud Build
(no local Docker).

Revision/traffic model:
- baseline: deploy serving 100% traffic (demo start state).
- preprod:  deploy a tagged revision with NO traffic; the tag URL is
  the preprod endpoint smoke tests run against (tag = pr-<number>).
- promote:  shift 100% traffic to a tag (the release manager's merge
  action on the service side).

The service runs with exactly one instance (min=max=1): chaos state and
flags.json are per-instance, so a second instance would dilute the
error rate the monitor sees.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from sdlc.engine import redact
from sdlc.ports.world import DeployError  # noqa: F401 (raised here, defined by the core)


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        sys.exit(f"deploy: required env var {name} is not set")
    return value


def _base_args() -> list[str]:
    return [
        "--project", _env("GCP_PROJECT"),
        "--region", _env("GCP_REGION", "australia-southeast2"),
        "--quiet",
    ]


def _service() -> str:
    return _env("CLOUD_RUN_SERVICE")


def _source_dir() -> str:
    value = (os.environ.get("PROJECT_CHECKOUT_DIR")
             or os.environ.get("CANDIDATE_APP_DIR"))  # legacy name
    if not value:
        sys.exit("deploy: PROJECT_CHECKOUT_DIR is not set — provision "
                 "one first (make deploy-baseline does both)")
    return value


# Transient gcloud failure markers — retrying is the correct response.
# "Requested entity was not found" is the fresh-project first-deploy race
# (observed live: repo/service creation racing the deploy itself); the
# rest are provider capacity/timeout flakes. Config and build errors
# match none of these and fail fast.
_TRANSIENT_MARKERS = (
    "Requested entity was not found",
    "UNAVAILABLE",
    "DEADLINE_EXCEEDED",
    "Internal error",
)


def _execute(args: list[str]) -> tuple[int, str]:
    """Run gcloud, STREAMING its stderr (live build progress) while
    keeping a tail for error classification and reporting."""
    from collections import deque

    proc = subprocess.Popen(args, stderr=subprocess.PIPE, text=True)
    tail: deque[str] = deque(maxlen=15)
    assert proc.stderr is not None
    for line in proc.stderr:
        print(line, end="", file=sys.stderr, flush=True)
        tail.append(line)
    proc.wait()
    return proc.returncode, "".join(tail)


def _run(args: list[str], attempts: int = 2) -> None:
    """Bounded, transient-aware execution (the invoker's 429 pattern,
    for infrastructure): a transient failure gets ONE retry; everything
    else fails fast with the redacted command AND gcloud's actual error."""
    print("+", " ".join(redact.env_args(a) for a in args), flush=True)
    for attempt in range(attempts):
        code, stderr_tail = _execute(args)
        if code == 0:
            return
        transient = any(m in stderr_tail for m in _TRANSIENT_MARKERS)
        if transient and attempt < attempts - 1:
            print(f"[deploy] transient failure (attempt {attempt + 1}/"
                  f"{attempts}); retrying in 10s", flush=True)
            time.sleep(10)
            continue
        raise DeployError(
            f"command failed (exit {code}): "
            + " ".join(redact.env_args(a) for a in args)
            + " — gcloud said: " + redact.env_args(stderr_tail.strip()[-500:]))


def _describe() -> dict:
    out = subprocess.run(
        ["gcloud", "run", "services", "describe", _service(),
         "--format", "json", *_base_args()],
        check=True, capture_output=True, text=True,
    ).stdout
    return json.loads(out)


def service_url() -> str:
    return _describe()["status"]["url"]


def tag_url(tag: str) -> str:
    for entry in _describe()["status"].get("traffic", []):
        if entry.get("tag") == tag and entry.get("url"):
            return entry["url"]
    sys.exit(f"deploy: no URL found for tag {tag}")


def deploy_baseline() -> str:
    """Deploy candidate-app source serving 100% traffic."""
    _run([
        "gcloud", "run", "deploy", _service(),
        "--source", _source_dir(),
        "--allow-unauthenticated",
        "--min-instances", "1", "--max-instances", "1",
        # CONFIG_TOKEN protects the chaos endpoint; value comes from the
        # local .env, never from code.
        "--set-env-vars", f"CONFIG_TOKEN={_env('CONFIG_TOKEN')}",
        *_base_args(),
    ])
    # Traffic may be PINNED to a pr-N tag from a previous release —
    # a plain deploy then leaves the new baseline revision at 0%.
    # Baseline means baseline: force traffic back to latest.
    _run([
        "gcloud", "run", "services", "update-traffic", _service(),
        "--to-latest", *_base_args(),
    ])
    url = service_url()
    print(f"baseline live at {url}")
    return url


def deploy_preprod(pr: int, source_dir: str | None = None) -> str:
    """Deploy a tagged, traffic-less revision for a PR branch checkout."""
    tag = f"pr-{pr}"
    _run([
        "gcloud", "run", "deploy", _service(),
        "--source", source_dir or _source_dir(),
        "--no-traffic", "--tag", tag,
        "--min-instances", "1", "--max-instances", "1",
        "--set-env-vars", f"CONFIG_TOKEN={_env('CONFIG_TOKEN')}",
        *_base_args(),
    ])
    url = tag_url(tag)
    print(f"preprod {tag} live at {url}")
    return url


def promote(tag: str) -> None:
    """Shift 100% of traffic to a tagged revision (release action)."""
    _run([
        "gcloud", "run", "services", "update-traffic", _service(),
        "--to-tags", f"{tag}=100",
        *_base_args(),
    ])
    print(f"traffic shifted to {tag}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("baseline")
    p = sub.add_parser("preprod")
    p.add_argument("pr", type=int)
    p.add_argument("--source-dir")
    p = sub.add_parser("promote")
    p.add_argument("tag")
    sub.add_parser("url")

    args = parser.parse_args()
    if args.cmd == "baseline":
        deploy_baseline()
    elif args.cmd == "preprod":
        deploy_preprod(args.pr, args.source_dir)
    elif args.cmd == "promote":
        promote(args.tag)
    elif args.cmd == "url":
        print(service_url())


if __name__ == "__main__":
    main()
