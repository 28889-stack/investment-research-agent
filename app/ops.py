from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import platform
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from app.maintenance_lock import maintenance_lock


APPLICATION_VERSION = "0.1.0"
LOGGER = logging.getLogger(__name__)


class BackupValidationError(ValueError):
    pass


@dataclass(frozen=True)
class CheckResult:
    status: str
    name: str
    message: str = ""


@dataclass(frozen=True)
class DoctorResult:
    checks: list[CheckResult]

    @property
    def exit_code(self) -> int:
        return 1 if any(item.status == "ERROR" for item in self.checks) else 0

    def render(self) -> str:
        return "\n".join(
            f"[{item.status}] {item.name}" + (f": {item.message}" if item.message else "")
            for item in self.checks
        )


def _sqlite_path(database_url: str) -> Path:
    if not database_url.startswith("sqlite:///") or database_url == "sqlite:///:memory:":
        raise ValueError("backup only supports file-backed SQLite")
    return Path(database_url.removeprefix("sqlite:///"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_backup(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    with sqlite3.connect(source, timeout=30) as source_db, sqlite3.connect(destination) as target_db:
        source_db.backup(target_db)


def create_backup(settings: Settings) -> Path:
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    destination = settings.backups_dir / f"mvp_backup_{stamp}"
    if destination.exists():
        raise FileExistsError(destination)
    destination.mkdir(parents=False)
    try:
        with maintenance_lock(settings, exclusive=True):
            _sqlite_backup(_sqlite_path(settings.database_url), destination / "research.db")
            _sqlite_backup(settings.checkpoint_database_path, destination / "checkpoints.db")
            artifact_target = destination / "artifacts"
            if settings.artifacts_dir.is_dir():
                shutil.copytree(settings.artifacts_dir, artifact_target)
            else:
                artifact_target.mkdir()
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    files: dict[str, dict[str, Any]] = {}
    for path in sorted(destination.rglob("*")):
        if not path.is_file() or path.name == "backup_manifest.json":
            continue
        relative = path.relative_to(destination).as_posix()
        files[relative] = {"sha256": _sha256(path), "size": path.stat().st_size}
    artifact_count = sum(1 for name in files if name.startswith("artifacts/"))
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "application_version": APPLICATION_VERSION,
        "artifact_count": artifact_count,
        "total_file_size": sum(item["size"] for item in files.values()),
        "files": files,
    }
    temporary = destination / ".backup_manifest.json.tmp"
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, destination / "backup_manifest.json")
    LOGGER.info(
        "Backup completed",
        extra={"component": "backup", "status": "COMPLETED", "duration_ms": None},
    )
    return destination


def _integrity(path: Path) -> None:
    if not path.is_file():
        raise BackupValidationError(f"SQLite 文件缺失: {path.name}")
    try:
        uri = f"file:{path.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        raise BackupValidationError(f"SQLite integrity check failed: {path.name}") from exc
    if result != ("ok",):
        raise BackupValidationError(f"SQLite integrity check failed: {path.name}")


def restore_check(backup: Path | str) -> dict[str, Any]:
    backup = Path(backup).resolve()
    manifest_path = backup / "backup_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = manifest["files"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise BackupValidationError("Backup Manifest 无法解析") from exc
    if not isinstance(files, dict):
        raise BackupValidationError("Backup Manifest files 无效")
    required = {"research.db", "checkpoints.db"}
    if not required.issubset(files):
        raise BackupValidationError("Backup Manifest 缺少必需数据库")
    calculated_size = 0
    for relative, metadata in files.items():
        if not isinstance(relative, str) or not isinstance(metadata, dict):
            raise BackupValidationError("Backup Manifest files 无效")
        candidate = (backup / relative).resolve()
        if backup not in candidate.parents or not candidate.is_file():
            raise BackupValidationError(f"备份文件缺失: {relative}")
        if _sha256(candidate) != metadata.get("sha256"):
            raise BackupValidationError(f"备份文件 SHA 不一致: {relative}")
        if candidate.stat().st_size != metadata.get("size"):
            raise BackupValidationError(f"备份文件大小不一致: {relative}")
        calculated_size += candidate.stat().st_size
    artifact_count = sum(1 for name in files if name.startswith("artifacts/"))
    if manifest.get("artifact_count") != artifact_count:
        raise BackupValidationError("Backup Manifest artifact_count 不一致")
    if manifest.get("total_file_size") != calculated_size:
        raise BackupValidationError("Backup Manifest total_file_size 不一致")
    with tempfile.TemporaryDirectory(prefix="mvp-restore-check-") as raw:
        restored = Path(raw) / "restored"
        shutil.copytree(backup, restored)
        _integrity(restored / "research.db")
        _integrity(restored / "checkpoints.db")
        with sqlite3.connect(
            f"file:{(restored / 'research.db').resolve()}?mode=ro", uri=True
        ) as connection:
            research_tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        if not {"research_runs", "run_events", "agent_executions", "tool_executions"}.issubset(research_tables):
            raise BackupValidationError("research.db 缺少必需数据表")
        with sqlite3.connect(
            f"file:{(restored / 'checkpoints.db').resolve()}?mode=ro", uri=True
        ) as connection:
            checkpoint_tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        if not {"checkpoints", "writes"}.issubset(checkpoint_tables):
            raise BackupValidationError("checkpoints.db 缺少必需数据表")
        json_checked = 0
        for path in sorted((restored / "artifacts").rglob("*.json"))[:50]:
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise BackupValidationError(f"Artifact JSON 无效: {path.name}") from exc
            json_checked += 1
        reports_checked = 0
        with sqlite3.connect(restored / "research.db") as connection:
            rows = connection.execute(
                "SELECT run_id, report_path FROM research_runs WHERE report_path IS NOT NULL"
            ).fetchall()
        for run_id, report_path in rows:
            report = restored / "artifacts" / str(run_id) / Path(str(report_path)).name
            if not report.is_file():
                raise BackupValidationError(f"报告文件缺失: {run_id}")
            report.read_text(encoding="utf-8")
            reports_checked += 1
    LOGGER.info(
        "Restore check completed",
        extra={"component": "backup", "status": "COMPLETED", "duration_ms": None},
    )
    return {
        "status": "valid",
        "files_checked": len(files),
        "json_checked": json_checked,
        "reports_checked": reports_checked,
    }


def run_doctor(
    settings: Settings, environ: dict[str, str] | None = None
) -> DoctorResult:
    from app.database import create_db_engine, create_session_factory, init_database
    from app.fundamental.workflow import FundamentalWorkflow
    from app.readiness import (
        _directory_writable,
        _sqlite_ready,
        configuration_issues,
        live_configuration,
        node_version_status,
    )
    from app.run_service import RunService
    from app.runtime.profiles import ProfileLoader
    from app.runtime.repository import RuntimeRepository
    from app.runtime.tool_registry import ToolRegistry, build_runtime_tools
    from app.technical.workflow import TechnicalWorkflow
    from app.tools.fundamental_tools import build_fundamental_tools
    from app.tools.technical_tools import build_technical_tools

    environ = os.environ if environ is None else environ
    checks: list[CheckResult] = []
    version = tuple(int(part) for part in platform.python_version_tuple()[:2])
    checks.append(CheckResult("OK" if version >= (3, 11) else "ERROR", "python version", platform.python_version()))
    node_ready, node_version = node_version_status(settings.pi_bridge_command)
    checks.append(CheckResult("OK" if node_ready else "ERROR", "node version", node_version))
    checks.append(
        CheckResult("OK" if settings.pi_bridge_entry.is_file() else "ERROR", "Pi Bridge build", "ready" if settings.pi_bridge_entry.is_file() else "entrypoint missing")
    )
    config_issues = configuration_issues(settings, environ)
    for issue in config_issues:
        checks.append(CheckResult("ERROR", "configuration", issue))
    if not config_issues:
        checks.append(
            CheckResult(
                "OK",
                "mock configuration" if settings.app_env == "development" else "startup configuration",
                settings.app_env,
            )
        )
        if settings.pi_runtime_mode == "live":
            try:
                from app.readiness import preflight_live_pi

                preflight_live_pi(settings)
                checks.append(CheckResult("OK", "live Pi model preflight"))
            except Exception as exc:
                checks.append(
                    CheckResult("ERROR", "live Pi model preflight", type(exc).__name__)
                )
    engine = None
    session_factory = None
    try:
        engine = create_db_engine(settings.database_url)
        init_database(engine)
        session_factory = create_session_factory(engine)
        research_ready = _sqlite_ready(
            _sqlite_path(settings.database_url),
            {"research_runs", "run_events", "agent_executions", "tool_executions"},
        )
        checks.append(CheckResult("OK" if research_ready else "ERROR", "research database"))
    except Exception as exc:
        checks.append(CheckResult("ERROR", "research database", type(exc).__name__))
    artifact_ready = _directory_writable(settings.artifacts_dir)
    checks.append(CheckResult("OK" if artifact_ready else "ERROR", "artifact directory"))
    if session_factory is not None:
        try:
            service = RunService(
                session_factory,
                settings.artifacts_dir,
                settings.pi_runtime_mode,
                settings.technical_workflow_version,
                settings.fundamental_workflow_version,
                settings.max_pending_runs,
            )
            repository = RuntimeRepository(session_factory)
            profiles = ProfileLoader(settings.agent_profile_dir)
            loaded = profiles.list_profiles()
            checks.append(CheckResult("OK", "profiles", str(len(loaded))))
            registry = ToolRegistry(repository)
            build_runtime_tools(registry, service, settings.tool_default_timeout)
            build_technical_tools(registry, service, repository, settings)
            build_fundamental_tools(registry, service, repository, settings)
            for profile in loaded:
                registry.validate_profile_permissions(profile)
            checks.append(CheckResult("OK", "tool registry", str(registry.tool_count)))
        except Exception as exc:
            checks.append(CheckResult("ERROR", "profiles", type(exc).__name__))
            checks.append(CheckResult("ERROR", "tool registry", type(exc).__name__))
        for name, workflow_type in (
            ("technical workflow", TechnicalWorkflow),
            ("fundamental workflow", FundamentalWorkflow),
        ):
            workflow = None
            try:
                workflow = workflow_type(settings, session_factory)
                checks.append(CheckResult("OK", name))
            except Exception as exc:
                checks.append(CheckResult("ERROR", name, type(exc).__name__))
            finally:
                if workflow is not None:
                    workflow.shutdown()
    checkpoint_ready = _sqlite_ready(
        settings.checkpoint_database_path, {"checkpoints", "writes"}
    )
    checks.append(CheckResult("OK" if checkpoint_ready else "ERROR", "checkpoint database"))
    live = live_configuration(settings, environ)
    modes = {
        "pi": settings.pi_runtime_mode,
        "market_data": settings.market_data_mode,
        "kronos": settings.kronos_mode,
        "fundamental_data": settings.fundamental_data_mode,
        "research_search": settings.research_search_mode,
    }
    for name, state in live.items():
        status = "OK" if modes[name] == "live" and state == "configured" else "SKIP"
        checks.append(CheckResult(status, f"live {name}", state))
    if engine is not None:
        engine.dispose()
    return DoctorResult(checks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ops")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor")
    subparsers.add_parser("backup")
    restore = subparsers.add_parser("restore-check")
    restore.add_argument("--backup", required=True, type=Path)
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    from app.logging_config import configure_logging

    configure_logging(settings, "backup")
    if args.command == "backup":
        print(create_backup(settings))
        return 0
    if args.command == "restore-check":
        print(json.dumps(restore_check(args.backup), ensure_ascii=False))
        return 0
    result = run_doctor(settings)
    print(result.render())
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
