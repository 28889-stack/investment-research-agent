from __future__ import annotations

import pytest
import pandas as pd

from app.fundamental.evidence import ResearchSourceError
from app.retrieval._akshare_common import _coerce_frame, _retry_call
from app.retrieval.akshare_news import AkshareNewsProvider
from app.retrieval.akshare_notices import AkshareNoticeProvider
from app.retrieval.akshare_reports import AkshareReportProvider


class _FakeAk:
    """Fake akshare module whose `stock_*` functions return canned DataFrames."""

    def __init__(self, news=None, reports=None, notices=None, raises=None) -> None:
        self._news = news
        self._reports = reports
        self._notices = notices
        self._raises = raises or {}
        self.calls = {"news": 0, "reports": 0, "notices": 0}

    def stock_news_em(self, *, symbol):
        self.calls["news"] += 1
        if "news" in self._raises:
            raise self._raises["news"]
        return self._news

    def stock_research_report_em(self, *, symbol):
        self.calls["reports"] += 1
        if "reports" in self._raises:
            raise self._raises["reports"]
        return self._reports

    def stock_individual_notice_report(self, *, security, symbol, begin_date, end_date):
        self.calls["notices"] += 1
        if "notices" in self._raises:
            raise self._raises["notices"]
        return self._notices


# ---------------------------------------------------------------------------
# akshare_news
# ---------------------------------------------------------------------------


def test_akshare_news_prefetches_body_into_content() -> None:
    frame = pd.DataFrame(
        [
            {
                "新闻标题": "茅台年报发布",
                "新闻链接": "https://finance.eastmoney.com/news/1.html",
                "新闻内容": "贵州茅台公布年度业绩……",
                "发布时间": "2026-03-28",
            }
        ]
    )
    ak = _FakeAk(news=frame)
    provider = AkshareNewsProvider(akshare_factory=lambda: ak)

    result = provider.search(query="年报", symbol="600519.SH", max_results=5, timeout=10)

    item = result.items[0]
    assert item.title == "茅台年报发布"
    assert item.source_name == "东方财富·新闻"
    assert item.source_kind == "news"
    assert item.content == "贵州茅台公布年度业绩……"
    assert item.summary == item.content
    assert item.date == "2026-03-28"
    assert ak.calls["news"] == 1


def test_akshare_news_strips_market_suffix_from_symbol() -> None:
    captured = {}

    class Ak:
        def stock_news_em(self, *, symbol):
            captured["symbol"] = symbol
            return pd.DataFrame(
                [
                    {
                        "新闻标题": "t",
                        "新闻链接": "https://example.com/x",
                        "新闻内容": "b",
                        "发布时间": "2026-01-01",
                    }
                ]
            )

    provider = AkshareNewsProvider(akshare_factory=lambda: Ak())
    provider.search(query="x", symbol="600519.SH", max_results=5, timeout=10)

    assert captured["symbol"] == "600519"


def test_akshare_news_retries_transient_failure() -> None:
    frame = pd.DataFrame(
        [
            {
                "新闻标题": "t",
                "新闻链接": "https://example.com/x",
                "新闻内容": "b",
                "发布时间": "2026-01-01",
            }
        ]
    )
    ak = _FakeAk(news=frame, raises={"news": RuntimeError("transient")})
    provider = AkshareNewsProvider(akshare_factory=lambda: ak, max_retries=2)

    with pytest.raises(ResearchSourceError, match="新闻检索失败"):
        provider.search(query="x", symbol="600519.SH", max_results=5, timeout=10)

    # Initial attempt + 2 retries = 3 calls.
    assert ak.calls["news"] == 3


def test_akshare_news_empty_frame_raises() -> None:
    ak = _FakeAk(news=pd.DataFrame())
    provider = AkshareNewsProvider(akshare_factory=lambda: ak)

    with pytest.raises(ResearchSourceError, match="新闻为空"):
        provider.search(query="x", symbol="600519.SH", max_results=5, timeout=10)


def test_akshare_news_skips_rows_missing_url_or_title() -> None:
    frame = pd.DataFrame(
        [
            {"新闻标题": "", "新闻链接": "https://example.com/1", "新闻内容": "b", "发布时间": ""},
            {"新闻标题": "ok", "新闻链接": "", "新闻内容": "b", "发布时间": ""},
            {"新闻标题": "good", "新闻链接": "https://example.com/3", "新闻内容": "b", "发布时间": "2026-01-01"},
        ]
    )
    ak = _FakeAk(news=frame)
    provider = AkshareNewsProvider(akshare_factory=lambda: ak)

    result = provider.search(query="x", symbol="600519.SH", max_results=5, timeout=10)

    assert len(result.items) == 1
    assert result.items[0].title == "good"


# ---------------------------------------------------------------------------
# akshare_reports
# ---------------------------------------------------------------------------


