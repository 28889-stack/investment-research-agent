from pathlib import Path

from app.run_service import RunService
from app.runtime.orchestrator import RuntimeOrchestrator
from app.runtime.pi_client import MockPiClient
from app.runtime.profiles import ProfileLoader
from app.runtime.repository import RuntimeRepository


def make_orchestrator(settings, session_factory, *, scenario="tool_call", interrupt_after=None):
    return RuntimeOrchestrator(
        settings,
        session_factory,
        pi_client=MockPiClient(scenario),
        interrupt_after=interrupt_after,
    )


def test_runtime_graph_completes_agents_report_and_checkpoint(settings, session_factory):
    service = RunService(session_factory, settings.artifacts_dir)
    repository = RuntimeRepository(session_factory)
    run = service.create_run(symbol="600519", analysis_type="technical")
    service.claim_next_created_run()
    orchestrator = make_orchestrator(settings, session_factory)
    try:
        state = orchestrator.run(run.run_id)
        snapshot = orchestrator.graph.get_state(
            {"configurable": {"thread_id": run.run_id}}
        )
    finally:
        orchestrator.shutdown()

    saved = service.get_run(run.run_id)
    assert saved.status == "COMPLETED"
    assert saved.current_node == "write_runtime_report"
    assert saved.report_path and Path(saved.report_path).is_file()
    report = Path(saved.report_path).read_text(encoding="utf-8")
    assert "# Agent Runtime 验证报告" in report
    assert "本报告仅用于验证第二阶段 Agent Runtime 和流程编排能力" in report
    assert "投资建议" in report
    executions = repository.list_executions(run.run_id)
    assert [item.profile_id for item in executions] == [
        "full_runtime_smoke",
        "constrained_runtime_smoke",
    ]
    assert executions[0].tool_call_count == 1
    assert executions[1].tool_call_count == 0
    assert executions[0].session_id != executions[1].session_id
    assert state["full_execution_id"] == executions[0].execution_id
    assert settings.checkpoint_database_path.is_file()
    assert "raw_output" not in snapshot.values
    assert "messages" not in snapshot.values


def test_full_validation_fails_when_agent_skips_required_tool(settings, session_factory):
    service = RunService(session_factory, settings.artifacts_dir)
    run = service.create_run(symbol="600519", analysis_type="technical")
    service.claim_next_created_run()
    orchestrator = make_orchestrator(settings, session_factory, scenario="valid")
    try:
        orchestrator.run(run.run_id)
    finally:
        orchestrator.shutdown()

    saved = service.get_run(run.run_id)
    assert saved.status == "FAILED"
    assert saved.current_node == "mark_failed"


def test_runtime_graph_resumes_after_full_agent_without_duplicate_execution(
    settings, session_factory
):
    service = RunService(session_factory, settings.artifacts_dir)
    repository = RuntimeRepository(session_factory)
    run = service.create_run(symbol="AAPL", analysis_type="fundamental")
    service.claim_next_created_run()
    interrupted = make_orchestrator(
        settings, session_factory, interrupt_after=["run_full_agent"]
    )
    interrupted.run(run.run_id)
    interrupted.shutdown()
    first_execution = repository.list_executions(run.run_id)
    assert len(first_execution) == 1

    resumed = make_orchestrator(settings, session_factory)
    try:
        resumed.run(run.run_id)
    finally:
        resumed.shutdown()

    executions = repository.list_executions(run.run_id)
    assert len(executions) == 2
    assert executions[0].execution_id == first_execution[0].execution_id
    assert service.get_run(run.run_id).status == "COMPLETED"


def test_runtime_graph_routes_cancellation_to_terminal_node(settings, session_factory):
    service = RunService(session_factory, settings.artifacts_dir)
    run = service.create_run(symbol="600519", analysis_type="technical")
    service.claim_next_created_run()
    service.request_cancel(run.run_id)
    orchestrator = make_orchestrator(settings, session_factory)
    try:
        orchestrator.run(run.run_id)
    finally:
        orchestrator.shutdown()

    saved = service.get_run(run.run_id)
    assert saved.status == "CANCELLED"
    assert saved.current_node == "mark_cancelled"


def test_runtime_graph_marks_invalid_agent_output_failed_without_raw_output(
    settings, session_factory
):
    service = RunService(session_factory, settings.artifacts_dir)
    repository = RuntimeRepository(session_factory)
    run = service.create_run(symbol="600519", analysis_type="technical")
    service.claim_next_created_run()
    orchestrator = make_orchestrator(settings, session_factory, scenario="invalid_json")
    try:
        orchestrator.run(run.run_id)
    finally:
        orchestrator.shutdown()

    saved = service.get_run(run.run_id)
    assert saved.status == "FAILED"
    assert saved.current_node == "mark_failed"
    executions = repository.list_executions(run.run_id)
    assert executions
    assert all(item.validated_output_json is None for item in executions)
    assert "this is not json" not in (saved.error_message or "")


def test_agent_timeout_retries_once_but_permission_error_does_not(
    settings, session_factory
):
    service = RunService(session_factory, settings.artifacts_dir)
    repository = RuntimeRepository(session_factory)

    timed_out = service.create_run(symbol="AAPL", analysis_type="technical")
    service.claim_next_created_run()
    timeout_graph = make_orchestrator(settings, session_factory, scenario="timeout")
    timeout_graph.run(timed_out.run_id)
    timeout_graph.shutdown()
    assert [item.attempt for item in repository.list_executions(timed_out.run_id)] == [
        1,
        2,
    ]

    denied = service.create_run(symbol="MSFT", analysis_type="fundamental")
    service.claim_next_created_run()
    denied_graph = make_orchestrator(
        settings, session_factory, scenario="unauthorized_tool"
    )
    denied_graph.run(denied.run_id)
    denied_graph.shutdown()
    assert [item.attempt for item in repository.list_executions(denied.run_id)] == [1]


def test_recovery_marks_stale_running_execution_failed_and_uses_next_attempt(
    settings, session_factory
):
    service = RunService(session_factory, settings.artifacts_dir)
    repository = RuntimeRepository(session_factory)
    run = service.create_run(symbol="AAPL", analysis_type="technical")
    service.claim_next_created_run()
    profile = ProfileLoader(settings.agent_profile_dir).load("full_runtime_smoke")
    stale = repository.start_execution(
        run_id=run.run_id,
        node_name="run_full_agent",
        profile=profile,
        session_id="stale-session",
        attempt=1,
        input_context={},
        runtime_mode="mock",
        model_provider=None,
        model_name=None,
    )
    graph = make_orchestrator(settings, session_factory)
    try:
        graph.run(run.run_id)
    finally:
        graph.shutdown()

    executions = [
        item
        for item in repository.list_executions(run.run_id)
        if item.node_name == "run_full_agent"
    ]
    assert [(item.attempt, item.status) for item in executions] == [
        (1, "FAILED"),
        (2, "COMPLETED"),
    ]
    assert repository.get_execution(stale.execution_id).error_type == "RECOVERED_INCOMPLETE"
