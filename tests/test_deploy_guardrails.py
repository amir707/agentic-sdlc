"""Deploy failures degrade and never leak secrets (live-found, PR #33 of
candidate-app-2 era: a preprod gcloud failure killed the whole sprint AND
echoed CONFIG_TOKEN in the traceback)."""

import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from adapters import deploy
from orchestrator import driver
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
