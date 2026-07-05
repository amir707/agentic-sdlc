# ADR-0001: System-vs-project knowledge split

**Status:** accepted

## Context

The governor is a multi-project runtime system: candidate-app is one project
bundle, and a second project should be a new folder with the system
untouched. Knowledge about how agents behave comes from several owners
with different change cadences: the system's designers, each agent's
definition, the team running a given project, and task procedure.

## Decision

Knowledge splits by OWNER, with an explicit prompt-vs-structure line:

| Layer | Owner | Location | Injected? |
|---|---|---|---|
| Design invariants | system | `docs/design-invariants.md` | never — enforced structurally |
| Step base prompt (opens with core rules) | system | `sdlc_steps/<step>/prompts.md` | always, first |
| Step policy defaults | system | `sdlc_steps/<step>/policy.yaml`; cross-step keys in `sdlc_steps/policy.yaml`; pipeline flow control in `sdlc_steps/orchestrator/policy.yaml` | read by deterministic tools |
| Project customised prompt | project | `config/projects/<name>/sdlc_steps/<step>/customised-prompt.md` | after base; extends only |
| Project policy overrides | project | `config/projects/<name>/sdlc_steps/<step>/policy.yaml` | merged over step defaults |
| Project definition & seed | project | `config/projects/<name>/project.yaml`, `backlog.json`, `.env` | read by orchestrator/tools/adapters |
| ADRs (the "why") | system | `docs/adr/` | never — for humans and judges |

The project side MIRRORS the system hierarchy (the overlay pattern):
customising a step means creating the same-shaped path under your
project's config folder. Composition order at invocation: base
prompts.md → customised-prompt.md → task payload. Each base prompt
opens with core rules and states that customizations cannot override
them. A startup validator checks the active project bundle so a
malformed config fails at load, not mid-sprint.

Rule of thumb: invariants say what the SYSTEM is, a base prompt's core
rules say what a STEP never does, its playbook section says HOW,
project config says what THIS PROJECT currently wants, ADRs say WHY.

The policy test: if a value could change next quarter by team decision
(risk point costs, flag threshold, reviewer capacity, monitor windows),
it is policy, read by tools, referenced by skills by NAME, never copied.

## Consequences

A team tunes its project's config folder without touching the system;
a prompt change changes agent behavior and is versioned in git (audit
entries implicitly reference the prompt version via commit SHA);
context stays lean because each invocation loads only its own step's
prompts (progressive disclosure).
