from app.run_service import RunService
from app.runtime.profiles import ProfileLoader
from app.runtime.repository import RuntimeRepository
from app.ops import run_doctor


def _output():
    return {
        "task_id": "usage-test",
        "status": "completed",
        "summary": "ok",
        "findings": [],
        "new_evidence": [],
        "new_assumptions": [],
        "risks": [],
        "conflicts": [],
        "missing_information": [],
        "suggested_followups": [],
    }


def test_run_detail_aggregates_provider_usage_without_inventing_cost(client, app, settings) -> None:
    service = RunService(app.state.session_factory, settings.artifacts_dir)
    repository = RuntimeRepository(app.state.session_factory)
    run = service.create_run(symbol="600519", analysis_type="technical")
    execution = repository.start_execution(
        run_id=run.run_id,
        node_name="usage",
        profile=ProfileLoader(settings.agent_profile_dir).load("full_runtime_smoke"),
        session_id="usage-session",
        attempt=1,
        input_context={},
        runtime_mode="live",
        model_provider="openai",
        model_name="provider-model",
    )
    repository.complete_execution(
        execution.execution_id,
        _output(),
        tool_call_count=2,
        usage={
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "estimated_cost": 0.012,
            "cost_currency": "USD",
        },
    )

    detail = client.get(f"/api/runs/{run.run_id}").json()

    assert detail["usage"] == {
        "agent_calls": 1,
        "tool_calls": 2,
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "estimated_cost": 0.012,
        "cost_currency": "USD",
    }


def test_missing_provider_usage_remains_null(client) -> None:
    created = client.post(
        "/api/runs", json={"symbol": "600519", "analysis_type": "technical"}
    ).json()

    usage = client.get(f"/api/runs/{created['run_id']}").json()["usage"]

    assert usage["agent_calls"] == 0
    assert usage["tool_calls"] == 0
    assert usage["input_tokens"] is None
    assert usage["output_tokens"] is None
    assert usage["total_tokens"] is None
    assert usage["estimated_cost"] is None
    assert usage["cost_currency"] is None


def test_doctor_passes_complete_mock_configuration_without_printing_secrets(settings) -> None:
    result = run_doctor(settings, environ={"UNRELATED_SECRET": "never-print-me"})

    assert result.exit_code == 0
    names = {check.name for check in result.checks if check.status == "OK"}
    assert {
        "python version",
        "node version",
        "research database",
        "checkpoint database",
        "artifact directory",
        "profiles",
        "tool registry",
        "technical workflow",
        "fundamental workflow",
    } <= names
    assert "never-print-me" not in result.render()


def test_doctor_blocks_invalid_live_configuration(settings) -> None:
    live = settings.model_copy(update={"pi_runtime_mode": "live"})

    result = run_doctor(live, environ={})

    assert result.exit_code == 1
    assert any(check.status == "ERROR" and "PI_MODEL_PROVIDER" in check.message for check in result.checks)
