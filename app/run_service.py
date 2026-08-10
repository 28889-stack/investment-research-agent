from __future__ import annotations

import json
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

import bleach
import markdown
from sqlalchemy import Select, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.models import ResearchRun, RunEvent


TERMINAL_STATUSES = {"COMPLETED", "HUMAN_REVIEW_REQUIRED", "FAILED", "CANCELLED"}


class RunNotFoundError(Exception):
    pass


class RunConflictError(Exception):
    pass


class QueueFullError(Exception):
    pass


class ReportNotReadyError(Exception):
    pass


class ReportFileMissingError(Exception):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RunService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        artifacts_dir: Path,
        runtime_mode: str = "mock",
        technical_workflow_version: str = "technical_v1",
        fundamental_workflow_version: str = "fundamental_v1",
        max_pending_runs: int = 20,
    ) -> None:
        self.session_factory = session_factory
        self.artifacts_dir = Path(artifacts_dir)
        self.runtime_mode = runtime_mode
        self.technical_workflow_version = technical_workflow_version
        self.fundamental_workflow_version = fundamental_workflow_version
        self.max_pending_runs = max_pending_runs
        self._create_lock = threading.Lock()

    def create_run(
        self,
        *,
        symbol: str,
        analysis_type: str,
        policy_id: str = "general_research",
        as_of: str | date | None = None,
    ) -> ResearchRun:
        now = utc_now()
        as_of_value = as_of.isoformat() if isinstance(as_of, date) else as_of
        run_id = str(uuid4())
        run = ResearchRun(
            run_id=run_id,
            input_symbol=symbol.strip(),
            normalized_symbol=symbol.strip().upper(),
            analysis_type=analysis_type,
            policy_id=policy_id,
            as_of=as_of_value or date.today().isoformat(),
            status="CREATED",
            current_stage="等待 Worker",
            progress=0,
            cancel_requested=False,
            workflow_name=(
                self.technical_workflow_version
                if analysis_type == "technical"
                else self.fundamental_workflow_version
            ),
            workflow_version="v1",
            checkpoint_thread_id=run_id,
            current_node=None,
            runtime_mode=self.runtime_mode,
            created_at=now,
            updated_at=now,
        )
        with self._create_lock:
            with self.session_factory.begin() as session:
                pending = session.scalar(
                    select(func.count(ResearchRun.id)).where(
                        ResearchRun.status.not_in(TERMINAL_STATUSES)
                    )
                ) or 0
                if pending >= self.max_pending_runs:
                    raise QueueFullError("waiting queue is full")
                session.add(run)
                session.flush()
                session.add(
                    self._event(
                        run.run_id,
                        "RUN_CREATED",
                        run.current_stage,
                        "研究任务已创建",
                    )
                )
        return run

    def get_run(self, run_id: str) -> ResearchRun:
        with self.session_factory() as session:
            run = session.scalar(select(ResearchRun).where(ResearchRun.run_id == run_id))
            if run is None:
                raise RunNotFoundError(run_id)
            session.expunge(run)
            return run

    def list_runs(self, limit: int = 50) -> list[ResearchRun]:
        statement: Select[tuple[ResearchRun]] = (
            select(ResearchRun)
            .order_by(ResearchRun.created_at.desc(), ResearchRun.id.desc())
            .limit(limit)
        )
        with self.session_factory() as session:
            runs = list(session.scalars(statement))
            for run in runs:
                session.expunge(run)
            return runs

    def list_events(self, run_id: str) -> list[RunEvent]:
        with self.session_factory() as session:
            events = list(
                session.scalars(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id)
                    .order_by(RunEvent.created_at.asc(), RunEvent.id.asc())
                )
            )
            for event in events:
                session.expunge(event)
            return events

    def claim_next_created_run(self) -> str | None:
        now = utc_now()
        with self.session_factory.begin() as session:
            candidate_id = session.scalar(
                select(ResearchRun.id)
                .where(ResearchRun.status == "CREATED")
                .order_by(ResearchRun.created_at.asc(), ResearchRun.id.asc())
                .limit(1)
            )
            if candidate_id is None:
                return None

            claimed = session.execute(
                update(ResearchRun)
                .where(
                    ResearchRun.id == candidate_id,
                    ResearchRun.status == "CREATED",
                )
                .values(
                    status="RESOLVING_SECURITY",
                    current_stage="解析证券",
                    progress=15,
                    started_at=now,
                    updated_at=now,
                )
                .returning(ResearchRun.run_id)
            ).scalar_one_or_none()
            if claimed is None:
                return None

            session.add(
                self._event(
                    claimed,
                    "STATUS_CHANGED",
                    "解析证券",
                    "Worker 已领取任务，开始解析证券",
                    {"status": "RESOLVING_SECURITY", "progress": 15},
                )
            )
            return claimed

    def next_recoverable_run(self) -> str | None:
        with self.session_factory() as session:
            return session.scalar(
                select(ResearchRun.run_id)
                .where(
                    ResearchRun.status.in_(
                        {
                            "RESOLVING_SECURITY",
                            "ROUTING",
                            "RUNNING",
                            "TECH_RESEARCHING",
                            "KRONOS_ANALYZING",
                            "TECH_ASSEMBLING",
                            "REPORTING",
                            "LEAD_PLANNING",
                            "BUSINESS_RESEARCHING",
                            "INDUSTRY_RESEARCHING",
                            "LEAD_REVIEWING",
                            "FINANCIAL_RESEARCHING",
                            "VALUATION_RESEARCHING",
                            "LEAD_FINAL_REVIEWING",
                            "FUNDAMENTAL_WRITING",
                        }
                    ),
                )
                .order_by(ResearchRun.updated_at.asc(), ResearchRun.id.asc())
                .limit(1)
            )

    def next_stale_completed_fundamental(self) -> str | None:
        from app.fundamental.result_manifest import ResultManifestStore

        with self.session_factory() as session:
            runs = list(
                session.scalars(
                    select(ResearchRun)
                    .where(
                        ResearchRun.status == "COMPLETED",
                        ResearchRun.analysis_type == "fundamental",
                    )
                    .order_by(ResearchRun.updated_at.asc(), ResearchRun.id.asc())
                )
            )
        for run in runs:
            run_id = run.run_id
            directory = self.artifacts_dir / run_id
            if (
                run.report_path
                and Path(run.report_path).name == "fundamental_research_package.md"
                and not (directory / "fundamental_report.md").is_file()
            ):
                return run_id
            if not (directory / "result_manifest.json").is_file():
                if (directory / "lead_final_review.json").is_file() and not (directory / "fundamental_report.md").is_file():
                    return run_id
                continue
            try:
                if ResultManifestStore(directory, run_id, self.fundamental_workflow_version).audit():
                    return run_id
            except (OSError, ValueError):
                self.require_human_review(
                    run_id,
                    "result_manifest",
                    ["result_manifest.json 无法读取"],
                    "结果清单损坏",
                )
                continue
        return None

    def transition_run(
        self,
        run_id: str,
        *,
        status: str,
        stage: str,
        progress: int,
        event_type: str = "STATUS_CHANGED",
        message: str,
        normalized_symbol: str | None = None,
        resolved_symbol: str | None = None,
        security_name: str | None = None,
        data_version: str | None = None,
        report_path: str | None = None,
        error_message: str | None = None,
        current_node: str | None = None,
        event_key: str | None = None,
    ) -> ResearchRun:
        now = utc_now()
        values: dict[str, object] = {
            "status": status,
            "current_stage": stage,
            "progress": progress,
            "updated_at": now,
            "error_message": error_message,
        }
        if normalized_symbol is not None:
            values["normalized_symbol"] = normalized_symbol
        if resolved_symbol is not None:
            values["resolved_symbol"] = resolved_symbol
        if security_name is not None:
            values["security_name"] = security_name
        if data_version is not None:
            values["data_version"] = data_version
        if report_path is not None:
            values["report_path"] = report_path
        if status in TERMINAL_STATUSES:
            values["completed_at"] = now
        if current_node is not None:
            values["current_node"] = current_node

        with self.session_factory.begin() as session:
            run = session.scalar(select(ResearchRun).where(ResearchRun.run_id == run_id))
            if run is None:
                raise RunNotFoundError(run_id)
            for field, value in values.items():
                setattr(run, field, value)
            existing_event = None
            if event_key:
                existing_event = session.scalar(
                    select(RunEvent).where(RunEvent.event_key == event_key)
                )
            if existing_event is None:
                session.add(
                    self._event(
                        run_id,
                        event_type,
                        stage,
                        message,
                        {"status": status, "progress": progress},
                        event_key=event_key,
                    )
                )
        return self.get_run(run_id)

    def request_cancel(self, run_id: str) -> ResearchRun:
        now = utc_now()
        with self.session_factory.begin() as session:
            cancelled_before_claim = session.execute(
                update(ResearchRun)
                .where(
                    ResearchRun.run_id == run_id,
                    ResearchRun.status == "CREATED",
                )
                .values(
                    cancel_requested=True,
                    status="CANCELLED",
                    current_stage="任务已取消",
                    updated_at=now,
                    completed_at=now,
                )
                .returning(ResearchRun.run_id)
            ).scalar_one_or_none()
            if cancelled_before_claim is not None:
                session.add(
                    self._event(
                        run_id,
                        "RUN_CANCELLED",
                        "任务已取消",
                        "任务在 Worker 领取前已取消",
                    )
                )
            else:
                active = session.execute(
                    update(ResearchRun)
                    .where(
                        ResearchRun.run_id == run_id,
                        ResearchRun.status.not_in(TERMINAL_STATUSES),
                    )
                    .values(cancel_requested=True, updated_at=now)
                    .returning(ResearchRun.current_stage)
                ).scalar_one_or_none()
                if active is not None:
                    session.add(
                        self._event(
                            run_id,
                            "CANCEL_REQUESTED",
                            active,
                            "已收到取消请求，将在下一阶段前停止",
                        )
                    )
                else:
                    run = session.scalar(
                        select(ResearchRun).where(ResearchRun.run_id == run_id)
                    )
                    if run is None:
                        raise RunNotFoundError(run_id)
                    raise RunConflictError(run.status)
        return self.get_run(run_id)

    def complete_run(self, run_id: str, report_path: Path) -> bool:
        """Complete a reporting run unless a concurrent cancellation won."""
        now = utc_now()
        with self.session_factory.begin() as session:
            completed = session.execute(
                update(ResearchRun)
                .where(
                    ResearchRun.run_id == run_id,
                    ResearchRun.status == "REPORTING",
                    ResearchRun.cancel_requested.is_(False),
                )
                .values(
                    status="COMPLETED",
                    current_stage="任务完成",
                    progress=100,
                    report_path=str(report_path),
                    error_message=None,
                    updated_at=now,
                    completed_at=now,
                )
                .returning(ResearchRun.run_id)
            ).scalar_one_or_none()
            if completed is not None:
                session.add(
                    self._event(
                        run_id,
                        "RUN_COMPLETED",
                        "任务完成",
                        "研究报告已生成",
                        {"status": "COMPLETED", "progress": 100},
                    )
                )
                return True

            run = session.scalar(select(ResearchRun).where(ResearchRun.run_id == run_id))
            if run is None:
                raise RunNotFoundError(run_id)
            if run.status == "CANCELLED":
                return False
            if run.cancel_requested and run.status not in TERMINAL_STATUSES:
                run.status = "CANCELLED"
                run.current_stage = "任务已取消"
                run.updated_at = now
                run.completed_at = now
                session.add(
                    self._event(
                        run_id,
                        "RUN_CANCELLED",
                        "任务已取消",
                        "报告生成期间收到取消请求，任务未完成",
                    )
                )
                return False
            raise RunConflictError(run.status)

    def cancel_if_requested(self, run_id: str) -> bool:
        run = self.get_run(run_id)
        if not run.cancel_requested:
            return False
        if run.status != "CANCELLED":
            self.transition_run(
                run_id,
                status="CANCELLED",
                stage="任务已取消",
                progress=run.progress,
                event_type="RUN_CANCELLED",
                message="Worker 已停止任务",
            )
        return True

    def require_human_review(
        self, run_id: str, node: str, missing_information: list[str], reason: str
    ) -> ResearchRun:
        message = reason
        if missing_information:
            message = f"{reason}：{'；'.join(missing_information)}"
        now = utc_now()
        with self.session_factory.begin() as session:
            transitioned = session.execute(
                update(ResearchRun)
                .where(
                    ResearchRun.run_id == run_id,
                    ResearchRun.status.not_in({"FAILED", "CANCELLED", "HUMAN_REVIEW_REQUIRED"}),
                    ResearchRun.cancel_requested.is_(False),
                )
                .values(
                    status="HUMAN_REVIEW_REQUIRED",
                    current_stage="需要人工复核",
                    progress=95,
                    error_message=message,
                    current_node=node,
                    updated_at=now,
                    completed_at=now,
                )
                .returning(ResearchRun.run_id)
            ).scalar_one_or_none()
            if transitioned is not None:
                session.add(self._event(run_id, "HUMAN_REVIEW_REQUIRED", "需要人工复核", message))
            else:
                run = session.scalar(select(ResearchRun).where(ResearchRun.run_id == run_id))
                if run is None:
                    raise RunNotFoundError(run_id)
                if run.cancel_requested and run.status not in TERMINAL_STATUSES:
                    run.status = "CANCELLED"
                    run.current_stage = "任务已取消"
                    run.current_node = node
                    run.updated_at = now
                    run.completed_at = now
                    session.add(self._event(run_id, "RUN_CANCELLED", "任务已取消", "取消请求优先于人工复核终态"))
                elif run.status not in {"HUMAN_REVIEW_REQUIRED", "CANCELLED"}:
                    raise RunConflictError(run.status)
        return self.get_run(run_id)

    def reopen_for_stale_rebuild(self, run_id: str, node: str) -> ResearchRun:
        now = utc_now()
        with self.session_factory.begin() as session:
            run = session.scalar(select(ResearchRun).where(ResearchRun.run_id == run_id))
            if run is None:
                raise RunNotFoundError(run_id)
            if run.status != "COMPLETED" or run.analysis_type != "fundamental":
                raise RunConflictError(run.status)
            run.status = "RUNNING"
            run.current_stage = "重建 STALE 结果"
            run.progress = min(run.progress, 90)
            run.report_path = None
            run.completed_at = None
            run.updated_at = now
            run.current_node = node
            session.add(self._event(run_id, "STALE_REBUILD_STARTED", run.current_stage, f"从 {node} 重建失效结果"))
        return self.get_run(run_id)

    def get_report(self, run_id: str) -> tuple[ResearchRun, str, str]:
        run = self.get_run(run_id)
        if run.status != "COMPLETED" or not run.report_path:
            raise ReportNotReadyError(run_id)

        if run.analysis_type == "fundamental":
            from app.fundamental.result_manifest import ResultManifestStore

            directory = self.artifacts_dir / run.run_id
            path = Path(run.report_path)
            # Legacy phase-four completed runs keep their research package
            # readable until the Worker upgrades them to fundamental_v1. A
            # phase-five Manifest may already exist because it is now written
            # incrementally by upstream nodes.
            if path.name == "fundamental_research_package.md" and path.is_file():
                return self._render_report(run, path)
            if not (directory / "result_manifest.json").is_file():
                raise ReportNotReadyError(run_id)
            store = ResultManifestStore(directory, run.run_id, self.fundamental_workflow_version)
            try:
                stale = store.audit(persist=False)
                report_entry = store.load().results.get("fundamental_report")
            except (OSError, ValueError) as exc:
                raise ReportNotReadyError(run_id) from exc
            if stale or report_entry is None or report_entry.status != "current":
                raise ReportNotReadyError(run_id)

        path = Path(run.report_path)
        if not path.is_file():
            raise ReportFileMissingError(run_id)
        return self._render_report(run, path)

    def _render_report(self, run: ResearchRun, path: Path) -> tuple[ResearchRun, str, str]:
        if path.suffix.lower() == ".html":
            document = path.read_text(encoding="utf-8")
            start = document.find("<main")
            end = document.rfind("</main>")
            if start < 0 or end < start:
                raise ReportFileMissingError(run.run_id)
            body = document[start : end + len("</main>")]
            markdown_path = path.with_suffix(".md")
            markdown_text = markdown_path.read_text(encoding="utf-8") if markdown_path.is_file() else ""
            safe_html = bleach.clean(
                body,
                tags={"main", "header", "section", "article", "aside", "footer", "details", "summary", "canvas", "div", "span", "small", "b", "h1", "h2", "h3", "p", "ul", "ol", "li", "strong", "em", "code", "pre", "blockquote", "hr", "br", "table", "thead", "tbody", "tr", "th", "td", "a"},
                attributes={"*": ["class", "id", "data-report-visuals", "data-chart", "aria-label"], "a": ["href", "title"]},
                protocols={"http", "https"},
                strip=True,
            )
            return run, markdown_text, safe_html
        markdown_text = path.read_text(encoding="utf-8")
        rendered = markdown.markdown(
            markdown_text,
            extensions=["extra", "sane_lists"],
            output_format="html",
        )
        safe_html = bleach.clean(
            rendered,
            tags={
                "h1",
                "h2",
                "h3",
                "h4",
                "p",
                "ul",
                "ol",
                "li",
                "strong",
                "em",
                "code",
                "pre",
                "blockquote",
                "hr",
                "br",
                "table",
                "thead",
                "tbody",
                "tr",
                "th",
                "td",
                "a",
                "img",
            },
            attributes={"a": ["href", "title"], "img": ["src", "alt", "title"]},
            protocols={"http", "https"},
            strip=True,
        )
        return run, markdown_text, safe_html

    @staticmethod
    def _event(
        run_id: str,
        event_type: str,
        stage: str,
        message: str,
        payload: dict[str, object] | None = None,
        event_key: str | None = None,
    ) -> RunEvent:
        return RunEvent(
            run_id=run_id,
            event_type=event_type,
            stage=stage,
            message=message,
            payload_json=json.dumps(payload, ensure_ascii=False) if payload else None,
            event_key=event_key,
            created_at=utc_now(),
        )
