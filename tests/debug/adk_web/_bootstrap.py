"""Shared bootstrap for the adk web debug entries (DRY: the per-agent
folders below are 2-line stubs because `adk web` discovery imposes the
one-folder-per-agent shape, not because each agent needs its own wiring).

Each root_agent is built by the SAME adapter the pipeline uses
(adapters/adk/invoker.build_llm_agent), from the SAME spec (composed
prompt + declared tools) — so what you debug here is what runs.
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env")
load_dotenv(REPO_ROOT / "projects-config" / "candidate-app" / ".env")

from adapters.adk.invoker import build_llm_agent  # noqa: E402
from orchestrator.config import load_project      # noqa: E402

_SAMPLE_DIFF = """diff --git a/app/payments.py b/app/payments.py
--- a/app/payments.py
+++ b/app/payments.py
@@ -10,3 +10,4 @@
+    summary["service_fee"] = round(15734.50 * 0.015, 2)
"""


def make_root_agent(step: str):
    import importlib

    project = load_project("candidate-app")
    workspace = os.environ.get(
        "PROJECT_CHECKOUT_DIR",
        os.environ.get("CANDIDATE_APP_DIR",
                       str(REPO_ROOT.parent / "candidate-app")))
    spec_module = importlib.import_module(f"sdlc_steps.{step}.spec")

    # Per-step build arguments, mirroring orchestrator/driver.py.
    if step == "coder":
        spec = spec_module.build(project, workspace)
    elif step == "code_reviewer":
        spec = spec_module.build(project, workspace, _SAMPLE_DIFF)
    else:
        spec = spec_module.build(project)
    return build_llm_agent(spec)
