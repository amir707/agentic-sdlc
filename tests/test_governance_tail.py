"""Governance-tail tests: verify+label recomputation, monitor window
math, resolver hysteresis. All pure logic — no network, no keys."""

import textwrap
from collections import deque

from orchestrator.config import load_project
from sdlc_steps.monitor import window_error_rate
from sdlc_steps.incident_resolver import decide
from sdlc_steps.verify import verify


def _payments_repo(tmp_path):
    """Minimal candidate-app shape for blast radius computation."""
    app = tmp_path / "app"
    app.mkdir()
    (app / "__init__.py").write_text("")
    (app / "flags.py").write_text("def enabled(name): return False\n")
    (app / "payments.py").write_text("from app import flags\n")
    (app / "catalog.py").write_text("")
    (app / "main.py").write_text("from app import payments\nfrom app import catalog\n")
    return tmp_path


UNFLAGGED_PAYMENTS_DIFF = textwrap.dedent("""\
    diff --git a/app/payments.py b/app/payments.py
    --- a/app/payments.py
    +++ b/app/payments.py
    @@ -1,3 +1,5 @@
    +    summary["service_fee"] = round(captured * 0.015, 2)
    """)

FLAGGED_PAYMENTS_DIFF = textwrap.dedent("""\
    diff --git a/app/payments.py b/app/payments.py
    --- a/app/payments.py
    +++ b/app/payments.py
    @@ -1,3 +1,6 @@
    +    if flags.enabled("payments_service_fee"):
    +        summary["service_fee"] = round(captured * 0.015, 2)
    diff --git a/flags.json b/flags.json
    --- a/flags.json
    +++ b/flags.json
    @@ -1,2 +1,3 @@
    +  "payments_service_fee": false,
    """)

CATALOG_DIFF = textwrap.dedent("""\
    diff --git a/app/catalog.py b/app/catalog.py
    --- a/app/catalog.py
    +++ b/app/catalog.py
    @@ -1,3 +1,4 @@
    +COUNT = True
    """)


def test_verify_escalates_claimed_low_payments_change(tmp_path):
    """The PAY-102 trap: claimed low, touches the payments path,
    unflagged -> escalate to medium AND fire the flag invariant."""
    project = load_project("candidate-app")
    result = verify(UNFLAGGED_PAYMENTS_DIFF, "low", project,
                    _payments_repo(tmp_path))
    assert result.escalated and result.verified_risk == "medium"
    assert "payments" in result.escalation_reason
    assert result.needs_flag                      # policy_flag_required
    assert result.title_prefix == "[area:payments][risk:medium][flag:no]"


def test_verify_passes_flagged_change_after_fix(tmp_path):
    project = load_project("candidate-app")
    result = verify(FLAGGED_PAYMENTS_DIFF, "low", project,
                    _payments_repo(tmp_path))
    assert result.verified_risk == "medium" and result.escalated
    assert not result.needs_flag                  # flag now gates it
    assert result.title_prefix == "[area:payments][risk:medium][flag:yes]"


def test_verify_never_lowers_a_claim(tmp_path):
    """A high claim on a small catalog change stays high — verify
    escalates mismatches, it never launders risk down."""
    project = load_project("candidate-app")
    result = verify(CATALOG_DIFF, "high", project, _payments_repo(tmp_path))
    assert result.verified_risk == "high" and not result.escalated
    assert result.needs_flag  # high + unflagged still violates policy


def test_verify_low_catalog_change_stays_low(tmp_path):
    project = load_project("candidate-app")
    result = verify(CATALOG_DIFF, "low", project, _payments_repo(tmp_path))
    assert result.verified_risk == "low"
    assert not result.needs_flag
    assert result.title_prefix == "[area:catalog][risk:low][flag:no]"


# --- monitor window math -----------------------------------------------------

def test_window_error_rate():
    now = 100.0
    samples = deque([(now - 20, True), (now - 10, False), (now - 5, False),
                     (now - 1, True)])
    # window=15 sees fail, fail, ok -> 2/3 errors
    assert window_error_rate(samples, 15, now) == 1 - (1 / 3)
    assert window_error_rate(samples, 30, now) == 0.5
    assert window_error_rate(deque(), 15, now) is None


# --- resolver hysteresis -----------------------------------------------------

_POLICY = {"resolver_recovery_windows": 2, "error_threshold": 0.3,
           "window_seconds": 15}


def _incident():
    return {"id": 3, "area": "payments", "status": "open", "error_rate": 0.47}


def test_resolver_waits_for_consecutive_healthy_windows():
    # Only one healthy window: not yet.
    samples = {"payments": [{"error_rate": 0.5}, {"error_rate": 0.1}]}
    assert decide([_incident()], samples, _POLICY) == []
    # Healthy then unhealthy: the flap resets nothing to resolve.
    samples = {"payments": [{"error_rate": 0.1}, {"error_rate": 0.6}]}
    assert decide([_incident()], samples, _POLICY) == []


def test_resolver_resolves_after_sustained_recovery():
    samples = {"payments": [{"error_rate": 0.6}, {"error_rate": 0.2},
                            {"error_rate": 0.0}]}
    resolutions = decide([_incident()], samples, _POLICY)
    assert len(resolutions) == 1
    factors = resolutions[0]["factors"]
    assert factors["recent_error_rates"] == [0.2, 0.0]
    assert factors["healthy_windows"] == 2


def test_resolver_needs_enough_samples():
    samples = {"payments": [{"error_rate": 0.0}]}
    assert decide([_incident()], samples, _POLICY) == []


def test_verified_label_trusts_assessed_over_claim(tmp_path):
    """An overstated PM claim (high) on a small catalog change labels at
    the governor's own assessed risk (medium) — claims can overstate as
    well as understate; assessed + actual outrank them."""
    project = load_project("candidate-app")
    result = verify(CATALOG_DIFF, "high", project, _payments_repo(tmp_path),
                    assessed_risk="medium")
    assert result.verified_risk == "medium"
    assert not result.escalated          # the claim did not UNDERstate


def test_trap_escalates_even_when_assessor_saw_it_coming(tmp_path):
    """PAY-102 drift-proofing: even if the assessor rates the trap item
    medium, the escalation still fires because the CLAIM said low —
    claimed-vs-actual is the story, independent of assessor judgment."""
    project = load_project("candidate-app")
    result = verify(UNFLAGGED_PAYMENTS_DIFF, "low", project,
                    _payments_repo(tmp_path), assessed_risk="medium")
    assert result.verified_risk == "medium"
    assert result.escalated              # claimed low < verified medium
    assert result.needs_flag
