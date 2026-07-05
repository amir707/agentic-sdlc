# ADR-0003: Orchestrator-driven coordination, no A2A

**Status:** accepted

## Context

Multi-agent systems can coordinate peer-to-peer (A2A protocol, message
passing) or through a driver. This pipeline has fixed roles and a fixed
order: assess → pack → code → review → verify → CI → gate → release.

## Decision

All coordination is orchestrator-driven and artifact-mediated. Agents
are invocations, not daemons: nothing listens, polls, or gets notified;
the orchestrator invoking an agent IS the event. The PR is the medium
between coder and reviewer (comments, replies, approvals live there);
the live service is the medium between the chaos flag and the monitor —
they never communicate directly, the monitor discovers degradation by
probing the way a user would.

No A2A protocol anywhere; no queues, webhooks, or parallel worker
pools. One deliberate carve-out: the human approval gate blocks by
polling PR comments for a decision from an allowlisted approver —
blocking on human input is not agent coordination.

Same judgment applies to prompt loading: the orchestrator always knows
which step it is invoking, so it concatenates that step's base
prompts.md and the project's customised-prompt.md deterministically. No
agent-pulled prompt-discovery tool, no index — dynamic discovery is for
open-ended agents with unpredictable tasks, and these steps have fixed
roles.

## Consequences

The whole run is sequential and inspectable; every hand-off is visible
as an artifact (a PR event or a store record). The scaling path (README)
names the successors — webhook dispatch, work queues — as orchestrator swaps
that leave agents and the MCP surface untouched.
