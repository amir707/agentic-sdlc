# Evals

Two complementary layers, per the Agent Platform eval methodology:

1. **Deterministic pipeline eval** — `scripts/verify_demo.py` runs the
   seeded scenario and asserts the audit log contains the expected
   decisions and reason codes. The audit trail doubles as the
   assertion surface: compliance evidence and test oracle are the
   same table. No LLM-as-judge at demo scale.
2. **Per-agent dataset** — `risk_assessor_dataset.json` (Vertex
   evaluation SDK single-turn schema): seeded backlog items in,
   expected risk band / effort / split recommendation as reference.
   Run with the Agent Platform eval tooling (agents-cli eval) against
   the risk assessor; requires model keys and GCP credentials, so it
   is documented rather than wired into CI.

Case pay_102 is deliberate: the assessor SHOULD read the deceptive
item as low risk from its description — catching the lie post-code is
verify's job (claimed-vs-actual), not the assessor's.
