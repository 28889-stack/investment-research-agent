from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.report_export import build_export_document, content_disposition, export_filename
from app.database import create_db_engine, create_session_factory, init_database
from app.run_service import (
    ReportFileMissingError,
    ReportNotReadyError,
    RunConflictError,
    RunNotFoundError,
    QueueFullError,
    RunService,
)
from app.schemas import (
    AgentExecutionSummary,
    CancelResponse,
    ReportResponse,
    RunCreate,
    RunCreated,
    RunDetail,
    RunEventResponse,
    RunSummary,
    RuntimeHealth,
)
from app.runtime.profiles import ProfileLoader
from app.runtime.repository import RuntimeRepository
from app.runtime.output_validator import output_model_for_schema
from app.technical.schemas import KronosResult, TechnicalIndicators
from app.fundamental.schemas import AssumptionStore, EvidenceCollection, FundamentalWriterOutput, LeadFinalReviewOutput
from app.fundamental.result_manifest import ResultManifestStore
from app.runtime.tool_registry import ToolRegistry, build_runtime_tools
from app.tools.technical_tools import build_technical_tools
from app.tools.fundamental_tools import build_fundamental_tools
from app.readiness import _sqlite_ready, preflight_live_pi, readiness_report, validate_startup_config, workflow_build_status
from app.logging_config import configure_logging


STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    validate_startup_config(settings)
    preflight_live_pi(settings)
    configure_logging(settings, "web")
    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
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
    runtime_repository = RuntimeRepository(session_factory)
    profile_loader = ProfileLoader(settings.agent_profile_dir)
    tool_registry = ToolRegistry(runtime_repository)
    build_runtime_tools(tool_registry, service, settings.tool_default_timeout)
    build_technical_tools(
        tool_registry, service, runtime_repository, settings
    )
    build_fundamental_tools(
        tool_registry, service, runtime_repository, settings
    )
    for profile_id in (
        "full_runtime_smoke",
        "constrained_runtime_smoke",
        settings.technical_research_profile,
        settings.technical_assembly_profile,
        settings.fundamental_lead_profile,
        settings.business_research_profile,
        settings.industry_research_profile,
        settings.deep_research_profile,
        settings.financial_research_profile,
        settings.valuation_research_profile,
        settings.lead_synthesis_profile,
        settings.writer_planning_profile,
        settings.fundamental_writer_profile,
        settings.final_synthesis_profile,
        settings.chart_data_extractor_profile,
    ):
        tool_registry.validate_profile_permissions(profile_loader.load(profile_id))
    workflow_status = workflow_build_status(settings, session_factory)

    app = FastAPI(title="金融投研 Agent", version="0.1.0")
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.run_service = service
    app.state.runtime_repository = runtime_repository
    app.state.profile_loader = profile_loader
    app.state.tool_registry = tool_registry

    @app.middleware("http")
    async def structured_request_log(request, call_next):
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "HTTP request failed",
                extra={"component": "web", "node": request.url.path, "duration_ms": int((time.monotonic() - started) * 1000), "status": "FAILED", "error_type": type(exc).__name__},
            )
            raise
        logging.getLogger(__name__).info(
            "HTTP request completed",
            extra={"component": "web", "node": request.url.path, "duration_ms": int((time.monotonic() - started) * 1000), "status": str(response.status_code)},
        )
        return response
    app.mount(
        "/static",
        StaticFiles(directory=STATIC_DIR, check_dir=False),
        name="static",
    )

    @app.exception_handler(SQLAlchemyError)
    async def database_error_handler(_request, exc: SQLAlchemyError):
        logging.getLogger(__name__).error("数据库操作失败: %s", type(exc).__name__)
        return _json_error(500, "数据库操作失败，请稍后重试")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/readiness")
    def readiness():
        status_code, payload = readiness_report(
            settings,
            profiles_ready=bool(profile_loader.list_profiles()),
            tools_ready=tool_registry.tool_count > 0,
            workflow_status=workflow_status,
        )
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=status_code, content=payload)

    @app.get("/api/runtime/health", response_model=RuntimeHealth)
    def runtime_health() -> RuntimeHealth:
        return RuntimeHealth(
            runtime_mode=settings.pi_runtime_mode,
            bridge_status=("ready" if settings.pi_bridge_entry.is_file() else "not_built"),
            profiles_loaded=len(profile_loader.list_profiles()),
            tools_registered=tool_registry.tool_count,
            checkpoint_status=(
                "ready"
                if _sqlite_ready(settings.checkpoint_database_path, {"checkpoints", "writes"})
                else "unavailable"
            ),
        )

    @app.post("/api/runs", response_model=RunCreated, status_code=201)
    def create_run(payload: RunCreate) -> RunCreated:
        try:
            run = service.create_run(
                symbol=payload.symbol,
                analysis_type=payload.analysis_type,
                policy_id=payload.policy_id,
                as_of=payload.as_of,
            )
        except QueueFullError as exc:
            raise HTTPException(status_code=429, detail="等待队列已满，请稍后重试") from exc
        return RunCreated(run_id=run.run_id, status=run.status)

    @app.get("/api/runs", response_model=list[RunSummary])
    def list_runs(limit: int = Query(default=50, ge=1, le=50)) -> list[RunSummary]:
        return [
            _run_summary(run, settings, runtime_repository.usage_summary(run.run_id))
            for run in service.list_runs(limit)
        ]

    @app.get("/api/runs/{run_id}", response_model=RunDetail)
    def get_run(run_id: str) -> RunDetail:
        try:
            run = service.get_run(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="研究任务不存在") from exc
        events = [RunEventResponse.model_validate(event) for event in service.list_events(run_id)]
        return RunDetail(
            **_run_summary(
                run, settings, runtime_repository.usage_summary(run.run_id)
            ).model_dump(),
            started_at=run.started_at,
            completed_at=run.completed_at,
            events=events,
        )

    @app.get(
        "/api/runs/{run_id}/executions",
        response_model=list[AgentExecutionSummary],
    )
    def list_agent_executions(run_id: str) -> list[AgentExecutionSummary]:
        try:
            service.get_run(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="研究任务不存在") from exc
        summaries: list[AgentExecutionSummary] = []
        for execution in runtime_repository.list_executions(run_id):
            validated_summary = None
            if execution.validated_output_json:
                try:
                    profile = profile_loader.load(execution.profile_id)
                    schema_name = {
                        "lead_planning": "lead_plan_output",
                        "business_research": "specialist_research_output",
                        "industry_research": "specialist_research_output",
                        "deep_research": "specialist_research_output",
                        "lead_review": "lead_review_output",
                        "financial_research": "financial_research_draft",
                        "valuation_research": "valuation_research_output",
                        "lead_final_review": "lead_final_review_output",
                        "fundamental_writer": "fundamental_writer_output",
                    }.get(execution.node_name, profile.output_schema)
                    output = output_model_for_schema(schema_name).model_validate_json(
                        execution.validated_output_json
                    )
                    validated_summary = getattr(output, "summary", None) or getattr(
                        output, "trend", None
                    ) or getattr(output, "thesis", None) or getattr(
                        output, "research_thesis", None
                    )
                    if not validated_summary and getattr(output, "key_findings", None):
                        validated_summary = "；".join(output.key_findings)
                except ValueError:
                    validated_summary = None
            summaries.append(
                AgentExecutionSummary(
                    execution_id=execution.execution_id,
                    node_name=execution.node_name,
                    profile_id=execution.profile_id,
                    profile_version=execution.profile_version,
                    status=execution.status,
                    tool_call_count=execution.tool_call_count,
                    started_at=execution.started_at,
                    completed_at=execution.completed_at,
                    error_type=execution.error_type,
                    error_message=execution.error_message,
                    validated_summary=validated_summary,
                )
            )
        return summaries

    @app.post("/api/runs/{run_id}/cancel", response_model=CancelResponse)
    def cancel_run(run_id: str) -> CancelResponse:
        try:
            run = service.request_cancel(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="研究任务不存在") from exc
        except RunConflictError as exc:
            raise HTTPException(status_code=409, detail="该任务已结束，无法取消") from exc
        return CancelResponse(
            run_id=run.run_id,
            status=run.status,
            cancel_requested=run.cancel_requested,
        )

    @app.get("/api/runs/{run_id}/report", response_model=ReportResponse)
    def get_report(run_id: str) -> ReportResponse:
        try:
            run, markdown_text, safe_html = service.get_report(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="研究任务不存在") from exc
        except ReportNotReadyError as exc:
            raise HTTPException(status_code=409, detail="研究报告尚未生成") from exc
        except ReportFileMissingError as exc:
            raise HTTPException(status_code=404, detail="研究报告文件不存在") from exc
        indicator_version = None
        kronos_model_version = None
        chart_url = None
        evidence_count = None
        assumption_count = None
        ready_for_writer = None
        missing_information: list[str] = []
        writer_status = None
        report_status = None
        result_version = None
        stale_results: list[str] = []
        if run.analysis_type == "technical":
            artifact_dir = settings.artifacts_dir / run.run_id
            try:
                indicator_version = TechnicalIndicators.model_validate_json(
                    (artifact_dir / "technical_indicators.json").read_text(encoding="utf-8")
                ).script_version
                kronos_model_version = KronosResult.model_validate_json(
                    (artifact_dir / "kronos_result.json").read_text(encoding="utf-8")
                ).model_version
                if (artifact_dir / "technical_chart.png").is_file():
                    chart_url = f"/api/runs/{run.run_id}/artifacts/technical_chart.png"
            except (OSError, ValueError):
                pass
        elif run.analysis_type == "fundamental":
            artifact_dir = settings.artifacts_dir / run.run_id
            try:
                evidence_count = len(
                    EvidenceCollection.model_validate_json(
                        (artifact_dir / "evidence.json").read_text(encoding="utf-8")
                    ).items
                )
                assumption_count = len(
                    AssumptionStore.model_validate_json(
                        (artifact_dir / "assumptions.json").read_text(encoding="utf-8")
                    ).items
                )
                final_review = LeadFinalReviewOutput.model_validate_json(
                    (artifact_dir / "lead_final_review.json").read_text(encoding="utf-8")
                )
                ready_for_writer = final_review.ready_for_writer
                missing_information = final_review.missing_information
                metadata = _fundamental_result_metadata(run, settings)
                writer_status = metadata["writer_status"]
                report_status = metadata["report_status"]
                result_version = metadata["result_version"]
                stale_results = metadata["stale_results"]
            except (OSError, ValueError):
                pass
        return ReportResponse(
            run_id=run.run_id,
            input_symbol=run.input_symbol,
            normalized_symbol=run.normalized_symbol,
            analysis_type=run.analysis_type,
            policy_id=run.policy_id,
            as_of=run.as_of,
            markdown=markdown_text,
            html=safe_html,
            resolved_symbol=run.resolved_symbol,
            security_name=run.security_name,
            data_version=run.data_version,
            indicator_version=indicator_version,
            kronos_model_version=kronos_model_version,
            chart_url=chart_url,
            evidence_count=evidence_count,
            assumption_count=assumption_count,
            ready_for_writer=ready_for_writer,
            missing_information=missing_information,
            writer_status=writer_status,
            report_status=report_status,
            result_version=result_version,
            stale_results=stale_results,
        )

    @app.get("/api/runs/{run_id}/report/export")
    def export_report(run_id: str) -> Response:
        try:
            run, _markdown_text, safe_html = service.get_report(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="研究任务不存在") from exc
        except ReportNotReadyError as exc:
            raise HTTPException(status_code=409, detail="研究报告尚未生成") from exc
        except ReportFileMissingError as exc:
            raise HTTPException(status_code=404, detail="研究报告文件不存在") from exc
        expected_html = {
            "fundamental": "fundamental_report.html",
            "technical": "technical_report.html",
        }.get(run.analysis_type)
        if expected_html:
            report_path = Path(run.report_path or "").resolve()
            artifact_dir = (settings.artifacts_dir / run.run_id).resolve()
            if report_path.parent == artifact_dir and report_path.name == expected_html:
                document = report_path.read_text(encoding="utf-8")
                return Response(
                    content=document,
                    media_type="text/html; charset=utf-8",
                    headers={
                        "Content-Disposition": content_disposition(export_filename(run)),
                        "X-Content-Type-Options": "nosniff",
                    },
                )
            if run.analysis_type == "fundamental":
                raise HTTPException(status_code=404, detail="基本面 HTML 报告文件不存在")
        # Preserve a safe export path for legacy technical Markdown artifacts.
        document = build_export_document(run, safe_html, None)
        return Response(
            content=document,
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Disposition": content_disposition(export_filename(run)),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/api/runs/{run_id}/artifacts/technical_chart.png")
    def get_technical_chart(run_id: str) -> FileResponse:
        try:
            run = service.get_run(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail="研究任务不存在") from exc
        if run.analysis_type != "technical":
            raise HTTPException(status_code=404, detail="技术图表不存在")
        run_dir = (settings.artifacts_dir / run.run_id).resolve()
        chart_path = (run_dir / "technical_chart.png").resolve()
        if chart_path.parent != run_dir or not chart_path.is_file():
            raise HTTPException(status_code=404, detail="技术图表不存在")
        return FileResponse(chart_path, media_type="image/png")

    @app.get("/", include_in_schema=False)
    def index_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/runs/{run_id}", include_in_schema=False)
    def run_page(run_id: str) -> FileResponse:
        return FileResponse(STATIC_DIR / "run.html")

    @app.get("/runs/{run_id}/report", include_in_schema=False)
    def report_page(run_id: str) -> FileResponse:
        return FileResponse(STATIC_DIR / "report.html")

    return app


