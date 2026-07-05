"""Preprod CI (deterministic script, NOT an agent).

For a converged, verified PR branch: run the project's tests on the
checkout, deploy a tagged traffic-less Cloud Run revision (tag =
pr-<number>; the tag URL is the preprod endpoint), and smoke-test the
live tag URL — /health plus the changed areas' endpoints. Returns the
evidence the approver's dossier cites; the orchestrator posts it as a
PR comment and records it in the store.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

from engine import deploy


@dataclass
class CIResult:
    passed: bool
    revision_tag: str
    preprod_url: str
    commit_sha: str
    tests: str            # "passed" | tail of failing output
    smoke: dict[str, str]  # endpoint -> "200" | error


def run_preprod(pr: int, checkout_dir: str, areas: set[str],
                project) -> CIResult:
    checkout = Path(checkout_dir)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout,
                         capture_output=True, text=True).stdout.strip()

    # 1. Tests on the exact tree being deployed.
    proc = subprocess.run(
        [str(checkout / ".venv" / "bin" / "python"), "-m", "pytest", "-q"],
        cwd=checkout, capture_output=True, text=True, timeout=600)
    tests_ok = proc.returncode == 0
    tests = "passed" if tests_ok else (proc.stdout + proc.stderr)[-2000:]
    if not tests_ok:
        return CIResult(False, f"pr-{pr}", "", sha, tests, {})

    # 2. Tagged, traffic-less revision.
    preprod_url = deploy.deploy_preprod(pr, str(checkout))

    # 3. Smoke the live tag URL: health always, plus each changed area.
    endpoints = {"/health"}
    smoke_map = project.smoke_endpoints
    endpoints |= {smoke_map[a] for a in areas if a in smoke_map}
    smoke: dict[str, str] = {}
    ok = True
    for endpoint in sorted(endpoints):
        try:
            resp = httpx.get(preprod_url + endpoint, timeout=20)
            smoke[endpoint] = str(resp.status_code)
            ok &= resp.status_code == 200
        except httpx.HTTPError as exc:
            smoke[endpoint] = type(exc).__name__
            ok = False

    return CIResult(ok, f"pr-{pr}", preprod_url, sha, tests, smoke)


def format_comment(result: CIResult) -> str:
    """The PR comment that makes the PR carry its own evidence."""
    smoke_lines = "\n".join(f"- `{e}` → {s}" for e, s in result.smoke.items())
    status = "✅ PASSED" if result.passed else "❌ FAILED"
    return (f"**Preprod CI {status}**\n\n"
            f"- commit: `{result.commit_sha}`\n"
            f"- revision tag: `{result.revision_tag}`\n"
            f"- preprod URL: {result.preprod_url or 'n/a'}\n"
            f"- tests: {result.tests if result.tests == 'passed' else 'FAILED'}\n"
            f"{smoke_lines}")
