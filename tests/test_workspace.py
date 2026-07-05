"""Workspace + WorkspaceFactory against real local git repos: parallel
coders each get an isolated worktree, edits never bleed across items,
and both branches land on the origin."""

import subprocess
from pathlib import Path

import pytest

from orchestrator.workspace import Workspace, WorkspaceFactory


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    """An 'origin' repo with one commit on main, and a clone (the base
    checkout, like CANDIDATE_APP_DIR)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "test@test")
    _git(origin, "config", "user.name", "test")
    (origin / "app.py").write_text("VERSION = 1\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "baseline")

    base = tmp_path / "base"
    subprocess.run(["git", "clone", "-q", str(origin), str(base)],
                   capture_output=True, check=True)
    _git(base, "config", "user.email", "test@test")
    _git(base, "config", "user.name", "test")
    return origin, base


def test_worktrees_isolate_parallel_items(repos):
    origin, base = repos
    factory = WorkspaceFactory(base)
    ws1 = factory.for_item("PAY-101")
    ws2 = factory.for_item("CAT-201")

    # Worktrees live OUTSIDE the base checkout (its clean -fd must
    # never be able to delete a sibling item's work).
    assert not ws1.dir.is_relative_to(Path(base).resolve())
    assert ws1.dir != ws2.dir

    ws1.start_branch("item/PAY-101-x")
    ws2.start_branch("item/CAT-201-y")

    (ws1.dir / "payments.py").write_text("fee = 1\n")
    (ws2.dir / "catalog.py").write_text("count = 2\n")

    # No cross-bleed between concurrent workspaces.
    assert not (ws2.dir / "payments.py").exists()
    assert not (ws1.dir / "catalog.py").exists()

    ws1.commit_all("PAY-101: fee")
    ws2.commit_all("CAT-201: count")
    ws1.push("item/PAY-101-x", str(origin))
    ws2.push("item/CAT-201-y", str(origin))

    branches = _git(origin, "branch", "--list")
    assert "item/PAY-101-x" in branches and "item/CAT-201-y" in branches

    # The base checkout never noticed any of it.
    assert _git(base, "status", "--porcelain") == ""

    factory.cleanup()
    assert not ws1.dir.exists() and not ws2.dir.exists()


def test_start_branch_never_needs_main_checked_out(repos):
    """Regression: branching goes straight off origin/main, so it works
    in a worktree even while the base checkout holds main."""
    origin, base = repos
    factory = WorkspaceFactory(base)
    ws = factory.for_item("PAY-102")
    ws.start_branch("item/PAY-102-z")     # would fail if it checked out main
    assert _git(base, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert _git(ws.dir, "rev-parse", "--abbrev-ref", "HEAD") == "item/PAY-102-z"
    assert not ws.has_changes()
    factory.cleanup()


def test_base_workspace_still_works_sequentially(repos):
    origin, base = repos
    ws = Workspace(base)
    ws.start_branch("item/CORE-302-w")
    (ws.dir / "health.py").write_text("build = 'abc'\n")
    assert ws.has_changes()
    sha = ws.commit_all("CORE-302: build metadata")
    assert len(sha) == 40
    assert "health.py" in ws.diff_against("main")

