from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import update

from app.models import ResearchRun
from app.run_service import RunService
from app.runtime.pi_client import MockPiClient
from app.runtime.repository import RuntimeRepository
from app.technical.workflow import TechnicalWorkflow
from app.technical.report import generate_technical_report


def make_workflow(settings, session_factory, *, interrupt_after=None):
    return TechnicalWorkflow(
        settings,
        session_factory,
        pi_client=MockPiClient(),
        interrupt_after=interrupt_after,
    )


def create_claimed_run(service: RunService, symbol: str = "贵州茅台") -> str:
    run = service.create_run(
        symbol=symbol,
        analysis_type="technical",
        as_of="2026-08-05",
    )
    assert service.claim_next_created_run() == run.run_id
    return run.run_id


def test_technical_graph_has_exactly_five_business_nodes(settings, session_factory):
    workflow = make_workflow(settings, session_factory)
    try:
        nodes = set(workflow.graph.get_graph().nodes) - {"__start__", "__end__"}
    finally:
        workflow.shutdown()
    assert nodes == {
        "resolve_security",
        "technical_research",
        "kronos",
        "technical_assembly",
        "write_report",
    }


def test_technical_workflow_generates_all_artifacts_and_authoritative_report(
    settings, session_factory
):
    service = RunService(session_factory, settings.artifacts_dir)
    repository = RuntimeRepository(session_factory)
    run_id = create_claimed_run(service)
    workflow = make_workflow(settings, session_factory)
    try:
        state = workflow.run(run_id)
    finally:
        workflow.shutdown()

    run = service.get_run(run_id)
    assert run.status == "COMPLETED"
    assert run.workflow_name == "technical_v1"
    assert run.resolved_symbol == "600519.SH"
    assert run.security_name == "贵州茅台"
    directory = settings.artifacts_dir / run_id
    expected = {
        "market_data.csv",
        "technical_indicators.json",
        "technical_research.json",
        "technical_visuals.json",
        "technical_chart.png",
        "kronos_result.json",
        "technical_assembly.json",
        "technical_report.html",
        "technical_report.md",
    }
    assert expected.issubset({path.name for path in directory.iterdir()})
    indicators = json.loads((directory / "technical_indicators.json").read_text())
    kronos = json.loads((directory / "kronos_result.json").read_text())
    report = (directory / "technical_report.html").read_text(encoding="utf-8")
    assert "图 1：" in report
    assert str(indicators["support_resistance"]["support_20"]) in report
    assert f"{kronos['direction_probability']['up'] * 100:.2f}%" in report
    assert "本报告不构成投资建议、交易指令或收益承诺。" in report
    executions = repository.list_executions(run_id)
    assert [item.profile_id for item in executions] == [
        "technical_research",
        "technical_assembly",
    ]
    assert executions[0].tool_call_count == 3
    assert executions[1].tool_call_count == 0
    assert '<canvas data-chart="' in report
    assert 'class="technical-market-image"' in report
    assert 'src="data:image/png;base64,' in report
    assert "drawTechnicalChart" in report
    assert report.index("二、趋势分析") < report.index(
        'class="technical-market-image"'
    ) < report.index("三、量价关系")
    assert report.index("三、量价关系") < report.index("七、技术形态候选")
    if indicators["patterns"]:
        assert report.index("七、技术形态候选") < report.index(
            'class="pattern-visual"'
        )
        assert 'class="signal-rules"' in report
    else:
        assert report.index("七、技术形态候选") < report.index(
            'class="visual-empty"'
        )
    assert 'aria-label="本次识别的技术形态图表"' not in report
    assert (directory / "technical_chart.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert f"/api/runs/{run_id}/artifacts/technical_chart.png" not in report
    assert state["report_path"] == str(directory / "technical_report.html")


def test_technical_workflow_resume_does_not_repeat_research(settings, session_factory):
    service = RunService(session_factory, settings.artifacts_dir)
    repository = RuntimeRepository(session_factory)
    run_id = create_claimed_run(service, "600519")
    interrupted = make_workflow(
        settings, session_factory, interrupt_after=["technical_research"]
    )
    interrupted.run(run_id)
    interrupted.shutdown()
    first = repository.list_executions(run_id)
    assert [item.profile_id for item in first] == ["technical_research"]

    resumed = make_workflow(settings, session_factory)
    try:
        resumed.run(run_id)
    finally:
        resumed.shutdown()
    executions = repository.list_executions(run_id)
    assert [item.execution_id for item in executions[:1]] == [first[0].execution_id]
    assert len([item for item in executions if item.profile_id == "technical_research"]) == 1
    assert service.get_run(run_id).status == "COMPLETED"


def test_technical_market_overview_failure_blocks_report_generation(
    settings, session_factory, monkeypatch
):
    def fail_visuals(*_args, **_kwargs):
        raise RuntimeError("canvas unavailable")

    monkeypatch.setattr(
        "app.tools.technical_tools.build_technical_visuals",
        fail_visuals,
    )
    service = RunService(session_factory, settings.artifacts_dir)
    run_id = create_claimed_run(service)
    workflow = make_workflow(settings, session_factory)
    try:
        state = workflow.run(run_id)
    finally:
        workflow.shutdown()

    directory = settings.artifacts_dir / run_id
    assert service.get_run(run_id).status == "FAILED"
    assert state["error_message"] == "TECHNICAL_AGENT_FAILED"
    assert not (directory / "technical_visuals.json").exists()
    assert not (directory / "technical_report.html").exists()


def test_recovery_rebuilds_artifacts_when_market_csv_hash_changes(
    settings, session_factory
):
    service = RunService(session_factory, settings.artifacts_dir)
    repository = RuntimeRepository(session_factory)
    run_id = create_claimed_run(service)
    interrupted = make_workflow(
        settings, session_factory, interrupt_after=["technical_research"]
    )
    interrupted.run(run_id)
    interrupted.shutdown()
    market_path = settings.artifacts_dir / run_id / "market_data.csv"
    market_path.write_text(market_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    resumed = make_workflow(settings, session_factory)
    try:
        resumed.run(run_id)
    finally:
        resumed.shutdown()

    assert service.get_run(run_id).status == "COMPLETED"
    research = [
        item
        for item in repository.list_executions(run_id)
        if item.node_name == "technical_research"
    ]
    assert [(item.attempt, item.status) for item in research] == [
        (1, "FAILED"),
        (2, "COMPLETED"),
    ]


def test_recovery_replaces_stale_existing_report(settings, session_factory):
    service = RunService(session_factory, settings.artifacts_dir)
    run_id = create_claimed_run(service)
    interrupted = make_workflow(
        settings, session_factory, interrupt_after=["technical_assembly"]
    )
    interrupted.run(run_id)
    interrupted.shutdown()
    report_path = settings.artifacts_dir / run_id / "technical_report.html"
    report_path.write_text("stale report", encoding="utf-8")

    resumed = make_workflow(settings, session_factory)
    try:
        resumed.run(run_id)
    finally:
        resumed.shutdown()
    report = report_path.read_text(encoding="utf-8")
    assert "stale report" not in report
    assert "个股技术面分析报告" in report


def test_native_report_embeds_the_generated_baseline_chart(settings, session_factory):
    service = RunService(session_factory, settings.artifacts_dir)
    run_id = create_claimed_run(service)
    workflow = make_workflow(settings, session_factory)
    try:
        workflow.run(run_id)
    finally:
        workflow.shutdown()
    directory = settings.artifacts_dir / run_id
    assert service.get_run(run_id).status == "COMPLETED"
    image = directory / "technical_chart.png"
    assert image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    report = (directory / "technical_report.html").read_text(encoding="utf-8")
    assert 'class="technical-market-image"' in report
    assert 'src="data:image/png;base64,' in report


def test_checkpoint_thread_is_always_isolated_by_run_id(settings, session_factory):
    service = RunService(session_factory, settings.artifacts_dir)
    run_id = create_claimed_run(service)
    with session_factory.begin() as session:
        session.execute(
            update(ResearchRun)
            .where(ResearchRun.run_id == run_id)
            .values(checkpoint_thread_id="untrusted-other-run")
        )
    workflow = make_workflow(settings, session_factory)
    try:
        state = workflow.run(run_id)
        trusted = workflow.graph.get_state(
            {"configurable": {"thread_id": run_id}}
        )
        untrusted = workflow.graph.get_state(
            {"configurable": {"thread_id": "untrusted-other-run"}}
        )
    finally:
        workflow.shutdown()
    assert state["run_id"] == run_id
    assert trusted.values["run_id"] == run_id
    assert not untrusted.values


def test_technical_workflow_honors_cancel_before_node(settings, session_factory):
    service = RunService(session_factory, settings.artifacts_dir)
    run_id = create_claimed_run(service)
    service.request_cancel(run_id)
    workflow = make_workflow(settings, session_factory)
    try:
        workflow.run(run_id)
    finally:
        workflow.shutdown()
    assert service.get_run(run_id).status == "CANCELLED"


def test_technical_report_api_exposes_versions_and_scoped_chart(
    settings, session_factory, client
):
    service = RunService(session_factory, settings.artifacts_dir)
    run_id = create_claimed_run(service)
    workflow = make_workflow(settings, session_factory)
    try:
        workflow.run(run_id)
    finally:
        workflow.shutdown()

    response = client.get(f"/api/runs/{run_id}/report")
    assert response.status_code == 200
    payload = response.json()
    assert payload["data_version"].startswith("600519.SH_20260805_")
    assert payload["indicator_version"] == "tech_indicator_v1"
    assert payload["kronos_model_version"] == "mock_kronos_v1"
    assert "<canvas" in payload["html"]
    assert 'src="data:image/png;base64,' in payload["html"]
    assert payload["chart_url"] == f"/api/runs/{run_id}/artifacts/technical_chart.png"
    assert client.get("/api/runs/not-found/artifacts/technical_chart.png").status_code == 404


def test_report_omits_agent_invented_precise_numbers(settings, session_factory):
    service = RunService(session_factory, settings.artifacts_dir)
    run_id = create_claimed_run(service)
    workflow = make_workflow(settings, session_factory)
    try:
        workflow.run(run_id)
    finally:
        workflow.shutdown()
    directory = settings.artifacts_dir / run_id
    research_path = directory / "technical_research.json"
    research = json.loads(research_path.read_text(encoding="utf-8"))
    research["trend"] = "Agent 声称 DIF 当前为 1，支撑位为人民币123"
    research_path.write_text(json.dumps(research, ensure_ascii=False), encoding="utf-8")
    report_path = generate_technical_report(service.get_run(run_id), directory)
    report = report_path.read_text(encoding="utf-8")
    assert "人民币123" not in report
    assert "DIF 当前为 1" not in report
    assert "Agent 原叙述含精确数值，已省略" in report
