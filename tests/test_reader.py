from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.fundamental.evidence import ResearchSourceError
from app.retrieval.reader import (
    _is_readable,
    _strip_reader_header,
    read_with_reader,
)


# ---------------------------------------------------------------------------
# Readability gate
# ---------------------------------------------------------------------------


def test_is_readable_accepts_real_cjk_body() -> None:
    body = "紫金矿业集团股份有限公司 2025 年年度报告摘要。" * 60
    assert _is_readable(body)


def test_is_readable_accepts_real_latin_body() -> None:
    body = "The company reported strong revenue growth across all segments. " * 80
    assert _is_readable(body)


def test_is_readable_rejects_empty() -> None:
    assert not _is_readable("")
    assert not _is_readable("   \n\n  ")


def test_is_readable_rejects_page_chrome_below_threshold() -> None:
    # A rendered NAV/FOOTER shell: real words but far too few content units.
    chrome = "首页 公告 研报 财务 登录 注册 导航 底部 版权 客服"
    assert not _is_readable(chrome)


def test_is_readable_rejects_js_challenge_marker() -> None:
    # Enough content units to pass the count gate, but starts with a JS
    # challenge marker — the reader failed to render.
    body = "function a(a){return a} enable javascript to continue. " + "real body " * 200
    assert not _is_readable(body)


def test_strip_reader_header_drops_jina_metadata_block() -> None:
    raw = (
        "Title: 紫金矿业 2025 年年度报告摘要\n"
        "URL Source: https://static.cninfo.com.cn/finalpage/2026-03-20/123.PDF\n"
        "Markdown Content:\n"
        "紫金矿业集团股份有限公司 2025 年年度报告摘要内容正文。"
    )
    assert "Markdown Content:" not in _strip_reader_header(raw)
    assert _strip_reader_header(raw).startswith("紫金矿业集团股份有限公司")


def test_strip_reader_header_passes_through_without_marker() -> None:
    raw = "紫金矿业集团股份有限公司 2025 年年度报告摘要正文。"
    assert _strip_reader_header(raw) == raw


# ---------------------------------------------------------------------------
# Jina Reader: success
# ---------------------------------------------------------------------------


def _jina_success_body() -> str:
    header = (
        "Title: 紫金矿业年度报告\n"
        "URL Source: https://example.com/report\n"
        "Markdown Content:\n"
    )
    content = "紫金矿业集团股份有限公司 2025 年年度报告摘要，净利润 200.79 亿元，每 10 股分红 4.2 元，现金红利人民币 4.20 元。"
    return header + content * 40


def test_jina_read_returns_stripped_body_on_success(settings, monkeypatch) -> None:
    live = settings.model_copy(update={"research_reader": "jina"})

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_bytes(self):
            yield _jina_success_body().encode("utf-8")

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, _method, _url, **_kwargs):
            return Response()

    monkeypatch.setattr("app.retrieval.reader.httpx.Client", Client)
    monkeypatch.setattr("app.retrieval.reader.is_safe_public_url", lambda _url: True)

    result = read_with_reader("https://example.com/report", live)
    assert result.startswith("紫金矿业集团股份有限公司")
    assert "Markdown Content:" not in result


# ---------------------------------------------------------------------------
# Jina Reader: unreadable (chrome body) — fail fast, no retry
# ---------------------------------------------------------------------------


def test_jina_read_rejects_chrome_body_as_unreadable(settings, monkeypatch) -> None:
    live = settings.model_copy(update={"research_reader": "jina"})
    calls = {"n": 0}

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_bytes(self):
            # Page chrome only — below the readability threshold.
            yield b"Home News Reports Finance Login Footer"

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            calls["n"] += 1
            return Response()

    monkeypatch.setattr("app.retrieval.reader.httpx.Client", Client)
    monkeypatch.setattr("app.retrieval.reader.is_safe_public_url", lambda _url: True)

    with pytest.raises(ResearchSourceError, match="来源不可读"):
        read_with_reader("https://example.com/shell", live)
    # Unreadable must NOT be retried.
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Jina Reader: 4xx — fail fast, no retry
# ---------------------------------------------------------------------------


