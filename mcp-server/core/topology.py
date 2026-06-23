"""
MVP Тріада як типізований код.
Топологія системи — не runtime-стан, а архітектурний опис.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Layer:
    """Один шар архітектурної тріади."""
    name: str
    location: str                   # "cloud" | "edge" | "local"
    components: list[str]
    state_managed: bool             # чи зберігає персистентний стан


# Канонічна MVP Тріада
TRIAD: list[Layer] = [
    Layer(
        name="orchestration",
        location="cloud",
        components=["n8n-cloud"],
        state_managed=False,        # ADR-004: cloud не зберігає стан
    ),
    Layer(
        name="bridge",
        location="edge",
        components=["cloudflare-tunnel", "cloudflared"],
        state_managed=False,        # тільки транспорт
    ),
    Layer(
        name="local_monolith",
        location="local",
        components=["mcp-server", "qdrant", "ollama", "litellm", "redis", "n8n-local"],
        state_managed=True,         # єдине авторитативне джерело правди
    ),
]


def triad_by_name(name: str) -> Layer | None:
    return next((l for l in TRIAD if l.name == name), None)


def validate_triad() -> list[str]:
    """Повертає список архітектурних порушень. Порожній = ОК."""
    issues: list[str] = []
    state_layers = [l for l in TRIAD if l.state_managed]
    if len(state_layers) != 1:
        issues.append(
            f"ADR-004: exactly 1 state layer expected, got {len(state_layers)}: "
            + ", ".join(l.name for l in state_layers)
        )
    local = triad_by_name("local_monolith")
    if local and not local.state_managed:
        issues.append("topology: local_monolith must be state_managed=True")
    return issues
