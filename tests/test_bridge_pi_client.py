import sys
import pytest

from app.runtime import pi_client as pi_client_module
from app.runtime.pi_client import BridgePiClient
from app.runtime.exceptions import ToolBudgetExhaustedError, ToolNotAllowedError


@pytest.mark.parametrize(
    ("provider", "provider_variable"),
    [
        ("openai", "OPENAI_API_KEY"),
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("deepseek", "DEEPSEEK_API_KEY"),
    ],
)
def test_live_client_maps_configured_secret_to_provider_variable(
    settings, monkeypatch, provider, provider_variable
):
    monkeypatch.setenv("CUSTOM_PI_SECRET", "test-secret-value")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-be-forwarded")
    client = BridgePiClient(
        command="node",
        entrypoint=settings.pi_bridge_entry,
        runtime_mode="live",
        start_timeout=5,
        request_timeout=5,
        max_restarts=0,
        model_provider=provider,
        model_name="test-model",
        api_key_env_name="CUSTOM_PI_SECRET",
    )

    environment = client._restricted_environment()

    assert environment["CUSTOM_PI_SECRET"] == "test-secret-value"
    assert environment[provider_variable] == "test-secret-value"
    assert "UNRELATED_SECRET" not in environment


def test_python_client_completes_jsonl_tool_roundtrip(settings):
    client = BridgePiClient(
        command="node",
        entrypoint=settings.pi_bridge_entry,
        runtime_mode="mock",
        start_timeout=5,
        request_timeout=5,
        max_restarts=1,
        model_provider=None,
        model_name=None,
        api_key_env_name=None,
    )
    session_id = "integration-full"
    try:
        health = client.health_check()
        assert health["runtime_mode"] == "mock"
        client.create_session(
            session_id=session_id,
            profile={
                "profile_id": "full_runtime_smoke",
                "version": "v1",
                "mode": "full",
                "max_iterations": 3,
                "max_tool_calls": 3,
                "allowed_tools": ["runtime_echo"],
            },
            model={"runtime_mode": "mock"},
            tools=[
                {
                    "name": "runtime_echo",
                    "description": "echo",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                }
            ],
        )
        seen = []

        output = client.run_agent(
            session_id=session_id,
            system_prompt="runtime test",
            context={},
            task="runtime test",
            output_schema={},
            timeout_seconds=5,
            tool_handler=lambda name, arguments: (
                seen.append((name, arguments)) or {"echo": arguments["message"]}
            ),
        )

        assert "runtime_full_test" in output
        assert seen == [("runtime_echo", {"message": "bridge-ok"})]
        client.close_session(session_id)
    finally:
        client.shutdown()


def test_request_recovers_after_model_corrects_tool_arguments(settings, monkeypatch):
    client = BridgePiClient(
        command="node",
        entrypoint=settings.pi_bridge_entry,
        runtime_mode="mock",
        start_timeout=5,
        request_timeout=5,
        max_restarts=0,
        model_provider=None,
        model_name=None,
        api_key_env_name=None,
    )
    request_id = "recoverable-tool-error"
    monkeypatch.setattr(pi_client_module, "uuid4", lambda: request_id)
    monkeypatch.setattr(client, "_start", lambda: None)
    sent = []
    monkeypatch.setattr(client, "_send", sent.append)
    client._messages.put(
        {
            "id": "call-1",
            "type": "tool_call",
            "payload": {
                "request_id": request_id,
                "tool_name": "read_research_source",
                "arguments": {"evidence_type": "annual_report"},
            },
        }
    )
    client._messages.put(
        {
            "id": "call-2",
            "type": "tool_call",
            "payload": {
                "request_id": request_id,
                "tool_name": "read_research_source",
                "arguments": {"evidence_type": "historical_fact"},
            },
        }
    )
    client._messages.put(
        {"id": request_id, "type": "response", "payload": {"output": "ok"}}
    )

    def handler(_name, arguments):
        if arguments["evidence_type"] == "annual_report":
            raise ValueError("invalid evidence type")
        return {"evidence_id": "ev_001"}

    result = client._request("run_agent", {}, tool_handler=handler)

    assert result == {"output": "ok"}
    assert sent[1]["payload"] == {"error": "invalid evidence type"}
    assert sent[2]["payload"] == {"result": {"evidence_id": "ev_001"}}