def test_jina_read_4xx_is_unreadable_not_retried(settings, monkeypatch) -> None:
    live = settings.model_copy(update={"research_reader": "jina"})
    calls = {"n": 0}

    class Response:
        status_code = 404

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_bytes(self):
            yield b""

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            calls["n"] += 1
            return Response()

    monkeypatch.setattr("app.retrieval.reader.httpx.Client", Client)
    monkeypatch.setattr("app.retrieval.reader.is_safe_public_url", lambda _url: True)

    with pytest.raises(ResearchSourceError, match="来源不可读"):
        read_with_reader("https://example.com/missing", live)
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Jina Reader: transient (5xx/408/429/RequestError) — retry once
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [500, 502, 408, 429])
def test_jina_read_5xx_retries_once_then_reports_transient(
    settings, monkeypatch, status
) -> None:
    live = settings.model_copy(update={"research_reader": "jina"})
    calls = {"n": 0}

    class Response:
        def __init__(self):
            self.status_code = status

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_bytes(self):
            yield b""

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            calls["n"] += 1
            return Response()

    monkeypatch.setattr("app.retrieval.reader.httpx.Client", Client)
    monkeypatch.setattr("app.retrieval.reader.is_safe_public_url", lambda _url: True)
    monkeypatch.setattr("app.retrieval.reader.time.sleep", lambda _s: None)

    with pytest.raises(ResearchSourceError, match="瞬时网络错误"):
        read_with_reader("https://example.com/flaky", live)
    assert calls["n"] == 2


def test_jina_read_request_error_retries_once_then_reports_transient(
    settings, monkeypatch
) -> None:
    live = settings.model_copy(update={"research_reader": "jina"})
    calls = {"n": 0}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            calls["n"] += 1
            raise httpx.ConnectError("temporary")

    monkeypatch.setattr("app.retrieval.reader.httpx.Client", Client)
    monkeypatch.setattr("app.retrieval.reader.is_safe_public_url", lambda _url: True)
    monkeypatch.setattr("app.retrieval.reader.time.sleep", lambda _s: None)

    with pytest.raises(ResearchSourceError, match="瞬时网络错误"):
        read_with_reader("https://example.com/flaky", live)
    assert calls["n"] == 2


def test_jina_read_recovers_on_second_attempt_after_transient(
    settings, monkeypatch
) -> None:
    live = settings.model_copy(update={"research_reader": "jina"})
    calls = {"n": 0}

    class Response:
        def __init__(self, status_code: int):
            self.status_code = status_code

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_bytes(self):
            yield _jina_success_body().encode("utf-8")

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return Response(503)
            return Response(200)

    monkeypatch.setattr("app.retrieval.reader.httpx.Client", Client)
    monkeypatch.setattr("app.retrieval.reader.is_safe_public_url", lambda _url: True)
    monkeypatch.setattr("app.retrieval.reader.time.sleep", lambda _s: None)

    result = read_with_reader("https://example.com/recover", live)
    assert calls["n"] == 2
    assert result.startswith("紫金矿业集团股份有限公司")


# ---------------------------------------------------------------------------
# Bounded stream: oversized response is truncated (no unbounded read)
# ---------------------------------------------------------------------------


def test_jina_read_truncates_oversized_response(settings, monkeypatch) -> None:
    live = settings.model_copy(
        update={"research_reader": "jina", "research_max_source_chars": 1_000}
    )

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_bytes(self):
            yield ("紫金矿业" * 20000).encode("utf-8")  # well over 8000 bytes

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr("app.retrieval.reader.httpx.Client", Client)
    monkeypatch.setattr("app.retrieval.reader.is_safe_public_url", lambda _url: True)

    result = read_with_reader("https://example.com/huge", live)

    assert result
    assert len(result) == live.research_max_source_chars
    assert result == "紫金矿业" * 250


# ---------------------------------------------------------------------------
# Dispatch: backend selection + unsafe URL + unknown backend
# ---------------------------------------------------------------------------


def test_reader_rejects_unsafe_url_before_any_http(settings) -> None:
    live = settings.model_copy(update={"research_reader": "jina"})
    with pytest.raises(ResearchSourceError, match="禁止访问本地或内网 URL"):
        read_with_reader("http://192.168.1.1/secret", live)


