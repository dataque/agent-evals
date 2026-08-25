"""Transport layer: drive the system-under-test → transport-neutral RunRecord.

``AgUiSseTransport`` is the primary adapter (the live FE↔BE contract). An A2A
adapter can be added later behind the same ``Transport`` protocol without
touching scorers.
"""

from .a2a import A2ATransport
from .actuator import (
    OBSERVED_ONLY_DETAIL,
    actuator_url,
    probe_backend,
    probe_backend_model,
    probe_backend_options,
)
from .agui import AgUiSseTransport
from .auth import (
    CallableTokenProvider,
    LocalJwtMinter,
    StaticTokenProvider,
    TokenProvider,
)
from .base import (
    Identity,
    Session,
    SessionState,
    Transport,
    TransportError,
    TurnRequest,
)
from .projection import runrecord_to_trace, to_mlflow_row

__all__ = [
    "AgUiSseTransport",
    "OBSERVED_ONLY_DETAIL",
    "probe_backend",
    "probe_backend_model",
    "probe_backend_options",
    "actuator_url",
    "A2ATransport",
    "Transport",
    "TurnRequest",
    "Identity",
    "SessionState",
    "Session",
    "TransportError",
    "TokenProvider",
    "LocalJwtMinter",
    "StaticTokenProvider",
    "CallableTokenProvider",
    "runrecord_to_trace",
    "to_mlflow_row",
]
