# Golden Dataset (v1)

This directory defines a baseline "golden" eval dataset and schema for Agent Engine quality gates.

## Files

- `golden_dataset_v1.json`: versioned curated baseline cases.
- `golden_dataset_schema.json`: JSON schema for case shape and required fields.
- `agent_engine_eval_cases.json`: broader single-turn regression pack.
- `agent_engine_multiturn_cases.json`: multi-turn clarification and slot-fill regression pack.

## Case contract

Each case should include:

- `id`: stable unique identifier.
- `category`: functional area (`clarification`, `comparison`, `schema`, etc).
- `priority`: severity class (`P0`, `P1`, `P2`).
- `expected_mode`: one of `clarify`, `answer`, `error`.
- `prompt` or `turns`: single-turn or multi-turn input.
- Optional assertions: `must_contain_any`, `must_not_contain_any`.
- Optional `expected_response_type`: typed contract expectation (`clarification`, `result`, `error`, `text`).

## Release-gate usage

Use `scripts/agent-engine-create-eval.py` with strict flags:

- `--fail-on-assertion`
- `--min-pass-rate 0.95` (or stricter for stable releases)
- `--fail-on-priority P0`

Recommended baseline:

- P0 cases must all pass.
- Overall pass rate should stay above the chosen threshold.
- Any regression on clarification behavior blocks release.

Scoring notes:

- Mode/type assertions are evaluated from structured `response_type` payloads when present.
- Text assertions (`must_contain_any`, `must_not_contain_any`) remain secondary semantic checks.
