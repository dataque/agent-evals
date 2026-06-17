"""Metrics-backend adapters. ``JsonlSink`` is dependency-free; ``MlflowSink``
is the only module that imports ``mlflow`` (lazily, so importing it here is safe
even when the ``mlflow`` extra is not installed)."""

from .jsonl_sink import JsonlSink
from .mlflow_sink import MlflowSink

__all__ = ["JsonlSink", "MlflowSink"]
