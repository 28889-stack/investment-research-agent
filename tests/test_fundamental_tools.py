from __future__ import annotations

from pathlib import Path
import json

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
from app.fundamental.schemas import ResearchSearchResults, ResearchSource
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


def test_lead_planning_can_query_local_findkg_without_creating_evidence(
    settings, session_factory, tmp_path
) -> None:
    dataset = tmp_path / "FinDKG"
    dataset.mkdir()
    (dataset / "entity2id.txt").write_text(
        "Copper\t0\tPRODUCT\t10\n"
        "China Demand\t1\tCONCEPT\t8\n"
        "Mining Capex\t2\tCONCEPT\t8\n"
        "USD\t3\tFIN_INSTRUMENT\t4\n",
        encoding="utf-8",
    )
    (dataset / "relation2id.txt").write_text(
        "Impact\t0\nRelate_To\t1\n",
        encoding="utf-8",
    )
    (dataset / "train.txt").write_text(
        "0\t0\t1\t0\t0\n"
        "0\t0\t1\t1\t0\n"
        "0\t1\t2\t1\t0\n"
        "3\t0\t0\t1\t0\n",
        encoding="utf-8",
    )
    configured = settings.model_copy(update={"findkg_data_dir": dataset})
    _service, _repository, registry, profile, context, run_id = _setup(
        configured, session_factory
    )

    result = registry.execute(
        "query_findkg",
        {
            "entities": ["Copper", "USD"],
            "focus": "industry",
            "max_hops": 2,
            "limit": 15,
        },
        context,
        profile,
    )

    assert result["query_entities"] == ["Copper", "USD"]
    assert any(
        "USD" in {item["source"], item["target"]}
        for item in result["relationships"][:2]
    )
    assert {item["target"] for item in result["relationships"]} >= {
        "China Demand",
        "Mining Capex",
    }
    assert "China Demand" in result["related_entities"]
    assert result["research_hints"]
    assert not (configured.artifacts_dir / run_id / "evidence.json").exists()


def test_findkg_missing_dataset_returns_empty_hints_without_failing(
    settings, session_factory, tmp_path
) -> None:
    configured = settings.model_copy(update={"findkg_data_dir": tmp_path / "missing"})
    _service, _repository, registry, profile, context, run_id = _setup(
        configured, session_factory
    )

    result = registry.execute(
        "query_findkg", {"entities": ["Gold"], "focus": "macro"}, context, profile
    )

    assert result == {
        "query_entities": ["Gold"],
        "relationships": [],
        "related_entities": [],
        "research_hints": [],
    }
    assert not (configured.artifacts_dir / run_id / "evidence.json").exists()


def test_findkg_is_rejected_outside_lead_planning(settings, session_factory) -> None:
    service, _repository, registry, profile, context, run_id = _setup(
        settings, session_factory
    )
    service.transition_run(
        run_id,
        status="LEAD_REVIEWING",
        stage="Lead 审核",
        progress=54,
        message="test",
    )

    with pytest.raises(ToolNotAllowedError, match="Lead Planning"):
        registry.execute(
            "query_findkg", {"entities": ["Gold"], "focus": "macro"}, context, profile
        )


def test_financial_profile_cannot_call_search(settings, session_factory) -> None:
    _service, _repository, registry, profile, context, _run_id = _setup(
        settings, session_factory, "financial_research"
    )

    with pytest.raises(ToolNotAllowedError):
        registry.execute("search_research_sources", {"query": "unsafe"}, context, profile)


def test_industry_search_is_not_scoped_to_the_company_symbol(settings, session_factory, monkeypatch) -> None:
    live = settings.model_copy(update={"research_search_mode": "live"})
    captured: dict[str, str] = {}

    class Provider:
        def search(self, *, query, symbol, max_results, timeout):
            captured["query"] = query
            captured["symbol"] = symbol
            return ResearchSearchResults(items=[ResearchSource(
                result_id="src_001", title="铜供需", url="https://example.com/copper",
                source_name="行业来源", date="2026-01-01", summary="供需",
            )])

    monkeypatch.setattr("app.retrieval.registry.get_search_provider", lambda _name: Provider())
    _service, _repository, registry, profile, context, _run_id = _setup(
        live, session_factory, "industry_research"
    )

    registry.execute("search_research_sources", {"query": "紫金矿业 铜金锂业绩"}, context, profile)

    assert captured["symbol"] == ""
    assert "紫金矿业" not in captured["query"]


