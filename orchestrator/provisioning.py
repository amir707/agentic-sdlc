"""Engine-owned checkout provisioning: the SDLC materializes its own
working copy of the governed repo.

The local checkout is a CACHE of GitHub state, so the engine builds it
for itself: cloned on demand into a scratch directory, healed when
missing or broken, reused while a run is resuming, and DELETED when a
run completes cleanly (a crashed run leaves it in place so resume is
instant; `make reset` clears it too). Nothing precious ever lives here.

Locations: $AGENTIC_SDLC_SCRATCH, else <system tmp>/agentic-sdlc/, as
<project>/checkout. CANDIDATE_APP_DIR (project .env) optionally pins
the LOCATION somewhere inspectable — the engine still provisions and
heals it there.

Cloud note: this is exactly the ephemeral-worker pattern. A queue
worker deployed to Cloud Run/GKE clones per task onto container disk;
the venv step becomes a baked image layer; the clone credential comes
from workload identity or a GitHub App installation token instead of a
PAT. Demo-scale rung: the scratch clone's origin remote carries the
token URL (fetches need auth on a private repo) — transient by design,
removed with the checkout.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from orchestrator.workspace import Workspace


def _log(message: str) -> None:
    # Info goes to stderr so the CLI's stdout stays machine-readable
    # (scripts capture `python -m orchestrator.provisioning` output).
    print(f"[provision] {message}", file=sys.stderr, flush=True)


def scratch_root() -> Path:
    return Path(os.environ.get(
        "AGENTIC_SDLC_SCRATCH",
        str(Path(tempfile.gettempdir()) / "agentic-sdlc")))


def checkout_path(project_name: str) -> Path:
    override = os.environ.get("CANDIDATE_APP_DIR")
    if override:
        return Path(override).resolve()
    return scratch_root() / project_name / "checkout"


def provision(project_name: str, clone_url: str) -> Workspace:
    """Materialize (or heal) the working checkout; idempotent."""
    target = checkout_path(project_name)
    if not (target / ".git").exists():
        if target.exists():
            shutil.rmtree(target)  # husk without .git (e.g. orphaned)
        target.parent.mkdir(parents=True, exist_ok=True)
        _log(f"cloning {project_name} -> {target}")
        subprocess.run(["git", "clone", "-q", clone_url, str(target)],
                       check=True, capture_output=True, text=True)
    _ensure_venv(target)
    return Workspace(target)


def _ensure_venv(target: Path) -> None:
    """The governed repo's tests need its own deps; build once per
    provision (uv makes this seconds; a cloud image bakes it instead)."""
    requirements = next(
        (target / name for name in ("requirements-dev.txt", "requirements.txt")
         if (target / name).exists()), None)
    if requirements is None:
        return
    venv = target / ".venv"
    if (venv / "bin" / "python").exists():
        return
    if venv.is_symlink() or venv.exists():
        # husk or self-referential symlink (a poisoned clone) — rebuild
        _log("removing broken .venv before rebuild")
        if venv.is_symlink():
            venv.unlink()
        else:
            shutil.rmtree(venv, ignore_errors=True)
    _log(f"building venv from {requirements.name}")
    subprocess.run(["uv", "venv", "--python", "3.12", "-q",
                    str(target / ".venv")],
                   check=True, capture_output=True, text=True)
    subprocess.run(["uv", "pip", "install", "-q", "-p",
                    str(target / ".venv" / "bin" / "python"),
                    "-r", str(requirements)],
                   check=True, capture_output=True, text=True)


def deprovision(project_name: str) -> None:
    """Delete the scratch checkout and its worktrees — the engine
    cleans up after itself; GitHub keeps the truth."""
    target = checkout_path(project_name)
    worktrees = target.parent / f"{target.name}-worktrees"
    for path in (worktrees, target):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    _log(f"removed scratch checkout for {project_name}")


def main() -> None:
    """CLI for scripts: provision and print ONLY the checkout path.
    Usage: python -m orchestrator.provisioning --project candidate-app
           [--deprovision]"""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="candidate-app")
    parser.add_argument("--deprovision", action="store_true")
    args = parser.parse_args()

    if args.deprovision:
        deprovision(args.project)
        return

    repo_line = next(
        line for line in (Path(__file__).resolve().parent.parent /
                          "projects-config" / args.project /
                          "project.yaml").read_text().splitlines()
        if line.startswith("repo:"))
    repo = repo_line.split(":", 1)[1].strip()
    token = os.environ["GITHUB_TOKEN"]
    clone_url = f"https://x-access-token:{token}@github.com/{repo}.git"
    workspace = provision(args.project, clone_url)
    print(workspace.dir)


if __name__ == "__main__":
    main()
