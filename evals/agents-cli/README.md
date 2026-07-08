# agents-cli eval harness — risk assessor

Runs the Agent Platform eval loop (`agents-cli eval generate` + `grade`)
against the pipeline's **real** risk-assessor agent.

## Why a separate uv project

`agents-cli eval generate` runs `uv sync --dev --extra eval` in its own
root, so it needs a uv project with a `pyproject.toml` and an `eval`
extra. The parent repo is deliberately framework-neutral (`.venv` +
`requirements.txt`, ADR-0007), so the eval harness lives here as an
isolated uv project instead of contaminating the core.

The agent is **not reimplemented**: `risk_assessor_agent/agent.py` builds
it from the parent repo's real `sdlc_steps.risk_assessor.spec` via the
same `build_llm_agent` adapter the pipeline uses (same trick as
`tests/debug/adk_web/`). So the eval exercises the shipped agent.

## Grading — local & deterministic

The assessor's output is a structured `record_assessment` tool call, not
prose, so LLM-as-judge on text is the wrong instrument. `tests/eval/
eval_config.yaml` defines a local `custom_function` metric
(`execution: local`, no GCP/Vertex, no cost) that reads the tool call
from the trace and checks `risk` / `effort` / `recommend_split` against
the expected assessment for the item in the prompt.

## Run

```bash
uv sync --dev --extra eval   # once
./run_eval.sh                # seed scratch store -> generate -> grade
```

Needs the parent `.env` (GOOGLE_API_KEY + MCP_TOKEN_*). `generate` runs
Gemini over each case; `grade` is free and in-process. Results land in
`artifacts/grade_results/results_<ts>.{json,html}`.

A healthy run ends with:

```
assessment_matches_expected:
  num_cases_total: 4
  num_cases_valid: 4
  num_cases_error: 0
  mean_score: 0.83…
```

`num_cases_error: 0` means the plumbing worked end to end.

### Run the two steps by hand

`run_eval.sh` just wraps these (with a scratch store already up on 8799
and the parent `.env` exported):

```bash
uv run agents-cli eval generate --dataset tests/eval/datasets/risk_assessor_dataset.json
uv run agents-cli eval grade --traces "$(ls -t artifacts/traces/*.json | head -1)" \
     --config tests/eval/eval_config.yaml
```

## Inspecting results

```bash
# visual report in a browser
open "$(ls -t artifacts/grade_results/*.html | head -1)"

# per-case score + explanation
uv run python -c "import json,glob; \
d=json.load(open(sorted(glob.glob('artifacts/grade_results/*.json'))[-1])); \
[print(c['eval_case_index'], \
r['metric_results']['assessment_matches_expected']['score'], \
r['metric_results']['assessment_matches_expected']['explanation']) \
for c in d['eval_case_results'] for r in c['response_candidate_results']]"

# the raw agent output (the record_assessment tool call per case)
ls -t artifacts/traces/*.json | head -1
```

## Interpreting

`mean_score` is the fraction of (risk, effort, split) fields the agent
got right, averaged over cases. A perfect case scores 1.0. Divergences
are real signal about the assessor's calibration — e.g. reading the
deceptive `PAY-102` item as medium instead of the intended low is the
kind of finding the eval exists to surface, not a harness bug.

**Scores wobble run to run** (~0.75–0.85): the agent uses Gemini at its
default temperature, not 0, so inference isn't deterministic. For stable
numbers, pin temperature to 0 or average several samples per case — the
metric itself is fully deterministic.

## Troubleshooting

| Symptom | Fix |
|---|---|
| store won't start / `Address already in use` | something's on 8799: `lsof -ti :8799 \| xargs kill`, re-run |
| `GOOGLE_API_KEY` / auth errors | parent `.env` not sourced — check `grep GOOGLE_API_KEY ../../.env` shows a value |
| `uv sync` fails on the `eval` extra | re-run `uv sync --dev --extra eval`; needs network for `google-adk[eval]` |
| `No module named …` on agent load | a parent runtime dep is missing from `pyproject.toml` deps — add it and re-sync |
