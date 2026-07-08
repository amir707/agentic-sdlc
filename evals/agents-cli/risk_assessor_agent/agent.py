"""The risk assessor as an ADK root_agent for agents-cli eval.

Not a reimplementation: this builds the SAME agent the pipeline runs, by
importing the parent repo's real spec and the same `build_llm_agent`
adapter (exactly what tests/debug/adk_web/ does for `adk web`). So the
eval exercises the shipped agent, not a lookalike.

Runtime needs, mirroring a normal run: GOOGLE_API_KEY (Gemini) and the
delivery store reachable at DELIVERY_STORE_PORT with MCP_TOKEN_AGENTS —
the assessor's only tools are the store's get_item / record_assessment,
so `make mcp` must be running during `agents-cli eval generate`.
"""

import sys
from pathlib import Path

# evals/agents-cli/risk_assessor_agent/agent.py -> repo root is parents[3]
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")
load_dotenv(REPO_ROOT / "projects-config" / "candidate-app" / ".env")

from adapters.adk.invoker import build_llm_agent  # noqa: E402
from orchestrator.config import load_project      # noqa: E402
from sdlc_steps.risk_assessor import spec as risk_assessor_spec  # noqa: E402

_project = load_project("candidate-app")
root_agent = build_llm_agent(risk_assessor_spec.build(_project))
