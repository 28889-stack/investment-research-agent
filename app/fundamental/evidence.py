from __future__ import annotations

import ipaddress
import json
import os
import socket
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.fundamental.schemas import (
    EvidenceCollection,
    EvidenceItem,
    ResearchSearchResults,
    ResearchSource,
)


class EvidenceStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> EvidenceCollection:
        if not self.path.is_file():
            return EvidenceCollection(items=[])
        return EvidenceCollection.model_validate_json(self.path.read_text(encoding="utf-8"))

    def add(
        self,
        *,
        claim: str,
        content: str,
        source_name: str,
        url: str,
        date_value: str,
        location: str,
        evidence_type: str,
    ) -> EvidenceItem:
        collection = self.load()
        used = {int(item.id.partition("_")[2]) for item in collection.items}
        number = 1
        while number in used:
            number += 1
        item = EvidenceItem(
            id=f"ev_{number:03d}",
            claim=claim,
            content=content,
            source_name=source_name,
            url=url,
            date=date_value,
            location=location,
            type=evidence_type,
        )
        collection.items.append(item)
        self._write(collection)
        return item

    def validate_ids(self, ids: list[str]) -> None:
        available = {item.id for item in self.load().items}
        missing = set(ids) - available
        if missing:
            raise ValueError(f"Evidence ID 不存在: {', '.join(sorted(missing))}")

    def _write(self, collection: EvidenceCollection) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(
            json.dumps(collection.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


def is_safe_public_url(url: str) -> bool:
    """Hygiene guard for a single-user local app: reject local/in-network URLs.

    NOT enterprise SSRF hardening. Uses an explicit blocked-network list that
    deliberately excludes 198.18.0.0/15 (the local Fake-IP proxy range, which
    actually means "forward to the public internet via the system proxy").
    Avoids ipaddress.is_private/is_global because 198.18/15 is is_private=True,
    which would re-introduce the same false-positive that blocked all sources.
    """
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return False
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return False
    return not _is_blocked_address(hostname)


# Explicit local/in-network segments. 198.18.0.0/15 is intentionally absent:
# on this machine it is the Fake-IP proxy range routed to the public internet.
_BLOCKED_NETWORKS = [
    ipaddress.ip_network(network)
    for network in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "0.0.0.0/8",
        "::1/128",
        "fe80::/10",
        "fc00::/7",
    )
]


def _is_blocked_address(hostname: str) -> bool:
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, None)}
    except socket.gaierror:
        return True  # resolution failure is treated as blocked
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if any(ip in network for network in _BLOCKED_NETWORKS):
            return True
    return False


MOCK_SOURCES = {
    "src_001": {
        "title": "2025 年年度报告摘要",
        "url": "https://mock.local/annual-report",
        "source_name": "公司年度报告",
        "date": "2026-03-20",
        "summary": "披露公司主要业务、经营情况和财务概要。",
        "content": "公司主要从事茅台酒及系列酒的生产销售，并持续建设直销与批发渠道。",
    },
    "src_002": {
        "title": "白酒行业年度观察",
        "url": "https://mock.local/industry-outlook",
        "source_name": "Mock 行业研究机构",
        "date": "2026-02-10",
        "summary": "概述行业供需、竞争格局和渠道变化。",
        "content": "白酒行业需求具有周期性，头部品牌在渠道和品牌认知方面具有优势。",
    },
}


class ResearchSourceError(RuntimeError):
    code = "RESEARCH_SOURCE_FAILED"


def search_research_sources(
    query: str,
    symbol: str,
    settings,
    sources: list[str] | None = None,
) -> ResearchSearchResults:
    if not query.strip():
        raise ValueError("搜索关键词不能为空")
    if settings.research_search_mode == "mock":
        return ResearchSearchResults(
            items=[
                ResearchSource(result_id=result_id, **{key: value for key, value in item.items() if key != "content"})
                for result_id, item in list(MOCK_SOURCES.items())[: settings.research_search_max_results]
            ]
        )
    if settings.research_search_mode != "live":
        raise ResearchSourceError("RESEARCH_SOURCE_FAILED: 检索模式无效")
    try:
        from app.retrieval import get_search_provider
        from app.retrieval.aggregator import AggregatingSearchProvider

        provider = get_search_provider(settings.research_search_provider)
        # `sources` (direction filter) only applies to the aggregator, which fans
        # out to multiple sub-providers. It is never forwarded to a non-aggregator
        # provider: those keep the fixed 4-kwarg Protocol and have no notion of
        # multi-source direction grouping, so passing it would raise TypeError.
        if isinstance(provider, AggregatingSearchProvider):
            return provider.search(
                query=query,
                symbol=symbol,
                max_results=settings.research_search_max_results,
                timeout=settings.research_source_timeout,
                sources=sources,
            )
        return provider.search(
            query=query,
            symbol=symbol,
            max_results=settings.research_search_max_results,
            timeout=settings.research_source_timeout,
        )
    except ResearchSourceError:
        raise
    except Exception as exc:
        raise ResearchSourceError("RESEARCH_SOURCE_FAILED: 检索失败") from exc


