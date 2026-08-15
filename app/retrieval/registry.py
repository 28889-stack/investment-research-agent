from __future__ import annotations

import os

from app.fundamental.evidence import ResearchSourceError
from app.retrieval.protocol import ResearchSearchProvider


def get_search_provider(name: str) -> ResearchSearchProvider:
    """Closed registry: configuration cannot import or execute arbitrary code."""
    if name == "official_crawler":
        from app.retrieval.cninfo import CninfoSearchProvider

        return CninfoSearchProvider()
    if name == "akshare_news":
        from app.retrieval.akshare_news import AkshareNewsProvider

        return AkshareNewsProvider()
    if name == "akshare_reports":
        from app.retrieval.akshare_reports import AkshareReportProvider

        return AkshareReportProvider()
    if name == "akshare_notices":
        from app.retrieval.akshare_notices import AkshareNoticeProvider

        return AkshareNoticeProvider()
    if name == "tavily":
        from app.retrieval.tavily import TavilySearchProvider

        return TavilySearchProvider()
    if name == "keenable":
        from app.retrieval.keenable import KeenableSearchProvider

        return KeenableSearchProvider()
    if name == "firecrawl":
        from app.retrieval.firecrawl import FirecrawlSearchProvider

        return FirecrawlSearchProvider()
    if name == "aggregator":
        from app.retrieval.aggregator import AggregatingSearchProvider

        provider_names = _resolve_aggregator_providers()
        return AggregatingSearchProvider(provider_names)
    raise ValueError(f"未知检索适配器: {name}")


# Closed provider → source_kind catalog. The aggregator's direction filter
# resolves source_kind names (announcement/research_report/news/web) to the
# providers that emit them, so prompts can group by conceptual direction
# (披露/研报/Web) without knowing deployment-specific provider names.
PROVIDER_SOURCE_KINDS: dict[str, str] = {
    "official_crawler": "announcement",
    "akshare_notices": "announcement",
    "akshare_news": "news",
    "akshare_reports": "research_report",
    "tavily": "web",
    "keenable": "web",
    "firecrawl": "web",
}


def _resolve_aggregator_providers() -> list[str]:
    raw = os.getenv("RESEARCH_SEARCH_PROVIDERS", "")
    if not raw.strip():
        return ["official_crawler", "akshare_news", "akshare_reports", "akshare_notices"]
    names = [token.strip() for token in raw.split(",") if token.strip()]
    if "aggregator" in names:
        raise ResearchSourceError("RESEARCH_SOURCE_FAILED: 聚合来源不能包含 aggregator 自身")
    return names
