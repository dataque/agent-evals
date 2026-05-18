# agent-evals

Protocol-pluggable evaluation framework for tool-calling chat agents. MLflow-driven, project-pluggable, and protocol-agnostic — designed to evaluate the same scorer set across A2A (Phase 1) and ag-ui (Phase 2) protocols.

## Status

| Layer | Status |
|---|---|
| Core abstractions (`agent_evals/core/`) | Phase 1 |
| A2A protocol adapter | Phase 1 |
| MLflow runner | Phase 1 |
| Built-in + trace-aware scorers | Phase 1 |
| OAuth2 auth (Entra ID) | Phase 1 |
| `hr_agent_poc` project plug-in | Phase 1 (smoke test) |
| `backend` project plug-in via A2A | Phase 1 (production) |
| Tool Result Schema Adherence scorer | Phase 2 |
| Identity / Tenant Isolation harness | Phase 2 |
| CI / regression workflows | Phase 2 |
| **AGUI protocol adapter** | **Phase 3 (separate plan)** |

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
pip install -e projects/hr_agent_poc
pip install -e projects/backend

cp .env.example .env  # fill in Azure OpenAI + Entra ID credentials

# Smoke-test against the HR Agent PoC A2A endpoint
python -m agent_evals --project hr_agent_poc --target fa --scorers all

# Production eval against backend dev
python -m agent_evals --project backend --target dev --scorers builtin --auth-profile entra-dev
```

## Authoring a new project plug-in

See `docs/plugin_guide.md`.

## Provenance

Built on the patterns proven in `/Users/neo/projects/chat-evals/` (the HR Agent PoC). The PoC is preserved as a reference and is NOT modified by this repo; documents and scorer logic are ported, not shared.

The approved implementation plan (CR breakdown, repo layout, verification gates, out-of-scope items) lives at [`docs/implementation_plan.md`](docs/implementation_plan.md).
