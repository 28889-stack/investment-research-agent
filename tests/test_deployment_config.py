import pytest

from app.config import Settings
from app.readiness import ConfigurationError, validate_startup_config


def test_production_auth_requires_invite_and_cookie_secrets(tmp_path) -> None:
    settings = Settings(
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
    )

    with pytest.raises(ConfigurationError, match="ACCESS_INVITE_CODE"):
        validate_startup_config(settings, environ={})
