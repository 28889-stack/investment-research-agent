import os

import pytest

from app.config import Settings
from app.runtime.output_validator import OutputValidator
from app.runtime.pi_client import BridgePiClient
from app.runtime.profiles import ProfileLoader
from app.runtime.schemas import AgentNodeOutput
from app.runtime.tool_registry import RuntimeEchoInput, RuntimeEchoOutput


def live_settings_or_skip() -> Settings:
    settings = Settings.from_env()
    key_name = settings.pi_api_key_env_name
    if (
        settings.pi_runtime_mode != "live"
        or not settings.pi_model_provider
        or not settings.pi_model
        or not key_name
        or not os.environ.get(key_name)
    ):
        pytest.skip("Live Pi provider/model/API key is not configured")
    return settings


@pytest.mark.live
def test_optional_live_pi_session_returns_valid_schema():
    settings = live_settings_or_skip()
    profile = ProfileLoader(settings.agent_profile_dir).load("full_runtime_smoke")
    client = BridgePiClient(
        command=settings.pi_bridge_command,
        entrypoint=settings.pi_bridge_entry,
        runtime_mode="live",
        start_timeout=settings.pi_bridge_start_timeout,
        request_timeout=settings.pi_request_timeout,
        max_restarts=settings.pi_bridge_max_restarts,
        model_provider=settings.pi_model_provider,
        model_name=settings.pi_model,
        api_key_env_name=settings.pi_api_key_env_name,
    )
    session_id = "live-runtime-smoke"
    try:
        client.health_check()
        client.create_session(
            session_id=session_id,
            profile=profile.model_dump(mode="json"),
            model={
                "provider": settings.pi_model_provider,
                "name": settings.pi_model,
                "runtime_mode": "live",
            },
            tools=[
                {
                    "name": "runtime_echo",
                    "description": "返回输入内容，用于验证工具调用链路",
                    "input_schema": RuntimeEchoInput.model_json_schema(),
                    "output_schema": RuntimeEchoOutput.model_json_schema(),
                }
            ],
        )
        raw = client.run_agent(
            session_id=session_id,
            system_prompt=profile.system_prompt,
            context={"run": {"run_id": "live-smoke"}},
            task="调用 runtime_echo 一次并只返回固定 Schema JSON。",
            output_schema=AgentNodeOutput.model_json_schema(),
            timeout_seconds=profile.timeout_seconds,
            tool_handler=lambda name, arguments: {
                "echo": arguments["message"]
            },
        )
        assert OutputValidator(settings.max_agent_output_chars).validate(raw)
        client.close_session(session_id)
    finally:
        client.shutdown()