def test_industry_search_returns_empty_results_when_industry_provider_is_unavailable(
    settings, session_factory, monkeypatch
) -> None:
    live = settings.model_copy(update={"research_search_mode": "live"})

    class Provider:
        def search(self, **_kwargs):
            raise ResearchSourceError("RESEARCH_SOURCE_FAILED: Keenable 检索失败")

    monkeypatch.setattr("app.retrieval.registry.get_search_provider", lambda _name: Provider())
    _service, _repository, registry, profile, context, _run_id = _setup(
        live, session_factory, "industry_research"
    )

    result = registry.execute("search_research_sources", {"query": "全球铜供需"}, context, profile)

    assert result["items"] == []
    assert result["retrieval_notice"] == "行业外部检索暂不可用，基于已有资料继续研究"


def test_industry_mock_search_does_not_call_live_provider(
    settings, session_factory, monkeypatch
) -> None:
    def unexpected_live_provider(_name):
        raise AssertionError("Mock Industry 不应访问 Live Provider")

    monkeypatch.setattr(
        "app.retrieval.registry.get_search_provider", unexpected_live_provider
    )
    _service, _repository, registry, profile, context, _run_id = _setup(
        settings, session_factory, "industry_research"
    )

    result = registry.execute(
        "search_research_sources", {"query": "白酒行业供需"}, context, profile
    )

    assert result["items"]


def test_deep_research_can_continue_searching_and_switch_cards_without_a_read(
    settings, session_factory
) -> None:
    _service, _repository, registry, profile, context, run_id = _setup(
        settings, session_factory, "deep_research"
    )
    review_path = settings.artifacts_dir / run_id / "lead_review.json"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps({
        "symbol": "600519.SH", "business_status": "accepted", "industry_status": "accepted",
        "key_findings": [], "conflicts": [], "financial_questions": [], "missing_information": [],
        "followup_research_tasks": [],
        "deep_research_tasks": [
            {"task_id": "deep_01", "topic": "业绩", "scope": "公告", "research_questions": ["利润"], "priority_fact_types": [], "known_material": [], "excluded_claims": []},
            {"task_id": "deep_02", "topic": "行业", "scope": "供需", "research_questions": ["铜供需"], "priority_fact_types": [], "known_material": [], "excluded_claims": []},
        ],
    }, ensure_ascii=False), encoding="utf-8")

    first = registry.execute(
        "search_research_sources",
        {"query": "业绩", "task_card_id": "deep_01"},
        context,
        profile,
    )
    second = registry.execute(
        "search_research_sources",
        {"query": "业绩补充", "task_card_id": "deep_01"},
        context,
        profile,
    )
    next_card = registry.execute(
        "search_research_sources",
        {"query": "铜供需", "task_card_id": "deep_02"},
        context,
        profile,
    )

    assert first["items"]
    assert second["items"] == []
    assert "未发现新增来源" in second["retrieval_notice"]
    assert next_card["items"]


