"""
core — архітектурний хребет no-pilot MCP-стеку.

from core import no_pilot_principle, PRINCIPAL, TRIAD
from core import Memory, HealthStatus, ToolContract
"""
from .contracts import ToolContract, WriteToolContract
from .principles import (
    AUDIT_LOG_PATH,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    EMBEDDING_MODEL_DIGEST,
    MVP_MODE,
    NO_PILOT_PRINCIPLE,
    SOFT_DELETE_FIELD,
    no_pilot_principle,
)
from .schema import HealthStatus, Memory, Operator
from .schema import PRINCIPAL
from .topology import Layer, TRIAD, triad_by_name, validate_triad

__all__ = [
    # principles
    "EMBEDDING_MODEL", "EMBEDDING_MODEL_DIGEST", "EMBEDDING_DIMENSIONS",
    "SOFT_DELETE_FIELD", "MVP_MODE", "NO_PILOT_PRINCIPLE",
    "AUDIT_LOG_PATH", "no_pilot_principle",
    # schema
    "Memory", "HealthStatus", "Operator", "PRINCIPAL",
    # contracts
    "ToolContract", "WriteToolContract",
    # topology
    "Layer", "TRIAD", "triad_by_name", "validate_triad",
]
