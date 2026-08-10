from __future__ import annotations

import os
import time
from typing import Callable
from urllib.parse import urlparse

import httpx

from app.fundamental.evidence import ResearchSourceError, is_safe_public_url
from app.fundamental.schemas import ResearchSearchResults, ResearchSource

_API_URL = "https://api.tavily.com/search"


class TavilySearchProvider:
    """Generic web search adapter. API key is injected from env, never logged."""

    def __init__(
        self,
        client_factory: Callable[..., object] | None = None,
        api_key_env_name: str | None = None,
    ) -> None:
        # Resolved lazily at call time so module-level monkeypatching of
        # httpx.Client takes effect (binding it as a default arg would capture
        # the original class and defeat test patches).
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
        env_name = self._api_key_env_name or os.getenv("RESEARCH_SEARCH_API_KEY_ENV_NAME", "")
        api_key = os.getenv(env_name, "") if env_name else ""
        if not api_key:
            raise ResearchSourceError("RESEARCH_SOURCE_FAILED: Tavily API Key 未配置")
        client_factory = self._client_factory or httpx.Client
        headers = {"Authorization": f"Bearer {api_key}"}
        body = {
            "query": query,
            "search_depth": "basic",
            "topic": "finance",
            "max_results": max_results,
        }
        try:
            response = _retry_http(
                lambda: client_factory(
                    timeout=timeout, follow_redirects=False, trust_env=True
                ).post(_API_URL, headers=headers, json=body)
            )
            payload = response.json()
            items: list[ResearchSource] = []
            for item in payload.get("results", []):
                url = str(item.get("url", ""))
                if not is_safe_public_url(url):
                    continue
                items.append(
                    ResearchSource(
                        result_id=f"src_{len(items) + 1:03d}",
                        title=str(item.get("title", "")),
                        url=url,
                        source_name=urlparse(url).hostname or "",
                        date=str(item.get("published_date", "")),
                        summary=str(item.get("content", ""))[:2_000],
                        source_kind="web",
                    )
                )
            if not items:
                raise ValueError("无可用公网来源")
            return ResearchSearchResults(items=items)
        except ResearchSourceError:
            raise
        except Exception as exc:
            raise ResearchSourceError("RESEARCH_SOURCE_FAILED: Tavily 检索失败") from exc


def _retry_http(call):
    """Retry transient HTTP failures (5xx/408/429 and request errors) once."""
    for attempt in range(2):
        try:
            response = call()
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status not in {408, 429} and status < 500:
                raise
            if attempt == 1:
                raise
            time.sleep(0.1)
        except httpx.RequestError:
            if attempt == 1:
                raise
            time.sleep(0.1)
    raise RuntimeError("unreachable")
