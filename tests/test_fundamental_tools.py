from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.fundamental.evidence import (
    EvidenceStore,
    ResearchSourceError,
    _download_public_source,
    _read_public_text,
    read_research_source,
    search_research_sources,
)
from app.fundamental.schemas import ResearchSource
from app.run_service import RunService
from app.runtime.exceptions import ToolNotAllowedError
from app.runtime.profiles import ProfileLoader
from app.runtime.repository import RuntimeRepository
from app.runtime.schemas import ToolExecutionContext
from app.runtime.tool_registry import ToolRegistry
from app.tools.fundamental_tools import ReadSourceInput, build_fundamental_tools


def _setup(settings, session_factory, profile_id="fundamental_lead"):
    service = RunService(session_factory, settings.artifacts_dir)
    repository = RuntimeRepository(session_factory)
    registry = ToolRegistry(repository)
    build_fundamental_tools(registry, service, repository, settings)
    profile = ProfileLoader(settings.agent_profile_dir).load(profile_id)
    run = service.create_run(symbol="600519", analysis_type="fundamental", as_of="2026-08-05")
    service.transition_run(
        run.run_id,
        status="LEAD_PLANNING",
        stage="Lead 规划",
        progress=20,
        message="test",
        resolved_symbol="600519.SH",
        normalized_symbol="600519.SH",
        security_name="贵州茅台",
    )
    execution = repository.start_execution(
        run_id=run.run_id,
        node_name="lead_planning",
        profile=profile,
        session_id="fundamental-session",
        attempt=1,
        input_context={},
        runtime_mode="mock",
        model_provider=None,
        model_name=None,
    )
    context = ToolExecutionContext(
        run_id=run.run_id,
        agent_execution_id=execution.execution_id,
        profile_id=profile.profile_id,
        profile_mode=profile.mode,
    )
    return service, repository, registry, profile, context, run.run_id


def test_default_fundamental_configuration_is_mock(settings) -> None:
    assert settings.fundamental_data_mode == "mock"
    assert settings.research_search_mode == "mock"
    assert settings.fundamental_workflow_version == "fundamental_v1"
    assert settings.financial_metric_version == "financial_metric_v1"
    assert settings.valuation_script_version == "valuation_v1"


def test_mock_source_search_returns_fixed_results(settings) -> None:
    results = search_research_sources("贵州茅台 年报", "600519.SH", settings)

    assert len(results.items) >= 2
    assert all(item.result_id.startswith("src_") for item in results.items)
    assert all(item.url.startswith("https://mock.local/") for item in results.items)


def test_lead_tools_create_profile_and_evidence_artifacts(settings, session_factory) -> None:
    service, repository, registry, profile, context, run_id = _setup(settings, session_factory)

    company = registry.execute("get_company_profile", {}, context, profile)
    search = registry.execute("search_research_sources", {"query": "公司业务与年报"}, context, profile)
    evidence = registry.execute(
        "read_research_source",
        {
            "result_id": search["items"][0]["result_id"],
            "claim": "公司主要业务与品牌定位",
            "evidence_type": "historical_fact",
        },
        context,
        profile,
    )

    directory = settings.artifacts_dir / run_id
    assert company["symbol"] == "600519.SH"
    assert (directory / "company_profile.json").is_file()
    assert evidence["evidence_id"] == "ev_001"
    assert EvidenceStore(directory / "evidence.json").load().items[0].source_name
    assert [item.tool_name for item in repository.list_tool_executions(context.agent_execution_id)] == [
        "get_company_profile",
        "search_research_sources",
        "read_research_source",
    ]


def test_financial_profile_cannot_call_search(settings, session_factory) -> None:
    _service, _repository, registry, profile, context, _run_id = _setup(
        settings, session_factory, "financial_research"
    )

    with pytest.raises(ToolNotAllowedError):
        registry.execute("search_research_sources", {"query": "unsafe"}, context, profile)


def test_read_source_only_accepts_prior_search_result(settings, session_factory) -> None:
    _service, _repository, registry, profile, context, _run_id = _setup(settings, session_factory)

    with pytest.raises(ValueError, match="搜索结果"):
        registry.execute(
            "read_research_source",
            {"result_id": "src_missing", "claim": "x", "evidence_type": "historical_fact"},
            context,
            profile,
        )


def test_read_source_rejects_duplicate_result_id(settings, session_factory) -> None:
    """A result_id already read once must be rejected on the second read, so the
    agent cannot burn its tool budget re-reading the same source. This was a
    real failure mode against 紫金矿业 (src_002 read at both call 6 and 7)
    that exhausted the 10-call budget."""
    service, _repository, registry, profile, context, _run_id = _setup(settings, session_factory)

    search = registry.execute("search_research_sources", {"query": "公司业务与年报"}, context, profile)
    first = registry.execute(
        "read_research_source",
        {
            "result_id": search["items"][0]["result_id"],
            "claim": "第一次读取该来源",
            "evidence_type": "historical_fact",
        },
        context,
        profile,
    )
    assert first["evidence_id"] == "ev_001"

    with pytest.raises(ResearchSourceError, match="已被读取"):
        registry.execute(
            "read_research_source",
            {
                "result_id": search["items"][0]["result_id"],
                "claim": "重复读取同一来源",
                "evidence_type": "historical_fact",
            },
            context,
            profile,
        )


