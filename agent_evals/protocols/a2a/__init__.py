"""A2A JSON-RPC protocol adapter."""

from .adapter import A2AAdapter
from .client import A2ARequestError, A2AResponse, create_bff_thread, make_a2a_predict_fn

__all__ = [
    "A2AAdapter",
    "A2ARequestError",
    "A2AResponse",
    "create_bff_thread",
    "make_a2a_predict_fn",
]
