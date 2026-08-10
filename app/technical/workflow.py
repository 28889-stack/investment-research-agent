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
from app.run_service import RunService
from app.runtime.context_loader import ContextLoader
from app.runtime.exceptions import (
    AgentTimeoutError,
    BridgeCrashedError,
)
from app.runtime.output_validator import OutputValidator
from app.runtime.pi_adapter import PiAgentAdapter
from app.runtime.pi_client import BridgePiClient, PiClient
from app.runtime.profiles import ProfileLoader
from app.runtime.repository import RuntimeRepository
from app.runtime.tool_registry import ToolRegistry
from app.technical.kronos import KronosError, atomic_write_kronos, predict_kronos
from app.technical.market_data import load_persisted_market_data, resolve_security
from app.technical.report import generate_technical_report, technical_report_is_current
from app.technical.schemas import (
    KronosResult,
    TechnicalAssemblyOutput,
    TechnicalIndicators,
    TechnicalResearchOutput,
)
from app.tools.technical_tools import build_technical_tools


LOGGER = logging.getLogger(__name__)


class TechnicalGraphState(TypedDict, total=False):
    run_id: str
    resolved_symbol: str
    security_name: str
    data_version: str
    market_data_path: str
    indicators_path: str
    technical_research_path: str
    kronos_result_path: str
    technical_assembly_path: str
    report_path: str
    current_node: str
    error_message: str | None


