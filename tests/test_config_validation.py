from app.config import Settings
from app.readiness import ConfigurationError, configuration_issues, preflight_live_pi, validate_startup_config
from scripts.live_smoke import live_mode_issues
import pytest


def test_settings_expose_mvp_operations_defaults() -> None:
    settings = Settings()

    assert settings.app_env == "development"
    assert settings.allow_public_bind is False
    assert settings.max_pending_runs == 20
    assert settings.research_search_max_results == 8
    assert settings.logs_dir.as_posix() == "logs"
    assert settings.backups_dir.as_posix() == "backups"


def test_mock_development_configuration_is_accepted() -> None:
    validate_startup_config(Settings(), environ={})


def test_live_pi_requires_provider_model_and_environment_key() -> None:
    settings = Settings(pi_runtime_mode="live")

    with pytest.raises(ConfigurationError, match="PI_MODEL_PROVIDER"):
        validate_startup_config(settings, environ={})


def test_live_search_requires_tavily_environment_key() -> None:
    settings = Settings(
        research_search_mode="live",
        research_search_provider="tavily",
        research_search_api_key_env_name="TAVILY_API_KEY",
    )

    with pytest.raises(ConfigurationError, match="TAVILY_API_KEY"):
        validate_startup_config(settings, environ={})


def test_live_search_aggregator_requires_member_list() -> None:
    settings = Settings(
        research_search_mode="live",
        research_search_provider="aggregator",
        research_search_providers=[],
    )

    with pytest.raises(ConfigurationError, match="RESEARCH_SEARCH_PROVIDERS"):
        validate_startup_config(settings, environ={})


def test_live_search_aggregator_rejects_self_reference_and_unknown_sources() -> None:
    settings = Settings(
        research_search_mode="live",
        research_search_provider="aggregator",
        research_search_providers=["aggregator", "unknown_source"],
    )

    with pytest.raises(ConfigurationError, match="无效来源"):
        validate_startup_config(settings, environ={})


def test_live_search_aggregator_accepts_akshare_members_without_key(tmp_path) -> None:
    source_dir = tmp_path / "kronos"
    (source_dir / "model").mkdir(parents=True)
    (source_dir / "model" / "__init__.py").write_text("", encoding="utf-8")
    settings = Settings(
        app_env="production",
        storage_root=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'research.db'}",
        checkpoint_database_path=tmp_path / "checkpoints.db",
        artifacts_dir=tmp_path / "artifacts",
        logs_dir=tmp_path / "logs",
        backups_dir=tmp_path / "backups",
        pi_runtime_mode="live",
        pi_model_provider="deepseek",
        pi_model="deepseek-v4-flash",
        pi_api_key_env_name="DEEPSEEK_API_KEY",
        kronos_mode="live",
        kronos_model_name="NeoQuasar/Kronos-mini",
        kronos_source_dir=source_dir,
        research_search_mode="live",
        research_search_provider="aggregator",
        research_search_providers=[
            "official_crawler",
            "akshare_news",
            "akshare_reports",
            "akshare_notices",
        ],
    )

    issues = configuration_issues(settings, environ={"DEEPSEEK_API_KEY": "x"})
    assert not any("RESEARCH_SEARCH" in issue for issue in issues)


def test_deepseek_and_official_crawler_live_configuration_is_accepted(tmp_path) -> None:
    source_dir = tmp_path / "kronos"
    (source_dir / "model").mkdir(parents=True)
    (source_dir / "model" / "__init__.py").write_text("", encoding="utf-8")
    settings = Settings(
        storage_root=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'research.db'}",
        checkpoint_database_path=tmp_path / "checkpoints.db",
        artifacts_dir=tmp_path / "artifacts",
        logs_dir=tmp_path / "logs",
        backups_dir=tmp_path / "backups",
        app_env="production",
        pi_runtime_mode="live",
        pi_model_provider="deepseek",
        pi_model="deepseek-v4-flash",
        pi_api_key_env_name="DEEPSEEK_API_KEY",
        kronos_mode="live",
        kronos_model_name="NeoQuasar/Kronos-mini",
        kronos_source_dir=source_dir,
        research_search_mode="live",
        research_search_provider="official_crawler",
    )

    validate_startup_config(settings, environ={"DEEPSEEK_API_KEY": "test-only"})


