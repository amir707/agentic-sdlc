# Coder — candidate-app customised prompt

Loaded after the base sdlc-steps/coder/prompts.md; the candidate-app
repo's conventions.

## Repo layout

- `app/main.py` — FastAPI app, core endpoints (`/health`, chaos config).
- `app/payments.py` — area `payments` (`/payments/summary`).
- `app/catalog.py` — area `catalog` (`/catalog/items`).
- `app/flags.py` — feature flag helper; flags live in `flags.json`.
- `app/chaos.py` — governor demo rigging. NEVER touch chaos code, the
  config endpoints, or `CONFIG_TOKEN` handling in a story PR.
- `tests/` — pytest; run with `python -m pytest -q`.

## Feature flags

Flags are read from `flags.json` at request time via
`app.flags.enabled("flag_name")`. To gate new behavior: add the flag to
`flags.json` with value `false` (default OFF), branch on
`flags.enabled(...)` around the NEW behavior only, and test both
states. Flag names are snake_case and describe the behavior
(`payments_refund_totals`), not the ticket.

## Branches and PRs

- Branch name: `item/<ITEM-ID>-<short-slug>` (e.g.
  `item/PAY-101-refund-totals`).
- PR body: first line `Item: <ITEM-ID>`, then the item's claimed
  attributes (risk, impact, area) and a short summary of the approach.
- PR title: plain description; verify+label writes the
  `[area:...][risk:...][flag:...]` prefix AFTER review converges — do
  not add labels yourself.
- One item = one PR. Do not fold in unrelated improvements, refactors,
  or drive-by fixes; the reviewer rejects out-of-scope PRs.

## Tests

Every behavior change gets a test exercising it directly. Payments
changes always get at least one direct test (project reviewer rule).
Keep the full suite green: run pytest before opening or updating a PR.
