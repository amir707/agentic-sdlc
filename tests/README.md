# tests/ — mirrors the package tree

| dir | what it pins |
|---|---|
| `architecture/` | structural rules: the framework boundary and dependency direction, the definition ↔ graph ↔ diagram consistency |
| `governance/` | the shared rules: uniform outcomes, the human gate's authority model, the machine gates + deploy guardrails |
| `sprint/` | the sprint clock: per-item pipeline decisions (ADK-free) and the executing ADK item graph end-to-end |
| `release/` | the release clock: one pass over the queue, delegation, the release Workflow |
| `engine/` | mechanics: git workspace, heartbeat, redaction, config overlays + dependency graph + packer |
| `steps/` | step knowledge in action: verify escalation, resolver hysteresis |
| `adapters/` | GitHub / ADK invoker / store client auth / the ADK sprint Workflow |
| `store/` | the delivery store: real-server role matrix, backend contract (the Firestore door), report, vocabulary |
| `scripts/` | every operator script imports and answers `--help` |
| `debug/` | `adk web` dev entries (not tests) |

Run everything: `make test`. Run one area: `.venv/bin/python -m pytest tests/sprint`.
