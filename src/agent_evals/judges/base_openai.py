"""Direct LLM judges (the default backend): Azure OpenAI and OpenAI.

These prompt a chat model to score a response against natural-language criteria
and return strict JSON. They match the backend + prior-harness stack (Azure
OpenAI). The ``openai`` SDK is imported lazily so the core stays dependency-free.
"""

from __future__ import annotations

import json
import os

from ..core.judge import JudgeVerdict

_SYSTEM = (
    "You are a strict, impartial evaluation judge for an AI assistant. "
    "Given CRITERIA and the assistant's RESPONSE (with optional QUESTION, CONTEXT, "
    "and REFERENCE), decide how well the response satisfies the criteria. "
    "Respond with ONLY a compact JSON object of the form "
    '{"score": <number between 0 and 1>, "pass": <true|false>, "rationale": "<one sentence>"}. '
    "score 1.0 = fully satisfies the criteria; 0.0 = fails entirely."
)


def _build_user(criteria, response, question, context, reference) -> str:
    parts = [f"CRITERIA:\n{criteria}"]
    if question:
        parts.append(f"QUESTION:\n{question}")
    if context:
        parts.append(f"CONTEXT (e.g. tool outputs the response should rely on):\n{context}")
    if reference:
        parts.append(f"REFERENCE (a known-good answer):\n{reference}")
    parts.append(f"RESPONSE (to evaluate):\n{response}")
    return "\n\n".join(parts)


def _parse_verdict(raw: str) -> JudgeVerdict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):] if "{" in text else text
    try:
        obj = json.loads(text)
    except Exception:
        # last-ditch: find the first {...} span
        try:
            obj = json.loads(text[text.index("{"): text.rindex("}") + 1])
        except Exception:
            return JudgeVerdict(score=0.0, passed=None, rationale=f"unparseable judge output: {raw[:120]}")
    score = obj.get("score")
    try:
        score = max(0.0, min(1.0, float(score)))
    except (TypeError, ValueError):
        score = 0.0
    passed = obj.get("pass", obj.get("passed"))
    if not isinstance(passed, bool):
        passed = None
    return JudgeVerdict(score=score, passed=passed, rationale=str(obj.get("rationale", "")), raw=obj)


class _BaseLLMJudge:
    name = "llm"

    def __init__(self, *, model: str | None = None, temperature: float = 0.0,
                 max_tokens: int = 400, client=None) -> None:
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = client
        # Model families differ in request shape: GPT-5 / o-series renamed
        # ``max_tokens`` -> ``max_completion_tokens``, accept only the default
        # temperature, and some reject ``response_format``. Start with the classic
        # shape and degrade on the first 400, caching the result for later calls.
        self._token_param = "max_tokens"
        self._send_temperature = True
        self._send_response_format = True

    def _ensure_client(self):  # pragma: no cover - network/SDK glue
        raise NotImplementedError

    def _complete(self, messages: list[dict]) -> str:
        client = self._ensure_client()
        while True:
            kwargs: dict = {"model": self.model, "messages": messages,
                            self._token_param: self.max_tokens}
            if self._send_temperature and self.temperature is not None:
                kwargs["temperature"] = self.temperature
            if self._send_response_format:
                kwargs["response_format"] = {"type": "json_object"}
            try:
                resp = client.chat.completions.create(**kwargs)
                return resp.choices[0].message.content
            except Exception as exc:  # adapt to the model's parameter shape, then retry
                if self._adapt_params(exc):
                    continue
                raise

    def _adapt_params(self, exc: Exception) -> bool:
        """React to a 400 about an unsupported parameter by flipping one flag.

        Returns True (caller retries) only when a flag actually changed. Each flag
        flips at most once, so this loops at most three times before re-raising;
        any non-parameter error propagates unchanged."""
        msg = str(exc).lower()
        if not any(p in msg for p in
                   ("max_tokens", "max_completion_tokens", "temperature", "response_format")):
            return False
        changed = False
        if self._token_param == "max_tokens" and ("max_tokens" in msg or "max_completion_tokens" in msg):
            self._token_param = "max_completion_tokens"
            changed = True
        if self._send_temperature and "temperature" in msg:
            self._send_temperature = False
            changed = True
        if self._send_response_format and "response_format" in msg:
            self._send_response_format = False
            changed = True
        return changed

    def evaluate(self, *, criteria, response, question=None, context=None, reference=None) -> JudgeVerdict:
        user = _build_user(criteria, response, question, context, reference)
        try:
            raw = self._complete(
                [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]
            )
        except Exception as exc:  # never let a judge call abort scoring
            return JudgeVerdict(score=0.0, passed=None, rationale=f"judge error: {exc}",
                                raw={"error": str(exc)})
        return _parse_verdict(raw)


class OpenAIJudge(_BaseLLMJudge):
    name = "openai"

    def __init__(self, *, model: str | None = None, **kw) -> None:
        super().__init__(model=model or os.getenv("OPENAI_JUDGE_MODEL", "gpt-4o"), **kw)

    def _ensure_client(self):  # pragma: no cover - requires SDK + creds
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI()
        return self._client


class AzureOpenAIJudge(_BaseLLMJudge):
    """Default judge. Reads Azure config from the environment (matching the
    backend's deployment): AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT,
    AZURE_OPENAI_API_VERSION, AZURE_OPENAI_DEPLOYMENT_NAME."""

    name = "azure_openai"

    def __init__(self, *, model: str | None = None, **kw) -> None:
        super().__init__(model=model or os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME"), **kw)

    def _ensure_client(self):  # pragma: no cover - requires SDK + creds
        if self._client is None:
            from openai import AzureOpenAI

            self._client = AzureOpenAI(
                api_key=os.getenv("AZURE_OPENAI_API_KEY"),
                azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT", ""),
                api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            )
        return self._client
