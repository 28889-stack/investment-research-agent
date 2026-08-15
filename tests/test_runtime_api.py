from app.run_service import RunService
from app.runtime.profiles import ProfileLoader
from app.runtime.repository import RuntimeRepository


def test_runtime_health_exposes_control_plane_only(client):
    response = client.get("/api/runtime/health")

    assert response.status_code == 200
    assert response.json() == {
        "runtime_mode": "mock",
        "bridge_status": "ready",
            "profiles_loaded": 16,
        "tools_registered": 12,
        "checkpoint_status": "ready",
    }


def test_run_detail_includes_runtime_metadata(client):
    created = client.post(
        "/api/runs", json={"symbol": "600519", "analysis_type": "technical"}
    ).json()

    detail = client.get(f"/api/runs/{created['run_id']}").json()

    assert detail["current_node"] is None
    assert detail["runtime_mode"] == "mock"
    assert detail["checkpoint_enabled"] is True


def test_execution_api_returns_safe_summary_only(client, app, settings):
    service = RunService(app.state.session_factory, settings.artifacts_dir)
    repository = RuntimeRepository(app.state.session_factory)
    run = service.create_run(symbol="600519", analysis_type="technical")
    profile = ProfileLoader(settings.agent_profile_dir).load("full_runtime_smoke")
    execution = repository.start_execution(
        run_id=run.run_id,
        node_name="run_full_agent",
        profile=profile,
        session_id="safe-session",
        attempt=1,
        input_context={"private": "must-not-be-returned"},
        runtime_mode="mock",
        model_provider=None,
        model_name=None,
    )
    repository.complete_execution(
        execution.execution_id,
        {
            "task_id": "runtime_full_test",
            "status": "completed",
            "summary": "Runtime safe summary",
            "findings": [],
            "new_evidence": [],
            "new_assumptions": [],
            "risks": [],
            "conflicts": [],
            "missing_information": [],
            "suggested_followups": [],
        },
        tool_call_count=1,
    )

    response = client.get(f"/api/runs/{run.run_id}/executions")

    assert response.status_code == 200
    payload = response.json()[0]
    assert payload["validated_summary"] == "Runtime safe summary"
    assert set(payload) == {
        "execution_id",
        "node_name",
        "profile_id",
        "profile_version",
        "status",
        "tool_call_count",
        "started_at",
        "completed_at",
        "error_type",
        "error_message",
        "validated_summary",
    }
    assert "must-not-be-returned" not in response.text
    assert "safe-session" not in response.text


def test_execution_api_returns_404_for_unknown_run(client):
    assert client.get("/api/runs/not-found/executions").status_code == 404
