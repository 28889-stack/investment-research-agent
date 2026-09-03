from __future__ import annotations

import pytest

from app.fundamental.evidence import ResearchSourceError
from app.fundamental.schemas import ResearchSearchResults, ResearchSource
from app.retrieval.aggregator import AggregatingSearchProvider, _dedup_key


def _src(
    *,
    title: str,
    url: str,
    kind: str,
    date: str = "",
    content: str = "",
) -> ResearchSource:
    return ResearchSource(
        result_id="src_000",  # aggregator re-numbers anyway
        title=title,
        url=url,
        source_name="test",
        date=date,
        summary="",
        content=content,
        source_kind=kind,
    )


class _FakeProvider:
    """Minimal provider stub: returns a canned result, or raises on demand."""

    def __init__(self, items=None, raises=None) -> None:
        self._items = items or []
        self._raises = raises
        self.calls = 0

    def search(self, *, query, symbol, max_results, timeout):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return ResearchSearchResults(items=list(self._items))


def _patch_registry(monkeypatch, mapping):
    """Make aggregator.get_search_provider(name) return the stub from `mapping`."""

    def fake_get(name):
        if name not in mapping:
            raise ValueError(f"未知检索适配器: {name}")
        return mapping[name]

    monkeypatch.setattr("app.retrieval.aggregator.get_search_provider", fake_get)


# ---------------------------------------------------------------------------
# Fan-out + failure isolation
# ---------------------------------------------------------------------------


def test_aggregator_fans_out_to_all_providers(monkeypatch) -> None:
    a = _FakeProvider(items=[_src(title="a", url="https://a.example/1", kind="news")])
    b = _FakeProvider(items=[_src(title="b", url="https://b.example/1", kind="news")])
    _patch_registry(monkeypatch, {"a": a, "b": b})

    provider = AggregatingSearchProvider(["a", "b"])
    result = provider.search(query="x", symbol="600519.SH", max_results=10, timeout=10)

    assert a.calls == 1 and b.calls == 1
    assert {item.title for item in result.items} == {"a", "b"}


def test_aggregator_isolates_per_source_failure_and_keeps_survivors(monkeypatch) -> None:
    ok = _FakeProvider(items=[_src(title="ok", url="https://ok.example/1", kind="news")])
    dead = _FakeProvider(raises=ResearchSourceError("RESEARCH_SOURCE_FAILED: boom"))
    _patch_registry(monkeypatch, {"ok": ok, "dead": dead})

    provider = AggregatingSearchProvider(["dead", "ok"])
    result = provider.search(query="x", symbol="600519.SH", max_results=10, timeout=10)

    assert dead.calls == 1
    assert [item.title for item in result.items] == ["ok"]


def test_aggregator_isolates_unexpected_exception_too(monkeypatch) -> None:
    """A non-ResearchSourceError fault must still be isolated per source."""
    ok = _FakeProvider(items=[_src(title="ok", url="https://ok.example/1", kind="news")])
    boom = _FakeProvider(raises=ValueError("unexpected"))
    _patch_registry(monkeypatch, {"ok": ok, "boom": boom})

    provider = AggregatingSearchProvider(["boom", "ok"])
    result = provider.search(query="x", symbol="600519.SH", max_results=10, timeout=10)

    assert [item.title for item in result.items] == ["ok"]


def test_aggregator_raises_when_all_sources_fail(monkeypatch) -> None:
    dead1 = _FakeProvider(raises=ResearchSourceError("RESEARCH_SOURCE_FAILED: a"))
    dead2 = _FakeProvider(raises=RuntimeError("b"))
    _patch_registry(monkeypatch, {"a": dead1, "b": dead2})

    provider = AggregatingSearchProvider(["a", "b"])
    with pytest.raises(ResearchSourceError, match="全部检索来源失败"):
        provider.search(query="x", symbol="600519.SH", max_results=10, timeout=10)


def test_aggregator_raises_when_configured_with_no_providers() -> None:
    provider = AggregatingSearchProvider([])
    with pytest.raises(ResearchSourceError, match="未配置任何来源"):
        provider.search(query="x", symbol="600519.SH", max_results=10, timeout=10)


def test_aggregator_raises_when_all_return_empty(monkeypatch) -> None:
    empty = _FakeProvider(items=[])
    _patch_registry(monkeypatch, {"a": empty})

    provider = AggregatingSearchProvider(["a"])
    with pytest.raises(ResearchSourceError, match="无结果"):
        provider.search(query="x", symbol="600519.SH", max_results=10, timeout=10)


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


