from __future__ import annotations

from typing import Any, Callable

from app.fundamental.evidence import ResearchSourceError
from app.fundamental.schemas import ResearchSearchResults, ResearchSource
from app.retrieval._akshare_common import _coerce_frame, _retry_call


class AkshareNewsProvider:
    """AKShare `stock_news_em` adapter: news metadata with **pre-fetched body**.

    The endpoint already returns the article body in the `新闻内容` column, so
    no download is needed at read time — `content` is populated here and the
    read path uses it directly, bypassing SSRF/HTTP entirely.
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
            frame = _retry_call(lambda: ak.stock_news_em(symbol=code), self._max_retries)
        except Exception as exc:
            raise ResearchSourceError("RESEARCH_SOURCE_FAILED: 东方财富新闻检索失败") from exc
        frame = _coerce_frame(frame)
        if frame.empty:
            raise ResearchSourceError("RESEARCH_SOURCE_FAILED: 东方财富新闻为空")
        items: list[ResearchSource] = []
        for _, row in frame.iterrows():
            title = str(row.get("新闻标题", "")).strip()
            url = str(row.get("新闻链接", "")).strip()
            body = str(row.get("新闻内容", "")).strip()
            date = str(row.get("发布时间", "")).strip()
            if not url or not title:
                continue
            items.append(
                ResearchSource(
                    result_id=f"src_{len(items) + 1:03d}",
                    title=title,
                    url=url,
                    source_name="东方财富·新闻",
                    date=date,
                    summary=body[:2_000],
                    content=body,
                    source_kind="news",
                )
            )
            if len(items) >= max_results:
                break
        if not items:
            raise ResearchSourceError("RESEARCH_SOURCE_FAILED: 东方财富新闻无可用条目")
        return ResearchSearchResults(items=items)

    def _load_akshare(self) -> Any:
        if self._akshare_factory is not None:
            return self._akshare_factory()
        import akshare as ak

        return ak
