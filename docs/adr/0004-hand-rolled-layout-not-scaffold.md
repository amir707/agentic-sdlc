# ADR-0004: Hand-rolled repo layout, agents-cli skills as reference

**Status:** accepted

## Context

Google's `agents-cli` scaffolds ADK projects (`scaffold create`) with
deployment Terraform, CI/CD variants, an ADK FastAPI serving surface,
and A2A routes. Its skills say "do not write agent code until a project
is scaffolded." Using it demonstrates the Agents CLI rubric concept in
its intended form — but this build deploys no agent endpoint (the
governor runs locally; only the candidate app is deployed), disavows
A2A (ADR-0003), and every generated line would still need human
verification before judging.

## Decision

Hand-rolled layout. The Agents CLI suite is used as it was used: seven
first-party skills (`~/.agents/skills/google-agents-cli-*`) loaded at
DEV TIME by Claude Code for ADK API patterns, eval methodology, and
workflow guidance while building this repo. The scaffold's serving and
deploy machinery is exactly the part this architecture does not want.

## Consequences

The repo contains only code that earns its place and can be verified
line-by-line before judging. The Agents CLI concept is claimed honestly
in the writeup as build tooling (rubric accepts Code or Video for agent
skills), alongside the runtime `sdlc_steps/` prompt mechanism. The cost:
no scaffold-generated conveniences; accepted at this repo size.