def test_aggregator_dedups_same_url_across_sources(monkeypatch) -> None:
    dup_url = "https://www.example.com/report.pdf?utm_source=x#frag"
    a = _FakeProvider(items=[_src(title="from-a", url=dup_url, kind="news")])
    b = _FakeProvider(items=[_src(title="from-b", url=dup_url, kind="news")])
    _patch_registry(monkeypatch, {"a": a, "b": b})

    provider = AggregatingSearchProvider(["a", "b"])
    result = provider.search(query="x", symbol="600519.SH", max_results=10, timeout=10)

    assert len(result.items) == 1
    # First-seen wins; from-a came from provider "a" which ran first.
    assert result.items[0].title == "from-a"


def test_dedup_key_normalizes_host_path_drops_query_fragment() -> None:
    assert _dedup_key("https://WWW.example.com/a/b/") == "www.example.com|/a/b"
    assert _dedup_key("https://www.example.com/a/b?x=1") == "www.example.com|/a/b"
    assert _dedup_key("https://www.example.com/a/b#frag") == "www.example.com|/a/b"
    assert _dedup_key("http://www.example.com/a/b") == _dedup_key("https://www.example.com/a/b")


# ---------------------------------------------------------------------------
# Rerank
# ---------------------------------------------------------------------------


def test_aggregator_reranks_by_source_kind_priority(monkeypatch) -> None:
    """Announcement beats research_report beats news beats web, regardless of
    fan-out order."""
    items = [
        _src(title="web-late", url="https://w.example/1", kind="web", date="2026-04-01"),
        _src(title="news", url="https://n.example/1", kind="news", date="2026-04-01"),
        _src(title="report", url="https://r.example/1", kind="research_report", date="2026-04-01"),
        _src(title="announce", url="https://a.example/1", kind="announcement", date="2026-04-01"),
    ]
    _patch_registry(monkeypatch, {"only": _FakeProvider(items=items)})

    provider = AggregatingSearchProvider(["only"])
    result = provider.search(query="x", symbol="600519.SH", max_results=10, timeout=10)

    titles = [item.title for item in result.items]
    assert titles == ["announce", "report", "news", "web-late"]


def test_aggregator_reranks_by_date_desc_within_same_kind(monkeypatch) -> None:
    items = [
        _src(title="old-news", url="https://n.example/old", kind="news", date="2026-01-01"),
        _src(title="new-news", url="https://n.example/new", kind="news", date="2026-06-01"),
        _src(title="mid-news", url="https://n.example/mid", kind="news", date="2026-03-01"),
    ]
    _patch_registry(monkeypatch, {"only": _FakeProvider(items=items)})

    provider = AggregatingSearchProvider(["only"])
    result = provider.search(query="x", symbol="600519.SH", max_results=10, timeout=10)

    titles = [item.title for item in result.items]
    assert titles == ["new-news", "mid-news", "old-news"]


def test_aggregator_kind_priority_stable_across_date_order(monkeypatch) -> None:
    """A newer news item must NOT outrank an older announcement."""
    items = [
        _src(title="fresh-news", url="https://n.example/1", kind="news", date="2026-12-31"),
        _src(title="stale-announce", url="https://a.example/1", kind="announcement", date="2020-01-01"),
    ]
    _patch_registry(monkeypatch, {"only": _FakeProvider(items=items)})

    provider = AggregatingSearchProvider(["only"])
    result = provider.search(query="x", symbol="600519.SH", max_results=10, timeout=10)

    titles = [item.title for item in result.items]
    assert titles == ["stale-announce", "fresh-news"]


def test_aggregator_empty_date_sorts_last_within_kind(monkeypatch) -> None:
    items = [
        _src(title="dated", url="https://n.example/1", kind="news", date="2026-01-01"),
        _src(title="undated", url="https://n.example/2", kind="news", date=""),
    ]
    _patch_registry(monkeypatch, {"only": _FakeProvider(items=items)})

    provider = AggregatingSearchProvider(["only"])
    result = provider.search(query="x", symbol="600519.SH", max_results=10, timeout=10)

    titles = [item.title for item in result.items]
    assert titles == ["dated", "undated"]


def test_aggregator_truncates_to_max_results(monkeypatch) -> None:
    items = [_src(title=f"n{i}", url=f"https://n.example/{i}", kind="news") for i in range(5)]
    _patch_registry(monkeypatch, {"only": _FakeProvider(items=items)})

    provider = AggregatingSearchProvider(["only"])
    result = provider.search(query="x", symbol="600519.SH", max_results=3, timeout=10)

    assert len(result.items) == 3


# ---------------------------------------------------------------------------
# Re-numbering + content pass-through
# ---------------------------------------------------------------------------


