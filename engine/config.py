"""Project bundle loading: prompts and policies via the overlay pattern.

The engine is project-agnostic. Everything about one governed project
lives under config/projects/<name>/, mirroring the root sdlc-steps/
hierarchy:

- prompt for a step   = sdlc-steps/<step>/prompts.md
                        + config/projects/<name>/sdlc-steps/<step>/customised-prompt.md (if present)
- policy for a step   = sdlc-steps/policy.yaml            (shared defaults)
                        <- sdlc-steps/<step>/policy.yaml   (step defaults)
                        <- config/.../sdlc-steps/policy.yaml        (project shared overrides)
                        <- config/.../sdlc-steps/<step>/policy.yaml (project step overrides)
                        (later layers deep-merge over earlier ones)

`load_project` validates the bundle up front so a malformed config
fails at load, not mid-sprint.
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SDLC_STEPS = ROOT / "sdlc-steps"
PROJECTS = ROOT / "config" / "projects"


class ConfigError(Exception):
    """Malformed project bundle; message includes a remediation hint."""


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text())
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return data


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@dataclass
class ProjectConfig:
    name: str
    repo: str
    service: str
    areas: dict[str, list[str]]
    default_area: str
    project_dir: Path
    _policies: dict[str, dict] = field(default_factory=dict)

    def policy(self, step: str) -> dict:
        """Resolved policy for one step (shared + step + project overlays)."""
        if step not in self._policies:
            layers = [
                _read_yaml(SDLC_STEPS / "policy.yaml"),
                _read_yaml(SDLC_STEPS / step / "policy.yaml"),
                _read_yaml(self.project_dir / "sdlc-steps" / "policy.yaml"),
                _read_yaml(self.project_dir / "sdlc-steps" / step / "policy.yaml"),
            ]
            merged: dict = {}
            for layer in layers:
                merged = _deep_merge(merged, layer)
            self._policies[step] = merged
        return self._policies[step]

    def prompt(self, step: str) -> str:
        """Composed prompt: engine base first, project customisation after.

        The base opens with core rules; the customised prompt extends
        and cannot override (stated in the base itself).
        """
        base = SDLC_STEPS / step / "prompts.md"
        if not base.exists():
            raise ConfigError(f"no base prompt for step {step!r} ({base})")
        parts = [base.read_text()]
        custom = (self.project_dir / "sdlc-steps" / step / "customised-prompt.md")
        if custom.exists():
            parts.append(custom.read_text())
        return "\n\n".join(parts)

    def area_for(self, file_path: str) -> str:
        """Deterministic module-to-area mapping (first matching prefix)."""
        for area, prefixes in self.areas.items():
            if any(file_path.startswith(prefix) for prefix in prefixes):
                return area
        return self.default_area

    def backlog_file(self) -> Path:
        return self.project_dir / "backlog.json"


def load_project(name: str) -> ProjectConfig:
    project_dir = PROJECTS / name
    if not project_dir.is_dir():
        raise ConfigError(
            f"no project {name!r} under {PROJECTS} — create it with scripts/setup.py")

    definition = _read_yaml(project_dir / "project.yaml")
    for key in ("repo", "areas", "default_area"):
        if key not in definition:
            raise ConfigError(
                f"{project_dir / 'project.yaml'} missing required key {key!r}")

    config = ProjectConfig(
        name=name,
        repo=definition["repo"],
        service=definition.get("cloud_run", {}).get("service", name),
        areas=definition["areas"],
        default_area=definition["default_area"],
        project_dir=project_dir,
    )
    _validate(config)
    return config


def _validate(config: ProjectConfig) -> None:
    """Fail fast on the mistakes that would otherwise surface mid-sprint."""
    if not config.policy("approver").get("approvers"):
        raise ConfigError(
            f"project {config.name!r} has no approvers — set them in "
            f"config/projects/{config.name}/sdlc-steps/approver/policy.yaml")

    packer = config.policy("sprint-packer")
    for key in ("risk_points", "risk_budget", "token_budget", "reviewer_capacity"):
        if key not in packer:
            raise ConfigError(f"sprint-packer policy missing {key!r}")

    monitor = config.policy("monitor")
    for key in ("probe_interval_seconds", "window_seconds",
                "error_threshold", "resolver_recovery_windows"):
        if key not in monitor:
            raise ConfigError(f"monitor policy missing {key!r}")

    flow = config.policy("orchestrator")
    for key in ("max_fix_iterations", "max_flag_fix_iterations"):
        if key not in flow:
            raise ConfigError(f"orchestrator policy missing {key!r}")

    if config.policy("code-reviewer").get("flag_required_min_risk") is None:
        raise ConfigError("shared policy missing flag_required_min_risk")

    for step in ("risk-assessor", "coder", "code-reviewer",
                 "approver", "release-manager"):
        config.prompt(step)  # raises if a base prompt is missing
