from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.runtime.profiles import ProfileLoader
from app.technical.schemas import TechnicalAssemblyOutput, TechnicalResearchOutput


def test_technical_profiles_have_required_permissions(settings) -> None:
    loader = ProfileLoader(settings.agent_profile_dir)
    research = loader.load("technical_research")
    assembly = loader.load("technical_assembly")
    assert research.mode == "full"
    assert research.allowed_tools == [
        "get_market_data",
        "calculate_technical_indicators",
        "get_technical_summary",
    ]
    assert research.output_schema == "technical_research_output"
    assert assembly.mode == "constrained"
    assert assembly.allowed_tools == []
    assert assembly.max_tool_calls == 0


def test_technical_research_schema_is_strict() -> None:
    payload = {
        "symbol": "600519.SH",
        "as_of": "2026-08-05",
        "data_version": "v1",
        "trend": "中期趋势向上",
        "volume_price": "量价配合一般",
        "momentum": "动量中性",
        "volatility": "波动可控",
        "support_resistance": "接近阻力位",
        "patterns": ["均线多头排列"],
        "short_term": "震荡",
        "medium_term": "偏强",
        "long_term": "需观察",
        "conflicts": ["动量与趋势冲突"],
        "risks": ["历史数据不代表未来"],
        "confidence": "medium",
    }
    assert TechnicalResearchOutput.model_validate(payload).confidence == "medium"
    with pytest.raises(ValidationError):
        TechnicalResearchOutput.model_validate({**payload, "confidence": "certain"})


def test_technical_assembly_preserves_conflicts() -> None:
    output = TechnicalAssemblyOutput.model_validate(
        {
            "symbol": "600519.SH",
            "as_of": "2026-08-05",
            "data_version": "v1",
            "summary": "存在分歧",
            "agreements": ["中期趋势"],
            "conflicts": ["指标偏强但模型中性"],
            "uncertainties": ["预测区间较宽"],
            "short_term": "谨慎",
            "medium_term": "观察",
            "long_term": "不确定",
            "risks": ["市场波动"],
            "conclusion": "保留不确定性",
            "disclaimer": "不构成投资建议",
        }
    )
    assert output.conflicts == ["指标偏强但模型中性"]
