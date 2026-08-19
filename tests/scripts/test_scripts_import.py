"""Every operator script must at least IMPORT and answer --help.

scripts/ had no tests; a refactor of the engine (steps moved out of
driver.py) silently broke scripts/reset_item.py's import for several
merges. This guard runs each script as the operator would — as a file,
with the repo root on sys.path only via the script's own bootstrap —
and asserts --help exits 0. Cheap, and it catches exactly that class
of break."""

import subprocess
import sys

import pytest

from sdlc.engine.paths import REPO_ROOT as ROOT
SCRIPTS = sorted(p for p in (ROOT / "scripts").glob("*.py"))


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_imports_and_answers_help(script, monkeypatch, tmp_path):
    # Isolated environment: no real .env, no real store file — a script
    # whose --help needs a live store or a token is a script that will
    # fail on an operator's first contact too.
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
           "DELIVERY_STORE_DB": str(tmp_path / "none.sqlite3")}
    proc = subprocess.run([sys.executable, str(script), "--help"],
                          capture_output=True, text=True, timeout=60,
                          cwd=tmp_path, env=env)
    assert proc.returncode == 0, (
        f"{script.name} --help failed:\n{proc.stderr[-800:]}")
