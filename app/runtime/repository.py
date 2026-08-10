from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import AgentExecution, ToolExecution
from app.run_service import utc_now
from app.runtime.schemas import AgentProfile
from app.runtime.security import public_execution_error, safe_error_message


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class RuntimeRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def start_execution(
        self,
        *,
        run_id: str,
        node_name: str,
        profile: AgentProfile,
        session_id: str,
        attempt: int,
        input_context: dict[str, Any],
        runtime_mode: str,
        model_provider: str | None,
        model_name: str | None,
    ) -> AgentExecution:
        with self.session_factory.begin() as session:
            existing = session.scalar(
                select(AgentExecution).where(
                    AgentExecution.run_id == run_id,
                    AgentExecution.node_name == node_name,
                    AgentExecution.attempt == attempt,
                )
            )
            if existing is not None:
                session.expunge(existing)
                return existing
            now = utc_now()
            execution = AgentExecution(
                execution_id=str(uuid4()),
                run_id=run_id,
                node_name=node_name,
                profile_id=profile.profile_id,
                profile_version=profile.version,
                session_id=session_id,
                status="RUNNING",
                attempt=attempt,
                input_context_json=_json(input_context),
                validated_output_json=None,
                error_type=None,
                error_message=None,
                model_provider=model_provider,
                model_name=model_name,
                runtime_mode=runtime_mode,
                tool_call_count=0,
                started_at=now,
                completed_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(execution)
            session.flush()
            session.expunge(execution)
            return execution

    def complete_execution(
        self, execution_id: str, validated_output: dict[str, Any], *, tool_call_count: int,
        usage: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        with self.session_factory.begin() as session:
            execution = self._require_execution(session, execution_id)
            execution.status = "COMPLETED"
            execution.validated_output_json = _json(validated_output)
            execution.error_type = None
            execution.error_message = None
            execution.tool_call_count = tool_call_count
            if usage:
                execution.input_tokens = _nonnegative_int(usage.get("input_tokens"))
                execution.output_tokens = _nonnegative_int(usage.get("output_tokens"))
                execution.total_tokens = _nonnegative_int(usage.get("total_tokens"))
                execution.estimated_cost = _nonnegative_float(usage.get("estimated_cost"))
                currency = usage.get("cost_currency")
                execution.cost_currency = str(currency)[:12] if currency else None
            execution.completed_at = now
            execution.updated_at = now

    def fail_execution(
        self, execution_id: str, error_type: str, error_message: str, *,
        tool_call_count: int = 0, usage: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        with self.session_factory.begin() as session:
            execution = self._require_execution(session, execution_id)
            execution.status = "FAILED"
            execution.validated_output_json = None
            execution.error_type = error_type[:100]
            # Provider/Bridge messages are diagnostics, not a public contract.
            # Persist only a stable typed message so API responses cannot expose
            # raw provider bodies, local paths, or credentials.
            execution.error_message = public_execution_error(error_type)
            execution.tool_call_count = tool_call_count
            if usage:
                execution.input_tokens = _nonnegative_int(usage.get("input_tokens"))
                execution.output_tokens = _nonnegative_int(usage.get("output_tokens"))
                execution.total_tokens = _nonnegative_int(usage.get("total_tokens"))
                execution.estimated_cost = _nonnegative_float(usage.get("estimated_cost"))
                currency = usage.get("cost_currency")
                execution.cost_currency = str(currency)[:12] if currency else None
            execution.completed_at = now
            execution.updated_at = now

    def get_execution(self, execution_id: str) -> AgentExecution:
        with self.session_factory() as session:
            execution = self._require_execution(session, execution_id)
            session.expunge(execution)
            return execution

    def list_executions(self, run_id: str) -> list[AgentExecution]:
        with self.session_factory() as session:
            records = list(
                session.scalars(
                    select(AgentExecution)
                    .where(AgentExecution.run_id == run_id)
                    .order_by(AgentExecution.id)
                )
            )
            for record in records:
                session.expunge(record)
            return records

    def usage_summary(self, run_id: str) -> dict[str, Any]:
        records = self.list_executions(run_id)
        token_records = [item for item in records if item.total_tokens is not None]
        cost_records = [item for item in records if item.estimated_cost is not None]
        currencies = {item.cost_currency for item in cost_records if item.cost_currency}
        return {
            "agent_calls": len(records),
            "tool_calls": sum(item.tool_call_count for item in records),
            "input_tokens": sum(item.input_tokens or 0 for item in token_records) if token_records else None,
            "output_tokens": sum(item.output_tokens or 0 for item in token_records) if token_records else None,
            "total_tokens": sum(item.total_tokens or 0 for item in token_records) if token_records else None,
            "estimated_cost": sum(item.estimated_cost or 0 for item in cost_records) if cost_records else None,
            "cost_currency": next(iter(currencies)) if len(currencies) == 1 else None,
        }

    def start_tool_execution(
        self,
        *,
        agent_execution_id: str,
        run_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolExecution:
        now = utc_now()
        record = ToolExecution(
            tool_execution_id=str(uuid4()),
            agent_execution_id=agent_execution_id,
            run_id=run_id,
            tool_name=tool_name,
            arguments_json=_json(arguments),
            result_summary_json=None,
            status="RUNNING",
            error_message=None,
            duration_ms=None,
            created_at=now,
            completed_at=None,
        )
        with self.session_factory.begin() as session:
            session.add(record)
            session.flush()
            session.expunge(record)
        return record

    def complete_tool_execution(
        self, tool_execution_id: str, result_summary: dict[str, Any], duration_ms: int
    ) -> None:
        with self.session_factory.begin() as session:
            record = self._require_tool_execution(session, tool_execution_id)
            record.status = "COMPLETED"
            record.result_summary_json = _json(result_summary)
            record.duration_ms = duration_ms
            record.completed_at = utc_now()

    def fail_tool_execution(
        self, tool_execution_id: str, error_message: str, duration_ms: int
    ) -> None:
        with self.session_factory.begin() as session:
            record = self._require_tool_execution(session, tool_execution_id)
            record.status = "FAILED"
            record.error_message = safe_error_message(error_message)
            record.duration_ms = duration_ms
            record.completed_at = utc_now()

    def list_tool_executions(self, agent_execution_id: str) -> list[ToolExecution]:
        with self.session_factory() as session:
            records = list(
                session.scalars(
                    select(ToolExecution)
                    .where(ToolExecution.agent_execution_id == agent_execution_id)
                    .order_by(ToolExecution.id)
                )
            )
            for record in records:
                session.expunge(record)
            return records

    @staticmethod
    def _require_execution(session: Session, execution_id: str) -> AgentExecution:
        execution = session.scalar(
            select(AgentExecution).where(AgentExecution.execution_id == execution_id)
        )
        if execution is None:
            raise ValueError(f"Agent execution not found: {execution_id}")
        return execution

    @staticmethod
    def _require_tool_execution(session: Session, tool_execution_id: str) -> ToolExecution:
        execution = session.scalar(
            select(ToolExecution).where(
                ToolExecution.tool_execution_id == tool_execution_id
            )
        )
        if execution is None:
            raise ValueError(f"Tool execution not found: {tool_execution_id}")
        return execution


def _nonnegative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _nonnegative_float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return None
