"""
Абстрактні доменні сутності — незалежні від сховища і транспорту.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class Memory:
    """
    Одиниця знання у Qdrant.
    Ніколи не видаляється фізично — тільки active=False (ADR-003).
    """
    text: str
    category: str
    source_agent: str
    id: str                             = field(default_factory=_new_id)
    embedding: list[float]              = field(default_factory=list, repr=False)
    active: bool                        = True
    embedding_model: str                = ""
    created_at: str                     = field(default_factory=_now)
    structured_metadata: dict[str, Any] = field(default_factory=dict)

    def to_qdrant_payload(self) -> dict[str, Any]:
        """
        Payload для Qdrant point.
        embedding не входить — це вектор, не payload.
        id не входить — це point id, не payload.
        text зберігається для майбутньої переіндексації.
        """
        return {
            "active": self.active,
            "text": self.text,
            "category": self.category,
            "source_agent": self.source_agent,
            "embedding_model": self.embedding_model,
            "created_at": self.created_at,
            **self.structured_metadata,
        }


@dataclass
class HealthStatus:
    """Стан одного компонента системи."""
    component: str
    status: str                         # "ok" | "degraded" | "down"
    latency_ms: float | None            = None
    details: dict[str, Any]             = field(default_factory=dict)
    error: str | None                   = None


@dataclass
class Operator:
    """Роман — Принципал системи, не 'користувач'."""
    name: str
    location: str
    cognitive_load_limit: int           = 7   # Miller's Law
    active_principles: list[str]        = field(default_factory=list)


# Канонічний Принципал
PRINCIPAL = Operator(
    name="Roman",
    location="Lutsk, Ukraine",
    cognitive_load_limit=7,
    active_principles=["ADR-001", "ADR-002", "ADR-003", "ADR-004", "ADR-005"],
)
