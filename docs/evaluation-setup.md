# Authoring, calibrating, and operating the eval

How the eval is built and how to change it. The harness, dataset, calibrated
judges, and a frozen baseline already exist — this is the reference for extending
them. For running mechanics see the [README](../README.md); for the metric
catalog, [metrics.md](metrics.md); for the frozen reference run,
[../BASELINE.md](../BASELINE.md).

## Data-independence (the core principle)

The eval runs against multiple environments (dev/UAT/prod) whose data differs, so
it must not depend on seeded/frozen records. Goldens assert **behaviour**, not
specific data:

- **Structural / behavioural goldens** — tool selection, routing, follow-up pill
  scenario + set (fixed UX strings), refusals/redirects, schema adherence,
  no-leakage, and judge metrics graded against whatever the tools returned. These
  hold in any environment.
- **No pinned data** — never hard-code requisition ids, recruiter names, role
  titles, specific skills, or a reference answer. (Answer Equivalence #6 is unused
  here for exactly this reason — it needs a pinned golden answer.)
- **Per-run facts** — whether an environment has matched requisitions / a complete
  profile / saved skills is derived from each run's tool results
  (`datasets/facts.py`), not configured. A case declares `requires:` a fact; if the
  run doesn't satisfy it, the case is **skipped and reported** (`cases.skipped_precondition`
  + `skipped_cases` in the summary), never silently dropped.

A baseline is therefore **per-environment** for absolute scores; the
data-independent metrics are the **cross-environment contract**.

## What each metric needs from the dataset

| Provide | Where | Metrics it drives |
|---|---|---|
| `expected_tool_calls`, `expected_tool_args`, `allowed_tool_calls`, `expected_routes`, `max_steps` | case | tool selection (#2), tool args (#3), plan quality (#19), step efficiency (#18) |
| `expected_scenario_id`, `expected_pills` | case | follow-up pills (#25) |
| `response_must_contain`, `forbidden_substrings` | case | string check (#22), safety negative-check (#7) |
| `must_refuse` + `expected_redirect`, `other_user_id` | case | refusal (#9), cross-user isolation (#8) |
| `expected_actions` | case | audit / action taken (#16) |
| `remembered_facts` | multi-turn case | knowledge retention (#11) |
| `rubric` | case | G-Eval (#17) |
| `requires` + tool result JSON Schemas | case + `contracts/tools/v1/` | precondition skip; schema adherence (#4) |
| `persona`, `topic_scope`, `thresholds`, `latency_total_sla_ms` | `targets.yaml` `scoring` | role adherence (#21), topic (#12), pass/fail, latency (#13) |
| a reachable LLM judge | `--judge` + env | faithfulness (#5), safety (#7), relevancy (#20), task completion (#1), conversation completeness (#10), bias (#15) |
| *(automatic from the stream)* | — | latency (#13), token/cost (#14, estimated on SSE), stream health (#24) |

## Authoring & extending the dataset

The suite is `datasets/hr/*.yaml`, one file per capability cluster (every `*.yaml`
in the directory loads as the `hr` suite). A case is single-turn
(`inputs.question`) or multi-turn (`inputs.turns[]`); `requires:` gates it on
run-derived facts. To add coverage:

- **A capability** → cases asserting the right tool/route + the pill scenario;
  keep assertions structural (no pinned data).
- **A context-dependent capability** (needs a role/draft established by a prior
  turn) → a multi-turn journey (see `journeys.yaml`), not a single-turn case.
- **A tool the agent calls** → its result JSON Schema in `contracts/tools/v1/`, so
  #4 covers it (an uncontracted tool is silently skipped).
- **A guardrail** → an adversarial case authored by intent (`must_refuse`,
  `other_user_id`, `forbidden_substrings`), never captured from current output.

## Judge calibration

The default judge criteria are generic; calibration tunes them to the domain and
to human agreement. Configured in `targets.yaml` `scoring`:

- **persona** (role adherence #21) and **topic_scope** (#12), derived from the
  orchestrator's system prompt — including the legitimate product affordances, so
  the judge doesn't penalize them as fabrication.
- **thresholds** — per-metric pass/fail; deterministic at 1.0, judge metrics tuned
  against a human-labelled sample (safety/refusal stricter).
- Judges receive **conversation history + tool outputs** as context (so recall and
  tool-/card-delivered results aren't misjudged), and metrics that don't apply to
  a refusal (task completion, faithfulness) **skip `must_refuse` cases** — refusal
  correctness (#9) owns those.

To recalibrate: run `--judge azure_openai`, read the judge rationales in
`scores.jsonl` against the transcripts, adjust criteria/thresholds until the
verdicts match human judgement, then re-freeze the baseline.

## Operating the eval

- **Run:** `agent-evals run --target <env> --suite hr --metrics all --judge azure_openai`.
- **Compare:** against [`BASELINE.md`](../BASELINE.md). In the same environment a
  drop in an anchor is a regression; across environments the data-independent
  metrics are the gate.
- **Track over time:** `--sink mlflow` against a durable tracking store; turn
  north-star aggregates (faithfulness/safety pass-rates, `latency.*.p95`,
  `abort_rate`) into build gates.
- **Automate:** schedule where the network reaches both the backend and the judge
  (generally in-pod) — a smoke subset pre-merge, the full suite nightly/per-release.
- **Record per run:** backend commit, judge deployment/api-version, environment.

## Porting to another agent

The seams make this a per-adapter change, not a rewrite: a new `Transport` (it
populates the same `RunRecord`), a new fact-deriver (the equivalent of
`datasets/facts.py`), that agent's tool contracts, and its `persona`/`topic_scope`.
`core/`, `scorers/`, and the metric catalog are unchanged.