def test_read_source_caps_reads_per_search(settings, session_factory, monkeypatch) -> None:
    """Each search's result_id namespace is capped at _MAX_READS_PER_SEARCH reads
    so a single search cannot consume the whole node budget. Enrich the mock
    fixture with 6 sources so the cap (4) is reachable via distinct result_ids:
    reads 1-4 succeed, the 5th distinct-id read is rejected with the per-search
    message (NOT the duplicate guard)."""
    from app.fundamental import evidence as evidence_module

    rich = {
        f"src_{i:03d}": {
            "title": f"mock source {i}",
            "url": f"https://mock.local/{i}",
            "source_name": "Mock 来源",
            "date": "2026-01-01",
            "summary": "mock summary",
            "content": f"mock body {i} " * 20,
        }
        for i in range(1, 7)
    }
    monkeypatch.setattr(evidence_module, "MOCK_SOURCES", rich)

    service, _repository, registry, profile, context, _run_id = _setup(settings, session_factory)
    search = registry.execute("search_research_sources", {"query": "公司业务"}, context, profile)
    assert len(search["items"]) >= 5

    for index in range(4):
        rid = search["items"][index]["result_id"]
        result = registry.execute(
            "read_research_source",
            {"result_id": rid, "claim": f"读 {rid}", "evidence_type": "historical_fact"},
            context,
            profile,
        )
        assert result["evidence_id"] == f"ev_{index + 1:03d}"

    # 5th distinct-id read trips the per-search cap, not the duplicate guard.
    with pytest.raises(ResearchSourceError, match="本轮搜索结果读取已达上限"):
        registry.execute(
            "read_research_source",
            {
                "result_id": search["items"][4]["result_id"],
                "claim": "第 5 次读取",
                "evidence_type": "historical_fact",
            },
            context,
            profile,
        )


def test_read_guard_resets_after_new_search(settings, session_factory, monkeypatch) -> None:
    """A new search re-numbers result_ids and must reset the read guard, so a
    fresh src_001 from the second search is readable even though the first
    search's src_001 was already consumed. Without the reset, result_ids stay
    scoped to an earlier search's consumed allowance."""
    from app.fundamental import evidence as evidence_module

    monkeypatch.setattr(
        evidence_module,
        "MOCK_SOURCES",
        {
            "src_001": {
                "title": "first search src_001",
                "url": "https://mock.local/a1",
                "source_name": "Mock 来源",
                "date": "2026-01-01",
                "summary": "s",
                "content": "body a " * 20,
            }
        },
    )
    service, _repository, registry, profile, context, _run_id = _setup(settings, session_factory)

    search_one = registry.execute("search_research_sources", {"query": "第一轮"}, context, profile)
    rid_one = search_one["items"][0]["result_id"]
    registry.execute(
        "read_research_source",
        {"result_id": rid_one, "claim": "读第一轮 src_001", "evidence_type": "historical_fact"},
        context,
        profile,
    )
    with pytest.raises(ResearchSourceError, match="已被读取"):
        registry.execute(
            "read_research_source",
            {"result_id": rid_one, "claim": "再读", "evidence_type": "historical_fact"},
            context,
            profile,
        )

    # Second search re-numbers to src_001 again; guard reset makes it readable.
    search_two = registry.execute("search_research_sources", {"query": "第二轮"}, context, profile)
    rid_two = search_two["items"][0]["result_id"]
    assert rid_two == "src_001"
    result = registry.execute(
        "read_research_source",
        {"result_id": rid_two, "claim": "读第二轮 src_001", "evidence_type": "historical_fact"},
        context,
        profile,
    )
    assert result["evidence_id"] == "ev_002"


def test_evidence_claim_allows_attributed_source_language() -> None:
    parsed = ReadSourceInput(
        result_id="src_001",
        claim="研报曾给出买入评级，本文仅记录该来源观点",
        evidence_type="historical_fact",
    )
    assert "买入评级" in parsed.claim