@pytest.mark.parametrize(
    ("tool_error", "expected_error"),
    [
        (ValueError("source unavailable"), None),
        (ToolBudgetExhaustedError("budget exhausted"), None),
        (ToolNotAllowedError("denied"), ToolNotAllowedError),
    ],
)
def test_response_only_preserves_fatal_tool_errors(
    settings, monkeypatch, tool_error, expected_error
):
    client = BridgePiClient(
        command="node",
        entrypoint=settings.pi_bridge_entry,
        runtime_mode="mock",
        start_timeout=5,
        request_timeout=5,
        max_restarts=0,
        model_provider=None,
        model_name=None,
        api_key_env_name=None,
    )
    request_id = "tool-error-response"
    monkeypatch.setattr(pi_client_module, "uuid4", lambda: request_id)
    monkeypatch.setattr(client, "_start", lambda: None)
    monkeypatch.setattr(client, "_send", lambda _message: None)
    client._messages.put(
        {
            "id": "call-1",
            "type": "tool_call",
            "payload": {
                "request_id": request_id,
                "tool_name": "read_research_source",
                "arguments": {},
            },
        }
    )
    client._messages.put(
        {"id": request_id, "type": "response", "payload": {"output": "degraded"}}
    )

    def handler(_name, _arguments):
        raise tool_error

    if expected_error is not None:
        with pytest.raises(expected_error):
            client._request("run_agent", {}, tool_handler=handler)
    else:
        assert client._request("run_agent", {}, tool_handler=handler) == {
            "output": "degraded"
        }


def test_budget_exhaustion_is_sent_to_agent_as_a_normal_tool_result(settings, monkeypatch):
    client = BridgePiClient(
        command="node",
        entrypoint=settings.pi_bridge_entry,
        runtime_mode="mock",
        start_timeout=5,
        request_timeout=5,
        max_restarts=0,
        model_provider=None,
        model_name=None,
        api_key_env_name=None,
    )
    request_id = "budget-exhaustion"
    monkeypatch.setattr(pi_client_module, "uuid4", lambda: request_id)
    monkeypatch.setattr(client, "_start", lambda: None)
    sent = []
    monkeypatch.setattr(client, "_send", sent.append)
    client._messages.put(
        {
            "id": "call-1",
            "type": "tool_call",
            "payload": {
                "request_id": request_id,
                "tool_name": "search_research_sources",
                "arguments": {},
            },
        }
    )
    client._messages.put(
        {"id": request_id, "type": "response", "payload": {"output": "partial"}}
    )

    result = client._request(
        "run_agent",
        {},
        tool_handler=lambda _name, _arguments: (_ for _ in ()).throw(
            ToolBudgetExhaustedError("工具预算已耗尽")
        ),
    )

    assert result == {"output": "partial"}
    assert sent[-1]["payload"] == {
        "result": {
            "status": "tool_budget_exhausted",
            "message": "工具预算已耗尽",
        }
    }


def test_bridge_stdout_contains_protocol_json_only(settings):
    client = BridgePiClient(
        command="node",
        entrypoint=settings.pi_bridge_entry,
        runtime_mode="mock",
        start_timeout=5,
        request_timeout=5,
        max_restarts=0,
        model_provider=None,
        model_name=None,
        api_key_env_name=None,
    )
    try:
        assert client.health_check()["status"] == "ok"
        assert client.protocol_errors == []
    finally:
        client.shutdown()


def test_bridge_start_failure_retries_once(tmp_path):
    script = tmp_path / "retry_bridge.py"
    script.write_text(
        """import json, pathlib, sys
marker = pathlib.Path(__file__).with_suffix('.started')
if not marker.exists():
    marker.write_text('1')
    raise SystemExit(2)
print(json.dumps({'id':'bridge','type':'ready','payload':{}}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({'id':request['id'],'type':'response','payload':{'status':'ok'}}), flush=True)
""",
        encoding="utf-8",
    )
    client = BridgePiClient(
        command=sys.executable,
        entrypoint=script,
        runtime_mode="mock",
        start_timeout=2,
        request_timeout=2,
        max_restarts=1,
        model_provider=None,
        model_name=None,
        api_key_env_name=None,
    )
    try:
        assert client.health_check()["status"] == "ok"
    finally:
        client.shutdown()


def test_tool_callback_error_does_not_desynchronize_next_request(settings):
    client = BridgePiClient(
        command="node",
        entrypoint=settings.pi_bridge_entry,
        runtime_mode="mock",
        start_timeout=5,
        request_timeout=5,
        max_restarts=1,
        model_provider=None,
        model_name=None,
        api_key_env_name=None,
    )
    session_id = "tool-error-session"
    try:
        client.create_session(
            session_id=session_id,
            profile={
                "profile_id": "full_runtime_smoke",
                "version": "v1",
                "mode": "full",
                "max_iterations": 3,
                "max_tool_calls": 3,
                "allowed_tools": ["runtime_echo"],
            },
            model={"runtime_mode": "mock"},
            tools=[
                {
                    "name": "runtime_echo",
                    "description": "echo",
                    "input_schema": {"type": "object"},
                    "output_schema": {"type": "object"},
                }
            ],
        )
        with pytest.raises(ToolNotAllowedError):
            client.run_agent(
                session_id=session_id,
                system_prompt="test",
                context={},
                task="test",
                output_schema={},
                timeout_seconds=5,
                tool_handler=lambda _name, _arguments: (_ for _ in ()).throw(
                    ToolNotAllowedError("denied")
                ),
            )
        client.close_session(session_id)
        assert client.health_check()["status"] == "ok"
    finally:
        client.shutdown()