def test_deep_failed_search_marks_topic_unresolved_and_allows_next_card(
    settings, session_factory, monkeypatch
) -> None:
    from app.tools import fundamental_tools

    _service, _repository, registry, profile, context, run_id = _setup(
        settings, session_factory, "deep_research"
    )
    review_path = settings.artifacts_dir / run_id / "lead_review.json"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps({
        "symbol": "600519.SH", "business_status": "accepted", "industry_status": "accepted",
        "key_findings": [], "conflicts": [], "financial_questions": [], "missing_information": [],
        "followup_research_tasks": [],
        "deep_research_tasks": [
            {"task_id": "deep_01", "topic": "业绩", "scope": "公告", "research_questions": ["利润"], "priority_fact_types": [], "known_material": [], "excluded_claims": []},
            {"task_id": "deep_02", "topic": "行业", "scope": "供需", "research_questions": ["铜供需"], "priority_fact_types": [], "known_material": [], "excluded_claims": []},
        ],
    }, ensure_ascii=False), encoding="utf-8")

    def search(query, *_args, **_kwargs):
        if query == "unavailable":
            raise ResearchSourceError("RESEARCH_SOURCE_FAILED: 上游来源暂不可用")
        return ResearchSearchResults(items=[ResearchSource(
            result_id="src_001", title="铜供需", url="https://example.com/copper",
            source_name="行业来源", date="2026-01-01", summary="供需",
        )])

    monkeypatch.setattr(fundamental_tools, "search_research_sources", search)

    unavailable = registry.execute(
        "search_research_sources", {"query": "unavailable", "task_card_id": "deep_01"}, context, profile
    )
    next_card = registry.execute(
        "search_research_sources", {"query": "铜供需", "task_card_id": "deep_02"}, context, profile
    )

    assert unavailable["items"] == []
    assert unavailable["retrieval_notice"] == "本专题检索暂不可用，标记为未解决并基于已有资料继续"
    assert next_card["items"][0]["title"] == "铜供需"


