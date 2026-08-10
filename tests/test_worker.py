from pathlib import Path
from threading import Event

from app.run_service import RunService
from app.worker import ResearchWorker
from app.runtime.orchestrator import RuntimeOrchestrator
from app.runtime.pi_client import MockPiClient
from app.runtime.repository import RuntimeRepository
from app.fundamental.workflow import FundamentalWorkflow


def create_run(service: RunService, analysis_type: str = "fundamental") -> str:
    run = service.create_run(
        symbol="600519",
        analysis_type=analysis_type,
        policy_id="general_research",
        as_of="2026-08-05",
    )
    return run.run_id


def test_worker_run_loop_honors_graceful_stop_event(settings, session_factory):
    stopped = Event()
    stopped.set()
    worker = ResearchWorker(settings, session_factory=session_factory, sleep_fn=lambda _: None)
    try:
        worker.run_forever(stop_event=stopped)
    finally:
        worker.shutdown()

    assert service_statuses(session_factory) == []


def test_worker_stop_requests_cancellation_for_active_run(settings, session_factory):
    service = RunService(session_factory, settings.artifacts_dir)
    run_id = create_run(service)
    worker = ResearchWorker(settings, session_factory=session_factory, sleep_fn=lambda _: None)
    assert worker.claim_next_run() == run_id
    worker.active_run_id = run_id

    worker.request_stop()

    assert service.get_run(run_id).cancel_requested is True


def service_statuses(session_factory):
    from sqlalchemy import select
    from app.models import ResearchRun

    with session_factory() as session:
        return list(session.scalars(select(ResearchRun.status)))


def test_worker_claims_created_run_only_once(settings, session_factory):
    service = RunService(session_factory, settings.artifacts_dir)
    run_id = create_run(service)
    worker = ResearchWorker(settings, session_factory=session_factory, sleep_fn=lambda _: None)

    claimed = worker.claim_next_run()
    second_claim = worker.claim_next_run()

    assert claimed == run_id
    assert second_claim is None
    assert service.get_run(run_id).status == "RESOLVING_SECURITY"


def test_worker_completes_run_and_generates_report(settings, session_factory, caplog):
    import logging

    caplog.set_level(logging.INFO)
    service = RunService(session_factory, settings.artifacts_dir)
    run_id = create_run(service)
    worker = ResearchWorker(settings, session_factory=session_factory, sleep_fn=lambda _: None)
    try:
        assert worker.run_once() is True
    finally:
        worker.shutdown()

    run = service.get_run(run_id)
    assert run.status == "COMPLETED"
    assert run.progress == 100
    assert run.report_path is not None
    report_path = Path(run.report_path)
    assert report_path.exists()
    report = report_path.read_text(encoding="utf-8")
    assert "# 个股基本面分析报告" in report
    assert "## 九、研究证据" in report
    assert "买入" not in report
    assert "卖出" not in report
    assert service.list_events(run_id)[-1].event_type == "RUN_COMPLETED"
    assert any(record.message == "Fundamental workflow completed" for record in caplog.records)


def test_worker_routes_technical_run_to_five_node_workflow(settings, session_factory, caplog):
    import logging

    caplog.set_level(logging.INFO)
    service = RunService(session_factory, settings.artifacts_dir)
    run_id = create_run(service, "technical")
    worker = ResearchWorker(settings, session_factory=session_factory, sleep_fn=lambda _: None)
    try:
        assert worker.run_once() is True
    finally:
        worker.shutdown()

    run = service.get_run(run_id)
    assert run.status == "COMPLETED"
    assert run.workflow_name == "technical_v1"
    assert run.report_path and Path(run.report_path).name == "technical_report.md"
    assert "# 个股技术面分析报告" in Path(run.report_path).read_text(encoding="utf-8")
    assert any(record.message == "Technical workflow completed" for record in caplog.records)


def test_worker_honors_cancel_requested_before_next_step(settings, session_factory):
    service = RunService(session_factory, settings.artifacts_dir)
    run_id = create_run(service)
    worker = ResearchWorker(settings, session_factory=session_factory, sleep_fn=lambda _: None)
    assert worker.claim_next_run() == run_id
    service.request_cancel(run_id)

    worker.process_run(run_id)

    run = service.get_run(run_id)
    assert run.status == "CANCELLED"
    assert run.progress < 100


