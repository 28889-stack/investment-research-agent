from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.runtime.context_loader import ContextLoader
from app.runtime.exceptions import AgentOutputError, ToolBudgetExhaustedError, ToolNotAllowedError
from app.runtime.output_validator import OutputValidator, output_model_for_schema
from app.runtime.pi_client import PiClient
from app.runtime.repository import RuntimeRepository
from app.runtime.schemas import AgentExecutionResult, AgentProfile, ToolExecutionContext
from app.runtime.tool_registry import ToolRegistry
from app.runtime.security import safe_error_message


LOGGER = logging.getLogger(__name__)


def _diagnostic_dir() -> Path | None:
    """Resolve the directory for transient validation-failure diagnostics.

    Controlled by the OUTPUT_DIAGNOSTIC_DIR env var so it stays opt-in and
    never leaks into the default production logging path. Returns None when
    disabled, in which case no diagnostic files are written.
    """

    target = os.environ.get("OUTPUT_DIAGNOSTIC_DIR", "").strip()
    if not target:
        return None
    path = Path(target)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_validation_diagnostic(
    *, run_id: str, node_name: str, schema_name: str, stage: str, error_code: str, error_message: str, raw_output: str
) -> None:
    """Persist a single raw output + error code for forensic debugging.

    The agent's raw/repaired outputs are otherwise transient (the bridge
    discards them), so without this hook a REPAIR_FAILED leaves only the
    generic public-facing message in the DB. Output here is model-generated
    research text, never credentials, but it is still gated behind
    OUTPUT_DIAGNOSTIC_DIR and deleted after a successful verification run.
    """

    directory = _diagnostic_dir()
    if directory is None:
        return
    stamp = time.strftime("%Y%m%dT%H%M%S")
    safe_node = node_name or "node"
    safe_stage = stage or "stage"
    target = directory / f"{stamp}_{run_id}_{safe_node}_{safe_stage}.json"
    payload = {
        "run_id": run_id,
        "node": node_name,
        "schema": schema_name,
        "stage": stage,
        "error_code": error_code,
        "error_message": safe_error_message(error_message, max_length=2000),
        "raw_output": raw_output,
    }
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


