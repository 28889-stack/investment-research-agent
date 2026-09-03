import json

import pytest

from app.run_service import RunService
from app.runtime.context_loader import ContextLoader
from app.runtime.exceptions import (
    AgentOutputError,
    AgentTimeoutError,
    ToolBudgetExhaustedError,
    ToolNotAllowedError,
)
from app.runtime.output_validator import OutputValidator
from app.runtime.pi_adapter import PiAgentAdapter
from app.runtime.pi_client import MockPiClient
from app.runtime.profiles import ProfileLoader
from app.runtime.repository import RuntimeRepository
from app.runtime.tool_registry import ToolRegistry, build_runtime_tools


class UsageMockPiClient(MockPiClient):
    def pop_usage(self, _session_id):
        return {
            "input_tokens": 11,
            "output_tokens": 7,
            "total_tokens": 18,
            "estimated_cost": None,
            "cost_currency": None,
        }


class FailedUsageMockPiClient(UsageMockPiClient):
    def __init__(self):
        super().__init__(scenario="invalid_json")


class BudgetExhaustionThenFinalClient(MockPiClient):
    """Simulates a live Agent receiving a budget-exhausted tool result then finalizing."""

    def run_agent(self, *, tool_handler, **kwargs):
        tool_handler("runtime_echo", {"message": "first"})
        with pytest.raises(ToolBudgetExhaustedError):
            tool_handler("runtime_echo", {"message": "second"})
        return json.dumps(
            {
                "task_id": "runtime_full_test",
                "status": "completed",
                "summary": "已基于预算耗尽前的工具结果完成输出。",
                "findings": [],
                "new_evidence": [],
                "new_assumptions": [],
                "risks": [],
                "conflicts": [],
                "missing_information": ["工具预算已耗尽，未继续检索。"],
                "suggested_followups": [],
            },
            ensure_ascii=False,
        )


def build_adapter(settings, session_factory, scenario="valid"):
    service = RunService(session_factory, settings.artifacts_dir)
    repository = RuntimeRepository(session_factory)
    registry = ToolRegistry(repository)
    build_runtime_tools(registry, service, settings.tool_default_timeout)
    client = MockPiClient(scenario=scenario)
    adapter = PiAgentAdapter(
        client=client,
        context_loader=ContextLoader(
            service, repository, max_context_chars=settings.max_agent_context_chars
        ),
        tool_registry=registry,
        repository=repository,
        output_validator=OutputValidator(settings.max_agent_output_chars),
        runtime_mode="mock",
        model_provider=None,
        model_name=None,
        repair_attempts=1,
        max_tool_calls_per_node=settings.max_tool_calls_per_node,
    )
    return service, repository, client, adapter


def test_full_agent_uses_only_registered_tool_and_saves_validated_output(
    settings, session_factory
):
    service, repository, client, adapter = build_adapter(
        settings, session_factory, scenario="tool_call"
    )
    run = service.create_run(symbol="600519", analysis_type="technical")
    profile = ProfileLoader(settings.agent_profile_dir).load("full_runtime_smoke")

    result = adapter.run(
        run.run_id, "run_full_agent", profile, "验证 Full Agent", []
    )

    assert result.output.status == "completed"
    assert result.tool_call_count == 1
    assert repository.get_execution(result.execution_id).status == "COMPLETED"
    tools = repository.list_tool_executions(result.execution_id)
    assert [record.tool_name for record in tools] == ["runtime_echo"]
    assert client.closed_sessions == [result.session_id]


def test_full_and_constrained_calls_use_isolated_sessions(settings, session_factory):
    service, repository, _client, adapter = build_adapter(settings, session_factory)
    run = service.create_run(symbol="600519", analysis_type="technical")
    profiles = ProfileLoader(settings.agent_profile_dir)
    full = adapter.run(
        run.run_id,
        "run_full_agent",
        profiles.load("full_runtime_smoke"),
        "full",
        [],
    )
    constrained = adapter.run(
        run.run_id,
        "run_constrained_agent",
        profiles.load("constrained_runtime_smoke"),
        "constrained",
        [f"execution:{full.execution_id}"],
    )

    assert full.session_id != constrained.session_id
    assert constrained.tool_call_count == 0
    assert repository.list_tool_executions(constrained.execution_id) == []


