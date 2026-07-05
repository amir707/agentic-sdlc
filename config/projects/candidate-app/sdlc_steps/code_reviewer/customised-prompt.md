# Code reviewer — candidate-app customised prompt

Loaded after the base sdlc_steps/code_reviewer/prompts.md; extends it
for this project, never overrides it.

- Any change under `app/payments*` must include at least one test that
  exercises the changed behavior directly, even if coverage numbers
  already look adequate.
- Treat changes to response payload shapes as contract changes: call out
  removed or renamed fields as blocking.
- The candidate app has no auth layer by design; do not request one.