def test_akshare_reports_returns_metadata_with_pdf_link() -> None:
    frame = pd.DataFrame(
        [
            {
                "报告名称": "贵州茅台深度研究",
                "报告PDF链接": "https://pdf.dfcfw.com/pdf/H3_2026.pdf",
                "机构名称": "中信证券",
                "投资评级": "买入",
                "日期": "2026-02-15",
            }
        ]
    )
    ak = _FakeAk(reports=frame)
    provider = AkshareReportProvider(akshare_factory=lambda: ak)

    result = provider.search(query="茅台", symbol="600519.SH", max_results=5, timeout=10)

    item = result.items[0]
    assert item.title == "贵州茅台深度研究"
    assert item.url == "https://pdf.dfcfw.com/pdf/H3_2026.pdf"
    assert item.source_kind == "research_report"
    assert item.content == ""  # body downloaded on read, not pre-fetched
    assert "中信证券" in item.summary and "买入" in item.summary
    assert item.date == "2026-02-15"


def test_akshare_reports_handles_alternate_column_names() -> None:
    frame = pd.DataFrame(
        [
            {
                "研报标题": "t",
                "PDF链接": "https://pdf.dfcfw.com/x.pdf",
                "研究机构": "机构A",
                "评级": "增持",
                "研报日期": "2026-03-01",
            }
        ]
    )
    ak = _FakeAk(reports=frame)
    provider = AkshareReportProvider(akshare_factory=lambda: ak)

    result = provider.search(query="x", symbol="600519.SH", max_results=5, timeout=10)

    assert result.items[0].title == "t"
    assert result.items[0].url == "https://pdf.dfcfw.com/x.pdf"
    assert result.items[0].date == "2026-03-01"


def test_akshare_reports_empty_raises() -> None:
    ak = _FakeAk(reports=pd.DataFrame())
    provider = AkshareReportProvider(akshare_factory=lambda: ak)

    with pytest.raises(ResearchSourceError, match="研报为空"):
        provider.search(query="x", symbol="600519.SH", max_results=5, timeout=10)


# ---------------------------------------------------------------------------
# akshare_notices
# ---------------------------------------------------------------------------


def test_akshare_notices_filters_by_keyword_and_returns_html_link() -> None:
    frame = pd.DataFrame(
        [
            {
                "公告标题": "贵州茅台2025年年度报告",
                "网址": "https://data.eastmoney.com/notices/detail/1.html",
                "公告类型": "年报",
                "公告日期": "2026-04-17",
            },
            {
                "公告标题": "关于召开股东大会的通知",
                "网址": "https://data.eastmoney.com/notices/detail/2.html",
                "公告类型": "股东大会",
                "公告日期": "2026-03-10",
            },
        ]
    )
    ak = _FakeAk(notices=frame)
    provider = AkshareNoticeProvider(akshare_factory=lambda: ak)

    result = provider.search(query="年度报告", symbol="600519.SH", max_results=5, timeout=10)

    assert len(result.items) == 1
    item = result.items[0]
    assert item.title == "贵州茅台2025年年度报告"
    assert item.source_kind == "announcement"
    assert item.url.endswith("/1.html")
    assert item.date == "2026-04-17"


def test_akshare_notices_no_keyword_match_returns_all_when_query_short() -> None:
    """A single-char keyword (len < 2) is dropped, so no client-side filter applies."""
    frame = pd.DataFrame(
        [
            {
                "公告标题": "任意公告",
                "网址": "https://data.eastmoney.com/x.html",
                "公告类型": "",
                "公告日期": "2026-01-01",
            }
        ]
    )
    ak = _FakeAk(notices=frame)
    provider = AkshareNoticeProvider(akshare_factory=lambda: ak)

    result = provider.search(query="年", symbol="600519.SH", max_results=5, timeout=10)

    assert len(result.items) == 1


def test_akshare_notices_no_match_raises() -> None:
    frame = pd.DataFrame(
        [
            {
                "公告标题": "完全不相关的东西",
                "网址": "https://data.eastmoney.com/x.html",
                "公告类型": "",
                "公告日期": "2026-01-01",
            }
        ]
    )
    ak = _FakeAk(notices=frame)
    provider = AkshareNoticeProvider(akshare_factory=lambda: ak)

    with pytest.raises(ResearchSourceError, match="无匹配条目"):
        provider.search(query="年度报告", symbol="600519.SH", max_results=5, timeout=10)


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def test_retry_call_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("boom")
        return "ok"

    assert _retry_call(flaky, max_retries=3) == "ok"
    assert calls["n"] == 3


def test_retry_call_gives_up_after_max_retries() -> None:
    def always_fails():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        _retry_call(always_fails, max_retries=1)


def test_coerce_frame_passes_dataframe_through() -> None:
    df = pd.DataFrame([{"a": 1}])
    assert _coerce_frame(df) is df


def test_coerce_frame_wraps_non_frame() -> None:
    wrapped = _coerce_frame([{"a": 1}])
    assert isinstance(wrapped, pd.DataFrame)
    assert wrapped.iloc[0]["a"] == 1
