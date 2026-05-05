# Golden Dataset — Executive Summary

**Project:** Cost intelligence stack (Vertex AI Agent Engine)  
**Purpose:** Versioned quality benchmark for automated evaluation of the cost and orchestration agents.

---

## What is the golden dataset?

The **golden dataset** is a small, curated set of test questions (“cases”) with **expected outcomes**. It is the authoritative baseline we use to answer:

- Does the assistant still **ask for clarification** when a question is ambiguous?
- Does it still **answer numerically** when the question is precise enough?
- Does it still **fail cleanly** on invalid schema requests?
- Does **multi-turn conversation** still fill missing details and then answer?

Each case has a stable ID and is versioned (currently **v1** in `golden_dataset_v1.json`).

---

## The three official files (golden bundle)

| File | Role |
|------|------|
| `scripts/evals/golden_dataset_v1.json` | The actual test cases (prompts, expected modes, assertions). |
| `scripts/evals/golden_dataset_schema.json` | JSON Schema validating the shape of each case. |
| `scripts/evals/golden_dataset_readme.md` | Short technical reference for developers. |

Together, these define **what we measure** and **how cases must be structured**.

**Note:** The larger regression packs (`agent_engine_eval_cases.json`, `agent_engine_multiturn_cases.json`) are **not** part of the golden bundle; they provide broader coverage beyond the curated baseline.

---

## What one “case” contains

Each case specifies:

| Field | Meaning |
|--------|--------|
| **id** | Stable identifier (e.g. `gdv1.answer.compare.services.last_30_days`). |
| **category** | Area under test (clarification, comparison, schema, etc.). |
| **priority** | **P0** = must not break in production; **P1/P2** = important but secondary. |
| **expected_mode** | High-level outcome: **clarify**, **answer**, or **error**. |
| **expected_response_type** | Typed contract: clarification / result / error (when applicable). |
| **prompt** OR **turns** | Single message, or a short multi-turn dialogue. |
| **must_contain_any** / **must_not_contain_any** | Optional text checks for additional safety. |

Scoring prefers **structured payloads** (e.g. `response_type`) when the agent returns them, so we are not relying only on free-form wording.

---

## Current contents (v1) — at a glance

| # | id (short) | Type | Priority | What it validates |
|---|------------|------|----------|---------------------|
| 1 | clarify top services (no top-N) | Single-turn | P0 | Ambiguous “most expensive” asks for scope (e.g. top N / time). |
| 2 | Cloud SQL spend this year + project | Single-turn | P0 | Explicit time + project → should return a **result**, not unnecessary clarification. |
| 3 | Compare Cloud SQL vs Vertex AI (30 days) | Single-turn | P0 | Enough context for **comparison answer** vs spurious clarification. |
| 4 | Unknown column error | Single-turn | P1 | Schema error path: invalid column → **error**, not hallucinated data. |
| 5 | Multi-turn fill time window | Multi-turn | P0 | User clarifies window after vague question → **result**. |
| 6 | Multi-turn compare scope + window | Multi-turn | P0 | User supplies entities + window across turns → **result**. |

**Counts:** 6 cases total — **4 single-turn**, **2 multi-turn**.  
**Severity:** **5 × P0**, **1 × P1**.

---

## How evaluation runs

1. **Harness:** `scripts/agent-engine-create-eval.py` loads `golden_dataset_v1.json`.
2. **Execution:** For each case, the script talks to the deployed **Agent Engine** (cost agent or orchestrator, depending on `--resource`).
3. **Local scoring:** The script computes **pass/fail** using expected mode, response type, and optional text checks. It outputs a JSON report under `logs/` and a summary (e.g. `5/6 passed`).
4. **Optional Vertex publish:** If `--publish-to-vertex` is used, Google’s **managed evaluation** also scores responses with rubric metrics (quality, tool use, hallucination, safety).

So: **golden dataset drives both strict regression checks and optional cloud-side quality scoring.**

---

## How pass/fail and “success percentage” work

- **Per case:** Pass if **all** checks for that case succeed (mode, type, and any must/must-not rules).
- **Overall:**  
  - **Pass rate** = (cases passed ÷ total cases), e.g. `83%` if 5 of 6 pass.  
  - **P0 gate:** Often all **P0** cases must pass before we treat a release as safe.

This is transparent and reproducible; the boss can see **which case IDs failed** in the report JSON.

---

## Relationship to Vertex “metrics”

- **Local deterministic scoring** — our rules on the golden cases (release gate).
- **Vertex rubrics** (when publishing) — additional **quality judgments** (`FINAL_RESPONSE_QUALITY`, `TOOL_USE_QUALITY`, `HALLUCINATION`, `SAFETY`), configurable including a cheaper **single-metric** mode.

They complement each other: local checks enforce contracts; Vertex metrics summarize holistic quality.

---

## Why this matters for stakeholders

| Benefit | Explanation |
|---------|---------------|
| **Regression safety** | Prevents silent breakage after model or routing changes. |
| **Transparency** | Every expectation is documented in JSON, not tribal knowledge. |
| **Version control** | v1 can evolve to v2 with a clear changelog. |
| **Cost control** | Same harness supports full runs vs smaller smoke subsets. |

---

## Suggested wording for slides (one paragraph)

*"We maintain a versioned golden dataset: a compact set of representative user questions with expected clarification, answer, or error behavior. Automated runs execute these against our deployed Vertex agents, producing pass/fail results and optional Google evaluation metrics. This gives us repeatable release gates for the cost intelligence assistant."*

---

*Document generated for reporting. Technical source files live under `scripts/evals/`.*
