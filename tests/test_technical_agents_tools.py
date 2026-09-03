from __future__ import annotations

import json

from app.run_service import RunService
from app.runtime.context_loader import ContextLoader
from app.runtime.output_validator import OutputValidator
from app.runtime.pi_adapter import PiAgentAdapter
from app.runtime.pi_client import MockPiClient
from app.runtime.profiles import ProfileLoader
from app.runtime.repository import RuntimeRepository
from app.runtime.tool_registry import ToolRegistry
from app.technical.kronos import atomic_write_kronos, predict_kronos
from app.technical.schemas import TechnicalResearchOutput
from app.tools.technical_tools import build_technical_tools


def build_technical_adapter(settings, session_factory):
    service = RunService(session_factory, settings.artifacts_dir)
    repository = RuntimeRepository(session_factory)
    registry = ToolRegistry(repository)
    build_technical_tools(registry, service, repository, settings)
    adapter = PiAgentAdapter(
        client=MockPiClient(),
        context_loader=ContextLoader(
            service,
            repository,
            max_context_chars=settings.max_agent_context_chars,
            tool_registry=registry,
        ),
        tool_registry=registry,
        repository=repository,
        output_validator=OutputValidator(settings.max_agent_output_chars),
        runtime_mode="mock",
        model_provider=None,
        model_name=None,
        repair_attempts=1,
        max_tool_calls_per_node=settings.max_tool_calls_per_node,
    )
    return service, repository, adapter


def test_technical_research_calls_three_tools_and_saves_typed_output(
    settings, session_factory
) -> None:
    service, repository, adapter = build_technical_adapter(settings, session_factory)
    run = service.create_run(symbol="贵州茅台", analysis_type="technical", as_of="2026-08-05")
    service.claim_next_created_run()
    service.transition_run(
        run.run_id,
        status="TECH_RESEARCHING",
        stage="技术指标研究",
        progress=30,
        message="resolved",
        normalized_symbol="600519.SH",
        resolved_symbol="600519.SH",
        security_name="贵州茅台",
    )
    profile = ProfileLoader(settings.agent_profile_dir).load("technical_research")
    result = adapter.run(run.run_id, "technical_research", profile, "研究技术指标", [])

    assert isinstance(result.output, TechnicalResearchOutput)
    assert result.tool_call_count == 3
    assert [item.tool_name for item in repository.list_tool_executions(result.execution_id)] == [
        "get_market_data",
        "calculate_technical_indicators",
        "get_technical_summary",
    ]
    directory = settings.artifacts_dir / run.run_id
    assert (directory / "market_data.csv").is_file()
    assert (directory / "technical_indicators.json").is_file()
    assert (directory / "technical_visuals.json").is_file()
    visuals = json.loads((directory / "technical_visuals.json").read_text())
    indicators = json.loads((directory / "technical_indicators.json").read_text())
    rendered_names = {
        annotation["label"]
        for chart in visuals["charts"]
        for annotation in chart["annotations"]
    }
    assert rendered_names == set(indicators["patterns"])


def test_technical_assembly_receives_only_validated_artifacts_and_uses_no_tools(
    settings, session_factory
) -> None:
    service, repository, adapter = build_technical_adapter(settings, session_factory)
    run = service.create_run(symbol="600519", analysis_type="technical", as_of="2026-08-05")
    service.claim_next_created_run()
    service.transition_run(
        run.run_id,
        status="TECH_RESEARCHING",
        stage="技术指标研究",
        progress=30,
        message="resolved",
        normalized_symbol="600519.SH",
        resolved_symbol="600519.SH",
        security_name="贵州茅台",
    )
    profiles = ProfileLoader(settings.agent_profile_dir)
    research = adapter.run(
        run.run_id, "technical_research", profiles.load("technical_research"), "研究", []
    )
    directory = settings.artifacts_dir / run.run_id
    (directory / "technical_research.json").write_text(
        json.dumps(research.output.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    import pandas as pd

    frame = pd.read_csv(directory / "market_data.csv", parse_dates=["date"])
    saved = service.get_run(run.run_id)
    kronos = predict_kronos(
        frame, saved.resolved_symbol, __import__("datetime").date.fromisoformat(saved.as_of), saved.data_version, settings
    )
    atomic_write_kronos(kronos, directory / "kronos_result.json")

    assembly = adapter.run(
        run.run_id,
        "technical_assembly",
        profiles.load("technical_assembly"),
        "组装",
        [
            "artifact:technical_research",
            "artifact:kronos_result",
            "artifact:technical_indicators",
        ],
    )
    assert assembly.tool_call_count == 0
    execution = repository.get_execution(assembly.execution_id)
    context = json.loads(execution.input_context_json)
    assert "technical_research" in context
    assert "kronos" in context
    assert "market_data" not in context
    assert "session" not in context
