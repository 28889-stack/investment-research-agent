import json

import pytest

from app.run_service import RunService
from app.fundamental.evidence import EvidenceStore
from app.runtime.context_loader import ContextLoader
from app.runtime.exceptions import ContextTooLargeError
from app.runtime.profiles import ProfileLoader
from app.runtime.repository import RuntimeRepository


def completed_upstream(repository, run_id, profile):
    execution = repository.start_execution(
        run_id=run_id,
        node_name="run_full_agent",
        profile=profile,
        session_id="full-session",
        attempt=1,
        input_context={"safe": True},
        runtime_mode="mock",
        model_provider=None,
        model_name=None,
    )
    repository.complete_execution(
        execution.execution_id,
        {
            "task_id": "runtime_full_test",
            "status": "completed",
            "summary": "Full runtime validated",
            "findings": [
                {
                    "claim": "Tool path works",
                    "evidence_ids": [],
                    "assumption_ids": [],
                    "confidence": "high",
                }
            ],
            "new_evidence": [],
            "new_assumptions": [],
            "risks": [],
            "conflicts": [],
            "missing_information": [],
            "suggested_followups": [],
        },
        tool_call_count=1,
    )
    return execution.execution_id


def test_full_context_contains_only_explicit_fields(settings, session_factory):
    service = RunService(session_factory, settings.artifacts_dir)
    repository = RuntimeRepository(session_factory)
    run = service.create_run(symbol="600519", analysis_type="technical")
    profile = ProfileLoader(settings.agent_profile_dir).load("full_runtime_smoke")
    loader = ContextLoader(service, repository, max_context_chars=30_000)

    context = loader.load_for_agent(
        run.run_id, profile, "run_full_agent", [], "验证 Full Agent"
    )

    assert set(context) == {"run", "node", "task", "allowed_tools", "output_schema"}
    assert set(context["run"]) == {"run_id", "input_symbol", "analysis_type", "as_of"}
    serialized = json.dumps(context)
    assert "database_url" not in serialized.lower()
    assert "environment" not in serialized.lower()


def test_constrained_context_reads_only_validated_upstream_summary(
    settings, session_factory
):
    service = RunService(session_factory, settings.artifacts_dir)
    repository = RuntimeRepository(session_factory)
    run = service.create_run(symbol="600519", analysis_type="technical")
    profiles = ProfileLoader(settings.agent_profile_dir)
    execution_id = completed_upstream(
        repository, run.run_id, profiles.load("full_runtime_smoke")
    )
    loader = ContextLoader(service, repository, max_context_chars=30_000)

    context = loader.load_for_agent(
        run.run_id,
        profiles.load("constrained_runtime_smoke"),
        "run_constrained_agent",
        [f"execution:{execution_id}"],
        "验证 Constrained Agent",
    )

    assert set(context) == {"run", "node", "task", "upstream", "output_schema"}
    assert set(context["upstream"]) == {"summary", "findings"}
    assert "input_context_json" not in json.dumps(context)
    assert "session" not in json.dumps(context).lower()


def test_constrained_context_cannot_read_execution_from_other_run(
    settings, session_factory
):
    service = RunService(session_factory, settings.artifacts_dir)
    repository = RuntimeRepository(session_factory)
    first = service.create_run(symbol="600519", analysis_type="technical")
    second = service.create_run(symbol="AAPL", analysis_type="fundamental")
    profiles = ProfileLoader(settings.agent_profile_dir)
    execution_id = completed_upstream(
        repository, first.run_id, profiles.load("full_runtime_smoke")
    )
    loader = ContextLoader(service, repository, max_context_chars=30_000)

    with pytest.raises(ValueError, match="不属于当前任务"):
        loader.load_for_agent(
            second.run_id,
            profiles.load("constrained_runtime_smoke"),
            "run_constrained_agent",
            [f"execution:{execution_id}"],
            "test",
        )


