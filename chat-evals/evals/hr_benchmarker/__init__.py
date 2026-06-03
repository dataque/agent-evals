"""
HRBenchmarker — local MLflow-based evaluation for the HR Agent system.

Provides the same interface as AICEBenchmarker but uses mlflow.genai.evaluate
directly with local file-based tracking.

The HRBenchmarker import is lazy because it pulls in mlflow, which the
trace-only A2A client and tests do not need.
"""

from .a2a_client import (
    A2ARequestError,
    A2AResponse,
    create_bff_thread,
    extract_text,
    make_a2a_predict_fn,
)


def __getattr__(name):
    if name == "HRBenchmarker":
        from .benchmarker import HRBenchmarker
        return HRBenchmarker
    raise AttributeError(name)


__all__ = [
    "HRBenchmarker",
    "A2ARequestError",
    "A2AResponse",
    "create_bff_thread",
    "extract_text",
    "make_a2a_predict_fn",
]
