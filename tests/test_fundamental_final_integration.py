from __future__ import annotations

import json

from app.fundamental.result_manifest import ResultManifestStore
from app.fundamental.workflow import FundamentalWorkflow
from app.run_service import RunService
from app.runtime.pi_client import MockPiClient
from app.runtime.repository import RuntimeRepository
from app.worker import ResearchWorker


def _service(settings, session_factory):
    return RunService(
        session_factory,
        settings.artifacts_dir,
        settings.pi_runtime_mode,
        settings.technical_workflow_version,
        settings.fundamental_workflow_version,
    )


def _execute(settings, session_factory, scenario="valid"):
    service = _service(settings, session_factory)
    run = service.create_run(symbol="贵州茅台", analysis_type="fundamental", as_of="2026-08-05")
    workflow = FundamentalWorkflow(settings, session_factory, pi_client=MockPiClient(scenario))
    try:
        state = workflow.run(run.run_id)
    finally:
        workflow.shutdown()
    return service, run.run_id, state


class CancelDuringWriterClient(MockPiClient):
    def __init__(self, service: RunService) -> None:
        super().__init__()
        self.service = service

    def run_agent(self, **kwargs):
        raw_output = super().run_agent(**kwargs)
        context = kwargs["context"]
        if context.get("node") != "fundamental_writer":
            return raw_output
        self.service.request_cancel(context["run"]["run_id"])
        output = json.loads(raw_output)
        output["status"] = "needs_more_research"
        output["missing_information"] = ["缺少主要业务分部收入数据"]
        return json.dumps(output, ensure_ascii=False)


def test_final_fundamental_flow_generates_writer_report_and_manifest(settings, session_factory) -> None:
    service, run_id, state = _execute(settings, session_factory)
    directory = settings.artifacts_dir / run_id
    execution = next(item for item in RuntimeRepository(session_factory).list_executions(run_id) if item.node_name == "fundamental_writer")
    manifest = ResultManifestStore(directory, run_id, "fundamental_v1").load()

    assert service.get_run(run_id).status == "COMPLETED"
    assert state["current_node"] == "write_fundamental_report"
    assert (directory / "fundamental_writer.json").is_file()
    assert (directory / "fundamental_report.md").is_file()
    assert (directory / "fundamental_research_package.md").is_file()
    assert (directory / "result_manifest.json").is_file()
    assert execution.status == "COMPLETED"
    assert execution.tool_call_count == 0
    assert manifest.results["fundamental_report"].status == "current"
    assert manifest.results["fundamental_report"].version == 1


def test_writer_execution_has_independent_session_and_scoped_safe_context(settings, session_factory) -> None:
    _service_obj, run_id, _state = _execute(settings, session_factory)
    executions = RuntimeRepository(session_factory).list_executions(run_id)
    writer = next(item for item in executions if item.node_name == "fundamental_writer")
    context = json.loads(writer.input_context_json)
    artifacts = context["artifacts"]

    assert len({item.session_id for item in executions}) == len(executions)
    assert set(artifacts) == {
        "lead_synthesis", "writer_plan", "business_research", "industry_research", "deep_research",
        "financial_research", "valuation_research", "lead_final_review", "retrieval_package", "assumptions",
        "company_profile_summary", "financial_metrics_summary", "valuation_result_summary",
    }
    assert "financial_data" not in artifacts
    assert "lead_plan" not in artifacts
    assert "evidence" not in artifacts
    assert "periods" not in artifacts["financial_metrics_summary"]
    assert "market_snapshot" not in artifacts["valuation_result_summary"]
    assert "content" not in artifacts["retrieval_package"]["items"][0]
    assert len(artifacts["retrieval_package"]["items"][0]["excerpt"]) <= 240


def test_lead_not_ready_skips_writer_and_requires_human_review(settings, session_factory) -> None:
    service, run_id, state = _execute(settings, session_factory, "lead_not_ready")
    directory = settings.artifacts_dir / run_id

    assert service.get_run(run_id).status == "HUMAN_REVIEW_REQUIRED"
    assert state["error_message"] == "HUMAN_REVIEW_REQUIRED"
    assert (directory / "fundamental_research_package.md").is_file()
    assert not (directory / "fundamental_writer.json").exists()
    assert not (directory / "fundamental_report.md").exists()
    assert all(item.node_name != "fundamental_writer" for item in RuntimeRepository(session_factory).list_executions(run_id))


