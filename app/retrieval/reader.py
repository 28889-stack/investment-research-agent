"""Reader service: unified public-source content extraction.

The local read path (``evidence._download_public_source`` + ``_extract_pdf_text``)
works for static HTML and real PDFs, but three common A-share source URL classes
defeat it:

1. JS-rendered detail shells (``data.eastmoney.com/notices/detail/...``) return
   ~6 KB of NAV/FOOTER/JS page chrome, NOT the announcement body (the body is a
   JS-linked PDF). The HTML extractor returns the chrome as a "successful" read.
2. Anti-bot research-report PDFs (``pdf.dfcfw.com/...``) return a JS challenge
   page with ``content-type: application/pdf``; ``_extract_pdf_text`` fails on
   the invalid header. Browser UA/Referer do NOT bypass it.
3. Real disclosure PDFs (``static.cninfo.com.cn/finalpage/...PDF``) extract
   fine, but only when they survive the aggregator rerank.

A reader service renders JS and normalizes all three classes to clean markdown.
This module wraps the reader HTTP call with:

- **Readability gating**: reject chrome/challenge bodies so unreadable sources
  fail honestly instead of returning junk that looks "successful".
- **Transient vs unreadable distinction**: transient failures (network error,
  408/429/5xx) are retried once; unreadable failures (4xx, chrome body) fail
  fast. The error message tells the agent which is which, so it stops burning
  the 10-call tool budget retrying sources that will never read.
- **Bounded streaming**: cap the response so a huge page cannot exhaust memory.

The reader is opt-in via ``settings.research_reader`` (``jina`` | ``firecrawl``
| ``none``). ``none`` keeps the legacy local path (and the offline/tests
contract) untouched — this module is only invoked when a reader is configured.
"""

from __future__ import annotations

import os
import re
import time
from typing import Callable

import httpx

from app.fundamental.evidence import ResearchSourceError, is_safe_public_url


# ---------------------------------------------------------------------------
# Readability gate
# ---------------------------------------------------------------------------

# Minimum "content units" (CJK chars + latin words) for a body to count as
# readable. Real disclosure/research bodies run into the thousands; a rendered
# NAV/FOOTER shell or a JS challenge page lands well under this. The threshold
# is deliberately a floor, not a tight bound — the gate is a safety net for when
# the reader itself returns chrome, not a precision classifier.
_MIN_CONTENT_UNITS = 200

# Markers of a JS challenge / anti-bot page that the reader could not render.
# Only consulted in the first ~1.5 KB so a real page that merely mentions one
# of these words further down is not penalized.
_CHALLENGE_MARKERS = ("function a(a)", "enable javascript", "access denied")

_CJK_RE = re.compile(r"[一-鿿]")
_WORD_RE = re.compile(r"[A-Za-z]{2,}")


def _is_readable(text: str) -> bool:
    """True if ``text`` looks like a real source body, not page chrome.

    A body is readable when it has enough content units (CJK characters plus
    latin words) and does not start with a JS-challenge marker. Empty bodies and
    rendered shells (NAV/FOOTER/JS) fall below the threshold.
    """
    stripped = text.strip()
    if not stripped:
        return False
    content_units = len(_CJK_RE.findall(stripped)) + len(_WORD_RE.findall(stripped))
    if content_units < _MIN_CONTENT_UNITS:
        return False
    head = stripped[:1500].lower()
    if any(marker in head for marker in _CHALLENGE_MARKERS):
        return False
    return True


def _strip_reader_header(text: str) -> str:
    """Drop the Jina Reader metadata header (Title:/URL Source:/Markdown Content:).

    Jina prepends a small metadata block before the rendered markdown. The body
    follows a ``Markdown Content:`` line. If the marker is absent (error block,
    or a non-Jina reader), return the text unchanged so the readability gate
    can decide.
    """
    marker = "Markdown Content:"
    index = text.find(marker)
    if index != -1:
        return text[index + len(marker):].strip()
    return text.strip()


# ---------------------------------------------------------------------------
# Transient signal
# ---------------------------------------------------------------------------


class _TransientReadError(Exception):
    """Internal signal: a transient reader failure the retry loop may retry."""


# ---------------------------------------------------------------------------
# Jina Reader (keyless, rate-limited; API key optional for higher limits)
# ---------------------------------------------------------------------------

_JINA_BASE = "https://r.jina.ai/"


