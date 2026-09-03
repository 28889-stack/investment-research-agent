from __future__ import annotations

import logging
import time
import signal
from threading import Event
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.database import create_db_engine, create_session_factory, init_database
from app.models import ResearchRun
from app.run_service import RunNotFoundError, RunService
from app.runtime.orchestrator import RuntimeOrchestrator
from app.technical.workflow import TechnicalWorkflow
from app.fundamental.workflow import FundamentalWorkflow
from app.readiness import preflight_live_pi, validate_startup_config
from app.logging_config import configure_logging
from app.maintenance_lock import MaintenanceLockBusy, maintenance_lock
from app.runtime.security import safe_error_message


LOGGER = logging.getLogger(__name__)


class ResearchWorker:
    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: sessionmaker[Session] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        orchestrator: RuntimeOrchestrator | None = None,
        technical_workflow: TechnicalWorkflow | None = None,
        fundamental_workflow: FundamentalWorkflow | None = None,
    ) -> None:
        self.settings = settings
        self.active_run_id: str | None = None
        self.sleep = sleep_fn
        if session_factory is None:
            engine = create_db_engine(settings.database_url)
            init_database(engine)
            session_factory = create_session_factory(engine)
        self.service = RunService(
            session_factory,
            settings.artifacts_dir,
            settings.pi_runtime_mode,
            settings.technical_workflow_version,
            settings.fundamental_workflow_version,
            settings.max_pending_runs,
        )
        self.orchestrator = orchestrator or RuntimeOrchestrator(
            settings,
            session_factory,
            report_writer=lambda run: self._generate_report(run),
        )
        self.technical_workflow = technical_workflow or TechnicalWorkflow(
            settings,
            session_factory,
        )
        self.fundamental_workflow = fundamental_workflow or FundamentalWorkflow(
            settings,
            session_factory,
        )

    def claim_next_run(self) -> str | None:
        return self.service.claim_next_created_run()

    def run_once(self) -> bool:
        try:
            # An exclusive cross-process lock enforces the MVP's one-active-run
            # boundary and also keeps backups from racing artifact writes.
            with maintenance_lock(self.settings, exclusive=True):
                run_id = self.service.next_recoverable_run()
                if run_id is None:
                    run_id = self.service.next_stale_completed_fundamental()
                if run_id is None:
                    run_id = self.claim_next_run()
                if run_id is None:
                    return False
                self.active_run_id = run_id
                try:
                    self.process_run(run_id)
                finally:
                    self.active_run_id = None
                return True
        except MaintenanceLockBusy:
            return False

    def request_stop(self) -> None:
        if self.active_run_id is None:
            return
        try:
            self.service.request_cancel(self.active_run_id)
        except Exception:
            LOGGER.warning("无法为活动任务登记停机取消请求")

    def process_run(self, run_id: str) -> None:
        try:
            run = self.service.get_run(run_id)
            if run.analysis_type == "technical":
                self.technical_workflow.run(run_id)
            else:
                self.fundamental_workflow.run(run_id)
        except Exception as exc:
            error_type = type(exc).__name__
            LOGGER.error(
                "Worker task failed",
                extra={"run_id": run_id, "workflow": None, "node": None, "execution_id": None, "duration_ms": None, "status": "FAILED", "error_type": error_type, "diagnostic": safe_error_message(str(exc), max_length=500)},
            )
            try:
                run = self.service.get_run(run_id)
                if run.cancel_requested:
                    self.service.cancel_if_requested(run_id)
                elif run.status not in {"COMPLETED", "HUMAN_REVIEW_REQUIRED", "CANCELLED"}:
                    self.service.transition_run(
                        run_id,
                        status="FAILED",
                        stage="任务失败",
                        progress=run.progress,
                        event_type="RUN_FAILED",
                        message="Worker 执行任务时发生错误",
                        error_message=f"Worker 执行失败（{error_type}）",
                    )
            except RunNotFoundError:
                LOGGER.error("失败任务不存在: %s", run_id)

    def _stop_if_cancelled(self, run_id: str) -> bool:
        return self.service.cancel_if_requested(run_id)

    def _generate_report(self, run: ResearchRun) -> Path:
        return self.orchestrator.generate_report(run)

    def shutdown(self) -> None:
        self.orchestrator.shutdown()
        self.technical_workflow.shutdown()
        self.fundamental_workflow.shutdown()

    def run_forever(self, *, stop_event: Event | None = None) -> None:
        stop_event = stop_event or Event()
        LOGGER.info("Research Worker 已启动")
        while not stop_event.is_set():
            processed = self.run_once()
            if not processed:
                if stop_event.wait(self.settings.worker_poll_interval):
                    break


def main() -> None:
    settings = Settings.from_env()
    validate_startup_config(settings)
    preflight_live_pi(settings)
    configure_logging(settings, "worker")
    stop_event = Event()

    worker = ResearchWorker(settings)

    def request_stop(_signum, _frame) -> None:
        stop_event.set()
        worker.request_stop()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        worker.run_forever(stop_event=stop_event)
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        LOGGER.info("Research Worker 已停止")
        worker.shutdown()


if __name__ == "__main__":
    main()
