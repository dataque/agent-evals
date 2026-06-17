"""Concrete scorers + a selectable registry (metric-id → scorer instance).

Scorers are pure over ``(EvalCase, RunRecord)``; judged scorers read a judge
(``scorer.judge`` if injected per-metric, else ``ctx.judge``) and never import a
judge backend directly. Multi-turn scorers (Phase 4) are added to
``SCORER_CLASSES`` as they land.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..core.scorer import Scorer
from .answer_equivalence import AnswerEquivalence
from .answer_relevancy import AnswerRelevancy
from .audit_action import AuditLogActionTaken
from .bias import Bias
from .conversation import ConversationCompleteness
from .faithfulness import Faithfulness
from .geval import GEval
from .isolation import CrossUserIsolation
from .knowledge_retention import KnowledgeRetention
from .latency import Latency
from .plan_quality import PlanQuality
from .refusal import RefusalCorrectness
from .role_adherence import RoleAdherence
from .safety import Safety
from .step_efficiency import StepEfficiency
from .stream_health import StreamHealthDetail
from .string_check import StringCheck
from .task_completion import TaskCompletion
from .token_cost import TokenCost
from .tool_arguments import ToolArgumentCorrectness
from .tool_result_schema import ToolResultSchemaAdherence
from .tool_selection import ToolSelectionAccuracy
from .topic import TopicAdherence
from .user_feedback import UserFeedbackSignal

SCORER_CLASSES: list[type] = [
    # deterministic / operational / probe (Phase 2)
    ToolSelectionAccuracy,
    ToolArgumentCorrectness,
    ToolResultSchemaAdherence,
    AuditLogActionTaken,
    StepEfficiency,
    StringCheck,
    Latency,
    TokenCost,
    StreamHealthDetail,
    CrossUserIsolation,
    UserFeedbackSignal,
    # judged single-turn (Phase 3)
    TaskCompletion,
    Faithfulness,
    AnswerEquivalence,
    Safety,
    RefusalCorrectness,
    TopicAdherence,
    Bias,
    GEval,
    AnswerRelevancy,
    RoleAdherence,
    # multi-turn + planning (Phase 4)
    ConversationCompleteness,
    KnowledgeRetention,
    PlanQuality,
]


def build_registry() -> dict[str, Scorer]:
    return {cls.spec.metric: cls() for cls in SCORER_CLASSES}


def get_scorers(selection: str | Iterable[str] = "all") -> list[Scorer]:
    """Resolve a scorer selection to instances.

    ``selection``: ``"all"``; ``"tier1"`` (#1–15) / ``"tier2"`` (#16–24); a family
    (``deterministic``/``operational``/``judge``/``probe``); or a comma-separated
    string / iterable of metric ids.
    """
    reg = build_registry()
    instances = list(reg.values())

    if selection in (None, "all"):
        return instances
    if selection == "tier1":
        return [s for s in instances if 1 <= s.spec.number <= 15]
    if selection == "tier2":
        return [s for s in instances if 16 <= s.spec.number <= 24]
    if selection in ("deterministic", "operational", "judge", "probe"):
        return [s for s in instances if s.spec.family.value == selection]

    ids = (
        [x.strip() for x in selection.split(",") if x.strip()]
        if isinstance(selection, str)
        else list(selection)
    )
    return [reg[i] for i in ids if i in reg]


__all__ = [
    "SCORER_CLASSES",
    "build_registry",
    "get_scorers",
    "ToolSelectionAccuracy",
    "ToolArgumentCorrectness",
    "ToolResultSchemaAdherence",
    "AuditLogActionTaken",
    "StepEfficiency",
    "StringCheck",
    "Latency",
    "TokenCost",
    "StreamHealthDetail",
    "CrossUserIsolation",
    "TaskCompletion",
    "Faithfulness",
    "AnswerEquivalence",
    "Safety",
    "RefusalCorrectness",
    "TopicAdherence",
    "Bias",
    "GEval",
    "AnswerRelevancy",
    "RoleAdherence",
    "ConversationCompleteness",
    "KnowledgeRetention",
    "PlanQuality",
    "UserFeedbackSignal",
]
