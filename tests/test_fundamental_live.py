from __future__ import annotations

import os
from datetime import date

import pytest

from app.fundamental.data import get_company_profile, get_financial_data
from app.fundamental.evidence import search_research_sources


@pytest.mark.fundamental_data_live
def test_akshare_live_company_and_financial_data(settings) -> None:
    if os.getenv("RUN_FUNDAMENTAL_DATA_LIVE") != "1":
        pytest.skip("set RUN_FUNDAMENTAL_DATA_LIVE=1 to run AKShare fundamental Live test")
    live = settings.model_copy(update={"fundamental_data_mode": "live", "fundamental_data_provider": "akshare"})

    profile = get_company_profile("600519.SH", date.today(), live)
    data = get_financial_data("600519.SH", date.today(), live)

    assert profile.symbol == "600519.SH"
    assert profile.data_source == "akshare"
    assert data.data_source == "akshare"
    assert len(data.periods) >= 5


@pytest.mark.research_search_live
def test_tavily_live_search_returns_public_sources(settings) -> None:
    if os.getenv("RUN_RESEARCH_SEARCH_LIVE") != "1":
        pytest.skip("set RUN_RESEARCH_SEARCH_LIVE=1 to run Live search test")
    provider = os.getenv("RESEARCH_SEARCH_PROVIDER", "aggregator")
    key_env = os.getenv("RESEARCH_SEARCH_API_KEY_ENV_NAME", "")
    # Keyless providers (official_crawler, akshare_*, aggregator with keyless
    # members) need no API key; only tavily/firecrawl require one.
    if provider in {"tavily", "firecrawl"} and (not key_env or not os.getenv(key_env)):
        pytest.skip("API key environment variable is not configured")
    members = [
        token.strip()
        for token in os.getenv("RESEARCH_SEARCH_PROVIDERS", "").split(",")
        if token.strip()
    ]
    update = {
        "research_search_mode": "live",
        "research_search_provider": provider,
    }
    if key_env:
        update["research_search_api_key_env_name"] = key_env
    if provider == "aggregator" and members:
        update["research_search_providers"] = members
    live = settings.model_copy(update=update)

    results = search_research_sources("贵州茅台 2025 年度报告", "600519.SH", live)

    assert results.items
    assert all(item.url.startswith(("http://", "https://")) for item in results.items)
    # Aggregator tags every item with its source kind for reranking/dedup.
    if provider == "aggregator":
        assert all(item.source_kind for item in results.items)