def test_live_kronos_requires_model_configuration() -> None:
    with pytest.raises(ConfigurationError, match="KRONOS_MODEL_NAME"):
        validate_startup_config(Settings(kronos_mode="live"), environ={})


def test_public_bind_requires_explicit_authorization() -> None:
    with pytest.raises(ConfigurationError, match="ALLOW_PUBLIC_BIND"):
        validate_startup_config(Settings(app_host="0.0.0.0"), environ={})


def test_storage_roots_reject_filesystem_root() -> None:
    with pytest.raises(ConfigurationError, match="ARTIFACTS_DIR"):
        validate_startup_config(Settings(artifacts_dir="/"), environ={})


def test_storage_paths_must_be_inside_root_and_disjoint(tmp_path) -> None:
    settings = Settings(
        storage_root=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'research.db'}",
        checkpoint_database_path=tmp_path / "checkpoints.db",
        artifacts_dir=tmp_path / "artifacts",
        logs_dir=tmp_path / "artifacts" / "logs",
        backups_dir=tmp_path / "backups",
    )

    with pytest.raises(ConfigurationError, match="ARTIFACTS_DIR 与 LOGS_DIR"):
        validate_startup_config(settings, environ={})


def test_bridge_command_must_exist_before_startup(tmp_path) -> None:
    with pytest.raises(ConfigurationError, match="PI_BRIDGE_COMMAND"):
        validate_startup_config(
            Settings(storage_root=tmp_path, pi_bridge_command="missing-node-command"),
            environ={},
        )


def test_bridge_command_must_report_supported_node_version(tmp_path) -> None:
    with pytest.raises(ConfigurationError, match="v22.19.0"):
        validate_startup_config(
            Settings(storage_root=tmp_path, pi_bridge_command="/bin/echo"),
            environ={},
        )


def test_live_provider_and_key_name_are_validated() -> None:
    settings = Settings(
        app_env="production",
        pi_runtime_mode="live",
        pi_model_provider="unsupported",
        pi_model="model",
        pi_api_key_env_name="bad-key-name",
    )

    with pytest.raises(ConfigurationError, match="仅支持 openai.*大写环境变量名"):
        validate_startup_config(settings, environ={})


def test_live_pi_model_typo_fails_local_bridge_preflight(settings, monkeypatch) -> None:
    monkeypatch.setenv("CUSTOM_PI_SECRET", "test-only-secret")
    live = settings.model_copy(
        update={
            "app_env": "production",
            "pi_runtime_mode": "live",
            "pi_model_provider": "openai",
            "pi_model": "definitely-not-a-real-model",
            "pi_api_key_env_name": "CUSTOM_PI_SECRET",
        }
    )

    with pytest.raises(ConfigurationError, match="本地预检失败"):
        preflight_live_pi(live)


def test_live_smoke_refuses_any_mock_component() -> None:
    technical = live_mode_issues(Settings(), "technical", environ={})
    fundamental = live_mode_issues(Settings(), "fundamental", environ={})

    assert technical == [
        "PI_RUNTIME_MODE must be live",
        "MARKET_DATA_MODE must be live",
        "KRONOS_MODE must be live",
    ]
    assert fundamental == [
        "PI_RUNTIME_MODE must be live",
        "FUNDAMENTAL_DATA_MODE must be live",
        "RESEARCH_SEARCH_MODE must be live",
    ]


def test_staging_requires_every_research_component_to_be_live() -> None:
    with pytest.raises(ConfigurationError, match="staging.*PI_RUNTIME_MODE"):
        validate_startup_config(Settings(app_env="staging"), environ={})


def test_development_rejects_partial_live_service_configuration() -> None:
    with pytest.raises(ConfigurationError, match="development.*MARKET_DATA_MODE"):
        validate_startup_config(
            Settings(app_env="development", market_data_mode="live"), environ={}
        )
