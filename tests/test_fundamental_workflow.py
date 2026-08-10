from __future__ import annotations

import json
from pathlib import Path

from app.fundamental.workflow import FundamentalWorkflow
from app.run_service import RunService
from app.runtime.pi_client import MockPiClient
from app.runtime.repository import RuntimeRepository


NODES = [
    "resolve_security",
    "lead_planning",
    "business_research",
    "industry_research",
    "lead_review",
    "deep_research",
    "assemble_retrieval_package",
    "financial_research",
    "valuation_research",
    "lead_final_review",
    "lead_synthesis",
    "writer_planning",
    "fundamental_writer",
    "write_fundamental_report",
]

ARTIFACTS = {
    "company_profile.json",
    "financial_data.json",
    "financial_metrics.json",
    "evidence.json",
    "assumptions.json",
    "lead_plan.json",
    "business_research.json",
    "industry_research.json",
    "lead_review.json",
    "deep_research.json",
    "financial_research.json",
    "valuation_result.json",
    "valuation_research.json",
    "lead_final_review.json",
    "retrieval_package.json",
    "lead_synthesis.json",
    "writer_plan.json",
    "fundamental_research_package.md",
    "fundamental_writer.json",
    "fundamental_report.md",
    "report_visuals.json",
    "fundamental_report.html",
    "result_manifest.json",
}


def _service(settings, session_factory):
    return RunService(
        session_factory,
        settings.artifacts_dir,
        settings.pi_runtime_mode,
        settings.technical_workflow_version,
        settings.fundamental_workflow_version,
    )


def _run(settings, session_factory):
    service = _service(settings, session_factory)
    run = service.create_run(symbol="贵州茅台", analysis_type="fundamental", as_of="2026-08-05")
    return service, run.run_id


def _workflow(settings, session_factory, interrupt_after=None):
    return FundamentalWorkflow(
        settings,
        session_factory,
        pi_client=MockPiClient(),
        interrupt_after=interrupt_after,
    )


class InvalidLeadEvidenceClient(MockPiClient):
    def run_agent(self, **kwargs):
        raw = super().run_agent(**kwargs)
        session = self.sessions[kwargs["session_id"]]
        if session["profile"]["profile_id"] == "fundamental_lead" and kwargs["context"].get("node") == "lead_planning":
            payload = json.loads(raw)
            payload["evidence_ids"] = ["ev_999"]
            return json.dumps(payload, ensure_ascii=False)
        return raw


def test_fundamental_graph_has_extended_research_and_writer_planning_nodes(settings, session_factory) -> None:
    workflow = _workflow(settings, session_factory)
    try:
        nodes = set(workflow.graph.get_graph().nodes) - {"__start__", "__end__"}
    finally:
        workflow.shutdown()
    assert nodes == set(NODES)


def test_fundamental_graph_adds_retrieval_synthesis_and_writer_planning_nodes(
    settings, session_factory
) -> None:
    workflow = _workflow(settings, session_factory)
    try:
        nodes = set(workflow.graph.get_graph().nodes) - {"__start__", "__end__"}
    finally:
        workflow.shutdown()

    assert {
        "assemble_retrieval_package",
        "lead_synthesis",
        "writer_planning",
    } <= nodes


def test_fundamental_mock_workflow_generates_complete_research_package(settings, session_factory) -> None:
    service, run_id = _run(settings, session_factory)
    workflow = _workflow(settings, session_factory)
    try:
        state = workflow.run(run_id)
    finally:
        workflow.shutdown()

    run = service.get_run(run_id)
    directory = settings.artifacts_dir / run_id
    assert run.status == "COMPLETED"
    assert run.workflow_name == "fundamental_v1"
    assert run.resolved_symbol == "600519.SH"
    assert ARTIFACTS == {item.name for item in directory.iterdir() if not item.name.startswith(".")}
    assert state["report_path"] == str(directory / "fundamental_report.html")
    package = (directory / "fundamental_research_package.md").read_text(encoding="utf-8")
    report = (directory / "fundamental_report.md").read_text(encoding="utf-8")
    metrics = json.loads((directory / "financial_metrics.json").read_text(encoding="utf-8"))
    valuation = json.loads((directory / "valuation_result.json").read_text(encoding="utf-8"))
    assert "本文档是第四阶段生成的基本面研究工作包" in package
    assert "# 个股基本面分析报告" in report
    assert str(metrics["cash_flow"][metrics["periods"][-1]]["free_cash_flow"]) in report
    assert str(valuation["relative"]["pe"]["value"]) in report
    assert "正式报告将在 Fundamental Writer 阶段生成" in package
    assert "本工作包不构成投资建议、交易指令或收益承诺。" in package


