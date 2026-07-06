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

Onboarding a NEW project: pass --project <name> for a name with no
bundle yet — setup scaffolds projects-config/<name>/ interactively
(repo, service, approvers) with a sample backlog and a README that
explains every extension point, then continues through the same steps.

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
    """Fill the example template with values (idempotent merge).
    Precedence: existing non-empty .env value > prompted value >
    the template's own default (a scaffolded bundle pre-fills e.g.
    CLOUD_RUN_SERVICE — that default must survive the merge)."""
    text = example.read_text()
    existing = _read_env(path)
    merged = {**values, **{k: v for k, v in existing.items() if v}}
    lines = []
    for line in text.splitlines():
        if "=" in line and not line.startswith("#"):
            key, template_default = line.split("=", 1)
            lines.append(f"{key}={merged.get(key, template_default)}")
        else:
            lines.append(line)
    path.write_text("\n".join(lines) + "\n")


def _prompt(existing: dict, key: str, label: str, secret_ok: bool = False) -> str:
    if existing.get(key):
        ok(f"{key} already set")
        return existing[key]
    value = input(f"  {label}\n  {key}= ").strip()
    if not value and not secret_ok:
        fail(f"{key} is required", "re-run setup when you have it")
    return value


PROJECT_YAML = """\
# Project definition: {name}.
# Everything the engine needs to know about this project lives in this
# folder; the engine itself is project-agnostic.

repo: {repo}

cloud_run:
  service: {service}

# Deterministic module-to-area map: diff analysis assigns each changed
# file the area of its first matching path prefix, else default_area.
# Areas drive risk verification, incident scoping, and release holds —
# refine these to match the repo's layout.
areas:
  {default_area}: []
default_area: {default_area}

# One representative endpoint per area: preprod smoke tests hit the
# changed area's endpoint; the synthetic monitor probes all of them.
smoke_endpoints:
  {default_area}: /health
"""

PROJECT_ENV_EXAMPLE = """\
# Project-scoped secrets for {name}. Copy to .env in this folder and
# fill in. NEVER committed (projects-config/*/.env is gitignored).

# Fine-grained PAT scoped to THIS project's repo only
# (contents + pull requests, read/write).
GITHUB_TOKEN=

# Where this project deploys (deploy tool only; agents never see these).
GCP_PROJECT=
GCP_REGION=
CLOUD_RUN_SERVICE={service}

# Protects the governed app's config endpoint (if it has one).
CONFIG_TOKEN=

# OPTIONAL: pin the working checkout somewhere inspectable. When
# unset, the engine provisions its own clone under the system tmp dir
# (and deletes it after a clean run) — no local copy is required.
#PROJECT_CHECKOUT_DIR=../{name}
"""

SAMPLE_BACKLOG = """\
[
  {{
    "id": "SAMPLE-1",
    "title": "Replace me: one small, well-groomed change",
    "description": "REPLACE with a real item. The description is what the coder implements — be as specific as a good ticket.",
    "type": "story",
    "implementation": "agent",
    "claimed_risk": "low",
    "claimed_impact": "low",
    "area_hint": "{default_area}",
    "priority_rank": 1
  }}
]
"""

PROJECT_README = """\
# {name} — project bundle

Everything project-specific lives HERE; the engine stays untouched.
Run a sprint with:

    make seed PROJECT={name}
    make orchestrate PROJECT={name}     # add PARALLEL=2 for two coders

## What the governed repo must provide

- Deployable via `gcloud run deploy --source`: a `Dockerfile`, or a
  buildpack-detectable app (Python: `requirements.txt` + `main.py` or
  a `Procfile`), listening on `$PORT`.
- A `/health` endpoint (or adjust `smoke_endpoints` in project.yaml) —
  preprod CI and the synthetic monitor probe it.
- A test suite runnable as `python -m pytest` (the coder's run_tests
  tool and preprod CI both use it).

## Files

- `project.yaml` — repo, Cloud Run service, area map, smoke endpoints.
  Refine `areas` early: they drive verification and release holds.
- `backlog.json` — the sprint input. Replace the SAMPLE item; keep the
  same fields (`implementation`: agent | human).
- `.env` (copy of `.env.example`) — this project's secrets.

## Extending per step (overlays, all optional)

Mirror the engine's step layout under `sdlc_steps/<step>/`:

- `customised-prompt.md` — appended to the engine's base prompt for
  that step; extends, never overrides its core rules.
- `policy.yaml` — deep-merged over engine defaults. The ones that
  matter first:
  - `approver/policy.yaml` -> `approvers: [github-logins]` (REQUIRED —
    who may /approve, /reject, /hold on PRs) and `gate_mode`
  - `coder/policy.yaml` -> `protected_paths` (files the coder must
    never write)
  - `verify/policy.yaml` -> `sensitive_areas` (areas that force a
    risk floor)
  - `sprint_packer/policy.yaml` -> `risk_budget`, `token_budget`

The full step list lives in the engine's `sdlc_steps/`; every step
with a `prompts.md` accepts both overlay files. See
`docs/architecture.md` (knowledge architecture) for the composition
order.
"""


