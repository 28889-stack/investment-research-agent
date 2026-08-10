from __future__ import annotations

import re
from typing import Any, Callable

from app.fundamental.evidence import ResearchSourceError
from app.fundamental.schemas import ResearchSearchResults, ResearchSource
from app.retrieval._akshare_common import _coerce_frame, _retry_call


class AkshareNoticeProvider:
    """AKShare `stock_individual_notice_report` adapter: notice metadata + HTML URL.

    The endpoint returns ~149 rows of company announcements with a `网址` column
    pointing to data.eastmoney.com/notices/detail HTML. Only metadata + URL are
    captured here; the body is downloaded on read (HTML path).
    """

    def __init__(
        self,
        akshare_factory: Callable[[], Any] | None = None,
        max_retries: int = 2,
    ) -> None:
        self._akshare_factory = akshare_factory
        self._max_retries = max_retries

    def search(
        self,
        *,
        query: str,
        symbol: str,
        max_results: int,
        timeout: float,
    ) -> ResearchSearchResults:
        ak = self._load_akshare()
        code = symbol.partition(".")[0]
        try:
            # akshare's stock_individual_notice_report signature is
            # (security=stock_code, symbol=notice_category, ...). `symbol` selects
            # the notice category from a fixed map ({"全部","财务报告",...}); passing
            # the stock code there raises KeyError. We pass the code as `security`
            # (filtered server-side to that company) and "全部" as the category, then
            # narrow further with the client-side keyword filter below.
            frame = _retry_call(
                lambda: ak.stock_individual_notice_report(
                    security=code,
                    symbol="全部",
                    begin_date="20200101",
                    end_date="20261231",
                ),
                self._max_retries,
            )
        except Exception as exc:
            raise ResearchSourceError("RESEARCH_SOURCE_FAILED: 东方财富公告检索失败") from exc
        frame = _coerce_frame(frame)
        if frame.empty:
            raise ResearchSourceError("RESEARCH_SOURCE_FAILED: 东方财富公告为空")
        keywords = [term for term in re.split(r"\s+", " ".join(query.split())) if len(term) >= 2]
        items: list[ResearchSource] = []
        for _, row in frame.iterrows():
            title = str(row.get("公告标题", "") or row.get("标题", "")).strip()
            url = str(row.get("网址", "") or row.get("公告链接", "")).strip()
            notice_type = str(row.get("公告类型", "") or "").strip()
            date = str(row.get("公告日期", "") or row.get("日期", "")).strip()
            if not url or not title:
                continue
            # Lightweight client-side keyword filter (endpoint has no query param).
            if keywords and not any(kw in title for kw in keywords):
                continue
            items.append(
                ResearchSource(
                    result_id=f"src_{len(items) + 1:03d}",
                    title=title,
                    url=url,
                    source_name=f"东方财富·公告{('·' + notice_type) if notice_type else ''}",
                    date=date,
                    summary=notice_type or "公司公告",
                    content="",
                    source_kind="announcement",
                )
            )
            if len(items) >= max_results:
                break
        if not items:
            raise ResearchSourceError("RESEARCH_SOURCE_FAILED: 东方财富公告无匹配条目")
        return ResearchSearchResults(items=items)

    def _load_akshare(self) -> Any:
        if self._akshare_factory is not None:
            return self._akshare_factory()
        import akshare as ak

        return ak