def test_aggregator_renumbers_result_ids_after_merge(monkeypatch) -> None:
    a = _FakeProvider(items=[_src(title="a1", url="https://a.example/1", kind="news")])
    b = _FakeProvider(items=[_src(title="b1", url="https://b.example/1", kind="news")])
    _patch_registry(monkeypatch, {"a": a, "b": b})

    provider = AggregatingSearchProvider(["a", "b"])
    result = provider.search(query="x", symbol="600519.SH", max_results=10, timeout=10)

    assert [item.result_id for item in result.items] == ["src_001", "src_002"]


def test_aggregator_preserves_prefetched_content(monkeypatch) -> None:
    """Content from a pre-fetching source (e.g. akshare_news) must survive the
    merge/rerank so the read path can use it without a download."""
    a = _FakeProvider(
        items=[_src(title="news-with-body", url="https://n.example/1", kind="news", content="预取正文")]
    )
    _patch_registry(monkeypatch, {"a": a})

    provider = AggregatingSearchProvider(["a"])
    result = provider.search(query="x", symbol="600519.SH", max_results=10, timeout=10)

    assert result.items[0].content == "预取正文"


def test_aggregator_unknown_source_kind_gets_default_priority(monkeypatch) -> None:
    """An untagged/unknown kind falls to priority 9 (below web=5)."""
    items = [
        _src(title="web", url="https://w.example/1", kind="web"),
        _src(title="unknown", url="https://u.example/1", kind="mystery"),
    ]
    _patch_registry(monkeypatch, {"only": _FakeProvider(items=items)})

    provider = AggregatingSearchProvider(["only"])
    result = provider.search(query="x", symbol="600519.SH", max_results=10, timeout=10)

    titles = [item.title for item in result.items]
    assert titles == ["web", "unknown"]


# ---------------------------------------------------------------------------
# Source preferences: Provider registration stays internal
# ---------------------------------------------------------------------------


def test_aggregator_sources_are_preferences_and_still_fan_out_to_all_providers(monkeypatch) -> None:
    """sources 只影响结果排序，不能收窄内部 Provider fan-out。"""
    notice = _FakeProvider(items=[_src(title="ann", url="https://an.example/1", kind="announcement")])
    report = _FakeProvider(items=[_src(title="rpt", url="https://rp.example/1", kind="research_report")])
    news = _FakeProvider(items=[_src(title="nws", url="https://nw.example/1", kind="news")])
    _patch_registry(
        monkeypatch,
        {"official_crawler": notice, "akshare_reports": report, "akshare_news": news},
    )
    # 让 PROVIDER_SOURCE_KINDS 把这些名映射回 kind（真实 catalog 已含）
    provider = AggregatingSearchProvider(
        ["official_crawler", "akshare_reports", "akshare_news"]
    )

    result = provider.search(
        query="x", symbol="600519.SH", max_results=10, timeout=10, sources=["research_report"]
    )
    assert [item.title for item in result.items] == ["rpt", "ann", "nws"]
    assert report.calls == 1 and notice.calls == 1 and news.calls == 1


def test_aggregator_sources_preserve_preference_order_before_default_authority(monkeypatch) -> None:
    """多个偏好按输入顺序排序，其余结果仍按默认权威性排序。"""
    notice = _FakeProvider(items=[_src(title="ann", url="https://an.example/1", kind="announcement")])
    report = _FakeProvider(items=[_src(title="rpt", url="https://rp.example/1", kind="research_report")])
    news = _FakeProvider(items=[_src(title="nws", url="https://nw.example/1", kind="news")])
    _patch_registry(
        monkeypatch,
        {"official_crawler": notice, "akshare_reports": report, "akshare_news": news},
    )
    provider = AggregatingSearchProvider(
        ["official_crawler", "akshare_reports", "akshare_news"]
    )

    result = provider.search(
        query="x",
        symbol="600519.SH",
        max_results=10,
        timeout=10,
        sources=["research_report", "news", "web"],
    )

    assert [item.title for item in result.items] == ["rpt", "nws", "ann"]
    assert report.calls == 1 and news.calls == 1 and notice.calls == 1


def test_aggregator_legacy_provider_name_is_only_a_preference(monkeypatch) -> None:
    """兼容旧 Provider 名，但不能让 Agent 用它限制搜索范围。"""
    notice = _FakeProvider(items=[_src(title="ann", url="https://an.example/1", kind="announcement")])
    report = _FakeProvider(items=[_src(title="rpt", url="https://rp.example/1", kind="research_report")])
    _patch_registry(monkeypatch, {"official_crawler": notice, "akshare_reports": report})
    provider = AggregatingSearchProvider(["official_crawler", "akshare_reports"])

    result = provider.search(
        query="x", symbol="600519.SH", max_results=10, timeout=10, sources=["akshare_reports"]
    )

    assert [item.title for item in result.items] == ["rpt", "ann"]
    assert report.calls == 1 and notice.calls == 1


