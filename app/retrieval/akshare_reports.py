from __future__ import annotations

from typing import Any, Callable

from app.fundamental.evidence import ResearchSourceError
from app.fundamental.schemas import ResearchSearchResults, ResearchSource
from app.retrieval._akshare_common import _coerce_frame, _retry_call


class AkshareReportProvider:
    """AKShare `stock_research_report_em` adapter: report metadata + PDF URL.

    The endpoint returns ~760 rows of analyst reports with a `报告PDF链接` column
    pointing to pdf.dfcfw.com. Only metadata + URL are captured here; the body
    is downloaded on read (PDF path). No pre-fetched body.
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
            frame = _retry_call(lambda: ak.stock_research_report_em(symbol=code), self._max_retries)
        except Exception as exc:
            raise ResearchSourceError("RESEARCH_SOURCE_FAILED: 东方财富研报检索失败") from exc
        frame = _coerce_frame(frame)
        if frame.empty:
            raise ResearchSourceError("RESEARCH_SOURCE_FAILED: 东方财富研报为空")
        items: list[ResearchSource] = []
        for _, row in frame.iterrows():
            title = str(row.get("报告名称", "") or row.get("研报标题", "")).strip()
            url = str(row.get("报告PDF链接", "") or row.get("PDF链接", "")).strip()
            institution = str(row.get("机构名称", "") or row.get("研究机构", "")).strip()
            rating = str(row.get("投资评级", "") or row.get("评级", "")).strip()
            date = str(row.get("日期", "") or row.get("研报日期", "")).strip()
            if not url or not title:
                continue
            summary_parts = [part for part in (institution, rating) if part]
            summary = " · ".join(summary_parts) if summary_parts else "券商研究报告"
            items.append(
                ResearchSource(
                    result_id=f"src_{len(items) + 1:03d}",
                    title=title,
                    url=url,
                    source_name=f"东方财富·研报{('·' + institution) if institution else ''}",
                    date=date,
                    summary=summary,
                    content="",
                    source_kind="research_report",
                )
            )
            if len(items) >= max_results:
                break
        if not items:
            raise ResearchSourceError("RESEARCH_SOURCE_FAILED: 东方财富研报无可用条目")
        return ResearchSearchResults(items=items)

    def _load_akshare(self) -> Any:
        if self._akshare_factory is not None:
            return self._akshare_factory()
        import akshare as ak

        return ak