def _run_summary(run, settings: Settings | None = None, usage: dict | None = None) -> RunSummary:
    metadata = _fundamental_result_metadata(run, settings) if settings and run.analysis_type == "fundamental" else {}
    return RunSummary(
        run_id=run.run_id,
        input_symbol=run.input_symbol,
        normalized_symbol=run.normalized_symbol,
        resolved_symbol=run.resolved_symbol,
        security_name=run.security_name,
        data_version=run.data_version,
        analysis_type=run.analysis_type,
        policy_id=run.policy_id,
        as_of=run.as_of,
        status=run.status,
        current_stage=run.current_stage,
        progress=run.progress,
        cancel_requested=run.cancel_requested,
        error_message=run.error_message,
        report_ready=run.status == "COMPLETED" and bool(run.report_path) and metadata.get("report_status", "current") == "current",
        created_at=run.created_at,
        updated_at=run.updated_at,
        current_node=run.current_node,
        runtime_mode=run.runtime_mode,
        checkpoint_enabled=bool(run.checkpoint_thread_id),
        usage=usage or {},
        **metadata,
    )


def _fundamental_result_metadata(run, settings: Settings) -> dict:
    directory = settings.artifacts_dir / run.run_id
    ready_for_writer = None
    missing_information: list[str] = []
    writer_status = "not_started"
    report_status = "not_generated"
    result_version = None
    stale_results: list[str] = []
    try:
        final = LeadFinalReviewOutput.model_validate_json((directory / "lead_final_review.json").read_text(encoding="utf-8"))
        ready_for_writer = final.ready_for_writer
        missing_information = list(final.missing_information)
    except (OSError, ValueError):
        pass
    try:
        writer = FundamentalWriterOutput.model_validate_json((directory / "fundamental_writer.json").read_text(encoding="utf-8"))
        writer_status = writer.status
        if writer.status == "needs_more_research":
            missing_information = list(writer.missing_information)
    except (OSError, ValueError):
        pass
    try:
        manifest_path = directory / "result_manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(manifest_path)
        store = ResultManifestStore(directory, run.run_id, settings.fundamental_workflow_version)
        stale_results = store.audit(persist=False)
        manifest = store.load()
        writer_entry = manifest.results.get("fundamental_writer")
        report_entry = manifest.results.get("fundamental_report")
        if writer_entry and writer_entry.status != "current":
            writer_status = writer_entry.status
        if report_entry:
            report_status = report_entry.status
            result_version = report_entry.version
        if "fundamental_report" in stale_results:
            report_status = "stale"
    except (OSError, ValueError):
        pass
    return {
        "writer_status": writer_status,
        "report_status": report_status,
        "ready_for_writer": ready_for_writer,
        "result_version": result_version,
        "stale_results": stale_results,
        "missing_information": missing_information,
    }


def _json_error(status_code: int, detail: str):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status_code, content={"detail": detail})


app = create_app()


def run_server(settings: Settings | None = None) -> None:
    import uvicorn

    settings = settings or app.state.settings
    application = app if settings is app.state.settings else create_app(settings)
    uvicorn.run(
        application,
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run_server()
