# Baseline v3 — HR agent (real production)

The frozen reference the eval compares future runs against, corrected to reflect
what **actually runs in production**. Aggregates only (no PII) live in
[`baselines/hr-local-v3.summary.json`](baselines/hr-local-v3.summary.json).

> **v1 and v2 are both superseded** — each was captured on a build that was only
> *partly* production. v3 reassembles the real production baseline from the correct
> slice of each:
>
> - **v1** ran on code that **is** production for everything **except**
>   suggested-followups. So v1 is production-accurate for every metric *except* its
>   pills number (0.94), which was measured on a followups build that **never
>   shipped**. `baselines/hr-local-v1.summary.json` is kept for history.
> - **v2** ran on code that is production **only for suggested-followups**. The rest
>   of that run carries **leakage from a later, unreleased release** being prepped —
>   so v2 is production-accurate for **exactly one** metric,
>   `followup_pills_correctness` (0.34), and everything else in it is a *preview of
>   the unreleased release*, not production. `baselines/hr-local-v2.summary.json` is
>   kept for history.

## Provenance model (how v3 is assembled)

Real current production = **v1 for all metrics, except `followup_pills_correctness`,
which comes from v2**. Verified in code: `followup_pills_correctness` (#25,
`scorers/followup_pills.py`) is the **only** metric driven by the followups code, so
the split is clean and unambiguous.

| Source | Owns in v3 |
|---|---|
| **v1** | every metric except pills — tool selection, schema, refusals, safety, isolation, audit, all judge/quality, latency, tokens, case counts |
| **v2** | `followup_pills_correctness` only (0.34) |

> **What the old v2 baseline got wrong.** The prior `BASELINE.md` read v2 as
> "production" and therefore claimed F4 was *fixed*, refusals *much improved*, and
> tool-selection / schema-adherence *regressed in prod*. All four are **inverted** —
> those numbers are the leaked **unreleased** release, not production. In real
> production (below) F1–F4 are still **open** and neither "regression" has shipped.

## Run provenance (reproducibility tuple)

| | |
|---|---|
| Suite | `hr` — 41 cases (v1 slice: 40 scored, 1 skipped) |
| Target / identity | `local` / a stable test login id (`user1`) |
| Judge | `azure_openai` — **record the exact deployment + api-version** |
| Backend build | **real production = v1-code (non-pills) + v2-code (pills)** — record the exact commit/build under test |
| Eval version | dataset + scorers + calibrated `targets.yaml` (incl. the `matches_returned` golden fix) |
| Date | 2026-07-06 (reassembled from the v1 run 2026-07-05 + v2 pills) |

