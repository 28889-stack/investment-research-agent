import json

import pytest

from app.runtime.exceptions import AgentOutputError
from app.runtime.output_validator import OutputValidator


def valid_output(**overrides) -> dict:
    output = {
        "task_id": "runtime_full_test",
        "status": "completed",
        "summary": "Runtime 调用成功",
        "findings": [
            {
                "claim": "白名单工具调用链路正常",
                "evidence_ids": [],
                "assumption_ids": [],
                "confidence": "high",
            }
        ],
        "new_evidence": [],
        "new_assumptions": [],
        "risks": [],
        "conflicts": [],
        "missing_information": [],
        "suggested_followups": [],
    }
    output.update(overrides)
    return output


@pytest.mark.parametrize(
    "raw",
    [
        lambda payload: json.dumps(payload, ensure_ascii=False),
        lambda payload: "```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```",
        lambda payload: "校验结果如下：\n" + json.dumps(payload, ensure_ascii=False) + "\n请查收。",
    ],
)
def test_extracts_and_validates_supported_json_forms(raw):
    validated = OutputValidator(max_output_chars=20_000).validate(raw(valid_output()))

    assert validated.summary == "Runtime 调用成功"
    assert validated.findings[0].confidence == "high"


@pytest.mark.parametrize(
    ("raw", "error_code"),
    [
        ("", "OUTPUT_EMPTY"),
        ("plain text only", "JSON_NOT_FOUND"),
        ("{not-json}", "JSON_INVALID"),
        (json.dumps({"status": "completed"}), "SCHEMA_INVALID"),
        (json.dumps(valid_output(findings=[{"claim": "x", "evidence_ids": [], "assumption_ids": [], "confidence": "certain"}])), "SCHEMA_INVALID"),
        (json.dumps(valid_output(secret="forbidden")), "FORBIDDEN_FIELD"),
    ],
)
def test_rejects_invalid_outputs_with_stable_error_codes(raw, error_code):
    with pytest.raises(AgentOutputError) as caught:
        OutputValidator(max_output_chars=20_000).validate(raw)

    assert caught.value.code == error_code


def test_rejects_output_over_configured_limit():
    with pytest.raises(AgentOutputError) as caught:
        OutputValidator(max_output_chars=10).validate(json.dumps(valid_output()))

    assert caught.value.code == "OUTPUT_TOO_LARGE"


def test_output_schema_rejects_false_investment_fields():
    payload = valid_output()
    payload["findings"][0]["target_price"] = 2100

    with pytest.raises(AgentOutputError) as caught:
        OutputValidator(max_output_chars=20_000).validate(json.dumps(payload))

    assert caught.value.code == "SCHEMA_INVALID"


@pytest.mark.parametrize(
    "summary",
    ["建议买入该股票", "目标价 120 元", "股价为 100 元", "Strong buy"],
)
def test_rejects_research_claims_from_runtime_smoke_output(summary):
    payload = valid_output(summary=summary)

    with pytest.raises(AgentOutputError) as caught:
        OutputValidator(max_output_chars=20_000).validate(
            json.dumps(payload, ensure_ascii=False)
        )

    assert caught.value.code == "FORBIDDEN_CONTENT"