def test_unauthorized_mock_tool_call_is_rejected_and_execution_failed(
    settings, session_factory
):
    service, repository, client, adapter = build_adapter(
        settings, session_factory, scenario="unauthorized_tool"
    )
    run = service.create_run(symbol="600519", analysis_type="technical")
    profile = ProfileLoader(settings.agent_profile_dir).load("full_runtime_smoke")

    with pytest.raises(ToolNotAllowedError):
        adapter.run(run.run_id, "run_full_agent", profile, "full", [])

    execution = repository.list_executions(run.run_id)[0]
    assert execution.status == "FAILED"
    assert execution.error_type == "TOOL_NOT_ALLOWED"
    assert client.closed_sessions == [execution.session_id]


def test_invalid_output_is_not_saved_and_execution_failed(settings, session_factory):
    service, repository, _client, adapter = build_adapter(
        settings, session_factory, scenario="invalid_json"
    )
    run = service.create_run(symbol="600519", analysis_type="technical")
    profile = ProfileLoader(settings.agent_profile_dir).load("full_runtime_smoke")

    with pytest.raises(AgentOutputError):
        adapter.run(run.run_id, "run_full_agent", profile, "full", [])

    execution = repository.list_executions(run.run_id)[0]
    assert execution.status == "FAILED"
    assert execution.validated_output_json is None


def test_timeout_closes_session_and_records_stable_error(settings, session_factory):
    service, repository, client, adapter = build_adapter(
        settings, session_factory, scenario="timeout"
    )
    run = service.create_run(symbol="600519", analysis_type="technical")
    profile = ProfileLoader(settings.agent_profile_dir).load("full_runtime_smoke")

    with pytest.raises(AgentTimeoutError):
        adapter.run(run.run_id, "run_full_agent", profile, "full", [])

    execution = repository.list_executions(run.run_id)[0]
    assert execution.error_type == "AGENT_TIMEOUT"
    assert client.closed_sessions == [execution.session_id]


def test_global_tool_cap_can_be_stricter_than_profile(settings, session_factory):
    limited = settings.model_copy(update={"max_tool_calls_per_node": 0})
    service, repository, _client, adapter = build_adapter(
        limited, session_factory, scenario="tool_call"
    )
    run = service.create_run(symbol="600519", analysis_type="technical")
    profile = ProfileLoader(settings.agent_profile_dir).load("full_runtime_smoke")

    with pytest.raises(ToolNotAllowedError):
        adapter.run(run.run_id, "run_full_agent", profile, "full", [])

    execution = repository.list_executions(run.run_id)[0]
    assert execution.tool_call_count == 0
    assert repository.list_tool_executions(execution.execution_id) == []


def test_full_agent_can_finalize_after_tool_budget_exhaustion(settings, session_factory):
    limited = settings.model_copy(update={"max_tool_calls_per_node": 1})
    service, repository, _client, adapter = build_adapter(limited, session_factory)
    adapter.client = BudgetExhaustionThenFinalClient()
    run = service.create_run(symbol="600519", analysis_type="technical")
    profile = ProfileLoader(limited.agent_profile_dir).load("full_runtime_smoke")

    result = adapter.run(run.run_id, "run_full_agent", profile, "full", [])

    assert result.tool_call_count == 1
    assert result.output.missing_information == ["工具预算已耗尽，未继续检索。"]
    assert repository.get_execution(result.execution_id).status == "COMPLETED"


def test_adapter_persists_usage_when_provider_reports_it(settings, session_factory):
    service, repository, _client, adapter = build_adapter(settings, session_factory)
    adapter.client = UsageMockPiClient()
    run = service.create_run(symbol="600519", analysis_type="technical")

    result = adapter.run(
        run.run_id,
        "usage_agent",
        ProfileLoader(settings.agent_profile_dir).load("full_runtime_smoke"),
        "usage",
        [],
    )

    execution = repository.get_execution(result.execution_id)
    assert execution.input_tokens == 11
    assert execution.output_tokens == 7
    assert execution.total_tokens == 18
    assert execution.estimated_cost is None


def test_adapter_persists_usage_when_provider_output_validation_fails(
    settings, session_factory
):
    service, repository, _client, adapter = build_adapter(settings, session_factory)
    adapter.client = FailedUsageMockPiClient()
    run = service.create_run(symbol="600519", analysis_type="technical")

    with pytest.raises(AgentOutputError):
        adapter.run(
            run.run_id,
            "failed_usage_agent",
            ProfileLoader(settings.agent_profile_dir).load("full_runtime_smoke"),
            "usage",
            [],
        )

    execution = repository.list_executions(run.run_id)[0]
    assert execution.status == "FAILED"
    assert execution.total_tokens == 18
    assert execution.estimated_cost is None
    assert execution.error_message == "Agent 输出未通过结构校验"
