# ADR-0007: The agent framework is an implementation detail

**Status:** accepted

## Context

The rubric scores meaningful ADK use, which creates pressure to let
ADK types leak everywhere. But the SDLC's value — governance under
budgets, claimed-vs-actual verification, human gates, audited release
decisions — is framework-independent, and frameworks churn faster than
processes.

## Decision

The core describes agents declaratively; exactly one adapter package
speaks ADK:

- `orchestrator/invoker.py` — the AgentInvoker PORT: `AgentSpec`
  (prompt, model name, tool NEEDS — plain callables or a `StoreTools`
  filter), `Invocation` (text + token usage), and the invoke Protocol.
  No framework imports.
- `adapters/adk/` — the one implementation (Google ADK 2):
  `invoker.py` materializes specs into `LlmAgent`s (LiteLLM bridging
  for non-Gemini models, MCP toolsets from `StoreTools`, token
  metering via `after_model_callback`, `output_schema` for tool-less
  agents); `workflow.py` renders the per-item pipeline as a native ADK
  `Workflow` whose routed cycle edges realize the definition's
  back-edges — same single-shot step functions the driver uses,
  nothing reimplemented.
- The composition root (`orchestrator/__main__.py`) is the only place
  a framework is chosen and injected.

Enforced structurally: `tests/test_framework_boundary.py` fails if
`orchestrator/`, `sdlc_steps/`, `tools/`, or `mcp_server/` ever import
`google.adk` (or the adapter), and asserts the Workflow expression
stays in parity with `orchestrator/definition.py`.

## Consequences

ADK is demonstrated deeply (LlmAgent, LiteLLM, McpToolset, callbacks,
output_schema, Workflow, adk web) yet remains swappable: a second
framework is one new adapter and one changed line at the composition
root. Structured verdicts (`orchestrator/schemas.py`) validate every
agent decision at the boundary regardless of framework.