def test_deep_search_card_limit_returns_unresolved_notice(
    settings, session_factory, monkeypatch
) -> None:
    from app.tools import fundamental_tools

    _service, _repository, registry, profile, context, run_id = _setup(
        settings, session_factory, "deep_research"
    )
    review_path = settings.artifacts_dir / run_id / "lead_review.json"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps({
        "symbol": "600519.SH", "business_status": "accepted", "industry_status": "accepted",
        "key_findings": [], "conflicts": [], "financial_questions": [], "missing_information": [],
        "followup_research_tasks": [],
        "deep_research_tasks": [
            {"task_id": "deep_01", "topic": "业绩", "scope": "公告", "research_questions": ["利润"], "priority_fact_types": [], "known_material": [], "excluded_claims": []},
        ],
    }, ensure_ascii=False), encoding="utf-8")

    calls = 0

    def search(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return ResearchSearchResults(items=[ResearchSource(
            result_id="src_001", title="业绩公告", url=f"https://example.com/results-{calls}",
            source_name="测试来源", date="2026-01-01", summary="业绩摘要", content="可信正文",
        )])

    monkeypatch.setattr(fundamental_tools, "search_research_sources", search)

    for query in ("业绩第一轮", "业绩第二轮"):
        result = registry.execute(
            "search_research_sources", {"query": query, "task_card_id": "deep_01"}, context, profile
        )
        registry.execute(
            "read_research_source",
            {"result_id": result["items"][0]["result_id"], "claim": "业绩事实", "evidence_type": "historical_fact"},
            context,
            profile,
        )

    capped = registry.execute(
        "search_research_sources", {"query": "不应发出第三轮请求", "task_card_id": "deep_01"}, context, profile
    )

    assert capped["items"] == []
    assert "本专题已达两轮检索上限" in capped["retrieval_notice"]
    assert "前两轮结果仍可读取" in capped["retrieval_notice"]


def test_specialist_round_result_ids_remain_readable_after_limit_notice(
    settings, session_factory, monkeypatch
) -> None:
    from app.tools import fundamental_tools

    configured = settings.model_copy(update={"research_search_mode": "live"})
    calls = 0

    def search(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return ResearchSearchResults(items=[ResearchSource(
            result_id="src_001",
            title=f"第 {calls} 轮来源",
            url=f"https://example.com/round-{calls}",
            source_name=f"来源 {calls}",
            date="2026-01-01",
            summary=f"第 {calls} 轮摘要",
            content=f"第 {calls} 轮正文",
        )])

    monkeypatch.setattr(fundamental_tools, "search_research_sources", search)
    _service, _repository, registry, profile, context, _run_id = _setup(
        configured, session_factory, "business_research"
    )

    first = registry.execute(
        "search_research_sources", {"query": "第一轮"}, context, profile
    )
    second = registry.execute(
        "search_research_sources", {"query": "第二轮"}, context, profile
    )
    capped = registry.execute(
        "search_research_sources", {"query": "第三轮"}, context, profile
    )

    first_id = first["items"][0]["result_id"]
    second_id = second["items"][0]["result_id"]
    assert first_id != second_id
    assert capped["items"] == []
    first_evidence = registry.execute(
        "read_research_source",
        {"result_id": first_id, "claim": "第一轮事实", "evidence_type": "historical_fact"},
        context,
        profile,
    )
    second_evidence = registry.execute(
        "read_research_source",
        {"result_id": second_id, "claim": "第二轮事实", "evidence_type": "historical_fact"},
        context,
        profile,
    )

    assert first_evidence["evidence_id"] == "ev_001"
    assert second_evidence["evidence_id"] == "ev_002"
    assert calls == 2


def test_specialist_second_round_backfills_novel_sources_without_repeating_urls(
    settings, session_factory, monkeypatch
) -> None:
    from app.tools import fundamental_tools

    configured = settings.model_copy(
        update={"research_search_mode": "live", "research_search_max_results": 2}
    )
    requested_limits: list[int | None] = []
    calls = 0

    def search(*_args, max_results=None, **_kwargs):
        nonlocal calls
        calls += 1
        requested_limits.append(max_results)
        urls = (
            ["https://example.com/a", "https://example.com/b"]
            if calls == 1
            else [
                "https://example.com/a",
                "https://example.com/b",
                "https://example.com/c",
                "https://example.com/d",
            ]
        )
        return ResearchSearchResults(items=[ResearchSource(
            result_id=f"src_{index:03d}", title=url.rsplit("/", 1)[-1], url=url,
            source_name="测试来源", date="2026-01-01", summary="摘要", content="正文",
        ) for index, url in enumerate(urls, 1)])

    monkeypatch.setattr(fundamental_tools, "search_research_sources", search)
    _service, _repository, registry, profile, context, _run_id = _setup(
        configured, session_factory, "business_research"
    )

    first = registry.execute(
        "search_research_sources", {"query": "第一轮"}, context, profile
    )
    second = registry.execute(
        "search_research_sources", {"query": "第二轮"}, context, profile
    )

    assert [item["url"] for item in first["items"]] == [
        "https://example.com/a", "https://example.com/b"
    ]
    assert [item["url"] for item in second["items"]] == [
        "https://example.com/c", "https://example.com/d"
    ]
    assert requested_limits[1] > configured.research_search_max_results


def test_search_tool_returns_bounded_metadata_without_prefetched_body(
    settings, session_factory, monkeypatch
) -> None:
    from app.tools import fundamental_tools

    configured = settings.model_copy(update={"research_search_mode": "live"})
    monkeypatch.setattr(
        fundamental_tools,
        "search_research_sources",
        lambda *_args, **_kwargs: ResearchSearchResults(items=[ResearchSource(
            result_id="src_001", title="长摘要来源", url="https://example.com/long",
            source_name="测试来源", date="2026-01-01", summary="x" * 3_000,
            content="不得进入搜索工具响应的预取正文",
        )]),
    )
    _service, _repository, registry, profile, context, _run_id = _setup(
        configured, session_factory, "business_research"
    )

    result = registry.execute(
        "search_research_sources", {"query": "长摘要"}, context, profile
    )

    assert len(result["items"][0]["summary"]) <= 800
    assert result["items"][0]["content"] == ""


def test_existing_evidence_url_is_reused_without_re_reading_source(
    settings, session_factory, monkeypatch
) -> None:
    from app.tools import fundamental_tools

    configured = settings.model_copy(update={"research_search_mode": "live"})
    read_calls = 0

    monkeypatch.setattr(
        fundamental_tools,
        "search_research_sources",
        lambda *_args, **_kwargs: ResearchSearchResults(items=[ResearchSource(
            result_id="src_001", title="同一来源", url="https://example.com/same",
            source_name="测试来源", date="2026-01-01", summary="摘要", content="可信正文",
        )]),
    )

    def read(source, *, claim, evidence_type, store, settings):
        nonlocal read_calls
        read_calls += 1
        return store.add(
            claim=claim, content=source.content, source_name=source.source_name,
            url=source.url, date_value=source.date, location="", evidence_type=evidence_type,
        )

    monkeypatch.setattr(fundamental_tools, "read_research_source", read)
    _service, repository, registry, profile, first_context, run_id = _setup(
        configured, session_factory, "business_research"
    )
    first_search = registry.execute(
        "search_research_sources", {"query": "第一次"}, first_context, profile
    )
    first = registry.execute(
        "read_research_source",
        {"result_id": first_search["items"][0]["result_id"], "claim": "第一次", "evidence_type": "historical_fact"},
        first_context,
        profile,
    )

    second_execution = repository.start_execution(
        run_id=run_id, node_name="business_research", profile=profile,
        session_id="business-second-attempt", attempt=2, input_context={},
        runtime_mode="mock", model_provider=None, model_name=None,
    )
    second_context = ToolExecutionContext(
        run_id=run_id, agent_execution_id=second_execution.execution_id,
        profile_id=profile.profile_id, profile_mode=profile.mode,
    )
    second_search = registry.execute(
        "search_research_sources", {"query": "第二次"}, second_context, profile
    )
    second = registry.execute(
        "read_research_source",
        {"result_id": second_search["items"][0]["result_id"], "claim": "第二次", "evidence_type": "historical_fact"},
        second_context,
        profile,
    )

    assert first["evidence_id"] == second["evidence_id"] == "ev_001"
    assert read_calls == 1


def test_deep_source_result_ids_are_scoped_to_one_attempt(
    settings, session_factory
) -> None:
    _service, repository, registry, profile, first_context, run_id = _setup(
        settings, session_factory, "deep_research"
    )
    review_path = settings.artifacts_dir / run_id / "lead_review.json"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps({
        "symbol": "600519.SH", "business_status": "accepted", "industry_status": "accepted",
        "key_findings": [], "conflicts": [], "financial_questions": [], "missing_information": [],
        "followup_research_tasks": [],
        "deep_research_tasks": [
            {"task_id": "deep_01", "topic": "业绩", "scope": "公告", "research_questions": ["利润"], "priority_fact_types": [], "known_material": [], "excluded_claims": []},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    first_search = registry.execute(
        "search_research_sources",
        {"query": "第一轮业绩", "task_card_id": "deep_01"},
        first_context,
        profile,
    )

    second_execution = repository.start_execution(
        run_id=run_id,
        node_name="deep_research",
        profile=profile,
        session_id="deep-retry-session",
        attempt=2,
        input_context={},
        runtime_mode="mock",
        model_provider=None,
        model_name=None,
    )
    second_context = ToolExecutionContext(
        run_id=run_id,
        agent_execution_id=second_execution.execution_id,
        profile_id=profile.profile_id,
        profile_mode=profile.mode,
    )

    with pytest.raises(
        ResearchSourceError,
        match="仅能读取当前 attempt 最新一轮搜索结果",
    ):
        registry.execute(
            "read_research_source",
            {
                "result_id": first_search["items"][0]["result_id"],
                "claim": "不应跨 attempt 读取临时结果",
                "evidence_type": "historical_fact",
            },
            second_context,
            profile,
        )


def test_deep_tool_phase_reserves_time_for_final_synthesis(
    settings, session_factory, monkeypatch
) -> None:
    from app.tools import fundamental_tools

    clock = [100.0]
    monkeypatch.setattr(fundamental_tools, "_monotonic", lambda: clock[0], raising=False)
    _service, _repository, registry, profile, context, run_id = _setup(
        settings, session_factory, "deep_research"
    )
    review_path = settings.artifacts_dir / run_id / "lead_review.json"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps({
        "symbol": "600519.SH", "business_status": "accepted", "industry_status": "accepted",
        "key_findings": [], "conflicts": [], "financial_questions": [], "missing_information": [],
        "followup_research_tasks": [],
        "deep_research_tasks": [
            {"task_id": "deep_01", "topic": "业绩", "scope": "公告", "research_questions": ["利润"], "priority_fact_types": [], "known_material": [], "excluded_claims": []},
        ],
    }, ensure_ascii=False), encoding="utf-8")

    first = registry.execute(
        "search_research_sources",
        {"query": "第一轮业绩", "task_card_id": "deep_01"},
        context,
        profile,
    )
    assert first["items"]

    clock[0] += 181.0
    cutoff = registry.execute(
        "search_research_sources",
        {"query": "超时前不应继续检索", "task_card_id": "deep_01"},
        context,
        profile,
    )

    assert cutoff["items"] == []
    assert "已为最终专题收束预留时间" in cutoff["retrieval_notice"]


def test_deep_read_after_tool_phase_returns_completion_notice(
    settings, session_factory, monkeypatch
) -> None:
    from app.tools import fundamental_tools

    clock = [100.0]
    monkeypatch.setattr(fundamental_tools, "_monotonic", lambda: clock[0])
    _service, _repository, registry, profile, context, run_id = _setup(
        settings, session_factory, "deep_research"
    )
    review_path = settings.artifacts_dir / run_id / "lead_review.json"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps({
        "symbol": "600519.SH", "business_status": "accepted", "industry_status": "accepted",
        "key_findings": [], "conflicts": [], "financial_questions": [], "missing_information": [],
        "followup_research_tasks": [],
        "deep_research_tasks": [
            {"task_id": "deep_01", "topic": "业绩", "scope": "公告", "research_questions": ["利润"], "priority_fact_types": [], "known_material": [], "excluded_claims": []},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    search = registry.execute(
        "search_research_sources",
        {"query": "第一轮业绩", "task_card_id": "deep_01"},
        context,
        profile,
    )

    clock[0] += 181.0
    completion = registry.execute(
        "read_research_source",
        {
            "result_id": search["items"][0]["result_id"],
            "claim": "超时边界后不再读取",
            "evidence_type": "historical_fact",
        },
        context,
        profile,
    )

    assert completion["evidence_id"] is None
    assert "已为最终专题收束预留时间" in completion["retrieval_notice"]


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


def test_duplicate_source_is_not_reexposed_in_a_later_search_round(
    settings, session_factory, monkeypatch
) -> None:
    """A later round should not spend model context on the same source URL.

    The first round's unique result ID remains the canonical temporary handle,
    including its one-read guard.
    """
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

    # The same URL is filtered from the second round; the first ID is not
    # rebound to a different source namespace.
    search_two = registry.execute("search_research_sources", {"query": "第二轮"}, context, profile)
    assert search_two["items"] == []
    assert "未发现新增来源" in search_two["retrieval_notice"]


@pytest.mark.parametrize("profile_id", ["business_research", "industry_research"])
def test_specialist_tools_return_completion_notice_after_second_search(
    settings, session_factory, profile_id
) -> None:
    _service, _repository, registry, profile, context, run_id = _setup(
        settings, session_factory, profile_id
    )

    registry.execute(
        "search_research_sources", {"query": "贵州茅台半年报渠道"}, context, profile
    )
    registry.execute(
        "search_research_sources", {"query": "贵州茅台项目"}, context, profile
    )
    completion = registry.execute(
        "search_research_sources", {"query": "第三轮"}, context, profile
    )

    assert completion["items"] == []
    assert "已达两轮检索上限" in completion["retrieval_notice"]

    audit = (settings.artifacts_dir / run_id / "research_loop.json").read_text(encoding="utf-8")
    role = "business" if profile_id == "business_research" else "industry"
    assert role in audit
    if role == "industry":
        assert "贵州茅台" not in audit.split('"query":', 1)[1]


def test_evidence_store_reuses_existing_url_content_pair(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "evidence.json")
    first = store.add(
        claim="首次归纳",
        content="同一份来源正文",
        source_name="来源",
        url="https://example.com/source",
        date_value="2026-01-01",
        location="",
        evidence_type="historical_fact",
    )
    repeated = store.add(
        claim="不同角色复用同一来源",
        content="同一份来源正文",
        source_name="来源",
        url="https://example.com/source",
        date_value="2026-01-01",
        location="",
        evidence_type="historical_fact",
    )

    assert repeated.id == first.id
    assert len(store.load().items) == 1


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


def test_live_html_stream_truncates_when_response_exceeds_limit(settings, monkeypatch, tmp_path) -> None:
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

    evidence = read_research_source(
        source,
        claim="oversized",
        evidence_type="historical_fact",
        store=EvidenceStore(tmp_path / "evidence.json"),
        settings=live,
    )

    assert len(evidence.content) == live.research_max_source_chars
    assert evidence.content == "x" * live.research_max_source_chars
    assert (tmp_path / "evidence.json").is_file()


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
