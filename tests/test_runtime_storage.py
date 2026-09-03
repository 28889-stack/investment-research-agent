import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from app.database import create_db_engine, init_database
from app.models import AgentExecution, ResearchRun, ToolExecution
from app.run_service import RunService
from app.runtime.repository import RuntimeRepository
from app.runtime.schemas import AgentProfile


def profile(profile_id: str = "full_runtime_smoke") -> AgentProfile:
    return AgentProfile.model_validate(
        {
            "profile_id": profile_id,
            "version": "v1",
            "role": "runtime_test",
            "mode": "full",
            "system_prompt": "Validate runtime only.",
            "allowed_tools": ["runtime_echo"],
            "max_iterations": 3,
            "max_tool_calls": 3,
            "context_policy": "task_scoped",
            "output_schema": "agent_node_output",
            "model": None,
            "timeout_seconds": 120,
        }
    )


def valid_output() -> dict:
    return {
        "task_id": "runtime_full_test",
        "status": "completed",
        "summary": "Runtime 调用成功",
        "findings": [],
        "new_evidence": [],
        "new_assumptions": [],
        "risks": [],
        "conflicts": [],
        "missing_information": [],
        "suggested_followups": [],
    }


def test_runtime_repository_persists_only_controlled_execution_data(
    settings, session_factory
):
    run_service = RunService(session_factory, settings.artifacts_dir)
    run = run_service.create_run(symbol="600519", analysis_type="technical")
    repository = RuntimeRepository(session_factory)

    execution = repository.start_execution(
        run_id=run.run_id,
        node_name="run_full_agent",
        profile=profile(),
        session_id="session-1",
        attempt=1,
        input_context={"run": {"run_id": run.run_id}},
        runtime_mode="mock",
        model_provider=None,
        model_name=None,
    )
    repository.complete_execution(execution.execution_id, valid_output(), tool_call_count=1)

    saved = repository.get_execution(execution.execution_id)
    assert saved.status == "COMPLETED"
    assert json.loads(saved.validated_output_json)["summary"] == "Runtime 调用成功"
    assert "reasoning" not in saved.validated_output_json.lower()
    assert saved.profile_version == "v1"


def test_execution_idempotency_reuses_same_run_node_and_attempt(settings, session_factory):
    service = RunService(session_factory, settings.artifacts_dir)
    run = service.create_run(symbol="600519", analysis_type="technical")
    repository = RuntimeRepository(session_factory)
    arguments = dict(
        run_id=run.run_id,
        node_name="run_full_agent",
        profile=profile(),
        session_id="session-1",
        attempt=1,
        input_context={},
        runtime_mode="mock",
        model_provider=None,
        model_name=None,
    )

    first = repository.start_execution(**arguments)
    second = repository.start_execution(**arguments)

    assert first.execution_id == second.execution_id


def test_existing_stage_one_database_is_migrated_without_deleting_runs(tmp_path):
    database_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        CREATE TABLE research_runs (
            id INTEGER PRIMARY KEY,
            run_id VARCHAR(36) UNIQUE NOT NULL,
            input_symbol VARCHAR(64) NOT NULL,
            normalized_symbol VARCHAR(64),
            analysis_type VARCHAR(20) NOT NULL,
            policy_id VARCHAR(64) NOT NULL,
            as_of VARCHAR(10) NOT NULL,
            status VARCHAR(32) NOT NULL,
            current_stage VARCHAR(100) NOT NULL,
            progress INTEGER NOT NULL,
            cancel_requested BOOLEAN NOT NULL,
            error_message TEXT,
            report_path TEXT,
            created_at VARCHAR(40) NOT NULL,
            updated_at VARCHAR(40) NOT NULL,
            started_at VARCHAR(40),
            completed_at VARCHAR(40)
        )
        """
    )
    connection.execute(
        """INSERT INTO research_runs
        (run_id, input_symbol, analysis_type, policy_id, as_of, status,
         current_stage, progress, cancel_requested, created_at, updated_at)
        VALUES ('legacy-run', '600519', 'technical', 'general_research',
                '2026-08-05', 'COMPLETED', '任务完成', 100, 0, 'now', 'now')"""
    )
    connection.commit()
    connection.close()

    engine = create_db_engine(f"sqlite:///{database_path}")
    init_database(engine)

    migrated = sqlite3.connect(database_path)
    columns = {
        row[1] for row in migrated.execute("PRAGMA table_info(research_runs)").fetchall()
    }
    assert {
        "workflow_name",
        "workflow_version",
        "checkpoint_thread_id",
        "current_node",
        "runtime_mode",
    }.issubset(columns)
    assert migrated.execute(
        "SELECT input_symbol FROM research_runs WHERE run_id='legacy-run'"
    ).fetchone() == ("600519",)
    tables = {
        row[0]
        for row in migrated.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {AgentExecution.__tablename__, ToolExecution.__tablename__}.issubset(tables)
    migrated.close()


def test_new_run_has_runtime_workflow_metadata(settings, session_factory):
    service = RunService(session_factory, settings.artifacts_dir)

    run = service.create_run(symbol="600519", analysis_type="technical")

    assert run.workflow_name == "technical_v1"
    assert run.workflow_version == "v1"
    assert run.checkpoint_thread_id == run.run_id
    assert run.runtime_mode == "mock"


def test_run_service_records_configured_live_runtime_mode(settings, session_factory):
    service = RunService(
        session_factory, settings.artifacts_dir, runtime_mode="live"
    )

    run = service.create_run(symbol="AAPL", analysis_type="fundamental")

    assert run.runtime_mode == "live"


def test_concurrent_database_initialization_is_idempotent(tmp_path):
    database_path = tmp_path / "concurrent.db"
    engines = [create_db_engine(f"sqlite:///{database_path}") for _ in range(2)]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(init_database, engine) for engine in engines]
        for future in futures:
            future.result()

    connection = sqlite3.connect(database_path)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    connection.close()
    assert {"research_runs", "run_events", "agent_executions", "tool_executions"}.issubset(tables)
