"""Sandboxed workspace tools for the coder agent.

The coder reasons and edits; everything consequential stays outside its
reach. These four functions are its ENTIRE effect surface: read/list/
write inside one checkout directory, and run that project's tests. Git
operations (branch, commit, push) and PR creation are performed by the
orchestrator afterwards — the agent cannot touch git, credentials, the
network, or any path outside the checkout (capability enforcement over
prompt enforcement).

FAILURE MODE MATTERS: a sandbox refusal is returned to the MODEL as an
"ERROR: ..." result, never raised — a raised tool exception aborts the
whole agent run in ADK, whereas an error result lets the model read the
refusal and self-correct (e.g. it once tried to list .git and killed
the pipeline).
"""

import subprocess
from pathlib import Path

def make_workspace_tools(repo_dir: str | Path,
                         protected_paths: tuple[str, ...] = ()) -> list:
    """protected_paths: project-owned write-denylist (from the coder
    step policy) — the engine knows nothing about any project's layout;
    only .git is engine-enforced (universal)."""
    root = Path(repo_dir).resolve()
    denylist = (".git",) + tuple(protected_paths)

    def _safe(path: str, writing: bool = False) -> Path:
        resolved = (root / path).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"path escapes the workspace: {path}")
        rel = str(resolved.relative_to(root))
        if writing and any(rel == d or rel.startswith(d + "/")
                           for d in denylist):
            raise ValueError(f"writing to {rel} is not permitted")
        if ".git" in resolved.parts:
            raise ValueError("the .git directory is off limits")
        return resolved

    def list_files(subdir: str = ".") -> list[str]:
        """List files in the workspace (relative paths), recursively."""
        try:
            base = _safe(subdir)
            return sorted(
                str(f.relative_to(root)) for f in base.rglob("*")
                if f.is_file() and ".git" not in f.parts
                and ".venv" not in f.parts and "__pycache__" not in f.parts)
        except (ValueError, OSError) as exc:
            return [f"ERROR: {exc}"]

    def read_file(path: str) -> str:
        """Read one file from the workspace."""
        try:
            return _safe(path).read_text()
        except (ValueError, OSError) as exc:
            return f"ERROR: {exc}"

    def write_file(path: str, content: str) -> str:
        """Write one file in the workspace (creates parent directories)."""
        try:
            target = _safe(path, writing=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            return f"wrote {path} ({len(content)} chars)"
        except (ValueError, OSError) as exc:
            return f"ERROR: {exc}"

    def run_tests() -> str:
        """Run the project's test suite; returns the tail of the output."""
        try:
            proc = subprocess.run(
                [str(root / ".venv" / "bin" / "python"), "-m", "pytest", "-q"],
                cwd=root, capture_output=True, text=True, timeout=300)
            output = (proc.stdout + proc.stderr)[-4000:]
            return f"exit code {proc.returncode}\n{output}"
        except (OSError, subprocess.SubprocessError) as exc:
            return f"ERROR: {exc}"

    return [list_files, read_file, write_file, run_tests]
