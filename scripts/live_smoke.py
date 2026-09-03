from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings
from app.database import create_db_engine, create_session_factory, init_database
from app.fundamental.schemas import AssumptionStore, EvidenceCollection
from app.readiness import configuration_issues, preflight_live_pi
from app.run_service import RunService
from app.worker import ResearchWorker
from app.maintenance_lock import maintenance_lock
from app.models import ResearchRun
from app.run_service import TERMINAL_STATUSES
from sqlalchemy import func, select


def live_mode_issues(
    settings: Settings, analysis_type: str, environ: dict[str, str] | None = None
) -> list[str]:
    requirements = {
        "technical": [
            ("PI_RUNTIME_MODE", settings.pi_runtime_mode),
            ("MARKET_DATA_MODE", settings.market_data_mode),
            ("KRONOS_MODE", settings.kronos_mode),
        ],
        "fundamental": [
            ("PI_RUNTIME_MODE", settings.pi_runtime_mode),
            ("FUNDAMENTAL_DATA_MODE", settings.fundamental_data_mode),
            ("RESEARCH_SEARCH_MODE", settings.research_search_mode),
        ],
    }
    issues = [f"{name} must be live" for name, value in requirements[analysis_type] if value != "live"]
    if not issues:
        issues.extend(configuration_issues(settings, os.environ if environ is None else environ))
    return issues


def execute_live_run(settings: Settings, analysis_type: str, symbol: str) -> dict:
    issues = live_mode_issues(settings, analysis_type)
    if issues:
        raise RuntimeError("; ".join(issues))
    preflight_live_pi(settings)
    engine = create_db_engine(settings.database_url)
    init_database(engine)
    session_factory = create_session_factory(engine)
    service = RunService(
        session_factory,
        settings.artifacts_dir,
        settings.pi_runtime_mode,
        settings.technical_workflow_version,
        settings.fundamental_workflow_version,
        settings.max_pending_runs,
    )
    with maintenance_lock(settings, exclusive=True):
        with session_factory() as session:
            pending = session.scalar(
                select(func.count(ResearchRun.id)).where(
                    ResearchRun.status.not_in(TERMINAL_STATUSES)
                )
            ) or 0
        if pending:
            engine.dispose()
            raise RuntimeError("Live smoke requires an empty pending queue")
        run = service.create_run(symbol=symbol, analysis_type=analysis_type)
        if service.claim_next_created_run() != run.run_id:
            engine.dispose()
            raise RuntimeError("Live smoke could not claim its isolated run")
        worker = ResearchWorker(settings, session_factory=session_factory, sleep_fn=lambda _: None)
        started = time.monotonic()
        try:
            worker.process_run(run.run_id)
        finally:
            worker.shutdown()
    completed = service.get_run(run.run_id)
    duration = round(time.monotonic() - started, 3)
    if completed.status != "COMPLETED" or not completed.report_path:
        engine.dispose()
        raise RuntimeError(f"Live run did not complete: {completed.status}")
    directory = settings.artifacts_dir / run.run_id
    evidence_count = 0
    assumption_count = 0
    if analysis_type == "fundamental":
        evidence_count = len(EvidenceCollection.model_validate_json((directory / "evidence.json").read_text(encoding="utf-8")).items)
        assumption_count = len(AssumptionStore.model_validate_json((directory / "assumptions.json").read_text(encoding="utf-8")).items)
    summary = {
        "run_id": run.run_id,
        "workflow": completed.workflow_name,
        "status": completed.status,
        "duration": duration,
        "report_path": Path(completed.report_path).name,
        "data_source": (
            "AKShare"
            if analysis_type == "technical"
            else f"AKShare + {settings.research_search_provider}"
        ),
        "model": settings.pi_model,
        "evidence_count": evidence_count,
        "assumption_count": assumption_count,
    }
    engine.dispose()
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--technical", metavar="SYMBOL")
    group.add_argument("--fundamental", metavar="SYMBOL")
    group.add_argument("--all", metavar="SYMBOL")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    requested = []
    if args.technical:
        requested = [("technical", args.technical)]
    elif args.fundamental:
        requested = [("fundamental", args.fundamental)]
    else:
        requested = [("technical", args.all), ("fundamental", args.all)]
    try:
        for analysis_type, symbol in requested:
            print(json.dumps(execute_live_run(settings, analysis_type, symbol), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"status": "FAILED", "error_type": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
