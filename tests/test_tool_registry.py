import time

import pytest
from pydantic import BaseModel

from app.run_service import RunService
from app.runtime.exceptions import (
    ToolInputValidationError,
    ToolNotAllowedError,
    ToolTimeoutError,
)
from app.runtime.profiles import ProfileLoader
from app.runtime.repository import RuntimeRepository
from app.runtime.schemas import ToolExecutionContext
from app.runtime.tool_registry import ToolDefinition, ToolRegistry, build_runtime_tools


class EmptyInput(BaseModel):
    pass


class EmptyOutput(BaseModel):
    ok: bool


def setup_registry(settings, session_factory):
    service = RunService(session_factory, settings.artifacts_dir)
    repository = RuntimeRepository(session_factory)
    registry = ToolRegistry(repository)
    build_runtime_tools(registry, service, settings.tool_default_timeout)
    profile = ProfileLoader(settings.agent_profile_dir).load("full_runtime_smoke")
    run = service.create_run(symbol="600519", analysis_type="technical")
    execution = repository.start_execution(
        run_id=run.run_id,
        node_name="run_full_agent",
        profile=profile,
        session_id="session-1",
        attempt=1,
        input_context={},
        runtime_mode="mock",
        model_provider=None,
        model_name=None,
    )
    context = ToolExecutionContext(
        run_id=run.run_id,
        agent_execution_id=execution.execution_id,
        profile_id=profile.profile_id,
        profile_mode=profile.mode,
    )
    return service, repository, registry, profile, context


def test_executes_runtime_echo_and_records_tool_call(settings, session_factory):
    _service, repository, registry, profile, context = setup_registry(
        settings, session_factory
    )

    result = registry.execute("runtime_echo", {"message": "bridge-ok"}, context, profile)

    assert result == {"echo": "bridge-ok"}
    records = repository.list_tool_executions(context.agent_execution_id)
    assert len(records) == 1
    assert records[0].status == "COMPLETED"
    assert records[0].tool_name == "runtime_echo"


def test_read_run_summary_exposes_only_whitelisted_fields(settings, session_factory):
    _service, _repository, registry, profile, context = setup_registry(
        settings, session_factory
    )

    result = registry.execute("read_run_summary", {}, context, profile)

    assert set(result) == {"run_id", "input_symbol", "analysis_type", "as_of", "status"}
    assert "database_url" not in str(result).lower()


def test_unknown_and_unauthorized_tools_are_rejected(settings, session_factory):
    _service, _repository, registry, profile, context = setup_registry(
        settings, session_factory
    )

    with pytest.raises(ToolNotAllowedError):
        registry.execute("shell", {}, context, profile)

    constrained = ProfileLoader(settings.agent_profile_dir).load(
        "constrained_runtime_smoke"
    )
    constrained_context = context.model_copy(
        update={"profile_id": constrained.profile_id, "profile_mode": "constrained"}
    )
    with pytest.raises(ToolNotAllowedError):
        registry.execute("runtime_echo", {"message": "no"}, constrained_context, constrained)


def test_invalid_tool_input_is_rejected(settings, session_factory):
    _service, _repository, registry, profile, context = setup_registry(
        settings, session_factory
    )

    with pytest.raises(ToolInputValidationError):
        registry.execute("runtime_echo", {"wrong": "field"}, context, profile)


def test_profile_permission_validation_rejects_unknown_and_full_only_tools(
    settings, session_factory
):
    _service, _repository, registry, _profile, _context = setup_registry(
        settings, session_factory
    )
    loader = ProfileLoader(settings.agent_profile_dir)
    constrained = loader.load("constrained_runtime_smoke").model_copy(
        update={"allowed_tools": ["runtime_echo"]}
    )
    unknown = loader.load("full_runtime_smoke").model_copy(
        update={"allowed_tools": ["missing_tool"]}
    )

    with pytest.raises(ToolNotAllowedError):
        registry.validate_profile_permissions(constrained)
    with pytest.raises(ToolNotAllowedError):
        registry.validate_profile_permissions(unknown)


def test_tool_timeout_is_failed_and_recorded(settings, session_factory):
    _service, repository, registry, profile, context = setup_registry(
        settings, session_factory
    )

    late_mutations = []

    def slow_handler(_arguments, _context):
        time.sleep(0.05)
        late_mutations.append("mutated")
        return {"ok": True}

    registry.register(
        ToolDefinition(
            name="slow_tool",
            description="test timeout",
            input_model=EmptyInput,
            output_model=EmptyOutput,
            allowed_modes={"full"},
            supported_profiles={profile.profile_id},
            timeout_seconds=0.001,
            cost_level="low",
            side_effect=False,
            handler=slow_handler,
        )
    )
    timed_profile = profile.model_copy(
        update={"allowed_tools": [*profile.allowed_tools, "slow_tool"]}
    )

    with pytest.raises(ToolTimeoutError):
        registry.execute("slow_tool", {}, context, timed_profile)

    assert repository.list_tool_executions(context.agent_execution_id)[-1].status == "FAILED"
    time.sleep(0.06)
    assert late_mutations == []


def test_tool_handler_failure_preserves_original_error_and_redacts_diagnostic(
    settings, session_factory, caplog
):
    _service, repository, registry, profile, context = setup_registry(
        settings, session_factory
    )

    def failing_handler(_arguments, _context):
        raise RuntimeError('provider failed {"api_key":"two word secret"}')

    registry.register(
        ToolDefinition(
            name="failing_tool",
            description="test failure",
            input_model=EmptyInput,
            output_model=EmptyOutput,
            allowed_modes={"full"},
            supported_profiles={profile.profile_id},
            timeout_seconds=1,
            cost_level="low",
            side_effect=False,
            handler=failing_handler,
        )
    )
    permitted = profile.model_copy(
        update={"allowed_tools": [*profile.allowed_tools, "failing_tool"]}
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        registry.execute("failing_tool", {}, context, permitted)

    record = repository.list_tool_executions(context.agent_execution_id)[-1]
    assert record.status == "FAILED"
    diagnostic = next(
        item.diagnostic for item in caplog.records if item.message == "Tool execution failed"
    )
    assert "two word secret" not in diagnostic