class TechnicalWorkflow:
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
        )
        self.repository = RuntimeRepository(session_factory)
        self.profile_loader = ProfileLoader(settings.agent_profile_dir)
        self.tool_registry = ToolRegistry(self.repository)
        build_technical_tools(
            self.tool_registry,
            self.service,
            self.repository,
            settings,
        )
        for profile_id in (
            settings.technical_research_profile,
            settings.technical_assembly_profile,
        ):
            self.tool_registry.validate_profile_permissions(
                self.profile_loader.load(profile_id)
            )
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
        self._checkpoint_connection = sqlite3.connect(
            checkpoint_path, check_same_thread=False
        )
        self.checkpointer = SqliteSaver(self._checkpoint_connection)
        self.checkpointer.setup()
        self.graph = self._build_graph(interrupt_after)

    def _build_graph(self, interrupt_after: list[str] | None):
        builder = StateGraph(TechnicalGraphState)
        nodes = [
            ("resolve_security", self._resolve_security),
            ("technical_research", self._technical_research),
            ("kronos", self._kronos),
            ("technical_assembly", self._technical_assembly),
            ("write_report", self._write_report),
        ]
        for name, handler in nodes:
            builder.add_node(name, handler)
        builder.add_edge(START, "resolve_security")
        for (current, _), (following, _) in zip(nodes, nodes[1:]):
            builder.add_conditional_edges(
                current,
                lambda state, following=following: (
                    "end" if state.get("error_message") else following
                ),
                {following: following, "end": END},
            )
        builder.add_edge("write_report", END)
        return builder.compile(
            checkpointer=self.checkpointer,
            interrupt_after=interrupt_after,
        )

    def run(self, run_id: str) -> TechnicalGraphState:
        started = time.monotonic()
        try:
            result = self._run_graph(run_id)
        except Exception as exc:
            LOGGER.warning("Technical workflow failed", extra={"component": "technical", "run_id": run_id, "workflow": self.settings.technical_workflow_version, "duration_ms": int((time.monotonic() - started) * 1000), "status": "FAILED", "error_type": type(exc).__name__})
            raise
        persisted_status = self.service.get_run(run_id).status
        log = LOGGER.info if persisted_status == "COMPLETED" else LOGGER.warning
        log(
            "Technical workflow completed" if persisted_status == "COMPLETED" else "Technical workflow stopped",
            extra={"component": "technical", "run_id": run_id, "workflow": self.settings.technical_workflow_version, "duration_ms": int((time.monotonic() - started) * 1000), "status": persisted_status, "error_type": result.get("error_message")},
        )
        return result

    def _run_graph(self, run_id: str) -> TechnicalGraphState:
        run = self.service.get_run(run_id)
        # The run_id is the isolation boundary. Never trust a mutable database
        # field to select another run's checkpoint state.
        config = {"configurable": {"thread_id": run_id}}
        snapshot = self.graph.get_state(config)
        if snapshot.values and snapshot.next:
            preflight_error = self._recovery_preflight(run, snapshot.next[0])
            if preflight_error:
                return TechnicalGraphState(
                    **{**snapshot.values, **preflight_error}
                )
            result = self.graph.invoke(None, config)
        elif snapshot.values:
            result = snapshot.values
        else:
            result = self.graph.invoke(
                {
                    "run_id": run_id,
                    "current_node": "",
                    "error_message": None,
                },
                config,
            )
        return TechnicalGraphState(**result)

    def shutdown(self) -> None:
        if self._owns_client:
            self.pi_client.shutdown()
        self._checkpoint_connection.close()

    def _resolve_security(self, state: TechnicalGraphState) -> TechnicalGraphState:
        node = "resolve_security"
        if self._cancelled(state["run_id"], node):
            return {"current_node": node, "error_message": "CANCELLED"}
        try:
            self._enter(state["run_id"], node, "RESOLVING_SECURITY", "证券解析", 10)
            run = self.service.get_run(state["run_id"])
            if run.resolved_symbol and run.security_name:
                resolved = resolve_security(run.resolved_symbol, self.settings)
                security_name = run.security_name
            else:
                resolved = resolve_security(run.input_symbol, self.settings)
                security_name = resolved.security_name
            self.service.transition_run(
                run.run_id,
                status="RESOLVING_SECURITY",
                stage="证券解析",
                progress=20,
                event_type="SECURITY_RESOLVED",
                message="证券已解析为标准 A 股代码",
                normalized_symbol=resolved.symbol,
                resolved_symbol=resolved.symbol,
                security_name=security_name,
                current_node=node,
                event_key=f"{run.run_id}:{node}:completed:1",
            )
            return {
                "current_node": node,
                "resolved_symbol": resolved.symbol,
                "security_name": security_name,
                "error_message": None,
            }
        except Exception as exc:
            return self._fail(state["run_id"], node, exc)

    def _technical_research(self, state: TechnicalGraphState) -> TechnicalGraphState:
        node = "technical_research"
        if self._cancelled(state["run_id"], node):
            return {"current_node": node, "error_message": "CANCELLED"}
        try:
            self._enter(state["run_id"], node, "TECH_RESEARCHING", "技术指标研究", 35)
            run = self.service.get_run(state["run_id"])
            directory = self._artifact_dir(run.run_id)
            output_path = directory / "technical_research.json"
            completed = self._completed_execution(run.run_id, node)
            if output_path.is_file() and completed:
                output = TechnicalResearchOutput.model_validate_json(
                    output_path.read_text(encoding="utf-8")
                )
                self._validate_identity(output, run)
                persisted_output = TechnicalResearchOutput.model_validate_json(
                    completed.validated_output_json or ""
                )
                if output != persisted_output:
                    raise ValueError(
                        "TECHNICAL_AGENT_FAILED: Research 文件与已校验执行不一致"
                    )
                completed_tools = {
                    item.tool_name
                    for item in self.repository.list_tool_executions(
                        completed.execution_id
                    )
                    if item.status == "COMPLETED"
                }
                if completed.tool_call_count < 3 or not {
                    "get_market_data",
                    "calculate_technical_indicators",
                    "get_technical_summary",
                }.issubset(completed_tools):
                    raise ValueError(
                        "TECHNICAL_AGENT_FAILED: 已完成执行缺少有效技术工具记录"
                    )
            else:
                profile = self.profile_loader.load(
                    self.settings.technical_research_profile
                )
                attempt = self._next_attempt(run.run_id, node)
                while True:
                    try:
                        result = self.adapter.run(
                            run.run_id,
                            node,
                            profile,
                            "依次调用三个技术工具并解释已计算的指标信号。",
                            [],
                            attempt=attempt,
                        )
                        break
                    except (
                        AgentTimeoutError,
                        BridgeCrashedError,
                    ):
                        if attempt >= 2:
                            raise
                        attempt += 1
                if result.tool_call_count < 3:
                    raise ValueError("TECHNICAL_AGENT_FAILED: 技术研究未完成三个工具调用")
                tool_names = {
                    item.tool_name
                    for item in self.repository.list_tool_executions(result.execution_id)
                    if item.status == "COMPLETED"
                }
                required = {
                    "get_market_data",
                    "calculate_technical_indicators",
                    "get_technical_summary",
                }
                if not required.issubset(tool_names):
                    raise ValueError("TECHNICAL_AGENT_FAILED: 技术工具执行不完整")
                output = TechnicalResearchOutput.model_validate(result.output)
                run = self.service.get_run(run.run_id)
                self._validate_identity(output, run)
                _atomic_model(output, output_path)
            run = self.service.get_run(run.run_id)
            return {
                "current_node": node,
                "resolved_symbol": run.resolved_symbol or "",
                "security_name": run.security_name or "",
                "data_version": run.data_version or "",
                "market_data_path": str(directory / "market_data.csv"),
                "indicators_path": str(directory / "technical_indicators.json"),
                "technical_research_path": str(output_path),
                "error_message": None,
            }
        except Exception as exc:
            return self._fail(state["run_id"], node, exc, "TECHNICAL_AGENT_FAILED")

    def _kronos(self, state: TechnicalGraphState) -> TechnicalGraphState:
        node = "kronos"
        if self._cancelled(state["run_id"], node):
            return {"current_node": node, "error_message": "CANCELLED"}
        try:
            self._enter(state["run_id"], node, "KRONOS_ANALYZING", "Kronos 分析", 60)
            run = self.service.get_run(state["run_id"])
            directory = self._artifact_dir(run.run_id)
            output_path = directory / "kronos_result.json"
            if output_path.is_file():
                result = KronosResult.model_validate_json(output_path.read_text(encoding="utf-8"))
                self._validate_identity(result, run)
            else:
                frame = load_persisted_market_data(
                    directory / "market_data.csv",
                    symbol=run.resolved_symbol or "",
                    as_of=date.fromisoformat(run.as_of),
                    expected_data_version=run.data_version or "",
                    min_bars=self.settings.market_data_min_bars,
                )
                for attempt in range(2):
                    try:
                        result = predict_kronos(
                            frame,
                            run.resolved_symbol or "",
                            date.fromisoformat(run.as_of),
                            run.data_version or "",
                            self.settings,
                        )
                        break
                    except KronosError as exc:
                        if attempt >= 1 or not exc.retryable:
                            raise
                self._validate_identity(result, run)
                atomic_write_kronos(result, output_path)
            return {
                "current_node": node,
                "resolved_symbol": run.resolved_symbol or "",
                "security_name": run.security_name or "",
                "data_version": run.data_version or "",
                "market_data_path": str(directory / "market_data.csv"),
                "indicators_path": str(directory / "technical_indicators.json"),
                "technical_research_path": str(
                    directory / "technical_research.json"
                ),
                "kronos_result_path": str(output_path),
                "error_message": None,
            }
        except Exception as exc:
            return self._fail(state["run_id"], node, exc, "KRONOS_FAILED")

    def _technical_assembly(self, state: TechnicalGraphState) -> TechnicalGraphState:
        node = "technical_assembly"
        if self._cancelled(state["run_id"], node):
            return {"current_node": node, "error_message": "CANCELLED"}
        try:
            self._enter(state["run_id"], node, "TECH_ASSEMBLING", "技术信号组装", 78)
            run = self.service.get_run(state["run_id"])
            directory = self._artifact_dir(run.run_id)
            output_path = directory / "technical_assembly.json"
            completed = self._completed_execution(run.run_id, node)
            if output_path.is_file() and completed:
                output = TechnicalAssemblyOutput.model_validate_json(
                    output_path.read_text(encoding="utf-8")
                )
                self._validate_identity(output, run)
                if completed.tool_call_count != 0:
                    raise ValueError("ASSEMBLY_FAILED: Assembly 非法调用工具")
                persisted_output = TechnicalAssemblyOutput.model_validate_json(
                    completed.validated_output_json or ""
                )
                if output != persisted_output:
                    raise ValueError(
                        "ASSEMBLY_FAILED: Assembly 文件与已校验执行不一致"
                    )
            else:
                profile = self.profile_loader.load(
                    self.settings.technical_assembly_profile
                )
                attempt = self._next_attempt(run.run_id, node)
                while True:
                    try:
                        result = self.adapter.run(
                            run.run_id,
                            node,
                            profile,
                            "对比已校验技术指标解释与 Kronos 结果，保留一致、冲突和不确定性。",
                            [
                                "artifact:technical_research",
                                "artifact:kronos_result",
                                "artifact:technical_indicators",
                            ],
                            attempt=attempt,
                        )
                        break
                    except (
                        AgentTimeoutError,
                        BridgeCrashedError,
                    ):
                        if attempt >= 2:
                            raise
                        attempt += 1
                if result.tool_call_count != 0:
                    raise ValueError("ASSEMBLY_FAILED: Assembly 必须保持零工具调用")
                output = TechnicalAssemblyOutput.model_validate(result.output)
                self._validate_identity(output, run)
                _atomic_model(output, output_path)
            return {
                "current_node": node,
                "resolved_symbol": run.resolved_symbol or "",
                "security_name": run.security_name or "",
                "data_version": run.data_version or "",
                "market_data_path": str(directory / "market_data.csv"),
                "indicators_path": str(directory / "technical_indicators.json"),
                "technical_research_path": str(
                    directory / "technical_research.json"
                ),
                "kronos_result_path": str(directory / "kronos_result.json"),
                "technical_assembly_path": str(output_path),
                "error_message": None,
            }
        except Exception as exc:
            return self._fail(state["run_id"], node, exc, "ASSEMBLY_FAILED")

    def _write_report(self, state: TechnicalGraphState) -> TechnicalGraphState:
        node = "write_report"
        if self._cancelled(state["run_id"], node):
            return {"current_node": node, "error_message": "CANCELLED"}
        try:
            self._enter(state["run_id"], node, "REPORTING", "生成报告", 92)
            run = self.service.get_run(state["run_id"])
            directory = self._artifact_dir(run.run_id)
            report_path = directory / "technical_report.md"
            if not technical_report_is_current(run, directory):
                report_path = generate_technical_report(run, directory)
            if not self.service.complete_run(run.run_id, report_path):
                return {"current_node": node, "error_message": "CANCELLED"}
            return {
                "current_node": node,
                "resolved_symbol": run.resolved_symbol or "",
                "security_name": run.security_name or "",
                "data_version": run.data_version or "",
                "market_data_path": str(directory / "market_data.csv"),
                "indicators_path": str(directory / "technical_indicators.json"),
                "technical_research_path": str(
                    directory / "technical_research.json"
                ),
                "kronos_result_path": str(directory / "kronos_result.json"),
                "technical_assembly_path": str(
                    directory / "technical_assembly.json"
                ),
                "report_path": str(report_path),
                "error_message": None,
            }
        except Exception as exc:
            return self._fail(state["run_id"], node, exc, "REPORT_GENERATION_FAILED")

    def _enter(
        self, run_id: str, node: str, status: str, stage: str, progress: int
    ) -> None:
        self.service.transition_run(
            run_id,
            status=status,
            stage=stage,
            progress=progress,
            event_type="LANGGRAPH_NODE_STARTED",
            message=f"进入技术面节点：{node}",
            current_node=node,
            event_key=f"{run_id}:{node}:started:1",
        )

    def _cancelled(self, run_id: str, node: str) -> bool:
        run = self.service.get_run(run_id)
        if not run.cancel_requested and run.status != "CANCELLED":
            return False
        if run.status != "CANCELLED" or run.current_node != node:
            self.service.transition_run(
                run_id,
                status="CANCELLED",
                stage="任务已取消",
                progress=run.progress,
                event_type="RUN_CANCELLED",
                message=f"技术面工作流在 {node} 前停止",
                current_node=node,
                event_key=f"{run_id}:{node}:cancelled:1",
            )
        return True

    def _fail(
        self,
        run_id: str,
        node: str,
        exc: Exception,
        fallback: str | None = None,
    ) -> TechnicalGraphState:
        code = str(getattr(exc, "code", fallback or type(exc).__name__))
        if ":" in code:
            code = code.split(":", 1)[0]
        run = self.service.get_run(run_id)
        if run.status not in {"COMPLETED", "FAILED", "CANCELLED"}:
            self.service.transition_run(
                run_id,
                status="FAILED",
                stage="技术面流程失败",
                progress=run.progress,
                event_type="RUN_FAILED",
                message=f"技术面节点失败（{code[:100]}）",
                error_message=f"Worker 执行失败（{code[:100]}）",
                current_node=node,
                event_key=f"{run_id}:{node}:failed:1",
            )
        return {"current_node": node, "error_message": code[:100]}

    def _artifact_dir(self, run_id: str) -> Path:
        directory = self.settings.artifacts_dir / run_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _recovery_preflight(
        self, run: Any, next_node: str
    ) -> TechnicalGraphState | None:
        order = [
            "resolve_security",
            "technical_research",
            "kronos",
            "technical_assembly",
            "write_report",
        ]
        if next_node not in order:
            return self._fail(run.run_id, "recovery_preflight", ValueError("未知恢复节点"))
        boundary = order.index(next_node)
        current = self.service.get_run(run.run_id)
        if boundary >= 1 and (not current.resolved_symbol or not current.security_name):
            result = self._resolve_security({"run_id": run.run_id})
            if result.get("error_message"):
                return result
            current = self.service.get_run(run.run_id)
        directory = self._artifact_dir(run.run_id)

        if boundary >= 2 and not self._research_artifacts_valid(current, directory):
            self._invalidate_execution(run.run_id, "technical_research")
            self._invalidate_execution(run.run_id, "technical_assembly")
            self._discard_files(
                directory,
                "market_data.csv",
                "technical_indicators.json",
                "technical_research.json",
                "technical_chart.png",
                "kronos_result.json",
                "technical_assembly.json",
                "technical_report.md",
            )
            result = self._technical_research({"run_id": run.run_id})
            if result.get("error_message"):
                return result
            current = self.service.get_run(run.run_id)

        if boundary >= 3 and not self._kronos_artifact_valid(current, directory):
            self._invalidate_execution(run.run_id, "technical_assembly")
            self._discard_files(
                directory,
                "kronos_result.json",
                "technical_assembly.json",
                "technical_report.md",
            )
            result = self._kronos({"run_id": run.run_id})
            if result.get("error_message"):
                return result

        if boundary >= 4 and not self._assembly_artifact_valid(current, directory):
            self._invalidate_execution(run.run_id, "technical_assembly")
            self._discard_files(
                directory,
                "technical_assembly.json",
                "technical_report.md",
            )
            result = self._technical_assembly({"run_id": run.run_id})
            if result.get("error_message"):
                return result
        return None

    def _research_artifacts_valid(self, run: Any, directory: Path) -> bool:
        try:
            load_persisted_market_data(
                directory / "market_data.csv",
                symbol=run.resolved_symbol,
                as_of=date.fromisoformat(run.as_of),
                expected_data_version=run.data_version,
                min_bars=self.settings.market_data_min_bars,
            )
            indicators = TechnicalIndicators.model_validate_json(
                (directory / "technical_indicators.json").read_text(encoding="utf-8")
            )
            research = TechnicalResearchOutput.model_validate_json(
                (directory / "technical_research.json").read_text(encoding="utf-8")
            )
            self._validate_identity(indicators, run)
            self._validate_identity(research, run)
            execution = self._completed_execution(run.run_id, "technical_research")
            if execution is None or execution.tool_call_count < 3:
                return False
            persisted = TechnicalResearchOutput.model_validate_json(
                execution.validated_output_json or ""
            )
            if research != persisted:
                return False
            tools = {
                item.tool_name
                for item in self.repository.list_tool_executions(execution.execution_id)
                if item.status == "COMPLETED"
            }
            return {
                "get_market_data",
                "calculate_technical_indicators",
                "get_technical_summary",
            }.issubset(tools)
        except (OSError, ValueError):
            return False

    def _kronos_artifact_valid(self, run: Any, directory: Path) -> bool:
        try:
            load_persisted_market_data(
                directory / "market_data.csv",
                symbol=run.resolved_symbol,
                as_of=date.fromisoformat(run.as_of),
                expected_data_version=run.data_version,
                min_bars=self.settings.market_data_min_bars,
            )
            result = KronosResult.model_validate_json(
                (directory / "kronos_result.json").read_text(encoding="utf-8")
            )
            self._validate_identity(result, run)
            return True
        except (OSError, ValueError):
            return False

    def _assembly_artifact_valid(self, run: Any, directory: Path) -> bool:
        try:
            result = TechnicalAssemblyOutput.model_validate_json(
                (directory / "technical_assembly.json").read_text(encoding="utf-8")
            )
            self._validate_identity(result, run)
            execution = self._completed_execution(run.run_id, "technical_assembly")
            if execution is None or execution.tool_call_count != 0:
                return False
            persisted = TechnicalAssemblyOutput.model_validate_json(
                execution.validated_output_json or ""
            )
            return result == persisted
        except (OSError, ValueError):
            return False

    def _invalidate_execution(self, run_id: str, node: str) -> None:
        execution = self._completed_execution(run_id, node)
        if execution is not None:
            self.repository.fail_execution(
                execution.execution_id,
                "ARTIFACT_INVALID",
                "恢复预检发现节点产物缺失、损坏或版本不一致",
                tool_call_count=execution.tool_call_count,
            )

    @staticmethod
    def _discard_files(directory: Path, *names: str) -> None:
        for name in names:
            (directory / name).unlink(missing_ok=True)

    def _completed_execution(self, run_id: str, node: str):
        return next(
            (
                execution
                for execution in self.repository.list_executions(run_id)
                if execution.node_name == node and execution.status == "COMPLETED"
            ),
            None,
        )

    def _next_attempt(self, run_id: str, node: str) -> int:
        records = [
            item
            for item in self.repository.list_executions(run_id)
            if item.node_name == node
        ]
        completed = next((item for item in records if item.status == "COMPLETED"), None)
        if completed:
            return completed.attempt
        for item in records:
            if item.status == "RUNNING":
                self.repository.fail_execution(
                    item.execution_id,
                    "RECOVERED_INCOMPLETE",
                    "Worker 恢复时发现未完成的 Agent Execution",
                    tool_call_count=item.tool_call_count,
                )
        attempt = max((item.attempt for item in records), default=0) + 1
        if attempt > 2:
            raise ValueError("Agent 节点已达最大尝试次数")
        return attempt

    @staticmethod
    def _validate_identity(output: Any, run: Any) -> None:
        if output.symbol != run.resolved_symbol or output.data_version != run.data_version:
            raise ValueError("输出证券或 data_version 与当前任务不一致")


def _atomic_model(model: BaseModel, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)
