# hr_agent_poc — project plug-in

Evaluates the **HR Agent PoC** A2A endpoint deployed from `/Users/neo/projects/chat-evals/`. Serves as the agent-evals framework's smoke test: a run here should reproduce chat-evals' baseline numbers (within LLM-judge variance).

## Endpoint targets

- `fa` — Azure Function App (direct A2A, function-key embedded in URL). No auth header required.
- `bff-dev` — BFF Dev environment (requires SSO Bearer token via `--token`).

Edit `targets.yaml` to add `bff-uat`, `bff-prod`, etc.

## Datasets

`profile` (`PROFILE_SKILLS_DATASET`) — 7 items mixing single- and multi-turn flows: skill suggestion, modification, confirmation; profile analysis; auto-match.

## Project-specific scorers

Three natural-language `Guidelines` rubrics (`professional_tone`, `hr_relevance`, `data_privacy`) — these were in chat-evals' core; in the framework, project policy lives with the project.

## Running

```bash
# from agent-evals root
pip install -e .
pip install -e projects/hr_agent_poc

# Smoke test against the FA target (no auth):
python -m agent_evals --project hr_agent_poc --target fa --scorers all

# Against bff-dev (needs SSO):
python -m agent_evals --project hr_agent_poc --target bff-dev --token "$SSO" --scorers all
```

## Comparing against chat-evals

The deterministic scorers (`response_completeness`, `tool_trace_f1`, `tool_argument_correctness`, `step_efficiency`, `plan_quality`, `audit_log_action_taken`, `card_format_correctness`) should produce **byte-identical** results to chat-evals on the same dataset against the same endpoint. The judged scorers (`Correctness`, `RelevanceToQuery`, `Safety`, the three `Guidelines`) vary within LLM-judge noise (typically ±0.05).
