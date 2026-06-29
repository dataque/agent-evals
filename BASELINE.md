# Baseline v1 — HR agent

The frozen reference the eval compares future runs against. Captured from the
first fully-calibrated judged run. Aggregates only (no PII) live in
[`baselines/hr-local-v1.summary.json`](baselines/hr-local-v1.summary.json).

## Run provenance (reproducibility tuple)

| | |
|---|---|
| Suite | `hr` — 41 cases, 6 clusters (40 scored, 1 skipped by precondition) |
| Target / identity | `local` / a stable test login id (`user1`) |
| Judge | `azure_openai` — **record the exact deployment + api-version** |
| Backend build | **record the backend commit/build under test** |
| Eval version | repo tag `baseline-v1` (dataset + scorers + calibrated `targets.yaml`) |
| Date | 2026-06-29 |

> The eval is **data-independent**: it asserts behaviour, not specific records, so
> it runs against any environment. Data-dependent cases self-skip per run (see
> `cases.skipped_precondition` + `skipped_cases` in the summary).

## Anchor scores

**Deterministic / operational** — expected to hold in any healthy environment:

| Metric | Baseline |
|---|---|
| tool_selection_accuracy, plan_quality, step_efficiency, stream_health, latency | **1.0** |
| tool_result_schema_adherence | 0.99 (the one miss is the `save_skills` defect, F4) |
| followup_pills_correctness | 0.94 |
| cross_user_isolation (deterministic probe) | 0.98 — see note under F1 |

**Judge** — calibrated; these are the quality anchors:

| Metric | Baseline | Metric | Baseline |
|---|---|---|---|
| faithfulness | 0.95 | role_adherence | 0.90 |
| answer_relevancy | 0.89 | task_completion | 0.88 |
| knowledge_retention | 0.87 | geval | 0.88 |
| topic_adherence | 0.79 | conversation_completeness | 0.71 |
| safety | 0.95 | bias | 1.00 |

**Known-RED (real defects — see [`docs/agent-findings.md`](docs/agent-findings.md))** — a fix flips these green:

| Metric | Baseline | Tracks |
|---|---|---|
| audit_log_action_taken | **0.0** | F4 — `save_skills` fails |
| refusal_correctness | **0.67** (56% pass) | F1 cross-user fabrication, F2 recruiter PII, F3 off-topic compliance |

> F1 note: the deterministic isolation probe (#8) scores ~clean because the agent
> fabricates in prose with no tool call; only the **judge** (refusal #9) catches it.

## How to use it

- **Regression check (same env):** re-run and compare. A drop in any anchor above
  is a regression; a rise in a known-RED metric means a defect was fixed (update
  this file). Judge metrics carry run-to-run noise — treat small moves as noise
  (run 2–3× for variance bands if precision matters).
- **Cross-env gate (dev/UAT/prod):** the deterministic + safety/bias/faithfulness/
  relevancy/role/topic anchors should hold **regardless of data**. Refusal/audit
  reflect the *current* agent defects and will rise as those are fixed.
- **Record per run:** the backend commit, judge deployment/api-version, and env —
  the reproducibility tuple above.
