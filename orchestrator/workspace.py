"""Git workspace management for agent work (deterministic, engine-owned).

The coder agent edits files in a checkout but never touches git: the
engine prepares the branch before invoking it and commits/pushes after
it finishes. Credentials appear only in the push remote URL constructed
by RepoHost, only here, never in agent context.
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
        """Fresh branch off up-to-date base; discards any local litter."""
        self._git("fetch", "origin", base)
        self._git("checkout", "-f", base)
        self._git("reset", "--hard", f"origin/{base}")
        self._git("clean", "-fd")
        self._git("checkout", "-B", branch)

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