def test_writer_needs_more_research_is_saved_without_formal_report(settings, session_factory) -> None:
    service, run_id, _state = _execute(settings, session_factory, "writer_needs_more_research")
    directory = settings.artifacts_dir / run_id
    writer = json.loads((directory / "fundamental_writer.json").read_text(encoding="utf-8"))

    assert service.get_run(run_id).status == "HUMAN_REVIEW_REQUIRED"
    assert writer["status"] == "needs_more_research"
    assert writer["missing_information"] == ["缺少主要业务分部收入数据"]
    assert not (directory / "fundamental_report.md").exists()


def test_cancel_request_wins_when_writer_requests_human_review(settings, session_factory) -> None:
    service = _service(settings, session_factory)
    run = service.create_run(symbol="贵州茅台", analysis_type="fundamental", as_of="2026-08-05")
    workflow = FundamentalWorkflow(
        settings, session_factory, pi_client=CancelDuringWriterClient(service)
    )
    try:
        state = workflow.run(run.run_id)
    finally:
        workflow.shutdown()

    current = service.get_run(run.run_id)
    assert current.status == "CANCELLED"
    assert state["error_message"] == "CANCELLED"
    assert not (settings.artifacts_dir / run.run_id / "fundamental_report.md").exists()


def test_run_api_exposes_phase_five_result_status(settings, session_factory, client) -> None:
    _service_obj, run_id, _state = _execute(settings, session_factory)

    payload = client.get(f"/api/runs/{run_id}").json()

    assert payload["writer_status"] == "completed"
    assert payload["report_status"] == "current"
    assert payload["ready_for_writer"] is True
    assert payload["result_version"] == 1
    assert payload["stale_results"] == []


def test_stale_formal_report_is_not_served(settings, session_factory, client) -> None:
    _service_obj, run_id, _state = _execute(settings, session_factory)
    assumptions_path = settings.artifacts_dir / run_id / "assumptions.json"
    assumptions = json.loads(assumptions_path.read_text(encoding="utf-8"))
    assumptions["items"][0]["value"] = 0.09
    assumptions_path.write_text(json.dumps(assumptions, ensure_ascii=False), encoding="utf-8")

    response = client.get(f"/api/runs/{run_id}/report")
    detail = client.get(f"/api/runs/{run_id}").json()

    assert response.status_code == 409
    assert detail["report_status"] == "stale"
    assert "valuation_result" in detail["stale_results"]
    assert detail["report_ready"] is False


def test_worker_rebuilds_assumption_dependents_and_increments_versions(settings, session_factory) -> None:
    service, run_id, _state = _execute(settings, session_factory)
    directory = settings.artifacts_dir / run_id
    before = ResultManifestStore(directory, run_id, "fundamental_v1").load()
    business_version = before.results["business_research"].version
    assumptions_path = directory / "assumptions.json"
    assumptions = json.loads(assumptions_path.read_text(encoding="utf-8"))
    assumptions["items"][0]["value"] = 0.09
    assumptions_path.write_text(json.dumps(assumptions, ensure_ascii=False), encoding="utf-8")

    worker = ResearchWorker(settings, session_factory=session_factory, sleep_fn=lambda _: None)
    try:
        assert worker.run_once() is True
    finally:
        worker.shutdown()

    after = ResultManifestStore(directory, run_id, "fundamental_v1").load()
    assert service.get_run(run_id).status == "COMPLETED"
    assert after.results["business_research"].version == business_version
    assert after.results["valuation_result"].version == before.results["valuation_result"].version + 1
    assert after.results["fundamental_writer"].version == before.results["fundamental_writer"].version + 1
    assert after.results["fundamental_report"].version == before.results["fundamental_report"].version + 1
    assert all(entry.status == "current" for entry in after.results.values())


def test_completed_phase_four_package_remains_readable_and_worker_upgrades_it(settings, session_factory) -> None:
    service = _service(settings, session_factory)
    run = service.create_run(symbol="贵州茅台", analysis_type="fundamental", as_of="2026-08-05")
    first = FundamentalWorkflow(
        settings, session_factory, pi_client=MockPiClient(), interrupt_after=["lead_final_review"]
    )
    try:
        first.run(run.run_id)
    finally:
        first.shutdown()
    package = settings.artifacts_dir / run.run_id / "fundamental_research_package.md"
    service.transition_run(
        run.run_id, status="COMPLETED", stage="旧版任务完成", progress=100,
        event_type="RUN_COMPLETED", message="phase four", report_path=str(package),
    )

    legacy_run, legacy_markdown, _html = service.get_report(run.run_id)
    assert legacy_run.status == "COMPLETED"
    assert legacy_markdown.startswith("# 基本面研究工作包")

    worker = ResearchWorker(settings, session_factory=session_factory, sleep_fn=lambda _: None)
    try:
        assert worker.run_once() is True
    finally:
        worker.shutdown()

    upgraded = service.get_run(run.run_id)
    assert upgraded.status == "COMPLETED"
    assert upgraded.report_path and upgraded.report_path.endswith("fundamental_report.html")
    assert (settings.artifacts_dir / run.run_id / "result_manifest.json").is_file()