def read_research_source(
    source: ResearchSource,
    *,
    claim: str,
    evidence_type: str,
    store: EvidenceStore,
    settings,
) -> EvidenceItem:
    if settings.research_search_mode == "mock":
        fixture = MOCK_SOURCES.get(source.result_id)
        if fixture is None:
            raise ValueError("搜索结果不存在")
        content = fixture["content"]
    elif source.content:
        content = source.content  # pre-fetched body (e.g. akshare news), no download
    else:
        content = _read_public_text(source.url, settings)
    return store.add(
        claim=claim,
        content=content[: settings.research_max_source_chars],
        source_name=source.source_name,
        url=source.url,
        date_value=source.date,
        location="",
        evidence_type=evidence_type,
    )


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if value:
            self.parts.append(value)


def _read_public_text(url: str, settings) -> str:
    if not is_safe_public_url(url):
        raise ResearchSourceError("RESEARCH_SOURCE_FAILED: 禁止访问本地或内网 URL")
    # Reader service: a JS-rendering extraction layer for sources the local
    # download+extract path cannot handle (JS-rendered detail shells, anti-bot
    # PDFs). ``research_reader == "none"`` keeps the legacy local path — this
    # preserves the offline/tests contract and the existing retry/oversized/
    # PDF-limit tests, which mock the local httpx download path.
    if settings.research_reader != "none":
        from app.retrieval.reader import read_with_reader

        return read_with_reader(url, settings)
    try:
        content, content_type = _download_public_source(url, settings)
        if "pdf" in content_type:
            return _extract_pdf_text(content, settings.research_max_source_chars)
        extractor = _TextExtractor()
        decoded = httpx.Response(200, content=content, headers={"content-type": content_type}).text
        extractor.feed(decoded[: settings.research_max_source_chars * 4])
        text = "\n".join(extractor.parts)
        if not text:
            raise ValueError("来源正文为空")
        return text[: settings.research_max_source_chars]
    except ResearchSourceError:
        raise
    except Exception as exc:
        raise ResearchSourceError("RESEARCH_SOURCE_FAILED: 来源读取失败") from exc


def _download_public_source(url: str, settings) -> tuple[bytes, str]:
    def fetch() -> tuple[bytes, str]:
        content = bytearray()
        with httpx.Client(
            timeout=settings.research_source_timeout,
            follow_redirects=False,
            trust_env=True,
        ) as client:
            with client.stream(
                "GET",
                url,
                headers={"User-Agent": "FinancialResearchAgent/1.0"},
            ) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                limit = (
                    settings.research_max_pdf_bytes
                    if "pdf" in content_type
                    else settings.research_max_source_chars * 8
                )
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > limit:
                        raise ValueError("来源响应过大")
        return bytes(content), content_type

    return _retry_http(fetch, response_expected=False)


def _retry_http(call, *, response_expected: bool = True):
    for attempt in range(2):
        try:
            result = call()
            if response_expected:
                result.raise_for_status()
            return result
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status not in {408, 429} and status < 500:
                raise
            if attempt == 1:
                raise
            time.sleep(0.1)
        except httpx.RequestError:
            if attempt == 1:
                raise
            time.sleep(0.1)
    raise RuntimeError("unreachable")


def _extract_pdf_text(content: bytes, max_chars: int) -> str:
    try:
        from io import BytesIO
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        if not text.strip():
            raise ValueError("PDF 无可直接提取文字")
        return text[:max_chars]
    except ImportError as exc:
        raise ResearchSourceError("RESEARCH_SOURCE_FAILED: PDF 文字提取依赖未安装") from exc