def _scaffold(project_dir: Path, name: str) -> None:
    """Multi-turn bundle generation for a project the engine has never
    seen: asks only what cannot be defaulted, writes the rest with
    guidance comments."""
    say(f"scaffolding projects-config/{name}/")
    repo = input("  governed GitHub repo (owner/name)= ").strip()
    if "/" not in repo:
        fail("repo must be owner/name", "e.g. acme/shop-api")
    service = input(f"  Cloud Run service name [{name}]= ").strip() or name
    approvers = [a.strip() for a in input(
        "  approver GitHub login(s), comma-separated= ").split(",")
        if a.strip()]
    if not approvers:
        fail("at least one approver is required",
             "these logins gate every merge via /approve on the PR")
    default_area = input("  default area name [core]= ").strip() or "core"

    project_dir.mkdir(parents=True)
    (project_dir / "project.yaml").write_text(PROJECT_YAML.format(
        name=name, repo=repo, service=service, default_area=default_area))
    (project_dir / ".env.example").write_text(PROJECT_ENV_EXAMPLE.format(
        name=name, service=service))
    (project_dir / "backlog.json").write_text(
        SAMPLE_BACKLOG.format(default_area=default_area))
    approver_dir = project_dir / "sdlc_steps" / "approver"
    approver_dir.mkdir(parents=True)
    (approver_dir / "policy.yaml").write_text(
        "# Who may decide the human gate (/approve, /reject, /hold).\n"
        "approvers:\n" +
        "".join(f"  - {a}\n" for a in approvers))
    (project_dir / "README.md").write_text(PROJECT_README.format(name=name))
    ok(f"bundle written — see projects-config/{name}/README.md for the "
       "extension points")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="candidate-app")
    parser.add_argument("--scaffold-only", action="store_true",
                        help="generate the project bundle and stop "
                             "(make try-setup wraps this)")
    args = parser.parse_args()
    project_dir = ROOT / "projects-config" / args.project

    if not (project_dir / "project.yaml").exists():
        if args.scaffold_only or input(
                f"  no bundle for {args.project!r} — scaffold one now? "
                "[Y/n] ").strip().lower() in ("", "y", "yes"):
            _scaffold(project_dir, args.project)
        else:
            fail(f"no project {args.project!r}",
                 "pick an existing folder under projects-config/ or let "
                 "setup scaffold it")
    elif args.scaffold_only:
        ok(f"bundle for {args.project!r} already exists — nothing to do")
    if args.scaffold_only:
        return

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
    checkout_key = ("PROJECT_CHECKOUT_DIR"
                    if "PROJECT_CHECKOUT_DIR" in p_example.read_text()
                    else "CANDIDATE_APP_DIR")  # legacy bundles
    if not project_env.get(checkout_key):
        pinned = input(
            f"  {checkout_key} (optional — Enter to let the engine "
            "provision its own checkout)= ").strip()
        if pinned:
            p_values[checkout_key] = pinned
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
    pinned = p_env.get(checkout_key)
    if pinned and not Path(pinned).joinpath(".git").exists():
        fail(f"{pinned} is not a git checkout",
             f"git clone https://github.com/{repo} {pinned} — or unset "
             f"{checkout_key} to let the engine provision its own")
    ok("checkout pinned" if pinned else
       "engine will provision its own checkout")

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
    import os
    store_db = os.environ.get("DELIVERY_STORE_DB") or (
        "delivery_store.sqlite3" if args.project == "candidate-app"
        else f"delivery_store-{args.project}.sqlite3")  # same rule as make
    seed = subprocess.run(
        [str(ROOT / ".venv" / "bin" / "python"),
         str(ROOT / "scripts" / "seed.py"), "--project", args.project],
        cwd=ROOT, env={**os.environ, "DELIVERY_STORE_DB": store_db},
        capture_output=True, text=True, timeout=120)
    if seed.returncode != 0:
        fail("seeding failed", seed.stderr[-200:])
    ok(seed.stdout.strip())

    say("7/7 baseline deploy (optional, several minutes)")
    if input(f"  deploy {args.project} baseline to Cloud Run now? [y/N] "
             ).strip().lower() == "y":
        import os
        os.environ.update(p_env)
        if not os.environ.get("PROJECT_CHECKOUT_DIR"):
            # deploy --source needs a working copy; the engine
            # provisions its own (same as make deploy-baseline)
            provision = subprocess.run(
                [str(ROOT / ".venv" / "bin" / "python"), "-m",
                 "orchestrator.provisioning", "--project", args.project],
                cwd=ROOT, capture_output=True, text=True, timeout=300)
            if provision.returncode != 0:
                fail("could not provision the checkout",
                     provision.stderr[-200:])
            os.environ["PROJECT_CHECKOUT_DIR"] = provision.stdout.strip()
            ok(f"checkout provisioned: {os.environ['PROJECT_CHECKOUT_DIR']}")
        # Preflight before burning Cloud Build minutes: --source needs a
        # Dockerfile or a buildpack-detectable app.
        src = Path(os.environ["PROJECT_CHECKOUT_DIR"])
        if not any((src / f).exists()
                   for f in ("Dockerfile", "Procfile", "main.py", "app.py",
                             "package.json", "go.mod")):
            fail(f"{src} has no Dockerfile/Procfile/main.py — Cloud Run "
                 "source deploy cannot build it",
                 "make the governed repo deployable first (see the "
                 "bundle README: 'What the governed repo must provide')")
        deploy = subprocess.run(
            [str(ROOT / ".venv" / "bin" / "python"), "-m", "adapters.deploy",
             "baseline"], cwd=ROOT)
        if deploy.returncode != 0:
            fail("baseline deploy failed",
                 "see output above; docs/setup-runbook.md §6-7")
        ok("baseline live")
    else:
        ok("skipped (make deploy-baseline later)")

    suffix = f" PROJECT={args.project}"
    say(f"done — the store is seeded from {args.project}'s backlog.json. "
        "Next, in separate terminals:\n"
        f"    make mcp{suffix}\n"
        f"    make monitor{suffix}\n"
        f"    make orchestrate{suffix}\n"
        f"  edited backlog.json? re-seed first: make seed{suffix}")


if __name__ == "__main__":
    main()
