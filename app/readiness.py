from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.config import Settings


class ConfigurationError(ValueError):
    pass


MINIMUM_NODE_VERSION = (22, 19, 0)


def node_version_status(command: str) -> tuple[bool, str]:
    if not command.strip() or shutil.which(command.strip()) is None:
        return False, "Node.js 不可用"
    try:
        output = subprocess.run(
            [command.strip(), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", output)
        if match is None:
            return False, f"无法识别版本: {output[:30]}"
        version = tuple(int(part) for part in match.groups())
        return version >= MINIMUM_NODE_VERSION, output
    except (OSError, subprocess.SubprocessError):
        return False, "Node.js 不可用"


def _sqlite_path(database_url: str) -> Path | None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix) or database_url == "sqlite:///:memory:":
        return None
    return Path(database_url[len(prefix):])


def _sqlite_ready(path: Path | None, required_tables: set[str] | None = None) -> bool:
    if path is None or not path.is_file():
        return False
    try:
        uri = f"file:{quote(str(path.resolve()))}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                return False
            if required_tables:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                return required_tables.issubset(tables)
            return True
    except (OSError, sqlite3.Error):
        return False


def _directory_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, prefix=".readiness-", delete=True):
            pass
        return True
    except OSError:
        return False


_LIVE_SEARCH_PROVIDERS = {
    "official_crawler",
    "akshare_news",
    "akshare_reports",
    "akshare_notices",
    "tavily",
    "keenable",
    "firecrawl",
    "aggregator",
}

_PROVIDERS_REQUIRING_KEY = {"tavily", "keenable", "firecrawl"}


def _research_search_ready(
    settings: Settings, environ: dict[str, str]
) -> bool:
    """A Live retrieval provider is configured when its name is valid and any
    required API key env var is present. akshare-backed providers and the
    official_crawler need no key. The aggregator is ready when its configured
    member list is non-empty (member readiness is not checked here so a single
    failing source does not gate the whole Live mode)."""
    provider = settings.research_search_provider
    if not provider or provider not in _LIVE_SEARCH_PROVIDERS:
        return False
    if provider in _PROVIDERS_REQUIRING_KEY:
        env_name = settings.research_search_api_key_env_name
        return bool(env_name and environ.get(env_name))
    if provider == "aggregator":
        return bool(settings.research_search_providers)
    return True


def live_configuration(settings: Settings, environ: dict[str, str] | None = None) -> dict[str, str]:
    environ = os.environ if environ is None else environ
    pi_ready = bool(
        settings.pi_model_provider
        and settings.pi_model
        and settings.pi_api_key_env_name
        and environ.get(settings.pi_api_key_env_name)
    )
    search_ready = _research_search_ready(settings, environ)
    return {
        "pi": "configured" if pi_ready else "not_configured",
        "market_data": "configured" if settings.market_data_provider else "not_configured",
        "kronos": "configured" if settings.kronos_model_name else "not_configured",
        "fundamental_data": "configured" if settings.fundamental_data_provider else "not_configured",
        "research_search": "configured" if search_ready else "not_configured",
    }


