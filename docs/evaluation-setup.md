# Building a complete evaluation

This guide takes you from *"the harness runs against the backend"* to a
**trustworthy, repeatable evaluation program** — using the HR agent (the BFF
backend's chat agent) as the worked example. The same steps apply to any agent
the framework drives.

Getting the plumbing working (transport, auth, thread creation, TLS, judge
connectivity) is covered elsewhere — the [README](../README.md) for running
against the backend, and [troubleshooting.md](troubleshooting.md) for the
judge/TLS/proxy issues. **This doc is what to build *on top of* that** so the
numbers are meaningful and the eval is something you can run on every change.

> The metric catalog these phases reference is [docs/metrics.md](metrics.md).

## Prerequisites — one clean run

Before this guide helps, a single run must complete end-to-end:

```bash
agent-evals run --target local --suite hr --metrics all --judge azure_openai --sink jsonl
```

You should see `summary.json` with `latency.abort_rate ≈ 0` **and** the nine
LLM-judge metrics present (`task_completion`, `faithfulness`, `safety`, …). If
judged metrics are missing, the judge is failing silently — see
[troubleshooting.md](troubleshooting.md) and read `scores.jsonl`.

---

## Phase A — Validate the baseline

1. **Confirm every metric populates** in `summary.json` (a missing metric = it
   failed or was skipped; the real reason is in `scores.jsonl`).
2. **Sanity-check the verdicts.** Open `runs.jsonl` (full transcripts) and
   `scores.jsonl` (per-case scores) and read 5–10 cases by hand: do the
   faithfulness / safety / relevancy judgements actually match the transcript?
   This is your calibration baseline — if the judge is obviously wrong here,
   fix Phase B.3 before trusting any aggregate.

## Phase B — Make the eval *content* real (the substance)

This is where most of the work is. The bundled `datasets/hr/` is a small
**example**, not a real suite.

### B.1 Build a representative dataset
Expand `datasets/hr/` to cover each capability with enough volume to be
statistically meaningful:
- **Per tool / subagent:** profile management (`get_skills`, `suggest_skills`,
  `get_talent_profile`, `analyze_talent_profile`, `save_skills`), requisition
  matching (`suggest_requisitions`, `view_requisition`), recruiter outreach
  (`draft_message`), and free-form career guidance.
- **Multi-turn scenarios** — required for conversation-completeness (#10) and
  knowledge-retention (#11).
- **Adversarial probes** — refusal (`must_refuse`, `expected_redirect`),
  cross-user isolation (`other_user_id`), and forbidden content
  (`forbidden_substrings`).
- **Golden expectations** wherever the outcome is deterministic
  (`expected_tool_calls`, `expected_tool_args`, `expected_routes`,
  `response_must_contain`, `remembered_facts`).

Curate with an HR subject-matter expert and version the suite.

### B.2 Complete the tool contracts
`tool_result_schema_adherence` (#4) only checks tools that have a registered
schema in `contracts/tools/v1/`. Any tool the agent calls without one is silently
skipped. Add a JSON Schema per tool, mirroring the **real frontend/backend result
shape**, so the contract check is actually covering your tools.

### B.3 Calibrate the judges
Out of the box the judge criteria are generic. For trustworthy scores:
- **Set the persona** in `targets.yaml` (`scoring.persona`) from the
  orchestrator's system prompt — this is what role-adherence (#21) judges against.
- **Tighten `scoring.topic_scope`** and per-metric criteria for the talent domain.
- **Set pass thresholds** per metric and a `scoring.latency_total_sla_ms`.
- **Pin the judge model** (deployment + api-version) so verdicts are stable.
- **Validate judge ↔ human agreement** on a labelled sample before trusting
  aggregates. Use `judges/benchmark.py:compare_judges` to A/B backends, and
  `judge.per_metric` in `targets.yaml` to route specific metrics to specific
  judges.

### B.4 Pin a deterministic test identity
The agent serves **only the caller's own profile** (resolved from the JWT user
login id), so your golden expectations depend on that profile's data. Use a
**fixed test login id whose profile is stable/seeded** — otherwise goldens drift
run-to-run.

## Phase C — Operationalize

### C.1 Capture a reproducible environment
Record what made the run work — `SSL_CERT_FILE`, lowercase `no_proxy` (incl. the
Azure host), `AGENT_EVALS_USER_LOGIN_ID`, `AGENT_EVALS_*_BASE_URL`, judge
`AZURE_OPENAI_*`, and the **backend build/commit under test** — in `.env` plus a
short runbook, so anyone can reproduce the exact run.

### C.2 Track runs and define gates
Run `--sink mlflow` against a **durable** tracking store (not the ephemeral
`./mlruns`) so runs are comparable over time. Pick north-star aggregates
(task_completion / faithfulness / safety pass-rates, `latency.*.p95`,
`abort_rate`) and turn them into **fail-the-build gates**.

### C.3 Automate
Schedule it where the network can reach **both** the backend and the judge —
which, given the private-endpoint constraints, generally means **in-pod**. A
smoke subset (`--limit` / a small `--metrics` set) pre-merge, the full suite
nightly / per-release.

## Phase D — Harden (HR-critical)

- **Safety / PII depth.** The agent handles sensitive profile and compensation
  data — add explicit PII-leak, prompt-injection, and cross-user-refusal cases
  beyond the example probe.
- **Side-effect verification.** Audit/action (#16) only proves a mutating tool
  (`save_skills`) ran with `ok` status, not that the datastore actually changed.
  Add a post-run verifier if you need real persistence assurance.
- **Know the SSE approximations.** Token/cost (#14) is *estimated* on SSE
  (`usage.source = estimated`) and subagent routes (#19) are *synthesized* from
  `STEP_*`/`Task` events. Accept them, or wire real signals from the backend.

---

## What each metric needs from you

| You provide | Where | Metrics it unlocks |
|---|---|---|
| `expected_tool_calls`, `expected_tool_args`, `expected_routes`, `allowed_tool_calls`, `max_steps` | dataset case | tool selection (#2), tool args (#3), plan quality (#19), step efficiency (#18) |
| `expected_response`, `response_must_contain` | dataset case | answer equivalence (#6), task completion (#1), string check (#22) |
| `must_refuse`+`expected_redirect`, `other_user_id`, `forbidden_substrings`, `remembered_facts`, `expected_actions` | dataset case | refusal (#9), isolation (#8), safety (#7), knowledge retention (#11), audit/action (#16) |
| tool result JSON Schemas | `contracts/tools/v1/` | tool result schema (#4), stream health (#24) |
| `persona`, `topic_scope`, rubric, thresholds, latency SLA | `targets.yaml` (`scoring`/`judge`) | role adherence (#21), topic (#12), G-Eval (#17), pass/fail + latency (#13) |
| a reachable LLM judge | `--judge` + env | faithfulness (#5), bias (#15), relevancy (#20), task completion (#1), conversation completeness (#10) |
| production feedback export | `agent-evals ingest-feedback` | user-feedback signal (#23) |
| *(nothing — automatic from the stream)* | — | latency (#13), token/cost (#14, estimated), stream health (#24) |

## Definition of done

A genuinely functioning eval = **Phase A + Phase B + C.1–C.2**: a clean run, a
real dataset with goldens, complete contracts, calibrated judges, a fixed test
identity, a reproducible env, and tracked runs with gates. Phase C.3 and Phase D
are hardening you can layer in once the core is trustworthy.

### Checklist

- [ ] One clean run; all metrics populate; verdicts sanity-checked against transcripts
- [ ] Dataset covers every tool/subagent, multi-turn, and adversarial probes, with goldens
- [ ] A tool contract for every tool the agent calls
- [ ] Persona, topic scope, thresholds, and latency SLA set; judge model pinned; judge validated vs humans
- [ ] Fixed test login id with seeded/stable profile data
- [ ] Reproducible `.env` + runbook; backend build under test recorded
- [ ] Durable MLflow tracking store; north-star gates defined
- [ ] Scheduled in-pod (smoke pre-merge, full nightly)
- [ ] Safety/PII coverage; mutation side-effects verified (optional)
