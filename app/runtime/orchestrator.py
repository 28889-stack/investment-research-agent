from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.run_service import RunService
from app.models import ResearchRun
from app.runtime.context_loader import ContextLoader
from app.runtime.exceptions import (
    AgentTimeoutError,
    BridgeCrashedError,
    BridgeStartError,
    ContextTooLargeError,
    ProfileNotFoundError,
    ProfileValidationError,
    ToolInputValidationError,
    ToolNotAllowedError,
)
from app.runtime.output_validator import OutputValidator
from app.runtime.pi_adapter import PiAgentAdapter
from app.runtime.pi_client import BridgePiClient, PiClient
from app.runtime.profiles import ProfileLoader
from app.runtime.repository import RuntimeRepository
from app.runtime.schemas import AgentNodeOutput, AgentProfile
from app.runtime.tool_registry import ToolRegistry, build_runtime_tools


class RuntimeGraphState(TypedDict, total=False):
    run_id: str
    analysis_type: str
    workflow_version: str
    current_node: str
    context_refs: list[str]
    full_execution_id: str
    full_result_ref: str
    constrained_execution_id: str
    constrained_result_ref: str
    report_path: str
    cancel_requested: bool
    error_type: str | None
    error_message: str | None


class RuntimeOrchestrator:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        *,
        pi_client: PiClient | None = None,
        interrupt_after: list[str] | None = None,
        report_writer: Callable[[ResearchRun], Path] | None = None,
    ) -> None:
        self.settings = settings
        self.report_writer = report_writer
        self.service = RunService(
            session_factory, settings.artifacts_dir, settings.pi_runtime_mode
        )
        self.repository = RuntimeRepository(session_factory)
        self.profile_loader = ProfileLoader(settings.agent_profile_dir)
        self.tool_registry = ToolRegistry(self.repository)
        build_runtime_tools(
            self.tool_registry, self.service, settings.tool_default_timeout
        )
        # Validate every profile and its tool permissions at worker startup.
        for required_profile_id in (
            "full_runtime_smoke",
            "constrained_runtime_smoke",
        ):
            self.profile_loader.load(required_profile_id)
        for required_profile_id in (
            "full_runtime_smoke",
            "constrained_runtime_smoke",
        ):
            self.tool_registry.validate_profile_permissions(
                self.profile_loader.load(required_profile_id)
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
        builder = StateGraph(RuntimeGraphState)
        builder.add_node("prepare_context", self._prepare_context)
        builder.add_node("run_full_agent", self._run_full_agent)
        builder.add_node("validate_full_result", self._validate_full_result)
        builder.add_node("run_constrained_agent", self._run_constrained_agent)
        builder.add_node(
            "validate_constrained_result", self._validate_constrained_result
        )
        builder.add_node("write_runtime_report", self._write_runtime_report)
        builder.add_node("mark_cancelled", self._mark_cancelled)
        builder.add_node("mark_failed", self._mark_failed)
        builder.add_edge(START, "prepare_context")
        normal_nodes = [
            ("prepare_context", "run_full_agent"),
            ("run_full_agent", "validate_full_result"),
            ("validate_full_result", "run_constrained_agent"),
            ("run_constrained_agent", "validate_constrained_result"),
            ("validate_constrained_result", "write_runtime_report"),
        ]
        for current, next_node in normal_nodes:
            builder.add_conditional_edges(
                current,
                lambda state, next_node=next_node: self._route(state, next_node),
                {
                    next_node: next_node,
                    "mark_cancelled": "mark_cancelled",
                    "mark_failed": "mark_failed",
                },
            )
        builder.add_edge("write_runtime_report", END)
        builder.add_edge("mark_cancelled", END)
        builder.add_edge("mark_failed", END)
        return builder.compile(
            checkpointer=self.checkpointer, interrupt_after=interrupt_after
        )

    def run(self, run_id: str) -> RuntimeGraphState:
        run = self.service.get_run(run_id)
        # run_id is the checkpoint isolation boundary for every workflow.
        config = {"configurable": {"thread_id": run_id}}
        snapshot = self.graph.get_state(config)
        if snapshot.values and snapshot.next:
            result = self.graph.invoke(None, config)
        elif snapshot.values and not snapshot.next:
            result = snapshot.values
        else:
            result = self.graph.invoke(
                {
                    "run_id": run_id,
                    "analysis_type": run.analysis_type,
                    "workflow_version": run.workflow_version or "v1",
                    "current_node": "",
                    "context_refs": [],
                    "cancel_requested": run.cancel_requested,
                    "error_type": None,
                    "error_message": None,
                },
                config,
            )
        return RuntimeGraphState(**result)

    def shutdown(self) -> None:
        if self._owns_client:
            self.pi_client.shutdown()
        self._checkpoint_connection.close()

    @staticmethod
    def _route(state: RuntimeGraphState, next_node: str) -> str:
        if state.get("cancel_requested"):
            return "mark_cancelled"
        if state.get("error_type"):
            return "mark_failed"
        return next_node

    def _prepare_context(self, state: RuntimeGraphState) -> RuntimeGraphState:
        if self._is_cancel_requested(state["run_id"]):
            return {"cancel_requested": True}
        try:
            self._enter_node(
                state["run_id"],
                "prepare_context",
                status="ROUTING",
                stage="准备 Agent Runtime 上下文",
                progress=25,
                normalized_symbol=self.service.get_run(state["run_id"])
                .input_symbol.strip()
                .upper(),
            )
            return {
                "current_node": "prepare_context",
                "workflow_version": "v1",
                "context_refs": [],
                "cancel_requested": False,
                "error_type": None,
                "error_message": None,
            }
        except Exception as exc:
            return self._error_state(exc)

    def _run_full_agent(self, state: RuntimeGraphState) -> RuntimeGraphState:
        return self._agent_node(
            state,
            node_name="run_full_agent",
            profile_id="full_runtime_smoke",
            task="调用一次白名单安全工具，验证 Full Agent Runtime，并仅输出固定 JSON。",
            context_refs=[],
            result_field="full_execution_id",
        )

    def _validate_full_result(self, state: RuntimeGraphState) -> RuntimeGraphState:
        if self._is_cancel_requested(state["run_id"]):
            return {"cancel_requested": True}
        try:
            self._enter_node(
                state["run_id"],
                "validate_full_result",
                status="RUNNING",
                stage="校验 Full Agent 结果",
                progress=50,
            )
            execution_id = state["full_execution_id"]
            execution = self.repository.get_execution(execution_id)
            self._require_valid_execution(state["run_id"], execution)
            successful_tools = [
                tool
                for tool in self.repository.list_tool_executions(execution_id)
                if tool.status == "COMPLETED"
                and tool.tool_name in {"runtime_echo", "read_run_summary"}
            ]
            if not successful_tools or execution.tool_call_count < 1:
                raise ToolNotAllowedError(
                    "Full Runtime Smoke Agent 未完成白名单工具调用"
                )
            return {
                "current_node": "validate_full_result",
                "full_result_ref": f"execution:{execution_id}",
                "context_refs": [f"execution:{execution_id}"],
            }
        except Exception as exc:
            return self._error_state(exc)

    def _run_constrained_agent(self, state: RuntimeGraphState) -> RuntimeGraphState:
        return self._agent_node(
            state,
            node_name="run_constrained_agent",
            profile_id="constrained_runtime_smoke",
            task="仅总结 Full Agent 已校验的 summary 与 findings，并输出固定 JSON。",
            context_refs=list(state.get("context_refs", [])),
            result_field="constrained_execution_id",
        )

    def _validate_constrained_result(
        self, state: RuntimeGraphState
    ) -> RuntimeGraphState:
        if self._is_cancel_requested(state["run_id"]):
            return {"cancel_requested": True}
        try:
            self._enter_node(
                state["run_id"],
                "validate_constrained_result",
                status="RUNNING",
                stage="校验 Constrained Agent 结果",
                progress=80,
            )
            execution_id = state["constrained_execution_id"]
            execution = self.repository.get_execution(execution_id)
            self._require_valid_execution(state["run_id"], execution)
            if execution.tool_call_count != 0:
                raise ToolNotAllowedError("Constrained Agent 发生了工具调用")
            return {
                "current_node": "validate_constrained_result",
                "constrained_result_ref": f"execution:{execution_id}",
            }
        except Exception as exc:
            return self._error_state(exc)

    def _write_runtime_report(self, state: RuntimeGraphState) -> RuntimeGraphState:
        if self._is_cancel_requested(state["run_id"]):
            self._mark_cancelled(state)
            return {"cancel_requested": True, "current_node": "mark_cancelled"}
        try:
            self._enter_node(
                state["run_id"],
                "write_runtime_report",
                status="REPORTING",
                stage="生成 Runtime 验证报告",
                progress=90,
            )
            report_path = (
                self.report_writer(self.service.get_run(state["run_id"]))
                if self.report_writer
                else self.generate_report(self.service.get_run(state["run_id"]))
            )
            if not self.service.complete_run(state["run_id"], report_path):
                report_path.unlink(missing_ok=True)
                self._mark_cancelled(state)
                return {"cancel_requested": True, "current_node": "mark_cancelled"}
            return {
                "current_node": "write_runtime_report",
                "report_path": str(report_path),
            }
        except Exception as exc:
            error = self._error_state(exc)
            self._mark_failed({**state, **error})
            return {**error, "current_node": "mark_failed"}

    def _mark_cancelled(self, state: RuntimeGraphState) -> RuntimeGraphState:
        run = self.service.get_run(state["run_id"])
        if run.status != "CANCELLED":
            self.service.transition_run(
                state["run_id"],
                status="CANCELLED",
                stage="任务已取消",
                progress=run.progress,
                event_type="RUN_CANCELLED",
                message="LangGraph 已在节点边界停止任务",
                current_node="mark_cancelled",
                event_key=f"{state['run_id']}:mark_cancelled:1",
            )
        else:
            self._set_current_node(state["run_id"], "mark_cancelled")
        return {"current_node": "mark_cancelled", "cancel_requested": True}

    def _mark_failed(self, state: RuntimeGraphState) -> RuntimeGraphState:
        run = self.service.get_run(state["run_id"])
        if run.status not in {"FAILED", "CANCELLED", "COMPLETED"}:
            error_type = state.get("error_type") or "RUNTIME_ERROR"
            self.service.transition_run(
                state["run_id"],
                status="FAILED",
                stage="Agent Runtime 执行失败",
                progress=run.progress,
                event_type="RUN_FAILED",
                message=f"LangGraph 节点失败（{error_type}）",
                error_message=f"Worker 执行失败（{error_type}）",
                current_node="mark_failed",
                event_key=f"{state['run_id']}:mark_failed:1",
            )
        return {"current_node": "mark_failed"}

    def _agent_node(
        self,
        state: RuntimeGraphState,
        *,
        node_name: str,
        profile_id: str,
        task: str,
        context_refs: list[str],
        result_field: str,
    ) -> RuntimeGraphState:
        if self._is_cancel_requested(state["run_id"]):
            return {"cancel_requested": True}
        try:
            self._enter_node(
                state["run_id"],
                node_name,
                status="RUNNING",
                stage=(
                    "执行 Full Agent Runtime"
                    if profile_id == "full_runtime_smoke"
                    else "执行 Constrained Agent Runtime"
                ),
                progress=40 if profile_id == "full_runtime_smoke" else 65,
            )
            profile = self.profile_loader.load(profile_id)
            attempt = self._next_attempt(state["run_id"], node_name)
            while True:
                try:
                    result = self.adapter.run(
                        state["run_id"],
                        node_name,
                        profile,
                        task,
                        context_refs,
                        attempt=attempt,
                    )
                    break
                except (AgentTimeoutError, BridgeCrashedError, BridgeStartError):
                    if attempt >= 2:
                        raise
                    attempt += 1
            return {
                "current_node": node_name,
                result_field: result.execution_id,
            }
        except Exception as exc:
            return self._error_state(exc)

    def _next_attempt(self, run_id: str, node_name: str) -> int:
        records = [
            item
            for item in self.repository.list_executions(run_id)
            if item.node_name == node_name
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
        next_attempt = max((item.attempt for item in records), default=0) + 1
        if next_attempt > 2:
            from app.runtime.exceptions import CheckpointError

            raise CheckpointError("Agent 节点已达最大尝试次数")
        return next_attempt

    @staticmethod
    def _require_valid_execution(run_id: str, execution: Any) -> None:
        if execution.run_id != run_id or execution.status != "COMPLETED":
            raise ValueError("Agent Execution 未完成或不属于当前任务")
        if not execution.validated_output_json:
            raise ValueError("Agent Execution 缺少已校验输出")
        AgentNodeOutput.model_validate_json(execution.validated_output_json)

    def _is_cancel_requested(self, run_id: str) -> bool:
        run = self.service.get_run(run_id)
        return run.cancel_requested or run.status == "CANCELLED"

    def _enter_node(
        self,
        run_id: str,
        node_name: str,
        *,
        status: str,
        stage: str,
        progress: int,
        normalized_symbol: str | None = None,
    ) -> None:
        self.service.transition_run(
            run_id,
            status=status,
            stage=stage,
            progress=progress,
            event_type="LANGGRAPH_NODE_STARTED",
            message=f"进入 LangGraph 节点：{node_name}",
            current_node=node_name,
            event_key=f"{run_id}:{node_name}:started:1",
            normalized_symbol=normalized_symbol,
        )

    def _set_current_node(self, run_id: str, node_name: str) -> None:
        run = self.service.get_run(run_id)
        self.service.transition_run(
            run_id,
            status=run.status,
            stage=run.current_stage,
            progress=run.progress,
            event_type="LANGGRAPH_NODE_RESTORED",
            message=f"恢复 LangGraph 节点：{node_name}",
            current_node=node_name,
            event_key=f"{run_id}:{node_name}:restored:1",
        )

    @staticmethod
    def _error_state(exc: Exception) -> RuntimeGraphState:
        code = str(getattr(exc, "code", exc.__class__.__name__))
        return {
            "error_type": code[:100],
            "error_message": f"节点执行失败（{code[:100]}）",
        }

    def generate_report(self, run: ResearchRun) -> Path:
        executions = self.repository.list_executions(run.run_id)
        full = next(
            item
            for item in executions
            if item.node_name == "run_full_agent" and item.status == "COMPLETED"
        )
        constrained = next(
            item
            for item in executions
            if item.node_name == "run_constrained_agent" and item.status == "COMPLETED"
        )
        full_output = AgentNodeOutput.model_validate_json(full.validated_output_json or "")
        constrained_output = AgentNodeOutput.model_validate_json(
            constrained.validated_output_json or ""
        )
        report_dir = self.settings.artifacts_dir / run.run_id / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "runtime_report.md"
        temporary_path = report_dir / "runtime_report.md.tmp"
        report = f"""# Agent Runtime 验证报告

## 任务信息

- 任务 ID：{run.run_id}
- 证券输入：{run.input_symbol}
- 分析类型：{run.analysis_type}
- 数据截止日期：{run.as_of}
- Runtime 模式：{run.runtime_mode}

## LangGraph 执行节点

prepare_context → run_full_agent → validate_full_result → run_constrained_agent → validate_constrained_result → write_runtime_report

## Full Agent 验证结果

{full_output.summary}

## Constrained Agent 验证结果

{constrained_output.summary}

## 工具权限验证

Full Agent 通过 Python ToolRegistry 执行 {full.tool_call_count} 次白名单工具；Constrained Agent 执行 {constrained.tool_call_count} 次工具。

## 输出 Schema 验证

两个 Agent 输出均通过 AgentNodeOutput Schema 校验后入库。

## Session 隔离验证

Full Session `{full.session_id}` 与 Constrained Session `{constrained.session_id}` 相互独立。

## Checkpoint 信息

- thread_id：{run.checkpoint_thread_id}
- workflow：{run.workflow_name} / {run.workflow_version}
- 独立 Checkpoint 数据库：已启用

## 当前阶段说明

本报告仅用于验证第二阶段 Agent Runtime 和流程编排能力，
不包含真实行情、财务数据、估值结果或投资建议。

该流程已替换第一阶段项目骨架中的手工模拟阶段，不生成个股研究结论。

## 个股研究报告功能边界

本阶段仅保留报告文件交付链路，不生成交易方向或价格预测。
"""
        temporary_path.write_text(report, encoding="utf-8")
        os.replace(temporary_path, report_path)
        return report_path