def configuration_issues(
    settings: Settings, environ: dict[str, str] | None = None
) -> list[str]:
    environ = os.environ if environ is None else environ
    issues: list[str] = []
    modes = {
        "PI_RUNTIME_MODE": settings.pi_runtime_mode,
        "MARKET_DATA_MODE": settings.market_data_mode,
        "KRONOS_MODE": settings.kronos_mode,
        "FUNDAMENTAL_DATA_MODE": settings.fundamental_data_mode,
        "RESEARCH_SEARCH_MODE": settings.research_search_mode,
    }
    if settings.app_env == "staging":
        mock_modes = [name for name, value in modes.items() if value != "live"]
        if mock_modes:
            issues.append(
                "staging 验收要求全部组件为 Live: " + ", ".join(mock_modes)
            )
    elif settings.app_env == "development":
        live_modes = [name for name, value in modes.items() if value != "mock"]
        if live_modes:
            issues.append(
                "development 要求全部组件为 Mock: " + ", ".join(live_modes)
            )
    if settings.app_host in {"0.0.0.0", "::", "[::]"} and not settings.allow_public_bind:
        issues.append("APP_HOST 为公网绑定地址时必须设置 ALLOW_PUBLIC_BIND=true")
    storage_root = settings.storage_root.resolve()
    if storage_root == Path("/"):
        issues.append("STORAGE_ROOT 不能指向文件系统根目录")
    directories = (
        ("ARTIFACTS_DIR", settings.artifacts_dir),
        ("LOGS_DIR", settings.logs_dir),
        ("BACKUPS_DIR", settings.backups_dir),
    )
    database_path = _sqlite_path(settings.database_url)
    if database_path is None:
        issues.append("DATABASE_URL 必须为本地 SQLite 文件")
    paths = [*directories, ("CHECKPOINT_DATABASE_PATH", settings.checkpoint_database_path)]
    if database_path is not None:
        paths.append(("DATABASE_URL", database_path))
    for label, path in paths:
        resolved = Path(path).resolve()
        if resolved == storage_root or storage_root not in resolved.parents:
            issues.append(f"{label} 必须位于 STORAGE_ROOT 内且不能等于根目录")
    resolved_directories = [(label, Path(path).resolve()) for label, path in directories]
    for index, (left_label, left) in enumerate(resolved_directories):
        for right_label, right in resolved_directories[index + 1 :]:
            if left == right or left in right.parents or right in left.parents:
                issues.append(f"{left_label} 与 {right_label} 不能重叠或互相包含")
    if database_path is not None:
        database_resolved = database_path.resolve()
        checkpoint_resolved = settings.checkpoint_database_path.resolve()
        if database_resolved == checkpoint_resolved:
            issues.append("DATABASE_URL 与 CHECKPOINT_DATABASE_PATH 不能相同")
        for label, directory in resolved_directories:
            if directory in database_resolved.parents or directory in checkpoint_resolved.parents:
                issues.append(f"SQLite 数据库不能位于 {label} 内")
    node_ready, node_version = node_version_status(settings.pi_bridge_command)
    if not node_ready:
        issues.append(f"PI_BRIDGE_COMMAND/Node.js 必须至少为 v22.19.0（{node_version}）")
    if settings.pi_runtime_mode == "live":
        if settings.pi_model_provider not in {"openai", "anthropic", "deepseek"}:
            issues.append("Live Pi 的 PI_MODEL_PROVIDER 仅支持 openai、anthropic 或 deepseek")
        if not settings.pi_model_provider:
            issues.append("Live Pi 缺少 PI_MODEL_PROVIDER")
        if not settings.pi_model:
            issues.append("Live Pi 缺少 PI_MODEL")
        if not settings.pi_api_key_env_name:
            issues.append("Live Pi 缺少 PI_API_KEY_ENV_NAME")
        elif not re.fullmatch(r"[A-Z_][A-Z0-9_]*", settings.pi_api_key_env_name):
            issues.append("PI_API_KEY_ENV_NAME 必须是大写环境变量名")
        elif not environ.get(settings.pi_api_key_env_name):
            issues.append(f"Live Pi 缺少环境变量 {settings.pi_api_key_env_name}")
    if settings.research_search_mode == "live":
        if not settings.research_search_provider:
            issues.append("Live 检索缺少 RESEARCH_SEARCH_PROVIDER")
        elif settings.research_search_provider not in _LIVE_SEARCH_PROVIDERS:
            allowed = "、".join(sorted(_LIVE_SEARCH_PROVIDERS))
            issues.append(
                f"Live 检索的 RESEARCH_SEARCH_PROVIDER 仅支持 {allowed}"
            )
        elif settings.research_search_provider in _PROVIDERS_REQUIRING_KEY:
            if not settings.research_search_api_key_env_name:
                issues.append("Live 检索缺少 RESEARCH_SEARCH_API_KEY_ENV_NAME")
            elif not re.fullmatch(
                r"[A-Z_][A-Z0-9_]*", settings.research_search_api_key_env_name
            ):
                issues.append("RESEARCH_SEARCH_API_KEY_ENV_NAME 必须是大写环境变量名")
            elif not environ.get(settings.research_search_api_key_env_name):
                issues.append(
                    f"Live 检索缺少环境变量 {settings.research_search_api_key_env_name}"
                )
        elif settings.research_search_provider == "aggregator":
            if not settings.research_search_providers:
                issues.append(
                    "aggregator 模式必须配置 RESEARCH_SEARCH_PROVIDERS（逗号分隔的来源列表）"
                )
            invalid = [
                name
                for name in settings.research_search_providers
                if name not in _LIVE_SEARCH_PROVIDERS or name == "aggregator"
            ]
            if invalid:
                issues.append(
                    "RESEARCH_SEARCH_PROVIDERS 含无效来源: " + "、".join(invalid)
                )
    if settings.kronos_mode == "live":
        if not settings.kronos_model_name:
            issues.append("Live Kronos 缺少 KRONOS_MODEL_NAME")
        if not (settings.kronos_source_dir / "model" / "__init__.py").is_file():
            issues.append("Live Kronos 缺少 KRONOS_SOURCE_DIR 下的官方 model 包")
    if settings.market_data_mode == "live" and settings.market_data_provider != "akshare":
        issues.append("Live 行情的 MARKET_DATA_PROVIDER 仅支持 akshare")
    if (
        settings.fundamental_data_mode == "live"
        and settings.fundamental_data_provider != "akshare"
    ):
        issues.append("Live 基本面数据的 FUNDAMENTAL_DATA_PROVIDER 仅支持 akshare")
    return issues


