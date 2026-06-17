"""Framework-neutral core: domain types, scoring contract, runner, aggregation.

Nothing in this package imports a transport library or a metrics backend.
"""

from .aggregate import f1, mean, pass_rate, percentile, precision_recall_f1
from .case import EvalCase, Expectations, Turn
from .judge import Judge, JudgeVerdict
from .run_record import (
    CompletionStatus,
    DerivedTiming,
    Event,
    NormalizedMessage,
    ReasoningSegment,
    RunError,
    RunRecord,
    Step,
    StreamHealth,
    SubagentRoute,
    TokenUsage,
    ToolCall,
    ToolStatus,
    UsageSource,
)
from .runner import RunReport, Runner, TurnDriver
from .scorer import (
    CaseResult,
    Family,
    Score,
    Scorer,
    ScorerSpec,
    ScoringContext,
    TurnScope,
)
from .sink import MetricsSink

__all__ = [
    "EvalCase",
    "Expectations",
    "Turn",
    "Judge",
    "JudgeVerdict",
    "RunRecord",
    "Event",
    "ToolCall",
    "ToolStatus",
    "Step",
    "SubagentRoute",
    "ReasoningSegment",
    "TokenUsage",
    "StreamHealth",
    "DerivedTiming",
    "RunError",
    "CompletionStatus",
    "UsageSource",
    "NormalizedMessage",
    "Score",
    "Scorer",
    "ScorerSpec",
    "ScoringContext",
    "CaseResult",
    "Family",
    "TurnScope",
    "MetricsSink",
    "Runner",
    "RunReport",
    "TurnDriver",
    "f1",
    "precision_recall_f1",
    "mean",
    "pass_rate",
    "percentile",
]
