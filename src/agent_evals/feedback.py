"""Offline ingestion of production user-feedback (metric #23).

Reads bulk thumbs/ratings/corrections (production telemetry) and logs aggregate
signals (thumbs-up rate, CSAT, correction rate) to any ``MetricsSink`` — the
same sinks the live eval runner uses, so feedback lands alongside eval metrics.
"""

from __future__ import annotations

import json
from pathlib import Path

from .core.aggregate import mean
from .core.sink import MetricsSink

_POSITIVE = {"up", "1", "true", "positive", "good", "thumbs_up"}


def load_feedback(path: str) -> list[dict]:
    """Load feedback records from a ``.jsonl`` (one object per line) or ``.json``
    (a list) file. Each record: ``{id?, thumbs?, rating?, correction?}``."""
    p = Path(path)
    text = p.read_text()
    if p.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    return data if isinstance(data, list) else data.get("records", [])


def _rating(rec: dict) -> float | None:
    if rec.get("rating") is not None:
        try:
            return max(0.0, min(1.0, float(rec["rating"])))
        except (TypeError, ValueError):
            return None
    thumbs = rec.get("thumbs")
    if thumbs is not None:
        return 1.0 if str(thumbs).lower() in _POSITIVE else 0.0
    return None


def aggregate_feedback(records: list[dict]) -> dict[str, float]:
    ratings = [r for r in (_rating(rec) for rec in records) if r is not None]
    corrections = [rec for rec in records if (rec.get("correction") or "").strip()]
    agg: dict[str, float] = {"user_feedback.n": float(len(records))}
    if ratings:
        agg["user_feedback.mean"] = mean(ratings)
        agg["user_feedback.thumbs_up_rate"] = sum(1 for r in ratings if r >= 0.5) / len(ratings)
    if records:
        agg["user_feedback.correction_rate"] = len(corrections) / len(records)
    return agg


def ingest(records: list[dict] | str, sink: MetricsSink, *, run_name: str = "user-feedback",
           params: dict | None = None) -> dict[str, float]:
    """Aggregate feedback records (or a path) and log the summary to ``sink``."""
    if isinstance(records, str):
        records = load_feedback(records)
    agg = aggregate_feedback(records)
    sink.start_run(name=run_name, params=params or {"source": "user_feedback", "n": len(records)})
    try:
        sink.log_summary(agg)
    finally:
        sink.end_run()
    return agg
