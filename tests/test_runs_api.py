from datetime import date


def create_run(client, analysis_type: str = "technical") -> dict:
    response = client.post(
        "/api/runs",
        json={
            "symbol": " 600519 ",
            "analysis_type": analysis_type,
            "policy_id": "general_research",
            "as_of": "2026-08-05",
        },
    )
    assert response.status_code == 201
    return response.json()


def test_health_check(client):
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_technical_run(client):
    body = create_run(client, "technical")

    assert body["status"] == "CREATED"
    assert body["run_id"]


def test_create_fundamental_run_uses_current_date_when_as_of_is_missing(client):
    response = client.post(
        "/api/runs",
        json={"symbol": "AAPL", "analysis_type": "fundamental"},
    )

    assert response.status_code == 201
    run = client.get(f"/api/runs/{response.json()['run_id']}").json()
    assert run["analysis_type"] == "fundamental"
    assert run["policy_id"] == "general_research"
    assert run["as_of"] == date.today().isoformat()


def test_create_run_rejects_blank_symbol(client):
    response = client.post(
        "/api/runs",
        json={"symbol": "  ", "analysis_type": "technical"},
    )

    assert response.status_code == 422


def test_create_run_rejects_invalid_analysis_type(client):
    response = client.post(
        "/api/runs",
        json={"symbol": "600519", "analysis_type": "combined"},
    )

    assert response.status_code == 422


def test_get_existing_run_includes_events(client):
    created = create_run(client)

    response = client.get(f"/api/runs/{created['run_id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["input_symbol"] == "600519"
    assert body["normalized_symbol"] == "600519"
    assert body["progress"] == 0
    assert body["report_ready"] is False
    assert [event["event_type"] for event in body["events"]] == ["RUN_CREATED"]


def test_get_missing_run_returns_404(client):
    response = client.get("/api/runs/missing")

    assert response.status_code == 404
    assert response.json()["detail"] == "研究任务不存在"


def test_list_runs_returns_newest_first(client):
    first = create_run(client, "technical")
    second = create_run(client, "fundamental")

    response = client.get("/api/runs")

    assert response.status_code == 200
    assert [item["run_id"] for item in response.json()[:2]] == [
        second["run_id"],
        first["run_id"],
    ]


def test_cancel_created_run(client):
    created = create_run(client)

    response = client.post(f"/api/runs/{created['run_id']}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    run = client.get(f"/api/runs/{created['run_id']}").json()
    assert run["cancel_requested"] is True
    assert run["events"][-1]["event_type"] == "RUN_CANCELLED"


def test_completed_run_cannot_be_cancelled(client, app):
    created = create_run(client)
    app.state.run_service.transition_run(
        created["run_id"],
        status="COMPLETED",
        stage="任务完成",
        progress=100,
        event_type="RUN_COMPLETED",
        message="任务已完成",
    )

    response = client.post(f"/api/runs/{created['run_id']}/cancel")

    assert response.status_code == 409


def test_unfinished_report_returns_409(client):
    created = create_run(client)

    response = client.get(f"/api/runs/{created['run_id']}/report")

    assert response.status_code == 409
    assert response.json()["detail"] == "研究报告尚未生成"