def test_aggregator_unknown_source_preference_is_ignored_without_blocking_search(monkeypatch) -> None:
    """网站别名或模型幻觉不能中断已有 Provider 的聚合检索。"""
    _patch_registry(
        monkeypatch,
        {"official_crawler": _FakeProvider(items=[_src(title="a", url="https://a.example/1", kind="announcement")])},
    )
    provider = AggregatingSearchProvider(["official_crawler"])

    result = provider.search(
        query="x", symbol="600519.SH", max_results=10, timeout=10,
        sources=["gsxt", "eastmoney", "sina"],
    )

    assert [item.title for item in result.items] == ["a"]


# ---------------------------------------------------------------------------
# Direction filter (V4): alias tolerance + unconfigured-direction fallback
# ---------------------------------------------------------------------------


def test_aggregator_source_preferences_accept_common_aliases(monkeypatch) -> None:
    """常见方向别名影响排序，但不会限制聚合来源。"""
    notice = _FakeProvider(items=[_src(title="ann", url="https://an.example/1", kind="announcement")])
    report = _FakeProvider(items=[_src(title="rpt", url="https://rp.example/1", kind="research_report")])
    _patch_registry(
        monkeypatch,
        {"official_crawler": notice, "akshare_reports": report},
    )
    provider = AggregatingSearchProvider(["official_crawler", "akshare_reports"])

    result = provider.search(
        query="x", symbol="600519.SH", max_results=10, timeout=10, sources=["disclosure"]
    )
    assert [item.title for item in result.items] == ["ann", "rpt"]
    assert notice.calls == 1 and report.calls == 1

    notice.calls = 0
    report.calls = 0
    result = provider.search(
        query="x", symbol="600519.SH", max_results=10, timeout=10, sources=["research"]
    )
    assert [item.title for item in result.items] == ["rpt", "ann"]
    assert report.calls == 1 and notice.calls == 1


def test_aggregator_chinese_source_preferences_reorder_results(monkeypatch) -> None:
    """中文方向名（公告/研报/新闻）也应解析为排序偏好。"""
    notice = _FakeProvider(items=[_src(title="ann", url="https://an.example/1", kind="announcement")])
    report = _FakeProvider(items=[_src(title="rpt", url="https://rp.example/1", kind="research_report")])
    news = _FakeProvider(items=[_src(title="nws", url="https://nw.example/1", kind="news")])
    _patch_registry(
        monkeypatch,
        {"official_crawler": notice, "akshare_reports": report, "akshare_news": news},
    )
    provider = AggregatingSearchProvider(["official_crawler", "akshare_reports", "akshare_news"])

    result = provider.search(
        query="x", symbol="600519.SH", max_results=10, timeout=10, sources=["公告"]
    )
    assert [item.title for item in result.items] == ["ann", "rpt", "nws"]

    result = provider.search(
        query="x", symbol="600519.SH", max_results=10, timeout=10, sources=["研报"]
    )
    assert [item.title for item in result.items] == ["rpt", "ann", "nws"]


def test_aggregator_unconfigured_source_preference_keeps_all_providers(monkeypatch) -> None:
    """未启用的方向只是偏好落空，不是检索失败。"""
    notice = _FakeProvider(items=[_src(title="ann", url="https://an.example/1", kind="announcement")])
    _patch_registry(monkeypatch, {"official_crawler": notice})
    provider = AggregatingSearchProvider(["official_crawler"])

    result = provider.search(
        query="x", symbol="600519.SH", max_results=10, timeout=10, sources=["web"]
    )
    assert [item.title for item in result.items] == ["ann"]
    assert notice.calls == 1


def test_aggregator_mixed_source_preferences_never_restrict_fan_out(monkeypatch) -> None:
    """合法或非法偏好混用都不得减少可用来源。"""
    notice = _FakeProvider(items=[_src(title="ann", url="https://an.example/1", kind="announcement")])
    _patch_registry(monkeypatch, {"official_crawler": notice})
    provider = AggregatingSearchProvider(["official_crawler"])

    result = provider.search(
        query="x", symbol="600519.SH", max_results=10, timeout=10, sources=["web", "nonsense"]
    )
    assert [item.title for item in result.items] == ["ann"]
    assert notice.calls == 1