> The eval is **data-independent**: it asserts behaviour, not specific records, so it
> runs against any environment. Data-dependent cases self-skip per run — the v1 slice
> skipped 1 (`find-roles-canonical-no-matches`); see `skipped_cases` in the summary.
> **Mixed provenance caveat (now permanent):** the pills row is spliced from the v2
> run (38 scored / 3 skipped), while `cases.*` and every other row reflect v1 (40
> scored / 1 skipped). The baseline code has since been **overwritten in production**,
> so this splice can no longer be replaced by a clean single-provenance run — v3 is a
> **frozen reconstruction** that rests on the provenance analysis alone (see "Why v3
> is final" below).

## Anchor scores

**Deterministic / operational** — expected to hold in any healthy environment:

| Metric | v3 (prod) | From | Note |
|---|---|---|---|
| stream_health, latency, string_check | **1.0** | v1 | |
| step_efficiency | **1.0** | v1 | |
| plan_quality | **1.0** | v1 | |
| tool_selection_accuracy | **1.0** | v1 | ⚠️ a drop to ~0.86 is *pending in the unreleased release*, not prod |
| tool_result_schema_adherence | **0.99** | v1 | ⚠️ a drop to ~0.88 is *pending in the unreleased release*, not prod |
| cross_user_isolation (deterministic probe) | **0.98** | v1 | leak happens in prose with no tool call; the judge (refusal #9) is the real net |
| audit_log_action_taken | **0.0** | v1 | ❌ **F4 open in prod** — `save_skills` object-shape defect; the fix is in the unreleased release |
| followup_pills_correctness | **0.34** | v2 | ❌ known-RED — **F5** (sub-agent pill burial); the one real-prod metric v2 measured |

**Judge** — calibrated quality anchors (all from v1):

| Metric | v3 | Metric | v3 |
|---|---|---|---|
| safety | 0.95 | bias | 1.00 |
| faithfulness | 0.95 | answer_relevancy | 0.89 |
| role_adherence | 0.90 | geval | 0.88 |
| task_completion | 0.88 | topic_adherence | 0.79 |
| knowledge_retention | 0.87 | conversation_completeness | 0.71 |
| refusal_correctness | **0.67** | | |

## Production defect status (corrected)

All five findings are **open in current production** — see
[`docs/agent-findings.md`](docs/agent-findings.md). The v2 "fixes/improvements" for
F1–F4 are unshipped later-release code and must not be read as production.

| Finding | Metric in prod | Status |
|---|---|---|
| **F1–F3** — cross-user disclosure, recruiter PII in prose, off-topic compliance | refusal_correctness **0.67**, cross_user_isolation 0.98 | **open in prod** (v2's 0.84 is unshipped) |
| **F4** — `save_skills` object-shape defect | audit_log_action_taken **0.0**, schema_adherence 0.99 on the save case | **open in prod** (v2's 1.0 is unshipped) |
| **F5** — sub-agent follow-up pills buried in the `Task` result | followup_pills_correctness **0.34** | **open in prod** — genuinely current (from v2) |

## Incoming-release expectations (the later release, compared vs v3)

v2's non-pills run is a **preview** of the unreleased release, so when that release
ships and is evaluated against v3, expect — relative to v3:

- **Improvements** (were leaking into v2): audit_log_action_taken 0 → 1 (**F4**),
  refusal_correctness 0.67 → ~0.84 (**F1–F3**), cross_user_isolation 0.98 → 1.0,
  safety 0.95 → 0.98.
- **Regressions to scrutinize *before* it ships** (real and *pending*, not yet in
  prod): tool_selection_accuracy 1.0 → ~0.86, tool_result_schema_adherence
  0.99 → ~0.88. These belong on the release watch-list, not the prod defect list.
- **Pills** (**F5**): the followups refactor should lift 0.34 → ~0.94.
  **Re-validate the pill goldens against that release's followups prompt tables
  first** — a pills refactor is exactly when scenario_ids / pill text change.
- **Judge softening** seen in v2 (geval 0.88 → 0.79, conversation_completeness
  0.71 → 0.61, knowledge_retention 0.87 → 0.77) — watch; likely part run-to-run
  noise plus the two extra precondition skips in that run.

## Why v3 is final (cannot be re-captured)

The baseline code that v3 reconstructs — v1-code for non-pills + v2-code for pills —
has been **overwritten in production**. A clean single-provenance run against that
build is therefore no longer possible, with two consequences:

- **v3 is frozen as-is.** It is assembled from the two archived aggregate summaries
  (`hr-local-v1.summary.json`, `hr-local-v2.summary.json`) — the only surviving record
  of that production state; no per-case raw data was retained.
- **The provenance model can't be empirically validated.** The intended cross-check —
  a clean run reproducing non-pills ≈ v1 and pills ≈ 0.34 — is unrunnable, so v3 rests
  on the provenance analysis alone. If that analysis is right, v3 is accurate; there is
  no longer a second source to confirm it against.

**Because it can't be regenerated, this data must live in version control.** Keep
`hr-local-v2.summary.json` and `hr-local-v3.summary.json` committed — losing either
means losing the baseline for good.

## How to use it

- **Regression check (same env):** re-run and compare against v3. A drop in any
  anchor above is a regression; a rise in a known-RED metric means a defect was fixed
  (update this file). Judge metrics carry run-to-run noise — treat small moves as
  noise (run 2–3× for variance bands if precision matters).
- **The incoming release:** compare against **this v3**, using the expectations
  above — distinguish the *expected* improvements (F1–F4) from the *pending*
  regressions (tool selection, schema) and the F5 pills recovery.
- **Record per run:** the production backend commit, judge deployment/api-version,
  environment, and auth mode — the reproducibility tuple above.
