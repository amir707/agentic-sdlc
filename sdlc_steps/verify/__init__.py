"""Verify + label (deterministic with a thin check, NOT an agent).

Runs after the review fix-loop converges (never in parallel: labels on
a moving diff are stale). The claimed-vs-actual guardrail:

1. Measure the diff: files touched, dependency closure (blast radius),
   flag coverage — all deterministic.
2. Recompute risk from what the change ACTUALLY touches, floor rules
   from the verify policy: closure touching a sensitive area or a wide
   radius means at least medium. Verified risk only ever goes UP from
   the claim (a mismatch escalates; verify never launders a high claim
   down).
3. Apply flag policy against VERIFIED risk (the reviewer already
   applied it against claimed risk — two independent checks): if
   verified risk meets flag_required_min_risk and no flag gates the new
   behavior, the PR must go back to its author (reason
   policy_flag_required).
4. Emit the verified labels for the PR title:
   [area:payments][risk:medium][flag:yes].
"""

from dataclasses import dataclass

from engine.dependency_graph import blast_radius
from engine.diff_analysis import files_touched, flag_coverage

_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclass
class VerifyResult:
    verified_risk: str
    claimed_risk: str
    escalated: bool
    escalation_reason: str | None
    areas: set[str]
    primary_area: str
    radius: set[str]
    flag: dict
    needs_flag: bool          # policy violated -> reject(policy_flag_required)
    title_prefix: str


def _module_to_path(module: str) -> str:
    return module if "/" in module or "." not in module \
        else module.replace(".", "/") + ".py"


def verify(diff_text: str, claimed_risk: str, project,
           checkout_dir: str) -> VerifyResult:
    files = files_touched(diff_text)
    radius = blast_radius(checkout_dir, files)
    radius_paths = {_module_to_path(m) for m in radius}
    areas = {project.area_for(p) for p in radius_paths} or {project.default_area}

    policy = project.policy("verify")
    sensitive = set(policy.get("sensitive_areas", []))
    wide = int(policy.get("wide_radius_modules", 4))

    # Deterministic risk floors from actual impact.
    floor, reason = "low", None
    touched_sensitive = areas & sensitive
    if touched_sensitive:
        floor = "medium"
        reason = f"dependency closure touches sensitive area(s): {sorted(touched_sensitive)}"
    if len(radius) >= wide:
        floor = "medium"
        reason = (reason + "; " if reason else "") + \
            f"wide blast radius ({len(radius)} modules)"

    escalated = _RISK_ORDER[floor] > _RISK_ORDER[claimed_risk]
    verified = floor if escalated else claimed_risk

    flag = flag_coverage(diff_text)
    threshold = project.policy("verify")["flag_required_min_risk"]
    needs_flag = (_RISK_ORDER[verified] >= _RISK_ORDER[threshold]
                  and not flag["covered"])

    # Primary label area: the most specific non-default area, else default.
    non_core = sorted(areas - {project.default_area})
    primary = non_core[0] if non_core else project.default_area

    title_prefix = (f"[area:{primary}]"
                    f"[risk:{verified}]"
                    f"[flag:{'yes' if flag['covered'] else 'no'}]")

    return VerifyResult(
        verified_risk=verified, claimed_risk=claimed_risk,
        escalated=escalated, escalation_reason=reason if escalated else None,
        areas=areas, primary_area=primary, radius=radius, flag=flag,
        needs_flag=needs_flag, title_prefix=title_prefix)
