"""Auth providers — produce HTTP header dicts for protocol adapters."""

from .base import AuthProvider, NoAuth
from .oauth2 import EntraIdAuth
from .static import BearerAuth, FunctionKeyAuth

__all__ = [
    "AuthProvider",
    "BearerAuth",
    "EntraIdAuth",
    "FunctionKeyAuth",
    "NoAuth",
]
