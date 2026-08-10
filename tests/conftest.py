from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        storage_root=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'research.db'}",
        artifacts_dir=tmp_path / "artifacts",
        logs_dir=tmp_path / "logs",
        backups_dir=tmp_path / "backups",
        checkpoint_database_path=tmp_path / "checkpoints.db",
        worker_poll_interval=0,
        worker_step_delay=0,
    )


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def session_factory(app):
    return app.state.session_factory
