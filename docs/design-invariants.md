# Design invariants

These describe what the GOVERNOR is. They are never injected into any
agent prompt — each is enforced structurally, in code, tests, and
interfaces. The rule behind all of them: prefer capability enforcement
over prompt enforcement. An agent cannot be talked out of a property it
has no tool to violate.

1. **The audit log is append-only.** Enforced by the delivery-store MCP
   server exposing no update or delete tool for the audit table. Tested
   in `tests/test_delivery_store.py`.

2. **All state is externalized.** Agents are stateless invocations; the
   truth lives in GitHub (code, PRs, review threads) and the delivery
   store (backlog, sprints, incidents, deploys, tokens, audit). Any
   agent can be killed and re-invoked without loss.

3. **No merge without human approval.** Enforced by pipeline shape: the
   release manager only ever RECEIVES PRs that carry a human approval
   record from the gate step. Its core rules restate this
   (defense-in-depth), but the structural gate is the enforcement.

4. **Rejection is one mechanism.** Every stage that can bounce a PR
   calls the same `reject(pr, reason_code, return_to, reasoning)` edge.
   Reasons are data (mostly policy-driven); new rejection reasons are
   new codes, never new mechanisms.

5. **Role-scoped store access.** Only the monitor role can open
   incidents or record health samples; only the resolver role can
   resolve incidents. Enforced by per-caller bearer tokens at the MCP
   server, not by convention.

6. **Agents never hold credentials for consequential systems.** Cloud
   credentials live only in the deploy tool; GitHub access goes through
   the RepoHost adapter; model keys live in env, read by the invoker.

7. **All agent loops are bounded.** Fix loops, flag-fix loops, and gate
   polling all have policy-defined caps; there are no unbounded agent
   loops anywhere.

8. **Coordination is orchestrator-driven and artifact-mediated.** No
   A2A protocol, no queues, no webhooks; the PR is the medium between
   coder and reviewer, and the live service is the medium between chaos
   and monitor (see ADR-0003).

9. **Project config can tighten an agent, never loosen it.** Agent core
   rules are engine-owned and immutable; project auxiliary rules are
   injected after them and cannot override them.
