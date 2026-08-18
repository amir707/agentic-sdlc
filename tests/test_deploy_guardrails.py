"""Deploy failures degrade and never leak secrets (live-found, PR #33 of
candidate-app-2 era: a preprod gcloud failure killed the whole sprint AND
echoed CONFIG_TOKEN in the traceback)."""

import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from adapters import deploy
from orchestrator import driver, pr_markers
from sdlc_steps import preprod_ci


def test_failed_gcloud_command_redacts_secrets(monkeypatch):
    def boom(args, check):
        raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(deploy.subprocess, "run", boom)

    with pytest.raises(deploy.DeployError) as exc:
        deploy._run(["gcloud", "run", "deploy", "svc",
                     "--set-env-vars", "CONFIG_TOKEN=supersecret123"])
    message = str(exc.value)
    assert "supersecret123" not in message
    assert "CONFIG_TOKEN=<redacted>" in message
    assert exc.value.__context__ is None  # raw command fully dropped


def test_deploy_error_fails_the_item_not_the_run(monkeypatch):
    """run_preprod_ci turns a DeployError into ci-failed (False) with an
    audited, redacted reason — the workflow routes it to `failed` and the
    sprint continues to the next item."""
    def boom(pr, checkout, areas, project):
        raise deploy.DeployError("command failed (exit 1): gcloud run "
                                 "deploy x --set-env-vars "
                                 "CONFIG_TOKEN=<redacted>")
    monkeypatch.setattr(preprod_ci, "run_preprod", boom)

    audits = []

    class Ctx:
        repo_host = SimpleNamespace(
            get_pr=lambda self_or_pr, pr=None: {"head_sha": "a" * 40},
            get_review_threads=lambda pr: [])
        board = SimpleNamespace(begin=lambda *a, **k: None,
                                finish=lambda *a, **k: None)

        async def audit(self, actor, decision, factors):
            audits.append((actor, decision, factors))

    ctx = Ctx()
    ctx.repo_host = SimpleNamespace(get_pr=lambda pr: {"head_sha": "a" * 40},
                                    get_review_threads=lambda pr: [])
    ctx.workspace = SimpleNamespace(dir="/tmp/checkout")
    ctx.project = SimpleNamespace(name="p")
    ok = asyncio.run(driver.run_preprod_ci(
        ctx, {"id": "PAY-1"}, 7, SimpleNamespace(areas={"payments"},
                                                 primary_area="payments")))
    assert ok is False
    assert any(d == "preprod_result" and f["passed"] is False
               and "<redacted>" in f["error"] for _, d, f in audits)


def test_transient_deploy_failure_retries_once(monkeypatch):
    """The fresh-project first-deploy race ('Requested entity was not
    found') gets ONE retry — observed live: the identical rerun
    succeeded. Bounded, like every loop."""
    calls = []

    def fake_execute(args):
        calls.append(1)
        if len(calls) == 1:
            return 1, "ERROR: NOT_FOUND: Requested entity was not found."
        return 0, ""
    monkeypatch.setattr(deploy, "_execute", fake_execute)
    monkeypatch.setattr(deploy.time, "sleep", lambda s: None)

    deploy._run(["gcloud", "run", "deploy", "svc"])  # must not raise
    assert len(calls) == 2


def test_config_errors_fail_fast_without_retry(monkeypatch):
    """A non-transient failure (config/build error) never retries — a
    doubled 3-minute build on a genuine failure helps nobody."""
    calls = []

    def fake_execute(args):
        calls.append(1)
        return 1, "ERROR: --no-traffic not supported when creating a new service."
    monkeypatch.setattr(deploy, "_execute", fake_execute)

    with pytest.raises(deploy.DeployError) as exc:
        deploy._run(["gcloud", "run", "deploy", "svc"])
    assert len(calls) == 1
    assert "--no-traffic not supported" in str(exc.value)  # the WHY surfaces


def test_transient_retry_exhaustion_reports_redacted(monkeypatch):
    def fake_execute(args):
        return 1, "ERROR: UNAVAILABLE. CONFIG_TOKEN=leakyvalue in output"
    monkeypatch.setattr(deploy, "_execute", fake_execute)
    monkeypatch.setattr(deploy.time, "sleep", lambda s: None)

    with pytest.raises(deploy.DeployError) as exc:
        deploy._run(["gcloud", "x", "--set-env-vars", "CONFIG_TOKEN=leakyvalue"])
    assert "leakyvalue" not in str(exc.value)
    assert "CONFIG_TOKEN=<redacted>" in str(exc.value)


def test_promote_failure_after_merge_escalates_not_crashes(monkeypatch):
    """The half-released state: merge landed, traffic shift failed. Must
    escalate the item with a manual-promote instruction — never kill the
    release pass after a merge."""
    sha = "a" * 40
    marker = pr_markers.marker("ci", sha, "passed")

    class Verified:
        needs_flag = False
        primary_area = "payments"
        verified_risk = "low"
        flag = {"covered": True}
        radius = set()
    monkeypatch.setattr(driver, "verify_once", _async_return(Verified()))
    monkeypatch.setattr(driver.schemas.ReleaseDecision, "model_validate",
                        classmethod(lambda cls, d: SimpleNamespace(
                            action="merge", reasoning="ok", factors={})))
    monkeypatch.setattr(driver, "extract_json", lambda text: {})

    def promote_boom(tag):
        raise deploy.DeployError("command failed (exit 1): gcloud ... "
                                 "— gcloud said: UNAVAILABLE")
    monkeypatch.setattr(deploy, "promote", promote_boom)

    audits, statuses = [], []

    class Ctx:
        repo_host = SimpleNamespace(
            get_pr=lambda pr: {"head_sha": sha, "head_ref": "b"},
            get_review_threads=lambda pr: [{"body": marker}],
            merge_pr=lambda pr: "merged-sha")
        workspace = SimpleNamespace(checkout_detached=lambda ref: None)
        board = SimpleNamespace(begin=lambda *a, **k: None,
                                finish=lambda *a, **k: None)
        project = SimpleNamespace(
            policy=lambda step: {"deploy_confidence_minutes": 10},
            prompt=lambda step: "release-manager instructions")

        async def invoke(self, spec, message):
            return SimpleNamespace(text="{}")

        async def audit(self, actor, decision, factors):
            audits.append((actor, decision, factors))

        async def set_status(self, item_id, status, pr=None):
            statuses.append((item_id, status, pr))

    outcome = asyncio.run(driver.decide_release_pr(
        Ctx(), {"id": "PAY-1", "pr": 7}, confidence=10))
    assert outcome == "escalated"
    assert ("PAY-1", "escalated", 7) in statuses
    assert any("traffic shift failed" in f.get("rule", "")
               for _, _, f in audits)


def _async_return(value):
    async def fn(*a, **k):
        return value
    return fn
