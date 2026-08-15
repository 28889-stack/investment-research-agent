from __future__ import annotations

import pytest

from app.charts.registry import ChartPluginRegistry
from app.charts.schemas import EvidenceChartCandidate, EvidenceChartPoint
from app.fundamental.schemas import (
    EvidenceCollection,
    EvidenceItem,
    PlannedVisual,
    WriterPlanOutput,
)
from app.fundamental.visuals import (
    build_default_fundamental_chart_registry,
    validate_evidence_chart_candidate,
)
from app.runtime.profiles import ProfileLoader


def _plan(**updates) -> PlannedVisual:
    payload = {
        "visual_id": "visual-performance",
        "section_id": "financial-analysis",
        "plugin_id": "financial_performance_trend",
        "analytical_question": "收入增长是否转化为利润增长",
        "source_mode": "structured",
        "metric_keys": ["revenue", "net_profit_attributable"],
        "allowed_evidence_ids": [],
        "allowed_assumption_ids": [],
        "preferred_chart_type": "combo",
        "time_range": "all_available",
        "unit_hint": "元",
        "placement": "after_claim",
        "caption_focus": "比较收入与归母净利润的变化方向",
        "comparison_mode": "time_series",
        "comparison_basis": "比较同一财务口径下收入与归母净利润的跨期变化",
        "priority": 1,
    }
    payload.update(updates)
    return PlannedVisual.model_validate(payload)


def test_writer_plan_visual_plan_cannot_embed_chart_values() -> None:
    with pytest.raises(ValueError):
        _plan(values=[1, 2, 3])

    plan = WriterPlanOutput(
        symbol="600519.SH",
        as_of="2026-08-05",
        title="基本面分析",
        executive_focus="关注经营质量",
        sections=[],
        key_findings=[],
        risks=[],
        missing_information=[],
        report_composition=[],
        visual_plan=[_plan()],
    )
    assert plan.visual_plan[0].plugin_id == "financial_performance_trend"


def test_registry_isolates_unknown_plugin_without_interrupting_other_charts() -> None:
    registry = build_default_fundamental_chart_registry()
    context = {
        "financial_data": {
            "periods": [
                {"period": "2024-12-31", "revenue": 100.0, "net_profit_attributable": 20.0},
                {"period": "2025-12-31", "revenue": 120.0, "net_profit_attributable": 25.0},
            ]
        },
        "financial_metrics": {
            "profitability": {
                "2024-12-31": {"net_margin": 0.20},
                "2025-12-31": {"net_margin": 0.21},
            },
            "cash_flow": {},
            "balance_sheet": {},
            "periods": ["2024-12-31", "2025-12-31"],
        },
        "valuation_result": {},
    }
    visuals = registry.materialize(
        [
            _plan(),
            _plan(
                visual_id="visual-margin",
                plugin_id="profitability_quality",
                metric_keys=["net_margin"],
                preferred_chart_type="line",
            ),
            _plan(
                visual_id="visual-unknown",
                plugin_id="not_registered",
            ),
        ],
        context,
    )

    assert [chart.status for chart in visuals.charts] == [
        "generated", "generated", "skipped"
    ]
    assert visuals.charts[0].series[0].values == [100.0, 120.0]
    assert visuals.charts[1].series[0].values == [0.20, 0.21]
    assert visuals.charts[2].skip_reason == "unknown_plugin"


def test_evidence_chart_candidate_requires_exact_allowed_source_data() -> None:
    evidence = EvidenceCollection(items=[
        EvidenceItem(
            id="ev_101",
            claim="产量",
            content="2024年矿产铜产量为107万吨，2025年达到115万吨。",
            source_name="年度报告",
            url="https://example.com/report",
            date="2026-03-20",
            location="经营回顾",
            type="historical_fact",
        )
    ])
    plan = _plan(
        visual_id="visual-production",
        section_id="business-analysis",
        plugin_id="production_capacity",
        source_mode="evidence",
        metric_keys=["矿产铜产量"],
        allowed_evidence_ids=["ev_101"],
        preferred_chart_type="bar",
        unit_hint="万吨",
    )
    valid = EvidenceChartCandidate(
        visual_id="visual-production",
        series_name="矿产铜产量",
        points=[
            EvidenceChartPoint(label="2024", value=107, unit="万吨", evidence_id="ev_101"),
            EvidenceChartPoint(label="2025", value=115, unit="万吨", evidence_id="ev_101"),
        ],
    )

    validate_evidence_chart_candidate(plan, valid, evidence)

    invalid = valid.model_copy(deep=True)
    invalid.points[1].value = 118
    with pytest.raises(ValueError, match="无法在 Evidence 中核验"):
        validate_evidence_chart_candidate(plan, invalid, evidence)


def test_empty_visual_plan_is_valid_and_materializes_no_charts() -> None:
    visuals = ChartPluginRegistry().materialize([], {})
    assert visuals.charts == []


def test_planned_visual_requires_an_explicit_comparison_basis() -> None:
    with pytest.raises(ValueError):
        _plan(comparison_basis="")


def test_registry_skips_a_chart_without_two_comparable_points() -> None:
    registry = build_default_fundamental_chart_registry()
    visuals = registry.materialize(
        [_plan()],
        {
            "financial_data": {
                "periods": [{
                    "period": "2025-12-31",
                    "revenue": 120.0,
                    "net_profit_attributable": 25.0,
                }]
            },
            "financial_metrics": {"periods": ["2025-12-31"]},
            "valuation_result": {},
        },
    )

    assert visuals.charts[0].status == "skipped"
    assert "比较" in visuals.charts[0].skip_reason


def test_chart_data_extractor_profile_is_constrained_and_tool_free(settings) -> None:
    profile = ProfileLoader(settings.agent_profile_dir).load("chart_data_extractor")
    assert profile.mode == "constrained"
    assert profile.max_iterations == 1
    assert profile.max_tool_calls == 0
    assert profile.allowed_tools == []
    assert profile.output_schema == "evidence_chart_extraction_output"
    assert "不得补齐" in profile.system_prompt
