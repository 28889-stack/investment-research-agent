from __future__ import annotations

import httpx
import pytest

from app.fundamental.evidence import ResearchSourceError
from app.retrieval.tavily import TavilySearchProvider


class _FakeResponse:
    def __init__(self, payload=None, status_code=200) -> None:
        self._payload = payload or {}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=httpx.Request("POST", "https://api.tavily.com/search"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return self._payload


class _FakeClient:
    """Fake httpx.Client: yields canned responses or raises via its post_fn."""

    def __init__(self, post_fn) -> None:
        self._post_fn = post_fn

    def post(self, *_args, **_kwargs):
        return self._post_fn()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def _client_factory(returning=None, raising=None):
    """Build a client_factory that yields a _FakeClient on each construction.

    `returning` is a list of _FakeResponse objects served in order; `raising`
    is a list of exceptions served in order. A fresh client is created per
    `client_factory(...)` call (the adapter builds one client per search).
    """
    state = {"returns": list(returning or []), "raises": list(raising or [])}

    def post_fn():
        if state["raises"]:
            raise state["raises"].pop(0)
        if state["returns"]:
            return state["returns"].pop(0)
        return _FakeResponse()

    def factory(*_args, **_kwargs):
        return _FakeClient(post_fn)

    return factory


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_tavily_search_returns_public_sources_with_web_kind() -> None:
    payload = {
        "results": [
            {
                "title": "贵州茅台年报",
                "url": "https://finance.example.com/maotai-2025",
                "content": "年度业绩摘要",
                "published_date": "2026-03-28",
            },
            {
                "title": "Intranet leak (filtered)",
                "url": "http://192.168.1.5/internal",
                "content": "should be dropped",
            },
        ]
    }
    factory = _client_factory(returning=[_FakeResponse(payload=payload)])

    provider = TavilySearchProvider(client_factory=factory, api_key_env_name="TEST_TAVILY_KEY")
    import os

    os.environ["TEST_TAVILY_KEY"] = "secret"

    result = provider.search(query="茅台 年报", symbol="600519.SH", max_results=5, timeout=10)

    assert len(result.items) == 1
    item = result.items[0]
    assert item.title == "贵州茅台年报"
    assert item.source_kind == "web"
    assert item.date == "2026-03-28"
    assert item.summary == "年度业绩摘要"


# ---------------------------------------------------------------------------
# Retry semantics
# ---------------------------------------------------------------------------


def test_tavily_retries_one_transient_network_failure() -> None:
    factory = _client_factory(
        raising=[httpx.ConnectError("transient")],
        returning=[_FakeResponse(payload={"results": [{"title": "ok", "url": "https://example.com/x", "content": "c"}]})],
    )
    provider = TavilySearchProvider(client_factory=factory, api_key_env_name="TEST_TAVILY_KEY")
    import os

    os.environ["TEST_TAVILY_KEY"] = "secret"

    result = provider.search(query="x", symbol="600519.SH", max_results=5, timeout=10)

    assert result.items[0].title == "ok"


def test_tavily_does_not_retry_authentication_failure() -> None:
    factory = _client_factory(returning=[_FakeResponse(status_code=401)])
    provider = TavilySearchProvider(client_factory=factory, api_key_env_name="TEST_TAVILY_KEY")
    import os

    os.environ["TEST_TAVILY_KEY"] = "invalid"

    with pytest.raises(ResearchSourceError, match="RESEARCH_SOURCE_FAILED"):
        provider.search(query="x", symbol="600519.SH", max_results=5, timeout=10)


def test_tavily_retries_server_5xx_failure() -> None:
    factory = _client_factory(
        returning=[
            _FakeResponse(status_code=503),
            _FakeResponse(payload={"results": [{"title": "ok", "url": "https://example.com/x", "content": "c"}]}),
        ]
    )
    provider = TavilySearchProvider(client_factory=factory, api_key_env_name="TEST_TAVILY_KEY")
    import os

    os.environ["TEST_TAVILY_KEY"] = "secret"

    result = provider.search(query="x", symbol="600519.SH", max_results=5, timeout=10)

    assert result.items[0].title == "ok"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_tavily_raises_when_api_key_not_configured() -> None:
    provider = TavilySearchProvider(client_factory=_client_factory(), api_key_env_name="MISSING_KEY")

    with pytest.raises(ResearchSourceError, match="API Key 未配置"):
        provider.search(query="x", symbol="600519.SH", max_results=5, timeout=10)


def test_tavily_raises_when_no_safe_public_results() -> None:
    payload = {
        "results": [
            {"title": "intranet1", "url": "http://10.0.0.1/a", "content": "c"},
            {"title": "intranet2", "url": "http://127.0.0.1/a", "content": "c"},
        ]
    }
    factory = _client_factory(returning=[_FakeResponse(payload=payload)])
    provider = TavilySearchProvider(client_factory=factory, api_key_env_name="TEST_TAVILY_KEY")
    import os

    os.environ["TEST_TAVILY_KEY"] = "secret"

    with pytest.raises(ResearchSourceError, match="RESEARCH_SOURCE_FAILED"):
        provider.search(query="x", symbol="600519.SH", max_results=5, timeout=10)