class PiAgentAdapter:
    def __init__(
        self,
        *,
        client: PiClient,
        context_loader: ContextLoader,
        tool_registry: ToolRegistry,
        repository: RuntimeRepository,
        output_validator: OutputValidator,
        runtime_mode: str,
        model_provider: str | None,
        model_name: str | None,
        repair_attempts: int,
        max_tool_calls_per_node: int | None = None,
    ) -> None:
        self.client = client
        self.context_loader = context_loader
        self.tool_registry = tool_registry
        self.repository = repository
        self.output_validator = output_validator
        self.runtime_mode = runtime_mode
        self.model_provider = model_provider
        self.model_name = model_name
        self.repair_attempts = repair_attempts
        self.max_tool_calls_per_node = max_tool_calls_per_node

    def run(
        self,
        run_id: str,
        node_name: str,
        profile: AgentProfile,
        task: str,
        context_refs: list[str],
        *,
        attempt: int = 1,
        output_schema_name: str | None = None,
    ) -> AgentExecutionResult:
        started_at = time.monotonic()
        schema_name = output_schema_name or profile.output_schema
        self.tool_registry.validate_profile_permissions(profile)
        context = self.context_loader.load_for_agent(
            run_id, profile, node_name, context_refs, task, output_schema_name=schema_name
        )
        session_id = str(uuid4())
        execution = self.repository.start_execution(
            run_id=run_id,
            node_name=node_name,
            profile=profile,
            session_id=session_id,
            attempt=attempt,
            input_context=context,
            runtime_mode=self.runtime_mode,
            model_provider=self.model_provider,
            model_name=profile.model or self.model_name,
        )
        if execution.status == "COMPLETED" and execution.validated_output_json:
            return AgentExecutionResult(
                execution_id=execution.execution_id,
                session_id=execution.session_id,
                node_name=execution.node_name,
                profile_id=execution.profile_id,
                profile_version=execution.profile_version,
                tool_call_count=execution.tool_call_count,
                output=output_model_for_schema(schema_name).model_validate_json(
                    execution.validated_output_json
                ),
                reused=True,
            )

        # When an idempotent execution row already exists, its session identity
        # remains authoritative; no new cross-session history is introduced.
        session_id = execution.session_id
        tool_call_count = 0
        session_created = False
        try:
            tools = [
                tool.public_descriptor()
                for tool in self.tool_registry.list_for_profile(profile)
            ]
            self.client.create_session(
                session_id=session_id,
                profile=profile.model_dump(mode="json"),
                model={
                    "provider": self.model_provider,
                    "name": profile.model or self.model_name,
                    "runtime_mode": self.runtime_mode,
                },
                tools=tools,
            )
            session_created = True
            execution_context = ToolExecutionContext(
                run_id=run_id,
                agent_execution_id=execution.execution_id,
                profile_id=profile.profile_id,
                profile_mode=profile.mode,
            )

            def execute_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                nonlocal tool_call_count
                effective_limit = profile.max_tool_calls
                if self.max_tool_calls_per_node is not None:
                    effective_limit = min(
                        effective_limit, self.max_tool_calls_per_node
                    )
                if tool_call_count >= effective_limit:
                    if effective_limit > 0:
                        raise ToolBudgetExhaustedError(
                            "工具预算已耗尽；请基于已生成的 Evidence 和未完成项立即输出，不要再调用工具"
                        )
                    raise ToolNotAllowedError("Agent 工具调用次数超过 Profile 上限")
                tool_call_count += 1
                return self.tool_registry.execute(
                    name, arguments, execution_context, profile
                )

            raw_output = self.client.run_agent(
                session_id=session_id,
                system_prompt=profile.system_prompt,
                context=context,
                task=task,
                output_schema=output_model_for_schema(schema_name).model_json_schema(),
                timeout_seconds=profile.timeout_seconds,
                tool_handler=execute_tool,
            )
            validated = self._validate_with_optional_repair(
                session_id, raw_output, profile, schema_name, run_id=run_id, node_name=node_name
            )
            usage_reader = getattr(self.client, "pop_usage", None)
            usage = usage_reader(session_id) if callable(usage_reader) else None
            self.repository.complete_execution(
                execution.execution_id,
                validated.model_dump(mode="json"),
                tool_call_count=tool_call_count,
                usage=usage,
            )
            LOGGER.info(
                "Agent execution completed",
                extra={
                    "component": "runtime",
                    "run_id": run_id,
                    "workflow": None,
                    "node": node_name,
                    "execution_id": execution.execution_id,
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                    "status": "COMPLETED",
                    "error_type": None,
                },
            )
            return AgentExecutionResult(
                execution_id=execution.execution_id,
                session_id=session_id,
                node_name=node_name,
                profile_id=profile.profile_id,
                profile_version=profile.version,
                tool_call_count=tool_call_count,
                output=validated,
            )
        except Exception as exc:
            error_type = getattr(exc, "code", exc.__class__.__name__.upper())
            usage_reader = getattr(self.client, "pop_usage", None)
            usage = usage_reader(session_id) if callable(usage_reader) else None
            self.repository.fail_execution(
                execution.execution_id,
                str(error_type),
                str(exc),
                tool_call_count=tool_call_count,
                usage=usage,
            )
            LOGGER.warning(
                "Agent execution failed",
                extra={
                    "component": "runtime",
                    "run_id": run_id,
                    "workflow": None,
                    "node": node_name,
                    "execution_id": execution.execution_id,
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                    "status": "FAILED",
                    "error_type": str(error_type)[:100],
                    "diagnostic": safe_error_message(str(exc), max_length=500),
                },
            )
            raise
        finally:
            if session_created:
                try:
                    self.client.close_session(session_id)
                except Exception:
                    # The execution error is authoritative. A crashed bridge may
                    # make explicit close impossible, and must not mask it.
                    pass

    def _validate_with_optional_repair(
        self, session_id: str, raw_output: str, profile: AgentProfile, schema_name: str, *, run_id: str = "unknown", node_name: str = "unknown"
    ) -> Any:
        try:
            return self.output_validator.validate_for_schema(
                raw_output, schema_name
            )
        except AgentOutputError as first_error:
            _write_validation_diagnostic(
                run_id=run_id, node_name=node_name, schema_name=schema_name,
                stage="initial", error_code=first_error.code, error_message=str(first_error), raw_output=raw_output,
            )
            if self.repair_attempts < 1:
                raise
            repaired = self.client.repair_output(
                session_id=session_id,
                raw_output=raw_output,
                validation_error=f"{first_error.code}: {first_error}",
                output_schema=output_model_for_schema(schema_name).model_json_schema(),
                timeout_seconds=profile.timeout_seconds,
            )
            try:
                return self.output_validator.validate_for_schema(
                    repaired, schema_name
                )
            except AgentOutputError as repair_error:
                _write_validation_diagnostic(
                    run_id=run_id, node_name=node_name, schema_name=schema_name,
                    stage="repair", error_code=repair_error.code, error_message=str(repair_error), raw_output=repaired,
                )
                raise AgentOutputError(
                    "REPAIR_FAILED",
                    f"Agent 输出修复失败: {repair_error.code}",
                ) from repair_error
