from __future__ import annotations

import os
from typing import Callable
from urllib.parse import urlparse

import httpx

from app.fundamental.evidence import ResearchSourceError, is_safe_public_url
from app.fundamental.schemas import ResearchSearchResults, ResearchSource
from app.retrieval.tavily import _retry_http

_API_URL = "https://api.keenable.ai/v1/search"


class KeenableSearchProvider:
    """Keenable's native Web-search adapter; keys are env-injected only."""

    def __init__(
        self,
        client_factory: Callable[..., object] | None = None,
        api_key_env_name: str | None = None,
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
        env_name = self._api_key_env_name or os.getenv("RESEARCH_SEARCH_API_KEY_ENV_NAME", "")
        api_key = os.getenv(env_name, "") if env_name else ""
        if not api_key:
            raise ResearchSourceError("RESEARCH_SOURCE_FAILED: Keenable API Key 未配置")
        client_factory = self._client_factory or httpx.Client
        try:
            response = _retry_http(
                lambda: client_factory(
                    timeout=timeout, follow_redirects=False, trust_env=True
                ).post(_API_URL, headers={"X-API-Key": api_key}, json={"query": query})
            )
            items: list[ResearchSource] = []
            for item in response.json().get("results", []):
                url = str(item.get("url", ""))
                if not is_safe_public_url(url):
                    continue
                items.append(
                    ResearchSource(
                        result_id=f"src_{len(items) + 1:03d}",
                        title=str(item.get("title", "")),
                        url=url,
                        source_name=urlparse(url).hostname or "",
                        date=str(item.get("published_at", "")),
                        summary=str(item.get("snippet") or item.get("description") or "")[:2_000],
                        source_kind="web",
                    )
                )
            if not items:
                raise ValueError("无可用公网来源")
            return ResearchSearchResults(items=items[:max_results])
        except ResearchSourceError:
            raise
        except Exception as exc:
            raise ResearchSourceError("RESEARCH_SOURCE_FAILED: Keenable 检索失败") from exc