def validate_startup_config(
    settings: Settings, environ: dict[str, str] | None = None
) -> None:
    issues = configuration_issues(settings, environ)
    if issues:
        raise ConfigurationError("；".join(issues))


def preflight_live_pi(settings: Settings) -> None:
    if settings.pi_runtime_mode != "live":
        return
    from app.runtime.pi_client import BridgePiClient

    client = BridgePiClient(
        command=settings.pi_bridge_command,
        entrypoint=settings.pi_bridge_entry,
        runtime_mode="live",
        start_timeout=settings.pi_bridge_start_timeout,
        request_timeout=settings.pi_request_timeout,
        max_restarts=0,
        model_provider=settings.pi_model_provider,
        model_name=settings.pi_model,
        api_key_env_name=settings.pi_api_key_env_name,
    )
    try:
        client.validate_model()
    except Exception as exc:
        raise ConfigurationError(
            f"Live Pi provider/model 本地预检失败（{type(exc).__name__}）"
        ) from exc
    finally:
        client.shutdown()


def workflow_build_status(settings: Settings, session_factory) -> dict[str, bool]:
    from app.fundamental.workflow import FundamentalWorkflow
    from app.technical.workflow import TechnicalWorkflow

    results: dict[str, bool] = {}
    for name, workflow_type in (
        ("technical_workflow", TechnicalWorkflow),
        ("fundamental_workflow", FundamentalWorkflow),
    ):
        workflow = None
        try:
            workflow = workflow_type(settings, session_factory)
            results[name] = True
        except Exception:
            results[name] = False
        finally:
            if workflow is not None:
                workflow.shutdown()
    return results


def readiness_report(
    settings: Settings,
    *,
    profiles_ready: bool,
    tools_ready: bool,
    workflow_status: dict[str, bool] | None = None,
) -> tuple[int, dict[str, Any]]:
    workflow_status = workflow_status or {
        "technical_workflow": False,
        "fundamental_workflow": False,
    }
    checks = {
        "database": _sqlite_ready(
            _sqlite_path(settings.database_url),
            {"research_runs", "run_events", "agent_executions", "tool_executions"},
        ),
        "checkpoint": _sqlite_ready(
            settings.checkpoint_database_path, {"checkpoints", "writes"}
        ),
        "artifacts": _directory_writable(settings.artifacts_dir),
        "bridge": settings.pi_bridge_entry.is_file()
        and node_version_status(settings.pi_bridge_command)[0],
        "profiles": profiles_ready,
        "tools": tools_ready,
        "technical_workflow": workflow_status.get("technical_workflow", False),
        "fundamental_workflow": workflow_status.get("fundamental_workflow", False),
    }
    ready = all(checks.values())
    payload: dict[str, Any] = {
        "status": "ready" if ready else "not_ready",
        "environment": settings.app_env,
        **{name: "ready" if value else "unavailable" for name, value in checks.items()},
        "live_configuration": live_configuration(settings),
    }
    return (200 if ready else 503), payload
