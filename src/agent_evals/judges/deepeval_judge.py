"""DeepEval-backed judge adapter (behind the neutral Judge interface).

Maps our generic ``evaluate`` onto DeepEval's ``GEval`` rubric metric. Requires
the ``deepeval`` extra installed and a judge LLM configured for DeepEval
(``OPENAI_API_KEY`` or a custom model). Imported lazily; a backend/config failure
returns an error verdict rather than raising, so a run never aborts.
"""

from __future__ import annotations

from ..core.judge import JudgeVerdict


class DeepEvalJudge:
    name = "deepeval"

    def __init__(self, *, model: str | None = None) -> None:
        self.model = model

    def evaluate(self, *, criteria, response, question=None, context=None, reference=None) -> JudgeVerdict:
        try:
            from deepeval.metrics import GEval
            from deepeval.test_case import LLMTestCase, LLMTestCaseParams

            params = [LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT]
            if reference:
                params.append(LLMTestCaseParams.EXPECTED_OUTPUT)
            if context:
                params.append(LLMTestCaseParams.CONTEXT)

            kwargs = {"name": "agentevals", "criteria": criteria, "evaluation_params": params}
            if self.model:
                kwargs["model"] = self.model
            metric = GEval(**kwargs)
            test_case = LLMTestCase(
                input=question or "",
                actual_output=response,
                expected_output=reference,
                context=[context] if context else None,
            )
            metric.measure(test_case)
            score = max(0.0, min(1.0, float(metric.score or 0.0)))
            passed = metric.is_successful() if hasattr(metric, "is_successful") else score >= 0.5
            return JudgeVerdict(score=score, passed=bool(passed), rationale=getattr(metric, "reason", "") or "")
        except Exception as exc:  # missing extra / misconfigured model / API error
            return JudgeVerdict(score=0.0, passed=None, rationale=f"deepeval judge error: {exc}",
                                raw={"error": str(exc)})
