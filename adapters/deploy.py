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
    return _env("CLOUD_RUN_SERVICE", "candidate-app")


def _run(args: list[str]) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, check=True)


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
        "--source", _env("CANDIDATE_APP_DIR"),
        "--allow-unauthenticated",
        "--min-instances", "1", "--max-instances", "1",
        # CONFIG_TOKEN protects the chaos endpoint; value comes from the
        # local .env, never from code.
        "--set-env-vars", f"CONFIG_TOKEN={_env('CONFIG_TOKEN')}",
        *_base_args(),
    ])
    url = service_url()
    print(f"baseline live at {url}")
    return url


def deploy_preprod(pr: int, source_dir: str | None = None) -> str:
    """Deploy a tagged, traffic-less revision for a PR branch checkout."""
    tag = f"pr-{pr}"
    _run([
        "gcloud", "run", "deploy", _service(),
        "--source", source_dir or _env("CANDIDATE_APP_DIR"),
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
