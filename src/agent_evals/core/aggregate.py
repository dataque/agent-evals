"""Pure aggregation helpers: set-overlap scoring (F1) and distribution stats
(mean, pass-rate, percentiles). No internal dependencies — reused by scorers
and by the runner's summary step."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence


def precision_recall_f1(expected: Iterable, observed: Iterable) -> tuple[float, float, float]:
    exp, obs = set(expected), set(observed)
    if not exp and not obs:
        return 1.0, 1.0, 1.0
    tp = len(exp & obs)
    precision = tp / len(obs) if obs else 0.0
    recall = tp / len(exp) if exp else 0.0
    f1v = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return precision, recall, f1v


def f1(expected: Iterable, observed: Iterable) -> float:
    """F1 of two sets. 1.0 when both empty, 0.0 when exactly one is empty.

    Matches the semantics of the prior harness's ``_f1``.
    """
    return precision_recall_f1(expected, observed)[2]


def mean(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def pass_rate(flags: Iterable[bool | None]) -> float | None:
    fs = [f for f in flags if f is not None]
    return sum(1 for f in fs if f) / len(fs) if fs else None


def percentile(values: Sequence[float | None], p: float) -> float | None:
    """Linear-interpolated percentile (p in 0..100). Ignores None."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return float(vals[0])
    k = (len(vals) - 1) * (p / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return float(vals[int(k)])
    return float(vals[lo] * (hi - k) + vals[hi] * (k - lo))
