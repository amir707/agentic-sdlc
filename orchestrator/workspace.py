"""Git workspace management for agent work (deterministic, engine-owned).

The coder agent edits files in a checkout but never touches git: the
engine prepares the branch before invoking it and commits/pushes after
it finishes. Credentials appear only in the push remote URL constructed
by RepoHost, only here, never in agent context.

A checkout is a CACHE of GitHub state, not state itself — which is why
WorkspaceFactory can hand every item its own isolated worktree and
throw it away: parallel coders never share a filesystem, exactly as
cloud workers wouldn't.
"""

import subprocess
from pathlib import Path


class Workspace:
    def __init__(self, checkout_dir: str | Path):
        self.dir = Path(checkout_dir).resolve()

    def _git(self, *args: str) -> str:
        proc = subprocess.run(["git", *args], cwd=self.dir,
                              capture_output=True, text=True, check=True)
        return proc.stdout.strip()

    def start_branch(self, branch: str, base: str = "main") -> None:
        """Fresh branch off up-to-date origin/base; discards local litter.

        Deliberately never checks out `base` itself: git worktrees cannot
        check out a branch already held by another worktree, so branching
        straight off origin/<base> keeps this correct for both the base
        checkout and per-item worktrees."""
        self._git("fetch", "origin", base)
        self._git("checkout", "-f", "-B", branch, f"origin/{base}")
        # -e .venv: in worktrees the venv is a SYMLINK, which git's
        # '.venv/' ignore pattern does not cover (trailing slash matches
        # real directories only) — a bare clean deletes it and the next
        # test run dies on a missing interpreter.
        self._git("clean", "-fd", "-e", ".venv")

    def checkout(self, branch: str) -> None:
        self._git("fetch", "origin", branch)
        self._git("checkout", "-f", branch)
        self._git("reset", "--hard", f"origin/{branch}")

    def diff_against(self, base: str = "main") -> str:
        return self._git("diff", f"origin/{base}...HEAD")


    def has_changes(self) -> bool:
        return bool(self._git("status", "--porcelain"))

    def commit_all(self, message: str) -> str:
        """Commit everything the agent changed. No AI co-author trailers."""
        self._git("add", "-A")
        self._git("commit", "-m", message)
        return self._git("rev-parse", "HEAD")

    def push(self, branch: str, remote_url: str) -> None:
        """Push via a one-shot authenticated URL (never stored in config)."""
        self._git("push", remote_url, f"HEAD:refs/heads/{branch}", "--force")


class WorkspaceFactory:
    """Per-item isolated checkouts via git worktrees (parallel coders).

    Worktrees share the base checkout's object store (creation is
    instant) but each gets its own working directory, so concurrent
    coders physically cannot trample each other. They live OUTSIDE the
    base checkout (sibling folder) so nothing in the base ever sees
    them. The base's .venv is symlinked in so run_tests works without
    per-worktree installs.
    """

    def __init__(self, base_dir: str | Path):
        self.base = Path(base_dir).resolve()
        self.container = self.base.parent / f"{self.base.name}-worktrees"

    def for_item(self, item_id: str) -> Workspace:
        target = self.container / item_id
        if not target.exists():
            self.container.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "-C", str(self.base), "worktree", "add",
                 "--detach", "-f", str(target)],
                capture_output=True, text=True, check=True)
        # (Re)ensure the venv symlink even for a worktree left behind by
        # a crashed run — its link may be gone.
        venv = self.base / ".venv"
        link = target / ".venv"
        if venv.exists() and not link.exists():
            link.symlink_to(venv)
        return Workspace(target)

    def cleanup(self) -> None:
        if not self.container.exists():
            return
        for worktree in self.container.iterdir():
            subprocess.run(
                ["git", "-C", str(self.base), "worktree", "remove",
                 "--force", str(worktree)],
                capture_output=True, text=True)
        subprocess.run(["git", "-C", str(self.base), "worktree", "prune"],
                       capture_output=True, text=True)
        if not any(self.container.iterdir()):
            self.container.rmdir()
