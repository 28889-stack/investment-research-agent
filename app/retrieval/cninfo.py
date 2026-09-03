from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Callable

import httpx

from app.fundamental.schemas import ResearchSearchResults, ResearchSource


_BASE_URL = "https://www.cninfo.com.cn"
_STOCK_LIST_URL = f"{_BASE_URL}/new/data/szse_stock.json"
_QUERY_URL = f"{_BASE_URL}/new/hisAnnouncement/query"
_STATIC_URL = "https://static.cninfo.com.cn"

# CNInfo performs literal title matching. These candidates turn semantic Agent
# requests into terms commonly present in official announcement titles.
_CNINFO_FALLBACK_TERMS = (
    "年度报告",
    "半年度报告",
    "经营情况",
    "业绩公告",
)


def _query_candidates(query: str) -> list[str]:
    original = " ".join(query.split())
    candidates: list[str] = [original] if original else []
    compact = re.sub(r"[，,、；;。.!！？?：:/\\\\|]+", " ", original)
    terms = [term for term in re.split(r"\\s+", compact) if len(term) >= 2]
    for term in terms:
        if term not in candidates:
            candidates.append(term)
    for term in _CNINFO_FALLBACK_TERMS:
        if term not in candidates:
            candidates.append(term)
    candidates.append("")
    return candidates


class CninfoSearchProvider:
    """Keyless, allow-listed announcement search for the official CNInfo site."""

    def __init__(self, client_factory: Callable[..., object] = httpx.Client) -> None:
        self._client_factory = client_factory

    def search(
        self,
        *,
        query: str,
        symbol: str,
        max_results: int,
        timeout: float,
    ) -> ResearchSearchResults:
        code = symbol.partition(".")[0]
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError("巨潮检索仅支持 6 位 A 股证券代码")
        suffix = symbol.partition(".")[2].upper()
        column = "sse" if suffix == "SH" or code.startswith(("5", "6", "9")) else "szse"
        plate = "sh" if column == "sse" else "sz"
        headers = {
            "User-Agent": "FinancialResearchAgent/1.0",
            "Referer": f"{_BASE_URL}/",
            "Origin": _BASE_URL,
            "X-Requested-With": "XMLHttpRequest",
        }
        with self._client_factory(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            headers=headers,
        ) as client:
            stock_payload = self._request(lambda: client.get(_STOCK_LIST_URL)).json()
            security = next(
                (item for item in stock_payload.get("stockList", []) if item.get("code") == code),
                None,
            )
            if security is None or not security.get("orgId"):
                raise ValueError(f"巨潮证券列表中未找到 {code}")
            # CNInfo `searchkey` matches announcement titles literally, not
            # semantically. A natural-language research query often matches no
            # title at all, so an empty result here means "keyword未命中标题",
            # not "检索失败". Fall back to the company's recent official filings,
            # which are exactly the authoritative disclosures research needs.
            items: list[ResearchSource] = []
            last_error: Exception | None = None
            for search_key in _query_candidates(query):
                # Degrade per-candidate: a transient request failure on one term
                # must not abort the whole search. Remember it and try the next
                # candidate; only surface an error if every candidate fails.
                try:
                    response = self._request(
                        lambda key=search_key: client.post(
                            _QUERY_URL,
                            data={
                                "pageNum": "1",
                                "pageSize": str(max_results),
                                "column": column,
                                "tabName": "fulltext",
                                "plate": plate,
                                "stock": f"{code},{security['orgId']}",
                                "searchkey": key,
                                "secid": "",
                                "category": "",
                                "trade": "",
                                "seDate": "",
                            },
                            headers=headers,
                        )
                    )
                except (httpx.HTTPError, ValueError) as exc:
                    last_error = exc
                    continue
                items = self._to_sources(response.json(), security, code, max_results)
                if items:
                    break
        if not items:
            if last_error is not None:
                raise last_error
            raise ValueError("巨潮资讯未返回匹配公告")
        return ResearchSearchResults(items=items)

    @staticmethod
    def _to_sources(payload: dict, security: dict, code: str, max_results: int) -> list[ResearchSource]:
        items: list[ResearchSource] = []
        for announcement in payload.get("announcements") or []:
            relative_url = str(announcement.get("adjunctUrl", "")).lstrip("/")
            if not relative_url.startswith("finalpage/"):
                continue
            title = re.sub(r"<[^>]+>", "", unescape(str(announcement.get("announcementTitle", ""))))
            timestamp = announcement.get("announcementTime")
            published = ""
            if isinstance(timestamp, (int, float)):
                china_time = timezone(timedelta(hours=8))
                published = datetime.fromtimestamp(timestamp / 1000, tz=china_time).date().isoformat()
            items.append(
                ResearchSource(
                    result_id=f"src_{len(items) + 1:03d}",
                    title=title.strip(),
                    url=f"{_STATIC_URL}/{relative_url}",
                    source_name="巨潮资讯",
                    date=published,
                    summary=f"{announcement.get('secName', security.get('zwjc', code))} 公告原文",
                    source_kind="announcement",
                )
            )
            if len(items) >= max_results:
                break
        return items

    @staticmethod
    def _request(call):
        for attempt in range(2):
            try:
                response = call()
                response.raise_for_status()
                return response
            except httpx.HTTPStatusError as exc:
                if (exc.response.status_code < 500 and exc.response.status_code not in {408, 429}) or attempt:
                    raise
            except httpx.RequestError:
                if attempt:
                    raise
            time.sleep(0.1)
        raise RuntimeError("unreachable")
