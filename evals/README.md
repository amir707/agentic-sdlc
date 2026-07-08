# Evals

Two complementary layers, per the Agent Platform eval methodology:

1. **Deterministic pipeline eval** — `scripts/verify_demo.py` runs the
   seeded scenario and asserts the audit log contains the expected
   decisions and reason codes. The audit trail doubles as the
   assertion surface: compliance evidence and test oracle are the
   same table. No LLM-as-judge at demo scale.
2. **Per-agent eval (agents-cli)** — `risk_assessor_dataset.json` (Vertex
   evaluation SDK single-turn schema): seeded backlog items in, expected
   risk band / effort / split recommendation as reference. A runnable
   harness lives in [`agents-cli/`](agents-cli/README.md): it runs the
   pipeline's real risk assessor over the dataset (`agents-cli eval
   generate`) and grades the `record_assessment` tool calls with a local
   deterministic metric (`agents-cli eval grade`). One command:

   ```bash
   cd agents-cli && uv sync --dev --extra eval && ./run_eval.sh
   ```

   Needs the parent `.env` (GOOGLE_API_KEY + MCP_TOKEN_*); grading is
   local, so no GCP project or Vertex is required. Not in CI (it spends
   Gemini quota and is non-deterministic); it is a manual/on-demand eval.

Case pay_102 is deliberate: the assessor SHOULD read the deceptive
item as low risk from its description — catching the lie post-code is
verify's job (claimed-vs-actual), not the assessor's.