def _read_jina_once(url: str, settings) -> str:
    endpoint = _JINA_BASE + url
    headers = {
        "X-Return-Format": "markdown",
        "User-Agent": "FinancialResearchAgent/1.0",
    }
    api_key = _resolve_reader_key(settings)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    timeout = settings.research_reader_timeout or settings.research_source_timeout
    # Markdown text ceiling in bytes. Real bodies are tens of KB; a runaway
    # page is bounded before it exhausts memory.
    byte_limit = settings.research_max_source_chars * 8
    chunks: list[bytes] = []
    total = 0
    try:
        with httpx.Client(
            timeout=timeout, follow_redirects=True, trust_env=True
        ) as client:
            with client.stream("GET", endpoint, headers=headers) as response:
                if response.status_code in {408, 429} or response.status_code >= 500:
                    raise _TransientReadError(f"jina status {response.status_code}")
                if response.status_code >= 400:
                    raise ResearchSourceError(
                        "RESEARCH_SOURCE_FAILED: 来源不可读（reader 返回 4xx），请跳过该来源不要重试"
                    )
                for chunk in response.iter_bytes():
                    remaining = byte_limit - total
                    if remaining <= 0:
                        break
                    chunks.append(chunk[:remaining])
                    total += min(len(chunk), remaining)
                    if len(chunk) > remaining:
                        break
    except httpx.RequestError as exc:
        raise _TransientReadError(str(exc)) from exc

    raw = b"".join(chunks).decode("utf-8", errors="replace")
    body = _strip_reader_header(raw)
    if not _is_readable(body):
        raise ResearchSourceError(
            "RESEARCH_SOURCE_FAILED: 来源不可读（页面无正文或被反爬），请跳过该来源不要重试"
        )
    return body[: settings.research_max_source_chars]


# ---------------------------------------------------------------------------
# Firecrawl scrape (key-gated alternative)
# ---------------------------------------------------------------------------

_FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v1/scrape"


def _read_firecrawl_once(url: str, settings) -> str:
    api_key = _resolve_reader_key(settings)
    if not api_key:
        raise ResearchSourceError(
            "RESEARCH_SOURCE_FAILED: Firecrawl reader API Key 未配置"
        )
    headers = {"Authorization": f"Bearer {api_key}"}
    body = {"url": url, "formats": ["markdown"]}
    timeout = settings.research_reader_timeout or settings.research_source_timeout
    try:
        with httpx.Client(
            timeout=timeout, follow_redirects=True, trust_env=True
        ) as client:
            response = client.post(_FIRECRAWL_SCRAPE_URL, headers=headers, json=body)
    except httpx.RequestError as exc:
        raise _TransientReadError(str(exc)) from exc
    if response.status_code in {408, 429} or response.status_code >= 500:
        raise _TransientReadError(f"firecrawl status {response.status_code}")
    if response.status_code >= 400:
        raise ResearchSourceError(
            "RESEARCH_SOURCE_FAILED: 来源不可读（reader 返回 4xx），请跳过该来源不要重试"
        )
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    markdown = str((data or {}).get("markdown") or "") if isinstance(data, dict) else ""
    if not _is_readable(markdown):
        raise ResearchSourceError(
            "RESEARCH_SOURCE_FAILED: 来源不可读（页面无正文或被反爬），请跳过该来源不要重试"
        )
    return markdown[: settings.research_max_source_chars]


# ---------------------------------------------------------------------------
# Dispatch + retry
# ---------------------------------------------------------------------------


def read_with_reader(url: str, settings) -> str:
    """Read a public source via the configured reader service.

    Returns clean markdown text. Raises ``ResearchSourceError`` whose message
    distinguishes:

    - **transient** (``瞬时网络错误``): network error or 408/429/5xx, retried
      once internally then surfaced — the agent may retry the source once.
    - **unreadable** (``来源不可读``): 4xx or a chrome/challenge body, not
      retried — the agent should skip the source and stop calling
      ``read_research_source`` on it.

    ``research_reader == "none"`` is a programming error here; the local
    fallback in ``evidence._read_public_text`` is responsible for that path and
    never calls this function.
    """
    if not is_safe_public_url(url):
        raise ResearchSourceError("RESEARCH_SOURCE_FAILED: 禁止访问本地或内网 URL")
    backend = settings.research_reader
    if backend == "jina":
        fetch: Callable[[], str] = lambda: _read_jina_once(url, settings)
    elif backend == "firecrawl":
        fetch = lambda: _read_firecrawl_once(url, settings)
    else:
        raise ResearchSourceError(f"RESEARCH_SOURCE_FAILED: 未知 reader 服务: {backend}")

    last_exc: _TransientReadError | None = None
    for attempt in range(2):
        try:
            return fetch()
        except _TransientReadError as exc:
            last_exc = exc
            if attempt == 1:
                break
            time.sleep(0.2)
    raise ResearchSourceError(
        "RESEARCH_SOURCE_FAILED: 来源读取失败（瞬时网络错误，已重试一次仍失败）"
    ) from last_exc


def _resolve_reader_key(settings) -> str:
    env_name = getattr(settings, "research_reader_api_key_env_name", "") or ""
    if not env_name:
        return ""
    return os.getenv(env_name, "")
