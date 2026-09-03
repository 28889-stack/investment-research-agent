import json

import pytest

from app.ops import BackupValidationError, create_backup, restore_check
from app.maintenance_lock import MaintenanceLockBusy, maintenance_lock
from app.run_service import RunService


def _completed_run(settings, session_factory):
    service = RunService(session_factory, settings.artifacts_dir)
    run = service.create_run(symbol="600519", analysis_type="technical")
    directory = settings.artifacts_dir / run.run_id
    directory.mkdir(parents=True, exist_ok=True)
    report = directory / "technical_report.md"
    report.write_text("# report", encoding="utf-8")
    (directory / "metadata.json").write_text('{"ok":true}', encoding="utf-8")
    service.transition_run(
        run.run_id,
        status="COMPLETED",
        stage="done",
        progress=100,
        message="done",
        report_path=str(report),
    )
    return run


def test_backup_uses_manifest_and_restore_check_is_non_destructive(settings, session_factory) -> None:
    run = _completed_run(settings, session_factory)
    sentinel = settings.artifacts_dir / "keep.txt"
    sentinel.write_text("current-data", encoding="utf-8")

    backup = create_backup(settings)
    manifest = json.loads((backup / "backup_manifest.json").read_text(encoding="utf-8"))
    result = restore_check(backup)

    assert backup.parent == settings.backups_dir
    assert (backup / "research.db").is_file()
    assert (backup / "checkpoints.db").is_file()
    assert (backup / "artifacts" / run.run_id / "technical_report.md").is_file()
    assert manifest["application_version"] == "0.1.0"
    assert manifest["artifact_count"] >= 3
    assert manifest["total_file_size"] > 0
    assert len(manifest["files"]["research.db"]["sha256"]) == 64
    assert result["status"] == "valid"
    assert result["reports_checked"] == 1
    assert sentinel.read_text(encoding="utf-8") == "current-data"


def test_restore_check_rejects_missing_or_tampered_backup_file(settings, session_factory) -> None:
    _completed_run(settings, session_factory)
    backup = create_backup(settings)
    (backup / "research.db").write_bytes(b"tampered")

    with pytest.raises(BackupValidationError, match="SHA"):
        restore_check(backup)


def test_restore_check_requires_both_database_entries(settings, session_factory) -> None:
    _completed_run(settings, session_factory)
    backup = create_backup(settings)
    manifest_path = backup / "backup_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].pop("checkpoints.db")
    (backup / "checkpoints.db").unlink()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupValidationError, match="缺少必需数据库"):
        restore_check(backup)


def test_backup_refuses_to_race_active_worker(settings, session_factory) -> None:
    _completed_run(settings, session_factory)

    with maintenance_lock(settings, exclusive=True):
        with pytest.raises(MaintenanceLockBusy):
            create_backup(settings)
