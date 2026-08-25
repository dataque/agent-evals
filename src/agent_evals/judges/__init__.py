"""Provider-neutral judge backends behind ``core.judge.Judge``.

``AzureOpenAIJudge`` is the default; ``MlflowJudge`` and ``DeepEvalJudge`` are
adapters selectable per-metric; ``HeuristicJudge`` is a no-LLM offline double.
Porting to another judge framework = adding one class here.
"""

from .base_openai import AzureOpenAIJudge, OpenAIJudge
from .benchmark import compare_judges
from .deepeval_judge import DeepEvalJudge
from .heuristic import HeuristicJudge
from .mlflow_judge import MlflowJudge
from .select import apply_per_metric_judges, build_judge, describe_judge

__all__ = [
    "AzureOpenAIJudge",
    "OpenAIJudge",
    "MlflowJudge",
    "DeepEvalJudge",
    "HeuristicJudge",
    "build_judge",
    "apply_per_metric_judges",
    "describe_judge",
    "compare_judges",
]