def test_context_limit_raises_clear_error(settings, session_factory):
    service = RunService(session_factory, settings.artifacts_dir)
    repository = RuntimeRepository(session_factory)
    run = service.create_run(symbol="600519", analysis_type="technical")
    profile = ProfileLoader(settings.agent_profile_dir).load("full_runtime_smoke")
    loader = ContextLoader(service, repository, max_context_chars=20)

    with pytest.raises(ContextTooLargeError):
        loader.load_for_agent(run.run_id, profile, "run_full_agent", [], "long task")


def test_fundamental_context_uses_bounded_untrusted_evidence_excerpts(settings, session_factory):
    service = RunService(session_factory, settings.artifacts_dir)
    repository = RuntimeRepository(session_factory)
    run = service.create_run(symbol="600519", analysis_type="fundamental", as_of="2026-08-05")
    service.transition_run(
        run.run_id,
        status="BUSINESS_RESEARCHING",
        stage="公司业务研究",
        progress=32,
        message="test",
        resolved_symbol="600519.SH",
        normalized_symbol="600519.SH",
        security_name="贵州茅台",
    )
    store = EvidenceStore(settings.artifacts_dir / run.run_id / "evidence.json")
    for index in range(3):
        store.add(
            claim=f"claim-{index}",
            content=f"UNTRUSTED-{index}-" + "x" * 20_000,
            source_name="source",
            url="https://example.com/report",
            date_value="2026-03-20",
            location="",
            evidence_type="historical_fact",
        )
    loader = ContextLoader(service, repository, max_context_chars=30_000)
    profile = ProfileLoader(settings.agent_profile_dir).load("business_research")

    context = loader.load_for_agent(
        run.run_id,
        profile,
        "business_research",
        ["artifact:evidence"],
        "research",
    )

    items = context["artifacts"]["evidence"]["items"]
    assert all(item["content_is_untrusted"] is True for item in items)
    assert all(len(item["content_excerpt"]) <= 1_000 for item in items)
    assert all("content" not in item for item in items)


def test_lead_final_review_context_stays_bounded_with_many_long_evidence_items(
    settings, session_factory
):
    from app.fundamental.workflow import FundamentalWorkflow
    from app.runtime.pi_client import MockPiClient

    service = RunService(session_factory, settings.artifacts_dir)
    run = service.create_run(
        symbol="贵州茅台", analysis_type="fundamental", as_of="2026-08-05"
    )
    workflow = FundamentalWorkflow(settings, session_factory, pi_client=MockPiClient())
    try:
        workflow.run(run.run_id)
    finally:
        workflow.shutdown()

    store = EvidenceStore(settings.artifacts_dir / run.run_id / "evidence.json")
    for index in range(20):
        store.add(
            claim=f"long-claim-{index}",
            content=f"UNTRUSTED-{index}-" + "x" * 20_000,
            source_name="source",
            url="https://example.com/report",
            date_value="2026-03-20",
            location="",
            evidence_type="historical_fact",
        )

    profile = ProfileLoader(settings.agent_profile_dir).load("fundamental_lead")
    loader = ContextLoader(service, RuntimeRepository(session_factory), max_context_chars=30_000)
    context = loader.load_for_agent(
        run.run_id,
        profile,
        "lead_final_review",
        [
            "artifact:lead_plan",
            "artifact:business_research",
            "artifact:industry_research",
            "artifact:lead_review",
            "artifact:deep_research",
            "artifact:financial_data",
            "artifact:financial_metrics",
            "artifact:financial_research",
            "artifact:valuation_research",
                "artifact:retrieval_package",
            "artifact:assumptions",
        ],
        "汇总研究并判断是否可以交给 Writer",
        output_schema_name="lead_final_review_output",
    )

    assert len(json.dumps(context, ensure_ascii=False)) < 30_000
    assert "evidence" not in context["artifacts"]
    items = context["artifacts"]["retrieval_package"]["items"]
    assert all(len(item["excerpt"]) <= 240 for item in items)
