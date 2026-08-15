from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _settings(tmp_path: Path, *, invite_code: str = "invite-one") -> Settings:
    return Settings(
        app_env="production",
        app_host="0.0.0.0",
        allow_public_bind=True,
        storage_root=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'research.db'}",
        artifacts_dir=tmp_path / "artifacts",
        logs_dir=tmp_path / "logs",
        backups_dir=tmp_path / "backups",
        checkpoint_database_path=tmp_path / "checkpoints.db",
        access_auth_enabled=True,
        access_invite_code=invite_code,
        access_cookie_secret="test-signing-secret",
        access_cookie_secure=False,
    )


def test_invite_auth_protects_research_apis_but_not_health(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))

    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/runs").status_code == 401
        assert client.post("/api/auth/invite", json={"invite_code": "wrong"}).status_code == 401

        response = client.post("/api/auth/invite", json={"invite_code": "invite-one"})
        assert response.status_code == 204
        assert "research_access=" in response.headers["set-cookie"]

        assert client.get("/api/auth/session").json() == {"authenticated": True}
        assert client.post(
            "/api/runs", json={"symbol": "600519", "analysis_type": "technical"}
        ).status_code == 201


def test_changing_invite_code_invalidates_existing_session(tmp_path: Path) -> None:
    initial_app = create_app(_settings(tmp_path, invite_code="invite-one"))
    with TestClient(initial_app) as initial_client:
        assert initial_client.post(
            "/api/auth/invite", json={"invite_code": "invite-one"}
        ).status_code == 204
        session_cookie = initial_client.cookies.get("research_access")

    rotated_app = create_app(_settings(tmp_path, invite_code="invite-two"))
    with TestClient(rotated_app) as rotated_client:
        rotated_client.cookies.set("research_access", session_cookie)
        assert rotated_client.get("/api/runs").status_code == 401


def test_logout_revokes_browser_session(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))

    with TestClient(app) as client:
        client.post("/api/auth/invite", json={"invite_code": "invite-one"})
        assert client.post("/api/auth/logout").status_code == 204
        assert client.get("/api/runs").status_code == 401