def test_worker_marks_run_failed_on_unhandled_error(
    settings, session_factory, monkeypatch
):
    service = RunService(session_factory, settings.artifacts_dir)
    run_id = create_run(service)
    worker = ResearchWorker(settings, session_factory=session_factory, sleep_fn=lambda _: None)

    def fail_workflow(_run_id):
        raise RuntimeError("simulated report failure")

    monkeypatch.setattr(worker.fundamental_workflow, "run", fail_workflow)
    worker.run_once()

    run = service.get_run(run_id)
    assert run.status == "FAILED"
    assert run.error_message == "Worker 执行失败（RuntimeError）"
    assert service.list_events(run_id)[-1].event_type == "RUN_FAILED"


def test_worker_does_not_store_or_log_sensitive_exception_text(
    settings, session_factory, monkeypatch, caplog
):
    service = RunService(session_factory, settings.artifacts_dir)
    run_id = create_run(service)
    worker = ResearchWorker(settings, session_factory=session_factory, sleep_fn=lambda _: None)

    def fail_with_secret(_run_id):
        raise RuntimeError("API_KEY=secret-token")

    monkeypatch.setattr(worker.fundamental_workflow, "run", fail_with_secret)
    worker.run_once()

    run = service.get_run(run_id)
    assert "secret-token" not in (run.error_message or "")
    assert "secret-token" not in caplog.text


def test_cancellation_during_report_generation_wins_over_completion(
    settings, session_factory, monkeypatch
):
    service = RunService(session_factory, settings.artifacts_dir)
    run_id = create_run(service)
    worker = ResearchWorker(settings, session_factory=session_factory, sleep_fn=lambda _: None)
    from app.fundamental import workflow as fundamental_workflow_module

    generate_report = fundamental_workflow_module.generate_fundamental_report
    generated_path = None

    def generate_then_cancel(directory, **kwargs):
        nonlocal generated_path
        generated_path = generate_report(directory, **kwargs)
        service.request_cancel(run_id)
        return generated_path

    monkeypatch.setattr(fundamental_workflow_module, "generate_fundamental_report", generate_then_cancel)

    worker.run_once()

    run = service.get_run(run_id)
    assert run.status == "CANCELLED"
    assert run.cancel_requested is True
    assert run.current_node == "write_fundamental_report"
    assert run.report_path is None
    assert generated_path is not None
    assert not generated_path.exists()
    assert (settings.artifacts_dir / run_id / "fundamental_research_package.md").exists()


def test_idle_worker_returns_false(settings, session_factory):
    worker = ResearchWorker(settings, session_factory=session_factory, sleep_fn=lambda _: None)

    assert worker.run_once() is False


def test_worker_recovers_interrupted_langgraph_before_claiming_new_run(
    settings, session_factory
):
    service = RunService(session_factory, settings.artifacts_dir)
    repository = RuntimeRepository(session_factory)
    interrupted_id = create_run(service)
    new_id = create_run(service)
    assert service.claim_next_created_run() == interrupted_id
    interrupted = FundamentalWorkflow(
        settings,
        session_factory,
        pi_client=MockPiClient(),
        interrupt_after=["lead_planning"],
    )
    interrupted.run(interrupted_id)
    interrupted.shutdown()

    worker = ResearchWorker(settings, session_factory=session_factory, sleep_fn=lambda _: None)
    try:
        assert worker.run_once() is True
    finally:
        worker.shutdown()

    assert service.get_run(interrupted_id).status == "COMPLETED"
    assert service.get_run(new_id).status == "CREATED"
    full_executions = [
        item
        for item in repository.list_executions(interrupted_id)
        if item.node_name == "lead_planning"
    ]
    assert len(full_executions) == 1


def test_worker_recovers_active_cancel_request_to_cancelled(settings, session_factory):
    service = RunService(session_factory, settings.artifacts_dir)
    run_id = create_run(service)
    assert service.claim_next_created_run() == run_id
    service.request_cancel(run_id)
    worker = ResearchWorker(settings, session_factory=session_factory, sleep_fn=lambda _: None)
    try:
        assert worker.run_once() is True
    finally:
        worker.shutdown()

    run = service.get_run(run_id)
    assert run.status == "CANCELLED"
    assert run.current_node == "resolve_security"
