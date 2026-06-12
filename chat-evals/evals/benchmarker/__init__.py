"""
LocalBenchmarker — local MLflow-based evaluation for an A2A agent.

Provides the same interface as the optional AICEBenchmarker but uses
mlflow.genai.evaluate directly with local file-based tracking.

The LocalBenchmarker import is lazy because it pulls in mlflow, which the
trace-only A2A client and tests do not need.
"""

from .a2a_client import (
    A2ARequestError,
    A2AResponse,
    create_graphql_thread,
    extract_text,
    make_a2a_predict_fn,
)


def __getattr__(name):
    if name == "LocalBenchmarker":
        from .benchmarker import LocalBenchmarker
        return LocalBenchmarker
    raise AttributeError(name)


__all__ = [
    "LocalBenchmarker",
    "A2ARequestError",
    "A2AResponse",
    "create_graphql_thread",
    "extract_text",
    "make_a2a_predict_fn",
]
