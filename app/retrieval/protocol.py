from __future__ import annotations

from typing import Protocol

from app.fundamental.schemas import ResearchSearchResults


class ResearchSearchProvider(Protocol):
    """Stable boundary for local adapters and a future MCP adapter."""

    def search(
        self,
        *,
        query: str,
        symbol: str,
        max_results: int,
        timeout: float,
    ) -> ResearchSearchResults: ...
