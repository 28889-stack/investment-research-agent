from __future__ import annotations

import os
from typing import Any, Callable

import httpx

from app.fundamental.evidence import ResearchSourceError, is_safe_public_url
from app.fundamental.schemas import ResearchSearchResults, ResearchSource

_API_URL = "https://api.firecrawl.dev/v2/search"


class FirecrawlSearchProvider:
    """Optional Firecrawl V2 metadata-only Web search adapter.

    Not in the default aggregator fan-out list. A user must set
    FIRECRAWL_API_KEY and add `firecrawl` to RESEARCH_SEARCH_PROVIDERS to enable.
    Page bodies remain the responsibility of the configured Reader service.
    """

    def __init__(
        self,
        client_factory: Callable[..., object] = httpx.Client,
        api_key_env_name: str = "FIRECRAWL_API_KEY",
    ) -> None:
        self._client_factory = client_factory
        self._api_key_env_name = api_key_env_name

    def search(
        self,
        *,
        query: str,
        symbol: str,
        max_results: int,
        timeout: float,
    ) -> ResearchSearchResults:
        del symbol
        api_key = os.getenv(self._api_key_env_name, "")
        if not api_key:
            raise ResearchSourceError("RESEARCH_SOURCE_FAILED: Firecrawl API Key 未配置")
        headers = {"Authorization": f"Bearer {api_key}"}
        body = {
            "query": query,
            "limit": max_results,
            "sources": [{"type": "web"}],
        }
        try:
            with self._client_factory(timeout=timeout, follow_redirects=False, trust_env=True) as client:
                response = client.post(_API_URL, headers=headers, json=body)
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
            data = payload.get("data")
            entries = data.get("web", []) if isinstance(data, dict) else []
            items: list[ResearchSource] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                url = str(entry.get("url") or entry.get("link") or "")
                if not is_safe_public_url(url):
                    continue
                summary = str(
                    entry.get("description") or entry.get("snippet") or ""
                )
                items.append(
                    ResearchSource(
                        result_id=f"src_{len(items) + 1:03d}",
                        title=str(entry.get("title") or ""),
                        url=url,
                        source_name="Firecrawl",
                        date=str(entry.get("publishedDate") or entry.get("date") or ""),
                        summary=summary[:2_000],
                        content="",
                        source_kind="web",
                    )
                )
            if not items:
                raise ValueError("无可用公网来源")
            return ResearchSearchResults(items=items)
        except ResearchSourceError:
            raise
        except Exception as exc:
            raise ResearchSourceError("RESEARCH_SOURCE_FAILED: Firecrawl 检索失败") from exc