def test_reader_unknown_backend_raises(settings, monkeypatch) -> None:
    live = settings.model_copy(update={"research_reader": "bogus"})
    monkeypatch.setattr("app.retrieval.reader.is_safe_public_url", lambda _url: True)
    with pytest.raises(ResearchSourceError, match="未知 reader 服务"):
        read_with_reader("https://example.com/x", live)


def test_reader_none_backend_raises_at_reader_layer(settings, monkeypatch) -> None:
    # read_with_reader is only called when research_reader != "none" (the
    # dispatch lives in evidence._read_public_text). Confirm the reader module
    # itself guards against "none" defensively if ever reached directly.
    live = settings.model_copy(update={"research_reader": "none"})
    monkeypatch.setattr("app.retrieval.reader.is_safe_public_url", lambda _url: True)
    with pytest.raises(ResearchSourceError, match="未知 reader 服务"):
        read_with_reader("https://example.com/x", live)


# ---------------------------------------------------------------------------
# Firecrawl scrape backend
# ---------------------------------------------------------------------------


def test_firecrawl_read_requires_api_key(settings, monkeypatch) -> None:
    live = settings.model_copy(
        update={
            "research_reader": "firecrawl",
            "research_reader_api_key_env_name": "",
        }
    )
    monkeypatch.setattr("app.retrieval.reader.is_safe_public_url", lambda _url: True)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    with pytest.raises(ResearchSourceError, match="API Key 未配置"):
        read_with_reader("https://example.com/report", live)


def test_firecrawl_read_returns_markdown_on_success(
    settings, monkeypatch
) -> None:
    live = settings.model_copy(
        update={
            "research_reader": "firecrawl",
            "research_reader_api_key_env_name": "FIRECRAWL_API_KEY",
        }
    )
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")
    monkeypatch.setattr("app.retrieval.reader.is_safe_public_url", lambda _url: True)

    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {"data": {"markdown": "紫金矿业 2025 年年度报告摘要正文。" * 60}}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, headers=None, json=None):
            captured["headers"] = headers
            captured["json"] = json
            return Response()

    monkeypatch.setattr("app.retrieval.reader.httpx.Client", Client)
    result = read_with_reader("https://example.com/report", live)
    assert result.startswith("紫金矿业")
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["json"]["formats"] == ["markdown"]


def test_firecrawl_read_4xx_is_unreadable(settings, monkeypatch) -> None:
    live = settings.model_copy(
        update={
            "research_reader": "firecrawl",
            "research_reader_api_key_env_name": "FIRECRAWL_API_KEY",
        }
    )
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")
    monkeypatch.setattr("app.retrieval.reader.is_safe_public_url", lambda _url: True)
    calls = {"n": 0}

    class Response:
        status_code = 403

        def json(self):
            return {}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            calls["n"] += 1
            return Response()

    monkeypatch.setattr("app.retrieval.reader.httpx.Client", Client)
    monkeypatch.setattr("app.retrieval.reader.time.sleep", lambda _s: None)
    with pytest.raises(ResearchSourceError, match="来源不可读"):
        read_with_reader("https://example.com/forbidden", live)
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Integration: evidence.read_research_source dispatches to reader
# ---------------------------------------------------------------------------


def test_read_research_source_uses_reader_when_configured(
    settings, monkeypatch, tmp_path
) -> None:
    from app.fundamental.evidence import EvidenceStore, read_research_source
    from app.fundamental.schemas import ResearchSource

    live = settings.model_copy(
        update={"research_search_mode": "live", "research_reader": "jina"}
    )
    called = {"reader": False}

    def fake_read_with_reader(url, _settings):
        called["reader"] = True
        return "紫金矿业 2025 年年度报告摘要正文内容。" * 60

    monkeypatch.setattr(
        "app.retrieval.reader.read_with_reader", fake_read_with_reader
    )

    source = ResearchSource(
        result_id="src_001",
        title="Annual report",
        url="https://example.com/report",
        source_name="example.com",
        date="2026-03-20",
        summary="summary",
    )
    item = read_research_source(
        source,
        claim="annual filing",
        evidence_type="historical_fact",
        store=EvidenceStore(tmp_path / "evidence.json"),
        settings=live,
    )
    assert called["reader"] is True
    assert item.content.startswith("紫金矿业")
