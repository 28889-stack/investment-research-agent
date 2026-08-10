from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.fundamental.data import get_financial_data, get_market_snapshot
from app.fundamental.financials import calculate_financial_metrics
from app.fundamental.research_package import generate_research_package, research_package_is_current
from app.fundamental.report import generate_fundamental_report
from app.fundamental.result_manifest import (
    ManifestInputChangedError,
    RESULT_ORDER,
    ResultManifestStore,
    sha256_file,
)
from app.fundamental.schemas import (
    AssumptionItem,
    AssumptionStore,
    CompanyProfile,
    EvidenceCollection,
    FinancialData,
    FinancialMetrics,
    FinancialResearchDraft,
    FinancialResearchOutput,
    FundamentalWriterOutput,
    LeadFinalReviewOutput,
    LeadSynthesisOutput,
    LeadPlanOutput,
    LeadReviewOutput,
    SpecialistResearchOutput,
    RetrievalPackage,
    RetrievalPackageItem,
    ValuationResearchOutput,
    ValuationResult,
    WriterPlanOutput,
    validate_references,
)
from app.fundamental.valuation import calculate_valuation
from app.fundamental.writer import WRITER_CONTEXT_REFS, validate_writer_output
from app.run_service import RunService
from app.runtime.context_loader import ContextLoader
from app.runtime.exceptions import AgentTimeoutError, BridgeCrashedError
from app.runtime.output_validator import OutputValidator, output_model_for_schema
from app.runtime.pi_adapter import PiAgentAdapter
from app.runtime.pi_client import BridgePiClient, PiClient
from app.runtime.profiles import ProfileLoader
from app.runtime.repository import RuntimeRepository
from app.runtime.tool_registry import ToolRegistry
from app.technical.market_data import resolve_security
from app.tools.fundamental_tools import build_fundamental_tools


LOGGER = logging.getLogger(__name__)


class FundamentalGraphState(TypedDict, total=False):
    run_id: str
    resolved_symbol: str
    security_name: str
    lead_plan_path: str
    business_path: str
    industry_path: str
    lead_review_path: str
    deep_research_path: str
    financial_data_path: str
    financial_metrics_path: str
    financial_research_path: str
    valuation_result_path: str
    valuation_research_path: str
    lead_final_review_path: str
    retrieval_package_path: str
    lead_synthesis_path: str
    writer_plan_path: str
    evidence_path: str
    assumptions_path: str
    report_path: str
    writer_path: str
    manifest_path: str
    current_node: str
    error_message: str | None


NODE_ORDER = [
    "resolve_security",
    "lead_planning",
    "business_research",
    "industry_research",
    "lead_review",
    "deep_research",
    "assemble_retrieval_package",
    "financial_research",
    "valuation_research",
    "lead_final_review",
    "lead_synthesis",
    "writer_planning",
    "fundamental_writer",
    "write_fundamental_report",
]


class FundamentalCalculationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FundamentalWorkflow:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        *,
        pi_client: PiClient | None = None,
        interrupt_after: list[str] | None = None,
    ) -> None:
        self.settings = settings
        self.service = RunService(
            session_factory,
            settings.artifacts_dir,
            settings.pi_runtime_mode,
            settings.technical_workflow_version,
            settings.fundamental_workflow_version,
        )
        self.repository = RuntimeRepository(session_factory)
        self.profile_loader = ProfileLoader(settings.agent_profile_dir)
        self.tool_registry = ToolRegistry(self.repository)
        build_fundamental_tools(self.tool_registry, self.service, self.repository, settings)
        for profile_id in (
            settings.fundamental_lead_profile,
            settings.business_research_profile,
            settings.industry_research_profile,
            settings.deep_research_profile,
            settings.financial_research_profile,
            settings.valuation_research_profile,
            settings.lead_synthesis_profile,
            settings.writer_planning_profile,
            settings.fundamental_writer_profile,
        ):
            self.tool_registry.validate_profile_permissions(self.profile_loader.load(profile_id))
        self._owns_client = pi_client is None
        self.pi_client = pi_client or BridgePiClient(
            command=settings.pi_bridge_command,
            entrypoint=settings.pi_bridge_entry,
            runtime_mode=settings.pi_runtime_mode,
            start_timeout=settings.pi_bridge_start_timeout,
            request_timeout=settings.pi_request_timeout,
            max_restarts=settings.pi_bridge_max_restarts,
            model_provider=settings.pi_model_provider or None,
            model_name=settings.pi_model or None,
            api_key_env_name=settings.pi_api_key_env_name or None,
        )
        self.adapter = PiAgentAdapter(
            client=self.pi_client,
            context_loader=ContextLoader(
                self.service,
                self.repository,
                max_context_chars=settings.max_agent_context_chars,
                tool_registry=self.tool_registry,
            ),
            tool_registry=self.tool_registry,
            repository=self.repository,
            output_validator=OutputValidator(settings.max_agent_output_chars),
            runtime_mode=settings.pi_runtime_mode,
            model_provider=settings.pi_model_provider or None,
            model_name=settings.pi_model or None,
            repair_attempts=settings.output_repair_attempts,
            max_tool_calls_per_node=settings.max_tool_calls_per_node,
        )
        checkpoint_path = Path(settings.checkpoint_database_path)
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self._checkpoint_connection = sqlite3.connect(checkpoint_path, check_same_thread=False)
        self.checkpointer = SqliteSaver(self._checkpoint_connection)
        self.checkpointer.setup()
        self.graph = self._build_graph(interrupt_after)

    def _build_graph(self, interrupt_after: list[str] | None):
        builder = StateGraph(FundamentalGraphState)
        handlers = self._handlers()
        for name, handler in zip(NODE_ORDER, handlers, strict=True):
            builder.add_node(name, handler)
        builder.add_edge(START, NODE_ORDER[0])
        for current, following in zip(NODE_ORDER, NODE_ORDER[1:]):
            builder.add_conditional_edges(
                current,
                lambda state, following=following: "end" if state.get("error_message") else following,
                {following: following, "end": END},
            )
        builder.add_edge(NODE_ORDER[-1], END)
        return builder.compile(checkpointer=self.checkpointer, interrupt_after=interrupt_after)

    def _handlers(self):
        return [
            self._resolve_security,
            self._lead_planning,
            self._business_research,
            self._industry_research,
            self._lead_review,
            self._deep_research,
            self._assemble_retrieval_package,
            self._financial_research,
            self._valuation_research,
            self._lead_final_review,
            self._lead_synthesis,
            self._writer_planning,
            self._fundamental_writer,
            self._write_fundamental_report,
        ]

    def run(self, run_id: str) -> FundamentalGraphState:
        started = time.monotonic()
        try:
            result = self._run_graph(run_id)
        except Exception as exc:
            LOGGER.warning("Fundamental workflow failed", extra={"component": "fundamental", "run_id": run_id, "workflow": self.settings.fundamental_workflow_version, "duration_ms": int((time.monotonic() - started) * 1000), "status": "FAILED", "error_type": type(exc).__name__})
            raise
        persisted_status = self.service.get_run(run_id).status
        log = LOGGER.info if persisted_status == "COMPLETED" else LOGGER.warning
        log(
            "Fundamental workflow completed" if persisted_status == "COMPLETED" else "Fundamental workflow stopped",
            extra={"component": "fundamental", "run_id": run_id, "workflow": self.settings.fundamental_workflow_version, "duration_ms": int((time.monotonic() - started) * 1000), "status": persisted_status, "error_type": result.get("error_message")},
        )
        return result

    def _run_graph(self, run_id: str) -> FundamentalGraphState:
        run = self.service.get_run(run_id)
        config = {"configurable": {"thread_id": run_id}}
        snapshot = self.graph.get_state(config)
        restart_index = self._legacy_upgrade_index(run)
        if restart_index is None:
            restart_index = self._completed_stale_index(run)
        refreshed_run = self.service.get_run(run_id)
        if restart_index is not None and refreshed_run.status == "COMPLETED":
            self.service.reopen_for_stale_rebuild(run_id, NODE_ORDER[restart_index])
            self._discard_from(run_id, restart_index)
            base_state = dict(snapshot.values) if snapshot.values else {
                "run_id": run_id,
                "resolved_symbol": run.resolved_symbol or "",
                "security_name": run.security_name or "",
                "current_node": NODE_ORDER[restart_index - 1],
            }
            self.graph.update_state(
                config,
                {**base_state, "run_id": run_id, "error_message": None},
                as_node=NODE_ORDER[restart_index - 1],
            )
            result = self.graph.invoke(None, config)
            return FundamentalGraphState(**result)
        if refreshed_run.status == "HUMAN_REVIEW_REQUIRED":
            return FundamentalGraphState(
                **{**dict(snapshot.values), "run_id": run_id, "error_message": "HUMAN_REVIEW_REQUIRED"}
            )
        if snapshot.values and snapshot.next:
            preflight = self._recovery_preflight(run, snapshot.next[0])
            if preflight:
                return FundamentalGraphState(**{**snapshot.values, **preflight})
            result = self.graph.invoke(None, config)
        elif snapshot.values:
            result = snapshot.values
        else:
            result = self.graph.invoke({"run_id": run_id, "current_node": "", "error_message": None}, config)
        return FundamentalGraphState(**result)

    def shutdown(self) -> None:
        if self._owns_client:
            self.pi_client.shutdown()
        self._checkpoint_connection.close()

    def _resolve_security(self, state: FundamentalGraphState) -> FundamentalGraphState:
        node = "resolve_security"
        if self._cancelled(state["run_id"], node):
            return {"current_node": node, "error_message": "CANCELLED"}
        try:
            self._enter(state["run_id"], node, "RESOLVING_SECURITY", "证券解析", 8)
            run = self.service.get_run(state["run_id"])
            resolved = resolve_security(run.resolved_symbol or run.input_symbol, self.settings)
            name = run.security_name or resolved.security_name
            self.service.transition_run(
                run.run_id,
                status="RESOLVING_SECURITY",
                stage="证券解析",
                progress=12,
                event_type="SECURITY_RESOLVED",
                message="证券已解析为标准 A 股代码",
                normalized_symbol=resolved.symbol,
                resolved_symbol=resolved.symbol,
                security_name=name,
                current_node=node,
                event_key=f"{run.run_id}:{node}:completed:1",
            )
            return {"current_node": node, "resolved_symbol": resolved.symbol, "security_name": name, "error_message": None}
        except Exception as exc:
            return self._fail(state["run_id"], node, exc)

    def _lead_planning(self, state: FundamentalGraphState) -> FundamentalGraphState:
        return self._standard_agent_node(
            state,
            node="lead_planning",
            status="LEAD_PLANNING",
            stage="Lead 规划",
            progress=20,
            profile_id=self.settings.fundamental_lead_profile,
            schema_name="lead_plan_output",
            model=LeadPlanOutput,
            filename="lead_plan.json",
            task="读取公司资料，按披露/研报/Web 三方向最多各发一次聚合检索，避免同类冗余。search 返回的是元数据+摘要，先按摘要筛选，只对要引用的来源调用 read_research_source 读正文。首轮若已回答核心子问题即早停输出 lead_plan_output，最多再发 1 次互补主题检索。规划阶段工具预算上限 10 次，深度取证留给后续节点，绝不超预算调用。",
            context_refs=[],
            required_tools={"get_company_profile", "search_research_sources", "read_research_source"},
        )

    def _business_research(self, state: FundamentalGraphState) -> FundamentalGraphState:
        return self._standard_agent_node(
            state, node="business_research", status="BUSINESS_RESEARCHING", stage="公司业务研究", progress=32,
            profile_id=self.settings.business_research_profile, schema_name="specialist_research_output", model=SpecialistResearchOutput,
            filename="business_research.json", task="根据 Lead 范围，按披露/研报/Web 三方向最多各发一次聚合检索，避免同类冗余。search 返回的是元数据+摘要，先按摘要筛选，只对要引用的来源调用 read_research_source 读正文。检索成功后立即读取来源生成 Evidence，并在 findings 中引用实际返回的 Evidence ID。当 sub-questions 已全部回答且已覆盖 ≥2 类来源（announcement/research_report/news/web）时立即早停输出 specialist_research_output；仅当首轮未覆盖目标方向或核心子问题未答时，才再发 1 次互补主题聚合检索并读取来源。工具预算上限 10 次，预算耗尽前必须停止调用并立即输出 JSON。",
            context_refs=["artifact:company_profile", "artifact:lead_plan"],
            required_tools={"get_company_profile", "search_research_sources", "read_research_source"},
        )

    def _industry_research(self, state: FundamentalGraphState) -> FundamentalGraphState:
        return self._standard_agent_node(
            state, node="industry_research", status="INDUSTRY_RESEARCHING", stage="行业研究", progress=44,
            profile_id=self.settings.industry_research_profile, schema_name="specialist_research_output", model=SpecialistResearchOutput,
            filename="industry_research.json", task="根据 Lead 范围，按披露/研报/Web 三方向最多各发一次聚合检索，避免同类冗余。search 返回的是元数据+摘要，先按摘要筛选，只对要引用的来源调用 read_research_source 读正文。检索成功后立即读取来源生成 Evidence，并在 findings 中引用实际返回的 Evidence ID。当 sub-questions 已全部回答且已覆盖 ≥2 类来源（announcement/research_report/news/web）时立即早停输出 specialist_research_output；仅当首轮未覆盖目标方向或核心子问题未答时，才再发 1 次互补主题聚合检索并读取来源。工具预算上限 10 次，预算耗尽前必须停止调用并立即输出 JSON。",
            context_refs=["artifact:company_profile", "artifact:lead_plan"],
            required_tools={"search_research_sources", "read_research_source"},
        )

    def _lead_review(self, state: FundamentalGraphState) -> FundamentalGraphState:
        return self._standard_agent_node(
            state, node="lead_review", status="LEAD_REVIEWING", stage="Lead 审核", progress=54,
            profile_id=self.settings.fundamental_lead_profile, schema_name="lead_review_output", model=LeadReviewOutput,
            filename="lead_review.json", task="审核业务和行业研究，保留冲突、缺失信息并提出财务问题；把需要补充检索的事项整理为 followup_research_tasks，供下一节点执行一轮有上限的深度检索。",
            context_refs=["artifact:lead_plan", "artifact:business_research", "artifact:industry_research"],
            required_tools=set(), disable_profile_tools=True,
        )

    def _deep_research(self, state: FundamentalGraphState) -> FundamentalGraphState:
        node = "deep_research"
        if self._cancelled(state["run_id"], node):
            return {"current_node": node, "error_message": "CANCELLED"}
        try:
            self._enter(state["run_id"], node, "DEEP_RESEARCHING", "深度补充检索", 62)
            run = self.service.get_run(state["run_id"])
            directory = self._artifact_dir(run.run_id)
            review = LeadReviewOutput.model_validate_json(
                (directory / "lead_review.json").read_text(encoding="utf-8")
            )
            tasks = list(dict.fromkeys(
                [*review.followup_research_tasks, *review.missing_information, *review.financial_questions]
            ))
            result = self._run_agent(
                run.run_id,
                node,
                self.profile_loader.load(self.settings.deep_research_profile),
                "specialist_research_output",
                "根据 Lead Review 的补充任务执行一轮深度检索。若任务已被可靠证据回答，给出具体口径、期间和 Evidence 引用；仍无法核实的任务保留在 missing_information。只围绕这些补充任务检索，不重复泛化首轮研究。",
                ["artifact:lead_plan", "artifact:business_research", "artifact:industry_research", "artifact:lead_review"],
            )
            output = SpecialistResearchOutput.model_validate(result.output)
            completed_tools = {
                item.tool_name
                for item in self.repository.list_tool_executions(result.execution_id)
                if item.status == "COMPLETED"
            }
            if tasks and not {"search_research_sources", "read_research_source"}.issubset(completed_tools):
                raise ValueError("deep_research 必须完成搜索和来源读取")
            self._identity(output, run)
            validate_references(output, self._evidence(directory), self._assumptions(directory))
            _atomic_model(output, directory / "deep_research.json")
            self._record_node_results(run.run_id, node)
            return self._paths(run.run_id, node, error_message=None)
        except Exception as exc:
            return self._fail(state["run_id"], node, exc, "DEEP_RESEARCH_FAILED")

    def _assemble_retrieval_package(self, state: FundamentalGraphState) -> FundamentalGraphState:
        node = "assemble_retrieval_package"
        if self._cancelled(state["run_id"], node):
            return {"current_node": node, "error_message": "CANCELLED"}
        try:
            self._enter(state["run_id"], node, "RETRIEVAL_PACKAGING", "整理检索资料包", 67)
            run = self.service.get_run(state["run_id"])
            directory = self._artifact_dir(run.run_id)
            package = RetrievalPackage(
                symbol=run.resolved_symbol or "",
                as_of=date.fromisoformat(run.as_of),
                items=[
                    RetrievalPackageItem(
                        evidence_id=item.id,
                        source_name=item.source_name,
                        source_type=item.type,
                        date=item.date,
                        claim=item.claim,
                        url=item.url,
                        excerpt=" ".join(item.content.split())[:240],
                    )
                    for item in self._evidence(directory).items
                ],
            )
            self._identity(package, run)
            _atomic_model(package, directory / "retrieval_package.json")
            self._record_node_results(run.run_id, node)
            return self._paths(run.run_id, node, error_message=None)
        except Exception as exc:
            return self._fail(state["run_id"], node, exc, "RETRIEVAL_PACKAGE_FAILED")

    def _financial_research(self, state: FundamentalGraphState) -> FundamentalGraphState:
        node = "financial_research"
        if self._cancelled(state["run_id"], node):
            return {"current_node": node, "error_message": "CANCELLED"}
        try:
            self._enter(state["run_id"], node, "FINANCIAL_RESEARCHING", "财务研究", 72)
            run = self.service.get_run(state["run_id"])
            directory = self._artifact_dir(run.run_id)
            data_path = directory / "financial_data.json"
            metrics_path = directory / "financial_metrics.json"
            output_path = directory / "financial_research.json"
            assumptions_path = directory / "assumptions.json"
            if not data_path.is_file():
                _atomic_model(get_financial_data(run.resolved_symbol or "", date.fromisoformat(run.as_of), self.settings), data_path)
            data = FinancialData.model_validate_json(data_path.read_text(encoding="utf-8"))
            self._identity(data, run)
            if not metrics_path.is_file():
                try:
                    calculated_metrics = calculate_financial_metrics(data, self.settings.financial_metric_version)
                except Exception as exc:
                    raise FundamentalCalculationError(
                        "FINANCIAL_CALCULATION_FAILED", "财务指标计算失败"
                    ) from exc
                _atomic_model(calculated_metrics, metrics_path)
            metrics = FinancialMetrics.model_validate_json(metrics_path.read_text(encoding="utf-8"))
            self._identity(metrics, run)
            completed = self._completed(run.run_id, node)
            if output_path.is_file() and assumptions_path.is_file() and completed:
                output = FinancialResearchOutput.model_validate_json(output_path.read_text(encoding="utf-8"))
                assumptions = AssumptionStore.model_validate_json(assumptions_path.read_text(encoding="utf-8"))
                self._identity(output, run)
                validate_references(output, self._evidence(directory), assumptions)
            else:
                result = self._run_agent(
                    run.run_id, node, self.profile_loader.load(self.settings.financial_research_profile), "financial_research_draft",
                    "解释 Python 计算的财务指标并提出简单估值假设。",
                    ["artifact:lead_plan", "artifact:lead_review", "artifact:business_research", "artifact:industry_research", "artifact:deep_research", "artifact:financial_data", "artifact:financial_metrics"],
                )
                try:
                    draft = FinancialResearchDraft.model_validate(result.output)
                    self._identity(draft, run)
                    evidence = self._evidence(directory)
                    validate_references(draft, evidence, AssumptionStore(items=[]))
                    assumptions = AssumptionStore(items=[
                        AssumptionItem(id=f"asm_{index:03d}", variable=item.variable, value=item.value, period=item.period, source=item.source, owner="financial_research")
                        for index, item in enumerate(draft.assumptions, 1)
                    ])
                    _atomic_model(assumptions, assumptions_path)
                    output = _financial_output_from_draft(draft, assumptions)
                    _atomic_model(output, output_path)
                except Exception as exc:
                    self.repository.fail_execution(
                        result.execution_id,
                        "SEMANTIC_VALIDATION_FAILED",
                        str(exc),
                        tool_call_count=result.tool_call_count,
                    )
                    raise
            self._record_node_results(run.run_id, node)
            return self._paths(run.run_id, node, error_message=None)
        except Exception as exc:
            return self._fail(state["run_id"], node, exc, "FINANCIAL_RESEARCH_FAILED")

    def _valuation_research(self, state: FundamentalGraphState) -> FundamentalGraphState:
        node = "valuation_research"
        if self._cancelled(state["run_id"], node):
            return {"current_node": node, "error_message": "CANCELLED"}
        try:
            self._enter(state["run_id"], node, "VALUATION_RESEARCHING", "估值研究", 82)
            run = self.service.get_run(state["run_id"])
            directory = self._artifact_dir(run.run_id)
            valuation_path = directory / "valuation_result.json"
            output_path = directory / "valuation_research.json"
            data = FinancialData.model_validate_json((directory / "financial_data.json").read_text(encoding="utf-8"))
            metrics = FinancialMetrics.model_validate_json((directory / "financial_metrics.json").read_text(encoding="utf-8"))
            assumptions = AssumptionStore.model_validate_json((directory / "assumptions.json").read_text(encoding="utf-8"))
            if not valuation_path.is_file():
                snapshot = get_market_snapshot(run.resolved_symbol or "", date.fromisoformat(run.as_of), self.settings)
                try:
                    calculated_valuation = calculate_valuation(
                        data, metrics, assumptions, snapshot,
                        self.settings.valuation_script_version,
                    )
                except Exception as exc:
                    raise FundamentalCalculationError(
                        "VALUATION_CALCULATION_FAILED", "估值计算失败"
                    ) from exc
                _atomic_model(calculated_valuation, valuation_path)
            valuation = ValuationResult.model_validate_json(valuation_path.read_text(encoding="utf-8"))
            self._identity(valuation, run)
            completed = self._completed(run.run_id, node)
            if output_path.is_file() and completed:
                output = ValuationResearchOutput.model_validate_json(output_path.read_text(encoding="utf-8"))
                persisted = ValuationResearchOutput.model_validate_json(completed.validated_output_json or "")
                if output != persisted:
                    raise ValueError("估值研究产物与已校验执行不一致")
            else:
                result = self._run_agent(
                    run.run_id, node, self.profile_loader.load(self.settings.valuation_research_profile), "valuation_research_output",
                    "仅解释 Python 估值结果、敏感性和风险。",
                    ["artifact:financial_research", "artifact:valuation_result", "artifact:assumptions"],
                )
                try:
                    output = ValuationResearchOutput.model_validate(result.output)
                    self._identity(output, run)
                    validate_references(output, self._evidence(directory), assumptions)
                    if not valuation_path.is_file():
                        raise ValueError("valuation_result.json 不存在")
                    _atomic_model(output, output_path)
                except Exception as exc:
                    self.repository.fail_execution(
                        result.execution_id,
                        "SEMANTIC_VALIDATION_FAILED",
                        str(exc),
                        tool_call_count=result.tool_call_count,
                    )
                    raise
            self._record_node_results(run.run_id, node)
            return self._paths(run.run_id, node, error_message=None)
        except Exception as exc:
            return self._fail(state["run_id"], node, exc, "VALUATION_RESEARCH_FAILED")

    def _lead_final_review(self, state: FundamentalGraphState) -> FundamentalGraphState:
        result = self._standard_agent_node(
            state, node="lead_final_review", status="LEAD_FINAL_REVIEWING", stage="最终审核", progress=90,
            profile_id=self.settings.fundamental_lead_profile, schema_name="lead_final_review_output", model=LeadFinalReviewOutput,
            filename="lead_final_review.json", task="审核已完成的结构化研究产物，保留真实冲突与缺失信息并建议报告大纲。financial_data 与 financial_metrics 是受信 Python 边界计算的权威财务数据，financial_research 是基于其生成的已校验叙述；核心财务数字（营收、净利润、毛利率、净利率、现金流等）以 financial_data/financial_metrics/financial_research 为准，无需在 evidence 摘录中重复引用，不构成缺失。Evidence 仅用于业务、行业、风险等定性披露的来源佐证。只有当某个已批准章节缺少对应的已校验研究产物、或存在阻断报告生成的实质信息缺口时，才写入 missing_information 并置 ready_for_writer=false；若各 approved_sections 均有已校验研究支撑，置 ready_for_writer=true。",
            context_refs=["artifact:lead_plan", "artifact:business_research", "artifact:industry_research", "artifact:lead_review", "artifact:deep_research", "artifact:financial_data", "artifact:financial_metrics", "artifact:financial_research", "artifact:valuation_research", "artifact:retrieval_package", "artifact:assumptions"],
            required_tools=set(), disable_profile_tools=True,
        )
        if not result.get("error_message"):
            generate_research_package(self._artifact_dir(state["run_id"]))
        return result

    def _lead_synthesis(self, state: FundamentalGraphState) -> FundamentalGraphState:
        return self._standard_agent_node(
            state,
            node="lead_synthesis",
            status="LEAD_SYNTHESIZING",
            stage="Lead 生成报告主线",
            progress=92,
            profile_id=self.settings.lead_synthesis_profile,
            schema_name="lead_synthesis_output",
            model=LeadSynthesisOutput,
            filename="lead_synthesis.json",
            task="基于已审核的研究简报生成报告主线、章节论点、资料采用/排除说明与允许引用范围。资料待补充事项只集中列入独立的‘优化建议’板块，不能阻断已经具备证据支持的章节叙事。不得生成交易指令或收益承诺。",
            context_refs=["artifact:lead_plan", "artifact:business_research", "artifact:industry_research", "artifact:lead_review", "artifact:deep_research", "artifact:financial_research", "artifact:valuation_research", "artifact:lead_final_review", "artifact:retrieval_package", "artifact:assumptions"],
            required_tools=set(),
        )

    def _writer_planning(self, state: FundamentalGraphState) -> FundamentalGraphState:
        return self._standard_agent_node(
            state,
            node="writer_planning",
            status="WRITER_PLANNING",
            stage="规划正式报告",
            progress=94,
            profile_id=self.settings.writer_planning_profile,
            schema_name="writer_plan_output",
            model=WriterPlanOutput,
            filename="writer_plan.json",
            task="根据 Lead 已批准的主线形成独立 Writer Plan：为每个章节安排叙事目标、顺序、允许引用/假设和图表强调意图。资料待补充事项仅进入独立的‘优化建议’板块，不得中断正文。",
            context_refs=["artifact:lead_synthesis", "artifact:lead_final_review", "artifact:business_research", "artifact:industry_research", "artifact:deep_research", "artifact:financial_research", "artifact:valuation_research", "artifact:financial_metrics", "artifact:valuation_result"],
            required_tools=set(),
        )

    def _fundamental_writer(self, state: FundamentalGraphState) -> FundamentalGraphState:
        node = "fundamental_writer"
        if self._cancelled(state["run_id"], node):
            return {"current_node": node, "error_message": "CANCELLED"}
        try:
            self._enter(state["run_id"], node, "FUNDAMENTAL_WRITING", "生成正式报告文字", 94)
            run = self.service.get_run(state["run_id"])
            directory = self._artifact_dir(run.run_id)
            if not research_package_is_current(directory):
                generate_research_package(directory)
            if not self._editable_inputs_available(run.run_id, node):
                return self._paths(run.run_id, node, error_message=self._review_state_error(run.run_id))
            store = self._refresh_manifest_upstream(run.run_id)
            expected_writer_inputs = store.input_hashes("fundamental_writer")
            final = LeadFinalReviewOutput.model_validate_json(
                (directory / "lead_final_review.json").read_text(encoding="utf-8")
            )
            if not final.ready_for_writer:
                error = self._require_human_review(run.run_id, node, final.missing_information, "Lead 判断材料尚不足")
                return self._paths(run.run_id, node, error_message=error)
            path = directory / "fundamental_writer.json"
            completed = self._completed(run.run_id, node)
            if path.is_file() and completed:
                output = FundamentalWriterOutput.model_validate_json(path.read_text(encoding="utf-8"))
                persisted = FundamentalWriterOutput.model_validate_json(completed.validated_output_json or "")
                if output != persisted:
                    raise ValueError("Writer 产物与已校验执行不一致")
                validate_writer_output(
                    output, symbol=run.resolved_symbol or "", as_of=run.as_of,
                    evidence=self._evidence(directory), assumptions=self._assumptions(directory),
                    tool_call_count=completed.tool_call_count,
                )
            else:
                self._verify_writer_input_references(directory)
                evidence_sha = sha256_file(directory / "evidence.json")
                assumptions_sha = sha256_file(directory / "assumptions.json")
                result = self._run_agent(
                    run.run_id, node, self.profile_loader.load(self.settings.fundamental_writer_profile),
                    "fundamental_writer_output", "将已校验研究材料组织为正式报告解释文字。", WRITER_CONTEXT_REFS,
                )
                try:
                    output = FundamentalWriterOutput.model_validate(result.output)
                    validate_writer_output(
                        output, symbol=run.resolved_symbol or "", as_of=run.as_of,
                        evidence=self._evidence(directory), assumptions=self._assumptions(directory),
                        tool_call_count=result.tool_call_count,
                    )
                    if evidence_sha != sha256_file(directory / "evidence.json") or assumptions_sha != sha256_file(directory / "assumptions.json"):
                        raise ValueError("Writer 不得修改 Evidence 或 Assumption")
                    _atomic_model(output, path)
                except Exception as exc:
                    self.repository.fail_execution(result.execution_id, "SEMANTIC_VALIDATION_FAILED", str(exc), tool_call_count=result.tool_call_count)
                    raise
            try:
                store.record("fundamental_writer", expected_inputs=expected_writer_inputs)
            except ManifestInputChangedError:
                error = self._require_human_review(run.run_id, node, ["Writer 生成期间输入发生变化"], "Writer 输入已失效")
                return self._paths(run.run_id, node, error_message=error)
            if output.status == "needs_more_research":
                error = self._require_human_review(run.run_id, node, output.missing_information, "Writer 判断材料不足")
                return self._paths(run.run_id, node, error_message=error)
            return self._paths(run.run_id, node, error_message=None)
        except Exception as exc:
            return self._fail(state["run_id"], node, exc, "FUNDAMENTAL_WRITER_FAILED")

    def _write_fundamental_report(self, state: FundamentalGraphState) -> FundamentalGraphState:
        node = "write_fundamental_report"
        if self._cancelled(state["run_id"], node):
            return {"current_node": node, "error_message": "CANCELLED"}
        try:
            self._enter(state["run_id"], node, "REPORTING", "生成正式基本面报告", 98)
            run = self.service.get_run(state["run_id"])
            directory = self._artifact_dir(run.run_id)
            store = ResultManifestStore(directory, run.run_id, self.settings.fundamental_workflow_version)
            stale = store.audit()
            if any(name not in {"report_visuals", "fundamental_report"} for name in stale):
                error = self._require_human_review(run.run_id, node, stale, "报告输入已失效")
                return self._paths(run.run_id, node, error_message=error)
            profile = self.profile_loader.load(self.settings.fundamental_writer_profile)
            writer_execution = self._completed(run.run_id, "fundamental_writer")
            markdown_path = generate_fundamental_report(
                directory, run_id=run.run_id, workflow_version=self.settings.fundamental_workflow_version,
                writer_profile_version=profile.version,
                writer_model_version=(
                    (writer_execution.model_name if writer_execution else None)
                    or self.settings.pi_model
                    or f"{self.settings.pi_runtime_mode}_runtime"
                ),
            )
            store.record("report_visuals")
            expected_report_inputs = store.input_hashes("fundamental_report")
            path = directory / "fundamental_report.html"
            try:
                store.record("fundamental_report", expected_inputs=expected_report_inputs)
            except ManifestInputChangedError:
                error = self._require_human_review(run.run_id, node, ["报告生成期间输入发生变化"], "报告输入已失效")
                return self._paths(run.run_id, node, error_message=error)
            if not self.service.complete_run(run.run_id, path):
                path.unlink(missing_ok=True)
                store.mark_failed("fundamental_report")
                return {"current_node": node, "error_message": "CANCELLED"}
            return self._paths(run.run_id, node, report_path=str(path), error_message=None)
        except Exception as exc:
            return self._fail(state["run_id"], node, exc, "FUNDAMENTAL_REPORT_FAILED")

    def _standard_agent_node(
        self, state: FundamentalGraphState, *, node: str, status: str, stage: str, progress: int,
        profile_id: str, schema_name: str, model: type[BaseModel], filename: str, task: str,
        context_refs: list[str], required_tools: set[str], disable_profile_tools: bool = False,
    ) -> FundamentalGraphState:
        if self._cancelled(state["run_id"], node):
            return {"current_node": node, "error_message": "CANCELLED"}
        try:
            self._enter(state["run_id"], node, status, stage, progress)
            run = self.service.get_run(state["run_id"])
            directory = self._artifact_dir(run.run_id)
            output_path = directory / filename
            completed = self._completed(run.run_id, node)
            if output_path.is_file() and completed:
                output = model.model_validate_json(output_path.read_text(encoding="utf-8"))
                persisted = output_model_for_schema(schema_name).model_validate_json(completed.validated_output_json or "")
                if output != persisted:
                    raise ValueError(f"{node} 产物与已校验执行不一致")
            else:
                profile = self.profile_loader.load(profile_id)
                if disable_profile_tools:
                    profile = profile.model_copy(update={"allowed_tools": [], "max_tool_calls": 0})
                result = self._run_agent(run.run_id, node, profile, schema_name, task, context_refs)
                try:
                    output = model.model_validate(result.output)
                    tools = {
                        item.tool_name
                        for item in self.repository.list_tool_executions(result.execution_id)
                        if item.status == "COMPLETED"
                    }
                    if not required_tools.issubset(tools):
                        raise ValueError(f"{node} 工具权限或执行集合不正确")
                    self._identity(output, run)
                    validate_references(output, self._evidence(directory), self._assumptions(directory))
                    _atomic_model(output, output_path)
                except Exception as exc:
                    self.repository.fail_execution(
                        result.execution_id,
                        "SEMANTIC_VALIDATION_FAILED",
                        str(exc),
                        tool_call_count=result.tool_call_count,
                    )
                    raise
            self._record_node_results(run.run_id, node)
            return self._paths(run.run_id, node, error_message=None)
        except Exception as exc:
            return self._fail(state["run_id"], node, exc, self._error_code(node))

    def _run_agent(self, run_id: str, node: str, profile, schema_name: str, task: str, refs: list[str]):
        attempt = self._next_attempt(run_id, node)
        retries = 0
        while True:
            try:
                return self.adapter.run(run_id, node, profile, task, refs, attempt=attempt, output_schema_name=schema_name)
            except (AgentTimeoutError, BridgeCrashedError):
                if retries >= 1:
                    raise
                attempt += 1
                retries += 1

    def _enter(self, run_id: str, node: str, status: str, stage: str, progress: int) -> None:
        directory = self._artifact_dir(run_id)
        if (directory / "result_manifest.json").is_file():
            try:
                stale = ResultManifestStore(
                    directory, run_id, self.settings.fundamental_workflow_version
                ).audit()
            except (OSError, ValueError):
                error = self._require_human_review(
                    run_id, "result_manifest", ["result_manifest.json 无法读取"], "结果清单损坏"
                )
                raise FundamentalCalculationError(error, "结果清单损坏")
            stale_index = self._stale_node_index(stale)
            current_index = NODE_ORDER.index(node)
            if stale_index is not None and stale_index < current_index:
                error = self._require_human_review(
                    run_id, node, stale, "节点启动前发现更早的 STALE 输入"
                )
                raise FundamentalCalculationError(error, "节点输入已失效")
        self.service.transition_run(run_id, status=status, stage=stage, progress=progress, event_type="LANGGRAPH_NODE_STARTED", message=f"进入基本面节点：{node}", current_node=node, event_key=f"{run_id}:{node}:started:1")

    def _cancelled(self, run_id: str, node: str) -> bool:
        run = self.service.get_run(run_id)
        if not run.cancel_requested and run.status != "CANCELLED":
            return False
        if run.status != "CANCELLED" or run.current_node != node:
            self.service.transition_run(run_id, status="CANCELLED", stage="任务已取消", progress=run.progress, event_type="RUN_CANCELLED", message=f"基本面工作流在 {node} 前停止", current_node=node, event_key=f"{run_id}:{node}:cancelled:1")
        return True

    def _fail(self, run_id: str, node: str, exc: Exception, fallback: str | None = None) -> FundamentalGraphState:
        code = str(getattr(exc, "code", fallback or type(exc).__name__)).split(":", 1)[0]
        run = self.service.get_run(run_id)
        if run.status not in {"COMPLETED", "HUMAN_REVIEW_REQUIRED", "FAILED", "CANCELLED"}:
            self.service.transition_run(run_id, status="FAILED", stage="基本面流程失败", progress=run.progress, event_type="RUN_FAILED", message=f"基本面节点失败（{code[:100]}）", error_message=f"Worker 执行失败（{code[:100]}）", current_node=node, event_key=f"{run_id}:{node}:failed:1")
        return {"current_node": node, "error_message": code[:100]}

    def _artifact_dir(self, run_id: str) -> Path:
        directory = self.settings.artifacts_dir / run_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _evidence(self, directory: Path) -> EvidenceCollection:
        path = directory / "evidence.json"
        return EvidenceCollection.model_validate_json(path.read_text(encoding="utf-8")) if path.is_file() else EvidenceCollection(items=[])

    def _assumptions(self, directory: Path) -> AssumptionStore:
        path = directory / "assumptions.json"
        if not path.is_file():
            _atomic_model(AssumptionStore(items=[]), path)
        return AssumptionStore.model_validate_json(path.read_text(encoding="utf-8"))

    def _completed(self, run_id: str, node: str):
        return next((item for item in self.repository.list_executions(run_id) if item.node_name == node and item.status == "COMPLETED"), None)

    def _next_attempt(self, run_id: str, node: str) -> int:
        records = [item for item in self.repository.list_executions(run_id) if item.node_name == node]
        completed = next((item for item in records if item.status == "COMPLETED"), None)
        if completed:
            return completed.attempt
        for item in records:
            if item.status == "RUNNING":
                self.repository.fail_execution(item.execution_id, "RECOVERED_INCOMPLETE", "Worker 恢复时发现未完成执行", tool_call_count=item.tool_call_count)
        return max((item.attempt for item in records), default=0) + 1

    @staticmethod
    def _identity(output: Any, run: Any) -> None:
        if getattr(output, "symbol", None) != run.resolved_symbol:
            raise ValueError("基本面产物 symbol 与当前任务不一致")
        output_as_of = getattr(output, "as_of", None)
        if output_as_of is not None and str(output_as_of) != run.as_of:
            raise ValueError("基本面产物 as_of 与当前任务不一致")

    def _paths(self, run_id: str, node: str, **updates: Any) -> FundamentalGraphState:
        directory = self._artifact_dir(run_id)
        mapping = {
            "lead_plan_path": "lead_plan.json", "business_path": "business_research.json", "industry_path": "industry_research.json",
            "lead_review_path": "lead_review.json", "deep_research_path": "deep_research.json", "financial_data_path": "financial_data.json", "financial_metrics_path": "financial_metrics.json",
            "financial_research_path": "financial_research.json", "valuation_result_path": "valuation_result.json",
            "valuation_research_path": "valuation_research.json", "lead_final_review_path": "lead_final_review.json",
            "retrieval_package_path": "retrieval_package.json", "lead_synthesis_path": "lead_synthesis.json", "writer_plan_path": "writer_plan.json",
            "evidence_path": "evidence.json", "assumptions_path": "assumptions.json",
            "writer_path": "fundamental_writer.json", "manifest_path": "result_manifest.json",
        }
        result: dict[str, Any] = {"current_node": node}
        for field, name in mapping.items():
            if (directory / name).is_file():
                result[field] = str(directory / name)
        result.update(updates)
        return FundamentalGraphState(**result)

    def _recovery_preflight(self, run: Any, next_node: str) -> FundamentalGraphState | None:
        if next_node not in NODE_ORDER:
            return self._fail(run.run_id, "recovery_preflight", ValueError("未知恢复节点"))
        boundary = NODE_ORDER.index(next_node)
        directory = self._artifact_dir(run.run_id)
        if (directory / "result_manifest.json").is_file():
            if not self._editable_inputs_available(run.run_id, "recovery_preflight"):
                return {"current_node": "recovery_preflight", "error_message": self._review_state_error(run.run_id)}
            try:
                stale = ResultManifestStore(
                    directory, run.run_id, self.settings.fundamental_workflow_version
                ).audit()
            except (OSError, ValueError):
                error = self._require_human_review(run.run_id, "result_manifest", ["result_manifest.json 无法读取"], "结果清单损坏")
                return {"current_node": "result_manifest", "error_message": error}
            stale_index = self._stale_node_index(stale)
            if stale_index is not None and stale_index < boundary:
                self._discard_from(run.run_id, stale_index)
                handlers = self._handlers()
                for rebuild_index in range(stale_index, boundary):
                    result = handlers[rebuild_index]({"run_id": run.run_id})
                    if result.get("error_message"):
                        return result
                return None
        for index in range(boundary):
            if not self._node_valid(run.run_id, NODE_ORDER[index]):
                self._discard_from(run.run_id, index)
                handlers = self._handlers()
                for rebuild_index in range(index, boundary):
                    result = handlers[rebuild_index]({"run_id": run.run_id})
                    if result.get("error_message"):
                        return result
                break
        return None

    def _node_valid(self, run_id: str, node: str) -> bool:
        run = self.service.get_run(run_id)
        directory = self._artifact_dir(run_id)
        if node == "resolve_security":
            return bool(run.resolved_symbol and run.security_name)
        specifications = {
            "lead_planning": ("lead_plan.json", LeadPlanOutput, "lead_plan_output"),
            "business_research": ("business_research.json", SpecialistResearchOutput, "specialist_research_output"),
            "industry_research": ("industry_research.json", SpecialistResearchOutput, "specialist_research_output"),
            "lead_review": ("lead_review.json", LeadReviewOutput, "lead_review_output"),
            "deep_research": ("deep_research.json", SpecialistResearchOutput, "specialist_research_output"),
            "lead_final_review": ("lead_final_review.json", LeadFinalReviewOutput, "lead_final_review_output"),
            "lead_synthesis": ("lead_synthesis.json", LeadSynthesisOutput, "lead_synthesis_output"),
            "writer_planning": ("writer_plan.json", WriterPlanOutput, "writer_plan_output"),
        }
        try:
            if node in specifications:
                filename, model, schema = specifications[node]
                artifact = model.model_validate_json((directory / filename).read_text(encoding="utf-8"))
                self._identity(artifact, run)
                execution = self._completed(run_id, node)
                if not execution:
                    return False
                if artifact != output_model_for_schema(schema).model_validate_json(execution.validated_output_json or ""):
                    return False
                validate_references(artifact, self._evidence(directory), self._assumptions(directory))
                if node == "lead_planning":
                    company = CompanyProfile.model_validate_json((directory / "company_profile.json").read_text(encoding="utf-8"))
                    self._identity(company, run)
                return True
            if node == "financial_research":
                data = FinancialData.model_validate_json((directory / "financial_data.json").read_text(encoding="utf-8"))
                metrics = FinancialMetrics.model_validate_json((directory / "financial_metrics.json").read_text(encoding="utf-8"))
                output = FinancialResearchOutput.model_validate_json((directory / "financial_research.json").read_text(encoding="utf-8"))
                assumptions = AssumptionStore.model_validate_json((directory / "assumptions.json").read_text(encoding="utf-8"))
                self._identity(data, run); self._identity(metrics, run); self._identity(output, run)
                if metrics != calculate_financial_metrics(data, self.settings.financial_metric_version):
                    return False
                execution = self._completed(run_id, node)
                if not execution:
                    return False
                draft = FinancialResearchDraft.model_validate_json(execution.validated_output_json or "")
                expected_assumptions = AssumptionStore(items=[
                    AssumptionItem(
                        id=f"asm_{index:03d}", variable=item.variable, value=item.value,
                        period=item.period, source=item.source, owner="financial_research",
                    )
                    for index, item in enumerate(draft.assumptions, 1)
                ])
                if assumptions != expected_assumptions or output != _financial_output_from_draft(draft, assumptions):
                    return False
                validate_references(draft, self._evidence(directory), AssumptionStore(items=[]))
                validate_references(output, self._evidence(directory), assumptions)
                return True
            if node == "assemble_retrieval_package":
                package = RetrievalPackage.model_validate_json((directory / "retrieval_package.json").read_text(encoding="utf-8"))
                self._identity(package, run)
                return [item.evidence_id for item in package.items] == [item.id for item in self._evidence(directory).items]
            if node == "valuation_research":
                data = FinancialData.model_validate_json((directory / "financial_data.json").read_text(encoding="utf-8"))
                metrics = FinancialMetrics.model_validate_json((directory / "financial_metrics.json").read_text(encoding="utf-8"))
                assumptions = AssumptionStore.model_validate_json((directory / "assumptions.json").read_text(encoding="utf-8"))
                valuation = ValuationResult.model_validate_json((directory / "valuation_result.json").read_text(encoding="utf-8"))
                output = ValuationResearchOutput.model_validate_json((directory / "valuation_research.json").read_text(encoding="utf-8"))
                self._identity(valuation, run); self._identity(output, run)
                expected = calculate_valuation(
                    data, metrics, assumptions, valuation.market_snapshot,
                    self.settings.valuation_script_version,
                )
                if valuation != expected:
                    return False
                execution = self._completed(run_id, node)
                if not execution or output != ValuationResearchOutput.model_validate_json(execution.validated_output_json or ""):
                    return False
                validate_references(output, self._evidence(directory), assumptions)
                return True
            if node == "fundamental_writer":
                output = FundamentalWriterOutput.model_validate_json((directory / "fundamental_writer.json").read_text(encoding="utf-8"))
                execution = self._completed(run_id, node)
                if not execution or output != FundamentalWriterOutput.model_validate_json(execution.validated_output_json or ""):
                    return False
                validate_writer_output(
                    output, symbol=run.resolved_symbol or "", as_of=run.as_of,
                    evidence=self._evidence(directory), assumptions=self._assumptions(directory),
                    tool_call_count=execution.tool_call_count,
                )
                return output.status == "completed"
            if node == "write_fundamental_report":
                manifest = ResultManifestStore(directory, run_id, self.settings.fundamental_workflow_version)
                entry = manifest.load().results.get("fundamental_report")
                return bool(entry and entry.status == "current" and not manifest.audit() and (directory / "fundamental_report.html").is_file())
        except (OSError, ValueError, KeyError, TypeError):
            return False
        return False

    def _discard_from(self, run_id: str, index: int) -> None:
        directory = self._artifact_dir(run_id)
        for node in NODE_ORDER[index:]:
            execution = self._completed(run_id, node)
            if execution:
                self.repository.fail_execution(execution.execution_id, "ARTIFACT_INVALID", "恢复预检发现产物缺失或损坏", tool_call_count=execution.tool_call_count)
        files_by_index = {
            1: ["company_profile.json", "evidence.json", "assumptions.json", "lead_plan.json"],
            2: ["business_research.json"], 3: ["industry_research.json"], 4: ["lead_review.json"],
            5: ["deep_research.json"],
            6: ["retrieval_package.json"],
            7: ["financial_data.json", "financial_metrics.json", "assumptions.json", "financial_research.json"],
            8: ["valuation_result.json", "valuation_research.json"],
            9: ["lead_final_review.json", "fundamental_research_package.md"],
            10: ["lead_synthesis.json"],
            11: ["writer_plan.json"],
            12: ["fundamental_writer.json"],
            13: ["fundamental_report.md", "report_visuals.json", "fundamental_report.html"],
        }
        for key, names in files_by_index.items():
            if key >= index:
                for name in names:
                    (directory / name).unlink(missing_ok=True)

    def _refresh_manifest_upstream(self, run_id: str) -> ResultManifestStore:
        directory = self._artifact_dir(run_id)
        store = ResultManifestStore(directory, run_id, self.settings.fundamental_workflow_version)
        manifest = store.load()
        if manifest.results:
            store.audit()
            manifest = store.load()
        for name in RESULT_ORDER[: RESULT_ORDER.index("fundamental_writer")]:
            path = directory / f"{name}.json"
            entry = manifest.results.get(name)
            if path.is_file() and (entry is None or entry.status != "current"):
                store.record(name)
                manifest = store.load()
        stale = [name for name in store.audit() if name not in {"fundamental_writer", "report_visuals", "fundamental_report"}]
        if stale:
            raise ValueError(f"Writer 输入仍为 STALE: {', '.join(stale)}")
        return store

    def _verify_writer_input_references(self, directory: Path) -> None:
        """写前证据核验：上游研究产物引用的 Evidence/Assumption ID 必须全部存在。

        在 ``_fundamental_writer`` 重生成分支、调用 ``_run_agent`` 之前执行。从
        ``evidence.json``/``assumptions.json`` 构建可用 ID 集合，逐一核验
        ``business_research``/``industry_research``/``financial_research``/``valuation_research``
        产物中引用的 evidence_id/assumption_id 是否已注册。缺失即抛 ``ValueError``，由
        ``_fundamental_writer`` 的 ``except Exception`` 捕获并归类为
        ``SEMANTIC_VALIDATION_FAILED`` / ``FUNDAMENTAL_WRITER_FAILED``，不新增 error_code、
        不新增状态、不新增产物文件。``lead_review``/``lead_final_review`` 不直接携带引用
        ID（见 schemas），故不在核验范围内。
        """
        available_evidence = {item.id for item in self._evidence(directory).items}
        available_assumptions = {item.id for item in self._assumptions(directory).items}

        def _check_evidence(ids: list[str]) -> None:
            missing = [eid for eid in ids if eid not in available_evidence]
            if missing:
                raise ValueError(
                    "写前核验失败：引用了不存在的 Evidence ID: " + ", ".join(missing)
                )

        def _check_assumptions(ids: list[str]) -> None:
            missing = [aid for aid in ids if aid not in available_assumptions]
            if missing:
                raise ValueError(
                    "写前核验失败：引用了不存在的 Assumption ID: " + ", ".join(missing)
                )

        business_path = directory / "business_research.json"
        if business_path.is_file():
            business = SpecialistResearchOutput.model_validate_json(
                business_path.read_text(encoding="utf-8")
            )
            for finding in business.findings:
                _check_evidence(finding.evidence_ids)

        deep_path = directory / "deep_research.json"
        if deep_path.is_file():
            deep = SpecialistResearchOutput.model_validate_json(
                deep_path.read_text(encoding="utf-8")
            )
            for finding in deep.findings:
                _check_evidence(finding.evidence_ids)

        industry_path = directory / "industry_research.json"
        if industry_path.is_file():
            industry = SpecialistResearchOutput.model_validate_json(
                industry_path.read_text(encoding="utf-8")
            )
            for finding in industry.findings:
                _check_evidence(finding.evidence_ids)

        financial_path = directory / "financial_research.json"
        if financial_path.is_file():
            financial = FinancialResearchOutput.model_validate_json(
                financial_path.read_text(encoding="utf-8")
            )
            _check_evidence(financial.evidence_ids)
            _check_assumptions(financial.assumption_ids)

        valuation_path = directory / "valuation_research.json"
        if valuation_path.is_file():
            valuation = ValuationResearchOutput.model_validate_json(
                valuation_path.read_text(encoding="utf-8")
            )
            _check_evidence(valuation.evidence_ids)
            _check_assumptions(valuation.assumption_ids)

    def _record_node_results(self, run_id: str, node: str) -> None:
        directory = self._artifact_dir(run_id)
        store = ResultManifestStore(directory, run_id, self.settings.fundamental_workflow_version)
        names_by_node = {
            "lead_planning": ["company_profile", "evidence", "assumptions", "lead_plan"],
            "business_research": ["evidence", "business_research"],
            "industry_research": ["evidence", "industry_research"],
            "lead_review": ["lead_review"],
            "deep_research": ["evidence", "deep_research"],
            "assemble_retrieval_package": ["retrieval_package"],
            "financial_research": ["financial_data", "financial_metrics", "assumptions", "financial_research"],
            "valuation_research": ["valuation_result", "valuation_research"],
            "lead_final_review": ["lead_final_review"],
            "lead_synthesis": ["lead_synthesis"],
            "writer_planning": ["writer_plan"],
        }
        for name in names_by_node.get(node, []):
            if (directory / f"{name}.json").is_file():
                store.record(name)
        if node == "industry_research" and (directory / "business_research.json").is_file():
            # Industry adds Evidence after Business. Rebaseline Business against
            # the final research-phase Evidence set without claiming a rebuild.
            store.refresh("business_research")

    def _completed_stale_index(self, run: Any) -> int | None:
        if run.analysis_type != "fundamental" or run.status != "COMPLETED":
            return None
        directory = self._artifact_dir(run.run_id)
        if not (directory / "result_manifest.json").is_file():
            return None
        if not self._editable_inputs_available(run.run_id, "result_manifest"):
            return None
        try:
            stale = ResultManifestStore(
                directory, run.run_id, self.settings.fundamental_workflow_version
            ).audit()
        except (OSError, ValueError):
            self._require_human_review(run.run_id, "result_manifest", ["result_manifest.json 无法读取"], "结果清单损坏")
            return None
        return self._stale_node_index(stale)

    @staticmethod
    def _stale_node_index(stale: list[str]) -> int | None:
        node_by_result = {
            "lead_plan": "lead_planning",
            "company_profile": "lead_planning",
            "evidence": "lead_planning",
            "business_research": "business_research",
            "industry_research": "industry_research",
            "lead_review": "lead_review",
            "deep_research": "deep_research",
            "retrieval_package": "assemble_retrieval_package",
            "financial_data": "financial_research",
            "financial_metrics": "financial_research",
            "assumptions": "financial_research",
            "financial_research": "financial_research",
            "valuation_result": "valuation_research",
            "valuation_research": "valuation_research",
            "lead_final_review": "lead_final_review",
            "lead_synthesis": "lead_synthesis",
            "writer_plan": "writer_planning",
            "fundamental_writer": "fundamental_writer",
            "fundamental_report": "write_fundamental_report",
        }
        indexes = [NODE_ORDER.index(node_by_result[name]) for name in stale if name in node_by_result]
        return min(indexes, default=None)

    def _editable_inputs_available(self, run_id: str, node: str) -> bool:
        directory = self._artifact_dir(run_id)
        missing: list[str] = []
        try:
            EvidenceCollection.model_validate_json((directory / "evidence.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            missing.append("evidence.json 缺失或无效")
        try:
            AssumptionStore.model_validate_json((directory / "assumptions.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            missing.append("assumptions.json 缺失或无效")
        if missing:
            self._require_human_review(run_id, node, missing, "关键研究输入缺失")
            return False
        return True

    def _require_human_review(
        self, run_id: str, node: str, missing: list[str], reason: str
    ) -> str:
        run = self.service.require_human_review(run_id, node, missing, reason)
        return "CANCELLED" if run.status == "CANCELLED" else "HUMAN_REVIEW_REQUIRED"

    def _review_state_error(self, run_id: str) -> str:
        return "CANCELLED" if self.service.get_run(run_id).status == "CANCELLED" else "HUMAN_REVIEW_REQUIRED"

    def _legacy_upgrade_index(self, run: Any) -> int | None:
        if run.analysis_type != "fundamental" or run.status != "COMPLETED":
            return None
        directory = self._artifact_dir(run.run_id)
        if (directory / "fundamental_report.html").is_file():
            return None
        if (
            run.report_path
            and Path(run.report_path).name == "fundamental_research_package.md"
            and (directory / "lead_final_review.json").is_file()
        ):
            return NODE_ORDER.index("lead_synthesis")
        return None

    @staticmethod
    def _error_code(node: str) -> str:
        return {
            "lead_planning": "LEAD_AGENT_FAILED", "business_research": "BUSINESS_RESEARCH_FAILED",
            "industry_research": "INDUSTRY_RESEARCH_FAILED", "lead_review": "LEAD_REVIEW_FAILED",
            "deep_research": "DEEP_RESEARCH_FAILED",
            "lead_final_review": "LEAD_REVIEW_FAILED",
        }.get(node, "LEAD_AGENT_FAILED")


def _atomic_model(model: BaseModel, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _financial_output_from_draft(
    draft: FinancialResearchDraft, assumptions: AssumptionStore
) -> FinancialResearchOutput:
    return FinancialResearchOutput(
        symbol=draft.symbol,
        summary=draft.summary,
        growth_analysis=draft.growth_analysis,
        profitability_analysis=draft.profitability_analysis,
        cash_flow_analysis=draft.cash_flow_analysis,
        balance_sheet_analysis=draft.balance_sheet_analysis,
        earnings_drivers=draft.earnings_drivers,
        assumption_ids=[item.id for item in assumptions.items],
        risks=draft.risks,
        evidence_ids=draft.evidence_ids,
        confidence=draft.confidence,
    )
