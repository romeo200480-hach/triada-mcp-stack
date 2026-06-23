"""
tools — MCP-тули зовнішнього шару.
Реєструються у server.py через register_all(mcp).
"""
from __future__ import annotations


def register_all(mcp_instance) -> None:
    """Реєструє всі тули у FastMCP-інстанції."""
    from .health_check_all import health_check_all
    from .save_to_local_memory import save_to_local_memory
    from .query_local_memory import query_local_memory
    mcp_instance.tool()(health_check_all)
    mcp_instance.tool()(save_to_local_memory)
    mcp_instance.tool()(query_local_memory)
    from .ask_council import ask_council
    mcp_instance.tool()(ask_council)
    from .count_memory import count_memory
    mcp_instance.tool()(count_memory)