def test_completed_stale_run_rebuilds_even_when_checkpoint_file_is_missing(settings, session_factory) -> None:
    service, run_id, _state = _execute(settings, session_factory)
    directory = settings.artifacts_dir / run_id
    before = ResultManifestStore(directory, run_id, "fundamental_v1").load()
    assumptions_path = directory / "assumptions.json"
    assumptions = json.loads(assumptions_path.read_text(encoding="utf-8"))
    assumptions["items"][0]["value"] = 0.09
    assumptions_path.write_text(json.dumps(assumptions, ensure_ascii=False), encoding="utf-8")
    settings.checkpoint_database_path.unlink()

    worker = ResearchWorker(settings, session_factory=session_factory, sleep_fn=lambda _: None)
    try:
        assert worker.run_once() is True
    finally:
        worker.shutdown()

    after = ResultManifestStore(directory, run_id, "fundamental_v1").load()
    assert service.get_run(run_id).status == "COMPLETED"
    assert after.results["valuation_result"].version == before.results["valuation_result"].version + 1
    assert after.results["fundamental_report"].version == before.results["fundamental_report"].version + 1


def test_corrupt_manifest_requires_human_review_without_starving_created_run(settings, session_factory) -> None:
    service, run_id, _state = _execute(settings, session_factory)
    (settings.artifacts_dir / run_id / "result_manifest.json").write_text("broken", encoding="utf-8")
    technical = service.create_run(symbol="600519", analysis_type="technical", as_of="2026-08-05")

    worker = ResearchWorker(settings, session_factory=session_factory, sleep_fn=lambda _: None)
    try:
        assert worker.run_once() is True
    finally:
        worker.shutdown()

    assert service.get_run(run_id).status == "HUMAN_REVIEW_REQUIRED"
    assert service.get_run(technical.run_id).status == "COMPLETED"


def test_active_checkpoint_stale_inputs_rebuild_from_earliest_affected_node(settings, session_factory) -> None:
    service = _service(settings, session_factory)
    run = service.create_run(symbol="贵州茅台", analysis_type="fundamental", as_of="2026-08-05")
    first = FundamentalWorkflow(
        settings, session_factory, pi_client=MockPiClient(), interrupt_after=["fundamental_writer"]
    )
    try:
        first.run(run.run_id)
    finally:
        first.shutdown()
    directory = settings.artifacts_dir / run.run_id
    before = ResultManifestStore(directory, run.run_id, "fundamental_v1").load()
    assumptions_path = directory / "assumptions.json"
    assumptions = json.loads(assumptions_path.read_text(encoding="utf-8"))
    assumptions["items"][0]["value"] = 0.09
    assumptions_path.write_text(json.dumps(assumptions, ensure_ascii=False), encoding="utf-8")

    resumed = FundamentalWorkflow(settings, session_factory, pi_client=MockPiClient())
    try:
        resumed.run(run.run_id)
    finally:
        resumed.shutdown()

    after = ResultManifestStore(directory, run.run_id, "fundamental_v1").load()
    assert service.get_run(run.run_id).status == "COMPLETED"
    assert after.results["business_research"].version == before.results["business_research"].version
    assert after.results["valuation_result"].version == before.results["valuation_result"].version + 1
    assert after.results["fundamental_writer"].version == before.results["fundamental_writer"].version + 1


def test_missing_evidence_transitions_to_human_review_instead_of_failed(settings, session_factory) -> None:
    service, run_id, _state = _execute(settings, session_factory)
    (settings.artifacts_dir / run_id / "evidence.json").unlink()

    worker = ResearchWorker(settings, session_factory=session_factory, sleep_fn=lambda _: None)
    try:
        assert worker.run_once() is True
    finally:
        worker.shutdown()

    assert service.get_run(run_id).status == "HUMAN_REVIEW_REQUIRED"
