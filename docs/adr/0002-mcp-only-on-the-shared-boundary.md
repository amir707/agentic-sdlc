# ADR-0002: MCP only on the one genuine shared boundary

**Status:** accepted

## Context

MCP is a rubric concept for this project, which creates pressure to
wrap everything in MCP servers. Most components here have exactly one
caller (deploy script, diff analysis, packer) — wrapping those would be
protocol decoration, not architecture.

## Decision

Exactly one custom MCP server: the delivery store. It is the single
piece of shared state crossed by EVERY component — assessor, packer,
monitor, CI, approver, release manager, including agents on two
different model families — so it is the one genuine shared boundary in
the system.

Placing MCP on that boundary enforces security properties at the
interface instead of by convention:

- append-only audit: no update/delete tool exists;
- separated detection and closure: the monitor role alone opens
  incidents and records health samples, the resolver role alone
  resolves, carried by per-caller bearer tokens.

Everything single-caller stays a plain Python function.

## Consequences

The MCP surface is small enough to read in one sitting and every tool
on it earns its place. The store can move from SQLite to Postgres
behind the same tool surface (scaling path), and the writeup can argue
the placement rather than just the presence of MCP.
