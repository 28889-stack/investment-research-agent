from __future__ import annotations

import time
import logging
import signal
import threading
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.run_service import RunService
from app.runtime.exceptions import (
    ToolInputValidationError,
    ToolNotAllowedError,
    ToolTimeoutError,
)
from app.runtime.repository import RuntimeRepository
from app.runtime.schemas import AgentProfile, ToolExecutionContext
from app.runtime.security import safe_error_message


LOGGER = logging.getLogger(__name__)


class RuntimeEchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=2_000)


class RuntimeEchoOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    echo: str


class ReadRunSummaryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadRunSummaryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    input_symbol: str
    analysis_type: str
    as_of: str
    status: str


ToolHandler = Callable[[BaseModel, ToolExecutionContext], dict[str, Any]]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    allowed_modes: set[str]
    supported_profiles: set[str]
    timeout_seconds: float
    cost_level: str
    side_effect: bool
    handler: ToolHandler

    def public_descriptor(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
            "output_schema": self.output_model.model_json_schema(),
        }


class ToolRegistry:
    def __init__(self, repository: RuntimeRepository) -> None:
        self.repository = repository
        self._tools: dict[str, ToolDefinition] = {}

    @property
    def tool_count(self) -> int:
        return len(self._tools)

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, tool_name: str) -> ToolDefinition:
        tool = self._tools.get(tool_name)
        if tool is None:
            raise ToolNotAllowedError(f"工具未注册或不允许调用: {tool_name}")
        return tool

    def list_for_profile(self, profile: AgentProfile) -> list[ToolDefinition]:
        self.validate_profile_permissions(profile)
        return [self._tools[name] for name in profile.allowed_tools]

    def validate_profile_permissions(self, profile: AgentProfile) -> None:
        for name in profile.allowed_tools:
            tool = self._tools.get(name)
            if tool is None:
                raise ToolNotAllowedError(f"Profile 引用了未注册工具: {name}")
            if profile.mode not in tool.allowed_modes:
                raise ToolNotAllowedError(
                    f"{profile.mode} Profile 不允许调用工具: {name}"
                )
            if profile.profile_id not in tool.supported_profiles:
                raise ToolNotAllowedError(
                    f"工具 {name} 未授权给 Profile: {profile.profile_id}"
                )

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        execution_context: ToolExecutionContext,
        profile: AgentProfile,
    ) -> dict[str, Any]:
        # This second authorization check is the trust boundary. The bridge's
        # advertised tool list is only a convenience and is never trusted here.
        tool = self.get(tool_name)
        if (
            tool_name not in profile.allowed_tools
            or profile.mode not in tool.allowed_modes
            or profile.profile_id not in tool.supported_profiles
            or execution_context.profile_id != profile.profile_id
            or execution_context.profile_mode != profile.mode
        ):
            raise ToolNotAllowedError(f"Profile 无权调用工具: {tool_name}")

        try:
            validated_arguments = tool.input_model.model_validate(arguments)
        except ValidationError as exc:
            raise ToolInputValidationError(f"工具输入不符合 Schema: {tool_name}") from exc

        safe_arguments = _redact_sensitive(validated_arguments.model_dump(mode="json"))
        record = self.repository.start_tool_execution(
            agent_execution_id=execution_context.agent_execution_id,
            run_id=execution_context.run_id,
            tool_name=tool_name,
            arguments=safe_arguments,
        )
        started = time.monotonic()
        try:
            raw_result = _run_with_deadline(
                tool.handler,
                validated_arguments,
                execution_context,
                timeout_seconds=tool.timeout_seconds,
            )
            validated_result = tool.output_model.model_validate(raw_result)
            result = validated_result.model_dump(mode="json")
            duration_ms = max(0, int((time.monotonic() - started) * 1_000))
            self.repository.complete_tool_execution(
                record.tool_execution_id, result, duration_ms
            )
            LOGGER.info(
                "Tool execution completed",
                extra={
                    "component": "tool",
                    "run_id": execution_context.run_id,
                    "workflow": None,
                    "node": tool_name,
                    "execution_id": execution_context.agent_execution_id,
                    "duration_ms": duration_ms,
                    "status": "COMPLETED",
                    "error_type": None,
                },
            )
            return result
        except ToolTimeoutError as exc:
            duration_ms = max(0, int((time.monotonic() - started) * 1_000))
            message = f"工具执行超时: {tool_name}"
            self.repository.fail_tool_execution(
                record.tool_execution_id, message, duration_ms
            )
            LOGGER.warning(
                "Tool execution timed out",
                extra={"component": "tool", "run_id": execution_context.run_id, "workflow": None, "node": tool_name, "execution_id": execution_context.agent_execution_id, "duration_ms": duration_ms, "status": "FAILED", "error_type": "TOOL_TIMEOUT"},
            )
            raise ToolTimeoutError(message) from exc
        except Exception as exc:
            duration_ms = max(0, int((time.monotonic() - started) * 1_000))
            self.repository.fail_tool_execution(
                record.tool_execution_id, str(exc), duration_ms
            )
            LOGGER.warning(
                "Tool execution failed",
                extra={"component": "tool", "run_id": execution_context.run_id, "workflow": None, "node": tool_name, "execution_id": execution_context.agent_execution_id, "duration_ms": duration_ms, "status": "FAILED", "error_type": type(exc).__name__, "diagnostic": safe_error_message(str(exc), max_length=500)},
            )
            raise


