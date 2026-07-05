#!/usr/bin/env python3
"""Interactive installer: a validated, idempotent runbook.

Walks the operator through everything docs/setup-runbook.md records,
VALIDATING after every step (a bad value fails here with a remediation
hint, not mid-sprint) and SKIPPING anything already done — re-running
is always safe.

Steps:
  1. tooling check         (gcloud, gh, git, uv)
  2. engine secrets        (.env: model keys, MCP role tokens)
  3. project secrets       (projects-config/<p>/.env: PAT, GCP, chaos token)
  4. GCP APIs enabled      (run, cloudbuild, artifactregistry)
  5. python env            (.venv + requirements)
  6. seed the store
  7. optional baseline deploy + live smoke

Usage: python3 scripts/setup.py [--project candidate-app]
"""

import argparse
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def say(msg: str) -> None:
    print(f"\n=== {msg}", flush=True)


def ok(msg: str) -> None:
    print(f"  ✅ {msg}", flush=True)


def fail(msg: str, hint: str) -> None:
    print(f"  ❌ {msg}\n     hint: {hint}", flush=True)
    sys.exit(1)


def run(*cmd: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return dict(line.split("=", 1) for line in path.read_text().splitlines()
                if "=" in line and not line.startswith("#"))


def _write_env(path: Path, values: dict[str, str], example: Path) -> None:
    """Fill the example template with values (idempotent merge)."""
    text = example.read_text()
    existing = _read_env(path)
    merged = {**values, **{k: v for k, v in existing.items() if v}}
    lines = []
    for line in text.splitlines():
        key = line.split("=", 1)[0] if "=" in line and not line.startswith("#") else None
        lines.append(f"{key}={merged.get(key, '')}" if key else line)
    path.write_text("\n".join(lines) + "\n")


def _prompt(existing: dict, key: str, label: str, secret_ok: bool = False) -> str:
    if existing.get(key):
        ok(f"{key} already set")
        return existing[key]
    value = input(f"  {label}\n  {key}= ").strip()
    if not value and not secret_ok:
        fail(f"{key} is required", "re-run setup when you have it")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="candidate-app")
    args = parser.parse_args()
    project_dir = ROOT / "projects-config" / args.project

    say("1/7 tooling")
    for tool, hint in (("gcloud", "https://cloud.google.com/sdk/docs/install"),
                       ("gh", "brew install gh && gh auth login"),
                       ("git", "install git"),
                       ("uv", "brew install uv")):
        if shutil.which(tool):
            ok(tool)
        else:
            fail(f"{tool} not found", hint)
    if run("gh", "auth", "status").returncode != 0:
        fail("gh not authenticated", "gh auth login")
    ok("gh authenticated")

    say("2/7 engine secrets (.env)")
    env_path, env_example = ROOT / ".env", ROOT / ".env.example"
    engine = _read_env(env_path)
    values = {}
    values["ANTHROPIC_API_KEY"] = _prompt(engine, "ANTHROPIC_API_KEY",
                                          "Anthropic key (coder model)")
    values["GOOGLE_API_KEY"] = _prompt(engine, "GOOGLE_API_KEY",
                                       "Google AI Studio key (Gemini agents)")
    for token in ("MCP_TOKEN_AGENTS", "MCP_TOKEN_MONITOR", "MCP_TOKEN_RESOLVER"):
        if engine.get(token):
            ok(f"{token} already set")
        else:
            values[token] = secrets.token_urlsafe(24)
            ok(f"{token} generated")
    _write_env(env_path, values, env_example)
    if len({_read_env(env_path)[t] for t in
            ("MCP_TOKEN_AGENTS", "MCP_TOKEN_MONITOR", "MCP_TOKEN_RESOLVER")}) != 3:
        fail("MCP role tokens must be distinct", "clear them in .env and re-run")
    ok(".env written")

    say(f"3/7 project secrets ({project_dir / '.env'})")
    p_path, p_example = project_dir / ".env", project_dir / ".env.example"
    project_env = _read_env(p_path)
    p_values = {}
    p_values["GITHUB_TOKEN"] = _prompt(
        project_env, "GITHUB_TOKEN",
        "fine-grained PAT scoped to the governed repo "
        "(contents + pull requests, read/write)")
    p_values["GCP_PROJECT"] = _prompt(project_env, "GCP_PROJECT",
                                      "GCP project id (billing enabled)")
    p_values["GCP_REGION"] = project_env.get("GCP_REGION") or \
        (input("  GCP_REGION [australia-southeast2]= ").strip()
         or "australia-southeast2")
    if project_env.get("CONFIG_TOKEN"):
        ok("CONFIG_TOKEN already set")
    else:
        p_values["CONFIG_TOKEN"] = secrets.token_urlsafe(24)
        ok("CONFIG_TOKEN generated")
    default_dir = str((ROOT.parent / args.project).resolve())
    p_values["CANDIDATE_APP_DIR"] = project_env.get("CANDIDATE_APP_DIR") or \
        (input(f"  CANDIDATE_APP_DIR [{default_dir}]= ").strip() or default_dir)
    _write_env(p_path, p_values, p_example)
    p_env = _read_env(p_path)

    # validate: PAT can see the repo (naive parse: setup runs under the
    # system python3, which may not have PyYAML)
    repo = next(line.split(":", 1)[1].strip()
                for line in (project_dir / "project.yaml").read_text().splitlines()
                if line.startswith("repo:"))
    probe = run("curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                "-H", f"Authorization: Bearer {p_env['GITHUB_TOKEN']}",
                f"https://api.github.com/repos/{repo}")
    if probe.stdout.strip() != "200":
        fail(f"GITHUB_TOKEN cannot read {repo} (HTTP {probe.stdout.strip()})",
             "check the PAT's repository access + permissions")
    ok(f"PAT can read {repo}")
    if not Path(p_env["CANDIDATE_APP_DIR"]).joinpath(".git").exists():
        fail(f"{p_env['CANDIDATE_APP_DIR']} is not a git checkout",
             f"git clone https://github.com/{repo} {p_env['CANDIDATE_APP_DIR']}")
    ok("candidate checkout present")

    say("4/7 GCP APIs")
    if run("gcloud", "projects", "describe", p_env["GCP_PROJECT"]).returncode != 0:
        fail(f"cannot access GCP project {p_env['GCP_PROJECT']}",
             "gcloud auth login && check the project id")
    enable = run("gcloud", "services", "enable", "run.googleapis.com",
                 "cloudbuild.googleapis.com", "artifactregistry.googleapis.com",
                 "--project", p_env["GCP_PROJECT"], timeout=300)
    if enable.returncode != 0:
        fail("could not enable APIs", enable.stderr[-200:])
    ok("run/cloudbuild/artifactregistry enabled")

    say("5/7 python env")
    if not (ROOT / ".venv").exists():
        run("uv", "venv", "--python", "3.12", str(ROOT / ".venv"))
    install = run("uv", "pip", "install", "-q", "-p",
                  str(ROOT / ".venv" / "bin" / "python"),
                  "-r", str(ROOT / "requirements-dev.txt"), timeout=600)
    if install.returncode != 0:
        fail("dependency install failed", install.stderr[-200:])
    ok(".venv ready")

    say("6/7 seed the store")
    seed = run(str(ROOT / ".venv" / "bin" / "python"),
               str(ROOT / "scripts" / "seed.py"), "--project", args.project)
    if seed.returncode != 0:
        fail("seeding failed", seed.stderr[-200:])
    ok(seed.stdout.strip())

    say("7/7 baseline deploy (optional, several minutes)")
    if input("  deploy candidate-app baseline to Cloud Run now? [y/N] "
             ).strip().lower() == "y":
        import os
        os.environ.update(p_env)
        deploy = subprocess.run(
            [str(ROOT / ".venv" / "bin" / "python"), "-m", "adapters.deploy",
             "baseline"], cwd=ROOT)
        if deploy.returncode != 0:
            fail("baseline deploy failed",
                 "see output above; docs/setup-runbook.md §6-7")
        ok("baseline live")
    else:
        ok("skipped (make deploy-baseline later)")

    say("done — next: make mcp | make monitor | make orchestrate "
        "(three terminals), or scripts/demo.sh")


if __name__ == "__main__":
    main()
