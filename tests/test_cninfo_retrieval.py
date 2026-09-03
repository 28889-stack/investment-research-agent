from __future__ import annotations

import httpx

from app.retrieval.cninfo import CninfoSearchProvider


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Client:
    def __init__(self, *, keyword_misses: bool = False, only_match=None, raise_on=None):
        self.posts = []
        self.keyword_misses = keyword_misses
        self.only_match = only_match
        self.raise_on = set(raise_on or ())

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get(self, url):
        assert url.endswith("/new/data/szse_stock.json")
        return _Response(
            {
                "stockList": [
                    {
                        "code": "600519",
                        "orgId": "gssh0600519",
                        "zwjc": "贵州茅台",
                    }
                ]
            }
        )

    def post(self, url, *, data, headers):
        self.posts.append((url, data, headers))
        if data["searchkey"] in self.raise_on:
            raise httpx.ConnectError("temporary failure")
        if self.keyword_misses and data["searchkey"]:
            return _Response({"announcements": []})
        if self.only_match is not None and data["searchkey"] != self.only_match:
            return _Response({"announcements": []})
        return _Response(
            {
                "announcements": [
                    {
                        "announcementTitle": "贵州茅台2025年年度报告",
                        "announcementTime": 1776355200000,
                        "adjunctUrl": "finalpage/2026-04-17/1225114741.PDF",
                        "secName": "贵州茅台",
                    }
                ]
            }
        )


def test_cninfo_search_maps_symbol_and_returns_official_pdf() -> None:
    client = _Client()
    provider = CninfoSearchProvider(client_factory=lambda **_kwargs: client)

    results = provider.search(
        query="年度报告",
        symbol="600519.SH",
        max_results=5,
        timeout=10,
    )

    assert results.items[0].title == "贵州茅台2025年年度报告"
    assert results.items[0].url == "https://static.cninfo.com.cn/finalpage/2026-04-17/1225114741.PDF"
    assert results.items[0].source_name == "巨潮资讯"
    assert results.items[0].date == "2026-04-17"
    assert client.posts[0][1]["stock"] == "600519,gssh0600519"
    assert client.posts[0][1]["column"] == "sse"
    assert len(client.posts) == 1


def test_cninfo_search_falls_back_to_recent_filings_when_keyword_misses() -> None:
    client = _Client(keyword_misses=True)
    provider = CninfoSearchProvider(client_factory=lambda **_kwargs: client)

    results = provider.search(
        query="公司业务与年报",
        symbol="600519.SH",
        max_results=5,
        timeout=10,
    )

    search_keys = [post[1]["searchkey"] for post in client.posts]
    assert results.items[0].title == "贵州茅台2025年年度报告"
    assert search_keys[0] == "公司业务与年报"
    assert search_keys[-1] == ""
    assert "年度报告" in search_keys
    assert all(post[1]["stock"] == "600519,gssh0600519" for post in client.posts)


def test_cninfo_search_uses_specialized_term_before_empty_fallback() -> None:
    client = _Client(only_match="年度报告")
    provider = CninfoSearchProvider(client_factory=lambda **_kwargs: client)

    results = provider.search(
        query="经营 业务 竞争",
        symbol="600519.SH",
        max_results=5,
        timeout=10,
    )

    search_keys = [post[1]["searchkey"] for post in client.posts]
    assert results.items[0].title == "贵州茅台2025年年度报告"
    # Specialized term "年度报告" matched, so the empty-keyword fallback is never sent.
    assert search_keys[-1] == "年度报告"
    assert "" not in search_keys


def test_cninfo_search_degrades_to_next_term_after_transient_failure() -> None:
    client = _Client(
        only_match="年度报告",
        raise_on={"公司业务与年报"},
    )
    provider = CninfoSearchProvider(client_factory=lambda **_kwargs: client)

    results = provider.search(
        query="公司业务与年报",
        symbol="600519.SH",
        max_results=5,
        timeout=10,
    )

    assert results.items[0].title == "贵州茅台2025年年度报告"
    search_keys = [post[1]["searchkey"] for post in client.posts]
    # The failing candidate is retried by _request, then the search degrades.
    assert search_keys[:2] == ["公司业务与年报", "公司业务与年报"]
    assert search_keys[-1] == "年度报告"