def test_completed_fundamental_run_uses_html_report_as_the_final_artifact(
    settings, session_factory
) -> None:
    service, run_id = _run(settings, session_factory)
    workflow = _workflow(settings, session_factory)
    try:
        state = workflow.run(run_id)
    finally:
        workflow.shutdown()

    run = service.get_run(run_id)
    assert state["report_path"].endswith("fundamental_report.html")
    assert run.report_path and run.report_path.endswith("fundamental_report.html")
    assert (settings.artifacts_dir / run_id / "fundamental_report.html").is_file()


def test_agent_execution_permissions_and_assumption_handoff(settings, session_factory) -> None:
    _service_obj, run_id = _run(settings, session_factory)
    workflow = _workflow(settings, session_factory)
    try:
        workflow.run(run_id)
    finally:
        workflow.shutdown()
    executions = RuntimeRepository(session_factory).list_executions(run_id)
    completed = {item.node_name: item for item in executions if item.status == "COMPLETED"}

    assert set(completed) == {
        "lead_planning", "business_research", "industry_research", "lead_review",
        "deep_research",
        "lead_synthesis", "writer_planning",
        "financial_research", "valuation_research", "lead_final_review", "fundamental_writer",
    }
    assert completed["lead_planning"].tool_call_count == 3
    assert completed["business_research"].tool_call_count == 3
    assert completed["industry_research"].tool_call_count == 2
    assert completed["lead_review"].tool_call_count == 0
    assert completed["deep_research"].tool_call_count == 2
    assert completed["financial_research"].tool_call_count == 0
    assert completed["valuation_research"].tool_call_count == 0
    assert completed["lead_final_review"].tool_call_count == 0
    assert completed["lead_synthesis"].tool_call_count == 0
    assert completed["writer_planning"].tool_call_count == 0
    assert completed["fundamental_writer"].tool_call_count == 0
    directory = settings.artifacts_dir / run_id
    assumptions = json.loads((directory / "assumptions.json").read_text())["items"]
    valuation = json.loads((directory / "valuation_research.json").read_text())
    assert [item["id"] for item in assumptions] == ["asm_001", "asm_002", "asm_003"]
    assert valuation["assumption_ids"] == ["asm_001", "asm_002", "asm_003"]


def test_lead_review_tasks_are_passed_to_deep_research(settings, session_factory) -> None:
    _service_obj, run_id = _run(settings, session_factory)
    workflow = _workflow(settings, session_factory)
    try:
        workflow.run(run_id)
    finally:
        workflow.shutdown()

    execution = next(
        item for item in RuntimeRepository(session_factory).list_executions(run_id)
        if item.node_name == "deep_research"
    )
    context = json.loads(execution.input_context_json)
    assert context["node"] == "deep_research"
    assert "lead_review" in context["artifacts"]
    assert context["artifacts"]["lead_review"]["financial_questions"]
    assert "补充" in context["task"]


def test_fundamental_report_api_exposes_lightweight_package_metadata(settings, session_factory, client) -> None:
    _service_obj, run_id = _run(settings, session_factory)
    workflow = _workflow(settings, session_factory)
    try:
        workflow.run(run_id)
    finally:
        workflow.shutdown()

    response = client.get(f"/api/runs/{run_id}/report")
    assert response.status_code == 200
    payload = response.json()
    assert payload["evidence_count"] == 4
    assert payload["assumption_count"] == 3
    assert payload["ready_for_writer"] is True
    assert payload["writer_status"] == "completed"
    assert payload["report_status"] == "current"
    assert payload["result_version"] == 1
    assert payload["missing_information"] == []


def test_resume_does_not_repeat_completed_agent(settings, session_factory) -> None:
    _service_obj, run_id = _run(settings, session_factory)
    first = _workflow(settings, session_factory, interrupt_after=["business_research"])
    try:
        first.run(run_id)
    finally:
        first.shutdown()
    repository = RuntimeRepository(session_factory)
    assert len(repository.list_executions(run_id)) == 2

    resumed = _workflow(settings, session_factory)
    try:
        resumed.run(run_id)
    finally:
        resumed.shutdown()
    executions = repository.list_executions(run_id)
    assert len([item for item in executions if item.node_name == "lead_planning"]) == 1
    assert len([item for item in executions if item.node_name == "business_research"]) == 1


