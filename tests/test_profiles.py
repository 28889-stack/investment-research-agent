import json

import pytest

from app.runtime.exceptions import ProfileNotFoundError, ProfileValidationError
from app.runtime.profiles import AgentProfile, ProfileLoader


def valid_profile(**overrides) -> dict:
    profile = {
        "profile_id": "test_profile",
        "version": "v1",
        "role": "runtime_test",
        "mode": "full",
        "system_prompt": "Only validate the runtime.",
        "allowed_tools": ["runtime_echo"],
        "max_iterations": 3,
        "max_tool_calls": 3,
        "context_policy": "task_scoped",
        "output_schema": "agent_node_output",
        "model": None,
        "timeout_seconds": 120,
    }
    profile.update(overrides)
    return profile


def test_loads_full_and_constrained_profiles():
    loader = ProfileLoader("app/profiles")

    full = loader.load("full_runtime_smoke")
    constrained = loader.load("constrained_runtime_smoke")

    assert full.mode == "full"
    assert "runtime_echo" in full.allowed_tools
    assert constrained.mode == "constrained"
    assert constrained.allowed_tools == []
    assert {profile.profile_id for profile in loader.list_profiles()} == {
        "full_runtime_smoke",
        "constrained_runtime_smoke",
        "technical_research",
        "technical_assembly",
        "fundamental_lead",
        "business_research",
        "industry_research",
        "deep_research",
        "financial_research",
        "valuation_research",
        "lead_synthesis",
        "writer_planning",
        "fundamental_writer",
    }


def test_missing_profile_has_clear_error(tmp_path):
    loader = ProfileLoader(tmp_path)

    with pytest.raises(ProfileNotFoundError):
        loader.load("missing")


def test_invalid_mode_is_rejected(tmp_path):
    (tmp_path / "invalid.json").write_text(
        json.dumps(valid_profile(mode="autonomous")), encoding="utf-8"
    )

    with pytest.raises(ProfileValidationError):
        ProfileLoader(tmp_path)


def test_duplicate_profile_id_is_rejected(tmp_path):
    content = json.dumps(valid_profile())
    (tmp_path / "one.json").write_text(content, encoding="utf-8")
    (tmp_path / "two.json").write_text(content, encoding="utf-8")

    with pytest.raises(ProfileValidationError, match="重复"):
        ProfileLoader(tmp_path)


def test_profile_forbids_unknown_fields():
    with pytest.raises(ValueError):
        AgentProfile.model_validate(valid_profile(shell_access=True))


def test_constrained_profile_limits_are_strict():
    with pytest.raises(ValueError):
        AgentProfile.model_validate(
            valid_profile(mode="constrained", max_iterations=2, max_tool_calls=0)
        )