def test_live_search_retries_one_transient_network_failure(settings, monkeypatch) -> None:
    live = settings.model_copy(
        update={
            "research_search_mode": "live",
            "research_search_provider": "tavily",
            "research_search_api_key_env_name": "TEST_TAVILY_KEY",
        }
    )
    monkeypatch.setenv("TEST_TAVILY_KEY", "secret")
    monkeypatch.setenv("RESEARCH_SEARCH_API_KEY_ENV_NAME", "TEST_TAVILY_KEY")
    calls = 0

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [
                    {
                        "title": "Annual report",
                        "url": "https://example.com/report",
                        "content": "summary",
                    }
                ]
            }

    def post(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("temporary")
        return Response()

    class Client:
        def __init__(self, **_kwargs):
            pass

        def post(self, *_args, **_kwargs):
            return post(*_args, **_kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr("app.retrieval.tavily.httpx.Client", Client)
    monkeypatch.setattr("app.retrieval.tavily.is_safe_public_url", lambda _url: True)

    result = search_research_sources("annual report", "600519.SH", live)

    assert calls == 2
    assert result.items[0].title == "Annual report"


def test_live_search_does_not_retry_authentication_failure(settings, monkeypatch) -> None:
    live = settings.model_copy(
        update={
            "research_search_mode": "live",
            "research_search_provider": "tavily",
            "research_search_api_key_env_name": "TEST_TAVILY_KEY",
        }
    )
    monkeypatch.setenv("TEST_TAVILY_KEY", "invalid")
    monkeypatch.setenv("RESEARCH_SEARCH_API_KEY_ENV_NAME", "TEST_TAVILY_KEY")
    calls = 0
    request = httpx.Request("POST", "https://api.tavily.com/search")

    class Response:
        def raise_for_status(self):
            nonlocal calls
            calls += 1
            raise httpx.HTTPStatusError(
                "unauthorized",
                request=request,
                response=httpx.Response(401, request=request),
            )

        def json(self):
            return {}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def post(self, *_args, **_kwargs):
            return Response()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr("app.retrieval.tavily.httpx.Client", Client)

    with pytest.raises(Exception, match="RESEARCH_SOURCE_FAILED"):
        search_research_sources("annual report", "600519.SH", live)

    assert calls == 1


def test_live_source_read_retries_one_transient_network_failure(settings, monkeypatch, tmp_path) -> None:
    live = settings.model_copy(update={"research_search_mode": "live"})
    calls = 0

    class Response:
        content = b"<html><body>trusted public filing</body></html>"
        text = content.decode()
        headers = {"content-type": "text/html"}
        extensions = {
            "network_stream": type(
                "Stream", (), {"get_extra_info": lambda self, _name: ("93.184.216.34", 443)}
            )()
        }

        def raise_for_status(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_bytes(self):
            yield self.content

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.ConnectError("temporary")
            return Response()

    monkeypatch.setattr("app.fundamental.evidence.httpx.Client", Client)
    monkeypatch.setattr("app.fundamental.evidence.is_safe_public_url", lambda _url: True)
    source = ResearchSource(
        result_id="src_001",
        title="Annual report",
        url="https://example.com/report",
        source_name="example.com",
        date="2026-03-20",
        summary="summary",
    )

    result = read_research_source(
        source,
        claim="public filing",
        evidence_type="historical_fact",
        store=EvidenceStore(tmp_path / "evidence.json"),
        settings=live,
    )

    assert calls == 2
    assert result.content == "trusted public filing"


def test_live_source_stream_stops_when_response_exceeds_limit(settings, monkeypatch, tmp_path) -> None:
    live = settings.model_copy(
        update={"research_search_mode": "live", "research_max_source_chars": 1_000}
    )

    class Response:
        headers = {"content-type": "text/html"}
        extensions = {
            "network_stream": type(
                "Stream", (), {"get_extra_info": lambda self, _name: ("93.184.216.34", 443)}
            )()
        }

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield b"x" * 5_000
            yield b"y" * 5_000
            raise AssertionError("超限后不应继续读取")

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr("app.fundamental.evidence.httpx.Client", Client)
    monkeypatch.setattr("app.fundamental.evidence.is_safe_public_url", lambda _url: True)
    source = ResearchSource(
        result_id="src_001", title="Oversized", url="https://example.com/report",
        source_name="example.com", date="2026-03-20", summary="summary",
    )

    with pytest.raises(Exception, match="RESEARCH_SOURCE_FAILED"):
        read_research_source(
            source,
            claim="oversized",
            evidence_type="historical_fact",
            store=EvidenceStore(tmp_path / "evidence.json"),
            settings=live,
        )

    assert not (tmp_path / "evidence.json").exists()


def test_pdf_download_uses_bounded_pdf_limit_instead_of_html_text_limit(
    settings, monkeypatch
) -> None:
    live = settings.model_copy(
        update={"research_max_source_chars": 1_000, "research_max_pdf_bytes": 10_000}
    )

    class Response:
        headers = {"content-type": "application/pdf"}
        extensions = {
            "network_stream": type(
                "Stream", (), {"get_extra_info": lambda self, _name: ("93.184.216.34", 443)}
            )()
        }

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            yield b"%PDF" + b"x" * 8_996

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr("app.fundamental.evidence.httpx.Client", Client)

    content, content_type = _download_public_source("https://example.com/report.pdf", live)

    assert len(content) == 9_000
    assert content_type == "application/pdf"


def test_unsafe_url_is_rejected_before_download(settings, monkeypatch) -> None:
    """An unsafe (local/intranet) URL is rejected by is_safe_public_url before
    any network download is attempted. Verifies the simplified blacklist guard
    on the read path."""
    downloaded = []

    monkeypatch.setattr(
        "app.fundamental.evidence._download_public_source",
        lambda url, _settings: downloaded.append(url) or (b"x", "text/html"),
    )

    with pytest.raises(Exception, match="RESEARCH_SOURCE_FAILED"):
        _read_public_text("http://192.168.1.1/secret", settings)

    assert downloaded == []
