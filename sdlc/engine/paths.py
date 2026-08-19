"""Where the repository is — one truth, so no module does
Path(__file__).parent.parent arithmetic that silently breaks when a file
moves (it did, in the layout refactor)."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]   # sdlc/engine/paths.py -> repo
