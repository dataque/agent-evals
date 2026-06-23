"""Azure OpenAI judge via LangChain — mirrors the hr-agent's LLM path.

The hr-agent reaches the Azure deployment with ``langchain_openai.AzureChatOpenAI``
(api-key auth, the four ``AZURE_OPENAI_*`` env vars, **no** ``max_tokens``). Using
the identical client here means the judge inherits exactly the connection
behaviour that already works on the dev pod — same library, same config contract,
same wire request — so if the agent can reach the model, so can the judge.

Differences from the raw-SDK ``AzureOpenAIJudge`` that matter in practice:
  * no ``max_tokens`` is sent (the SDK judge sent ``max_tokens=400``, which newer
    deployments reject — they want ``max_completion_tokens``);
  * the client is built the same way as ``core/llm.py`` in the hr-agent, so it
    picks up the same proxy / TLS / endpoint behaviour from the environment.

``langchain-openai`` is imported lazily (``pip install -e ".[langchain]"``) so the
core stays dependency-free.
"""

from __future__ import annotations

import os

from ..core.judge import JudgeVerdict
from .base_openai import _SYSTEM, _build_user, _parse_verdict


class LangchainAzureJudge:
    """Default Azure judge. Reads the SAME environment the hr-agent reads:
    ``AZURE_OPENAI_API_KEY``, ``AZURE_OPENAI_ENDPOINT``,
    ``AZURE_OPENAI_DEPLOYMENT_NAME``, ``AZURE_OPENAI_API_VERSION``. Judge
    temperature comes from ``AZURE_OPENAI_JUDGE_TEMPERATURE`` (default ``0`` for
    deterministic scoring; set it to the agent's value if a deployment is picky)."""

    name = "langchain_azure"

    def __init__(self, *, model: str | None = None, temperature: float | None = None,
                 client=None, **_ignored) -> None:
        self.model = model or os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME")
        self.temperature = (
            temperature if temperature is not None
            else float(os.getenv("AZURE_OPENAI_JUDGE_TEMPERATURE", "0") or 0)
        )
        self._client = client

    def _ensure_client(self):  # pragma: no cover - requires SDK + creds
        if self._client is None:
            from langchain_openai import AzureChatOpenAI

            # Built exactly like the hr-agent's core/llm.py — note: NO max_tokens.
            self._client = AzureChatOpenAI(
                azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
                api_key=os.getenv("AZURE_OPENAI_API_KEY"),
                azure_deployment=self.model,
                api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
                temperature=self.temperature,
            )
        return self._client

    def evaluate(self, *, criteria, response, question=None, context=None, reference=None) -> JudgeVerdict:
        user = _build_user(criteria, response, question, context, reference)
        # role/content dicts are coerced by langchain — no langchain_core import needed,
        # which keeps this unit-testable with a fake client and no SDK installed.
        messages = [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]
        try:
            result = self._ensure_client().invoke(messages)
        except Exception as exc:  # never let a judge call abort scoring
            return JudgeVerdict(score=0.0, passed=None, rationale=f"judge error: {exc}",
                                raw={"error": str(exc)})
        raw = getattr(result, "content", result)
        if not isinstance(raw, str):
            raw = str(raw)
        return _parse_verdict(raw)