def _run_with_deadline(handler, arguments, context, *, timeout_seconds: float):
    """Use the Worker thread so timeout cannot leave a late-writing thread."""
    # Parallel Deep retrieval executes bounded provider/read lanes from worker
    # threads. POSIX interval timers are process-wide and can only be installed
    # safely by the main thread, so those lanes rely on each HTTP adapter's
    # explicit client timeout. Interactive Agent tool calls still use SIGALRM
    # below for the normal per-tool deadline.
    if threading.current_thread() is not threading.main_thread():
        return handler(arguments, context)
    if not hasattr(signal, "SIGALRM"):
        return handler(arguments, context)

    def timeout_handler(_signum, _frame):
        raise ToolTimeoutError("工具执行超时")

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, timeout_handler)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        return handler(arguments, context)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if any(word in key.lower() for word in ("key", "token", "password", "secret"))
                else _redact_sensitive(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


def build_runtime_tools(
    registry: ToolRegistry, run_service: RunService, default_timeout: float
) -> ToolRegistry:
    def runtime_echo(
        arguments: BaseModel, _context: ToolExecutionContext
    ) -> dict[str, Any]:
        assert isinstance(arguments, RuntimeEchoInput)
        return {"echo": arguments.message}

    def read_run_summary(
        _arguments: BaseModel, context: ToolExecutionContext
    ) -> dict[str, Any]:
        run = run_service.get_run(context.run_id)
        return {
            "run_id": run.run_id,
            "input_symbol": run.input_symbol,
            "analysis_type": run.analysis_type,
            "as_of": run.as_of,
            "status": run.status,
        }

    registry.register(
        ToolDefinition(
            name="runtime_echo",
            description="返回输入内容，用于验证工具调用链路",
            input_model=RuntimeEchoInput,
            output_model=RuntimeEchoOutput,
            allowed_modes={"full"},
            supported_profiles={"full_runtime_smoke"},
            timeout_seconds=default_timeout,
            cost_level="low",
            side_effect=False,
            handler=runtime_echo,
        )
    )
    registry.register(
        ToolDefinition(
            name="read_run_summary",
            description="读取当前任务的最小安全摘要",
            input_model=ReadRunSummaryInput,
            output_model=ReadRunSummaryOutput,
            allowed_modes={"full"},
            supported_profiles={"full_runtime_smoke"},
            timeout_seconds=default_timeout,
            cost_level="low",
            side_effect=False,
            handler=read_run_summary,
        )
    )
    return registry
