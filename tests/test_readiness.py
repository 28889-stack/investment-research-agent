def test_readiness_returns_local_component_status(client, settings) -> None:
    import json

    response = client.get("/api/readiness")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["environment"] == "development"
    assert payload["database"] == "ready"
    assert payload["checkpoint"] == "ready"
    assert payload["artifacts"] == "ready"
    assert payload["bridge"] == "ready"
    assert payload["profiles"] == "ready"
    assert payload["tools"] == "ready"
    assert payload["technical_workflow"] == "ready"
    assert payload["fundamental_workflow"] == "ready"
    assert payload["live_configuration"]["pi"] == "not_configured"
    for handler in __import__("logging").getLogger().handlers:
        handler.flush()
    records = [
        json.loads(line)
        for line in (settings.logs_dir / "web.log").read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        item["component"] == "web" and item["message"] == "HTTP request completed"
        for item in records
    )


def test_queue_limit_returns_429(client, app) -> None:
    app.state.settings = app.state.settings.model_copy(update={"max_pending_runs": 1})
    app.state.run_service.max_pending_runs = 1
    first = client.post(
        "/api/runs", json={"symbol": "600519", "analysis_type": "technical"}
    )
    second = client.post(
        "/api/runs", json={"symbol": "000001", "analysis_type": "technical"}
    )

    assert first.status_code == 201
    assert second.status_code == 429
    assert second.json()["detail"] == "等待队列已满，请稍后重试"


def test_readiness_returns_503_when_bridge_is_missing(settings) -> None:
    from fastapi.testclient import TestClient
    from app.main import create_app

    unavailable = settings.model_copy(
        update={"pi_bridge_entry": settings.artifacts_dir / "missing-bridge.js"}
    )
    app = create_app(unavailable)
    with TestClient(app) as client:
        response = client.get("/api/readiness")

    assert response.status_code == 503
    assert response.json()["bridge"] == "unavailable"


def test_readiness_does_not_recreate_a_missing_research_database(client, app, settings) -> None:
    from pathlib import Path

    app.state.engine.dispose()
    database = Path(settings.database_url.removeprefix("sqlite:///"))
    for candidate in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
        candidate.unlink(missing_ok=True)

    response = client.get("/api/readiness")

    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"
    assert not database.exists()


def test_readiness_rejects_database_without_required_schema(client, app, settings) -> None:
    import sqlite3
    from pathlib import Path

    app.state.engine.dispose()
    database = Path(settings.database_url.removeprefix("sqlite:///"))
    database.unlink()
    with sqlite3.connect(database):
        pass

    response = client.get("/api/readiness")

    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"
