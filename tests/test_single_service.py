from pathlib import Path

from app.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        storage_root=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'research.db'}",
        artifacts_dir=tmp_path / "artifacts",
        logs_dir=tmp_path / "logs",
        backups_dir=tmp_path / "backups",
        checkpoint_database_path=tmp_path / "checkpoints.db",
    )


def test_railway_port_is_used_when_app_port_is_not_set(monkeypatch) -> None:
    monkeypatch.delenv("APP_PORT", raising=False)
    monkeypatch.setenv("PORT", "7321")

    assert Settings.from_env().app_port == 7321


def test_single_service_starts_worker_and_stops_it_when_web_server_exits(tmp_path: Path) -> None:
    from app.service import run_single_service

    events: list[str] = []

    class FakeWorkerProcess:
        def start(self) -> None:
            events.append("worker:start")

        def is_alive(self) -> bool:
            return True

        def terminate(self) -> None:
            events.append("worker:terminate")

        def join(self, timeout=None) -> None:
            events.append("worker:join")

    class FakeServer:
        should_exit = False

        def run(self) -> None:
            events.append("web:run")

    run_single_service(
        _settings(tmp_path),
        application=object(),
        worker_process_factory=lambda _settings: FakeWorkerProcess(),
        server_factory=lambda _application, _settings: FakeServer(),
    )

    assert events == ["worker:start", "web:run", "worker:terminate", "worker:join"]