def test_corrupt_artifact_rebuilds_from_corresponding_node(settings, session_factory) -> None:
    _service_obj, run_id = _run(settings, session_factory)
    first = _workflow(settings, session_factory, interrupt_after=["industry_research"])
    try:
        first.run(run_id)
    finally:
        first.shutdown()
    path = settings.artifacts_dir / run_id / "business_research.json"
    path.write_text("broken", encoding="utf-8")

    resumed = _workflow(settings, session_factory)
    try:
        resumed.run(run_id)
    finally:
        resumed.shutdown()

    assert json.loads(path.read_text())["symbol"] == "600519.SH"
    records = [
        item for item in RuntimeRepository(session_factory).list_executions(run_id)
        if item.node_name == "business_research"
    ]
    assert [(item.attempt, item.status) for item in records] == [(1, "FAILED"), (2, "COMPLETED")]


def test_valid_but_tampered_financial_metrics_are_recomputed_on_resume(settings, session_factory) -> None:
    _service_obj, run_id = _run(settings, session_factory)
    first = _workflow(settings, session_factory, interrupt_after=["financial_research"])
    try:
        first.run(run_id)
    finally:
        first.shutdown()
    path = settings.artifacts_dir / run_id / "financial_metrics.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    latest = payload["periods"][-1]
    expected = payload["cash_flow"][latest]["free_cash_flow"]
    payload["cash_flow"][latest]["free_cash_flow"] = expected + 123.0
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    resumed = _workflow(settings, session_factory)
    try:
        resumed.run(run_id)
    finally:
        resumed.shutdown()

    repaired = json.loads(path.read_text(encoding="utf-8"))
    assert repaired["cash_flow"][latest]["free_cash_flow"] == expected
    records = [
        item for item in RuntimeRepository(session_factory).list_executions(run_id)
        if item.node_name == "financial_research"
    ]
    assert [(item.attempt, item.status) for item in records] == [(1, "FAILED"), (2, "COMPLETED")]


def test_valid_but_tampered_valuation_result_is_recomputed_on_resume(settings, session_factory) -> None:
    _service_obj, run_id = _run(settings, session_factory)
    first = _workflow(settings, session_factory, interrupt_after=["valuation_research"])
    try:
        first.run(run_id)
    finally:
        first.shutdown()
    path = settings.artifacts_dir / run_id / "valuation_result.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected = payload["relative"]["pe"]["value"]
    payload["relative"]["pe"]["value"] = expected + 1.0
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    resumed = _workflow(settings, session_factory)
    try:
        resumed.run(run_id)
    finally:
        resumed.shutdown()

    repaired = json.loads(path.read_text(encoding="utf-8"))
    assert repaired["relative"]["pe"]["value"] == expected
    records = [
        item for item in RuntimeRepository(session_factory).list_executions(run_id)
        if item.node_name == "valuation_research"
    ]
    assert [(item.attempt, item.status) for item in records] == [(1, "FAILED"), (2, "COMPLETED")]


def test_cancelled_fundamental_run_stops_before_first_node(settings, session_factory) -> None:
    service, run_id = _run(settings, session_factory)
    service.transition_run(run_id, status="RESOLVING_SECURITY", stage="解析证券", progress=1, message="test")
    service.request_cancel(run_id)
    workflow = _workflow(settings, session_factory)
    try:
        state = workflow.run(run_id)
    finally:
        workflow.shutdown()

    assert service.get_run(run_id).status == "CANCELLED"
    assert state["error_message"] == "CANCELLED"


def test_semantically_invalid_agent_output_is_not_kept_completed(settings, session_factory) -> None:
    service, run_id = _run(settings, session_factory)
    workflow = FundamentalWorkflow(settings, session_factory, pi_client=InvalidLeadEvidenceClient())
    try:
        state = workflow.run(run_id)
    finally:
        workflow.shutdown()

    execution = RuntimeRepository(session_factory).list_executions(run_id)[0]
    assert service.get_run(run_id).status == "FAILED"
    assert state["error_message"] == "LEAD_AGENT_FAILED"
    assert execution.status == "FAILED"
    assert execution.error_type == "SEMANTIC_VALIDATION_FAILED"
