"""
Protocol-інтерфейси — контракти для MCP-тулів.
Runtime-checkable: isinstance(obj, ToolContract) → True/False.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ToolContract(Protocol):
    """Базовий контракт для кожного MCP-тула."""
    semantic_name: str
    underlying_services: list[str]
    requires_auth: bool

    def can_be_reverted(self) -> bool: ...


@runtime_checkable
class WriteToolContract(Protocol):
    """
    Контракт для тулів із write side-effects.
    target_collection — куди пишемо (roman_memory_v1).
    audit_event_action — рядок дії у audit log (upsert, delete...).
    """
    semantic_name: str
    target_collection: str
    audit_event_action: str
