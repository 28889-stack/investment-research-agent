from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.charts.registry import ChartPluginRegistry
from app.charts.schemas import ChartSeries, ChartSpec, EvidenceChartCandidate
from app.fundamental.schemas import EvidenceCollection, PlannedVisual


COLORS = ("#163A5F", "#6F8294", "#202124", "#AAB2BB")


def _periods(context: dict[str, Any]) -> list[str]:
    metrics = context.get("financial_metrics") or {}
    periods = list(metrics.get("periods") or [])
    if periods:
        return periods
    return [item["period"] for item in (context.get("financial_data") or {}).get("periods", [])]


def _financial_period_map(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["period"]: item
        for item in (context.get("financial_data") or {}).get("periods", [])
    }


@dataclass(frozen=True)
class FinancialPerformancePlugin:
    plugin_id: str = "financial_performance_trend"

    def build(self, plan: PlannedVisual, context: dict[str, Any]) -> ChartSpec:
        labels = _periods(context)
        periods = _financial_period_map(context)
        metric_names = {
            "revenue": "营业收入",
            "net_profit": "净利润",
            "net_profit_attributable": "归母净利润",
            "operating_profit": "营业利润",
        }
        keys = [key for key in plan.metric_keys if key in metric_names] or [
            "revenue", "net_profit_attributable"
        ]
        series = [
            ChartSeries(
                series_id=key,
                name=metric_names[key],
                values=[periods.get(label, {}).get(key) for label in labels],
                style="bar" if index == 0 and plan.preferred_chart_type == "combo" else "line",
                axis="secondary" if index else "primary",
                color=COLORS[index % len(COLORS)],
            )
            for index, key in enumerate(keys)
        ]
        return _spec(plan, labels, series, "收入与利润变化应结合增长质量共同观察。")


@dataclass(frozen=True)
class MetricGroupPlugin:
    plugin_id: str
    group: str
    default_keys: tuple[str, ...]
    names: dict[str, str]

    def build(self, plan: PlannedVisual, context: dict[str, Any]) -> ChartSpec:
        labels = _periods(context)
        group = (context.get("financial_metrics") or {}).get(self.group, {})
        keys = [key for key in plan.metric_keys if key in self.names] or list(self.default_keys)
        series = [
            ChartSeries(
                series_id=key,
                name=self.names[key],
                values=[group.get(label, {}).get(key) for label in labels],
                style="bar" if plan.preferred_chart_type in {"bar", "stacked_bar"} else "line",
                color=COLORS[index % len(COLORS)],
            )
            for index, key in enumerate(keys)
        ]
        return _spec(plan, labels, series, plan.caption_focus)


@dataclass(frozen=True)
class ValuationSnapshotPlugin:
    plugin_id: str = "valuation_snapshot"

    def build(self, plan: PlannedVisual, context: dict[str, Any]) -> ChartSpec:
        raise ValueError("估值快照缺少同一口径的历史、同行或情景比较")


@dataclass(frozen=True)
class EvidenceSeriesPlugin:
    plugin_id: str

    def build(self, plan: PlannedVisual, context: dict[str, Any]) -> ChartSpec:
        candidates = [
            item for item in context.get("evidence_candidates", [])
            if item.visual_id == plan.visual_id
        ]
        if not candidates:
            raise ValueError("没有通过核验的 Evidence 图表数据")
        labels: list[str] = []
        for candidate in candidates:
            for point in candidate.points:
                if point.label not in labels:
                    labels.append(point.label)
        series: list[ChartSeries] = []
        lineage: dict[str, list[str]] = {}
        evidence_ids: list[str] = []
        for index, candidate in enumerate(candidates):
            point_map = {point.label: point for point in candidate.points}
            series_id = f"evidence_{index + 1}"
            series.append(ChartSeries(
                series_id=series_id,
                name=candidate.series_name,
                values=[point_map[label].value if label in point_map else None for label in labels],
                style="bar" if plan.preferred_chart_type in {"bar", "stacked_bar", "waterfall", "timeline"} else "line",
                color=COLORS[index % len(COLORS)],
            ))
            ids = list(dict.fromkeys(point.evidence_id for point in candidate.points))
            lineage[series_id] = [f"evidence:{item}" for item in ids]
            evidence_ids.extend(ids)
        return ChartSpec(
            chart_id=plan.visual_id,
            section_id=plan.section_id,
            plugin_id=plan.plugin_id,
            chart_type=plan.preferred_chart_type,
            title=plan.analytical_question,
            analytical_purpose=plan.analytical_question,
            labels=labels,
            series=series,
            unit=candidates[0].points[0].unit,
            explanation=plan.caption_focus or plan.analytical_question,
            observation_points=[plan.caption_focus] if plan.caption_focus else [],
            source_notes=[f"Evidence {item}" for item in dict.fromkeys(evidence_ids)],
            evidence_ids=list(dict.fromkeys(evidence_ids)),
            data_lineage=lineage,
            placement=plan.placement,
        )


def _spec(
    plan: PlannedVisual,
    labels: list[str],
    series: list[ChartSeries],
    explanation: str,
) -> ChartSpec:
    if not labels or not series or not any(
        value is not None for item in series for value in item.values
    ):
        raise ValueError("没有可用于制图的结构化数据")
    return ChartSpec(
        chart_id=plan.visual_id,
        section_id=plan.section_id,
        plugin_id=plan.plugin_id,
        chart_type=plan.preferred_chart_type,
        title=plan.analytical_question,
        analytical_purpose=plan.analytical_question,
        labels=labels,
        series=series,
        unit=plan.unit_hint,
        explanation=explanation or plan.analytical_question,
        observation_points=[plan.caption_focus] if plan.caption_focus else [],
        evidence_ids=plan.allowed_evidence_ids,
        assumption_ids=plan.allowed_assumption_ids,
        data_lineage={item.series_id: [f"structured:{item.series_id}"] for item in series},
        placement=plan.placement,
    )


def build_default_fundamental_chart_registry() -> ChartPluginRegistry:
    registry = ChartPluginRegistry()
    registry.register(FinancialPerformancePlugin())
    registry.register(MetricGroupPlugin(
        plugin_id="profitability_quality",
        group="profitability",
        default_keys=("gross_margin", "net_margin", "roe"),
        names={"gross_margin": "毛利率", "net_margin": "净利率", "roe": "ROE"},
    ))
    registry.register(MetricGroupPlugin(
        plugin_id="cashflow_capex",
        group="cash_flow",
        default_keys=("operating_cash_flow", "capital_expenditure", "free_cash_flow"),
        names={
            "operating_cash_flow": "经营现金流",
            "capital_expenditure": "资本开支",
            "free_cash_flow": "自由现金流",
        },
    ))
    registry.register(MetricGroupPlugin(
        plugin_id="balance_sheet_health",
        group="balance_sheet",
        default_keys=("debt_to_assets", "net_debt"),
        names={
            "debt_to_assets": "资产负债率",
            "net_debt": "净债务",
            "current_ratio": "流动比率",
        },
    ))
    registry.register(ValuationSnapshotPlugin())
    for plugin_id in (
        "business_mix", "production_capacity", "industry_supply_demand",
        "commodity_price_cycle", "project_timeline",
    ):
        registry.register(EvidenceSeriesPlugin(plugin_id=plugin_id))
    return registry


def validate_evidence_chart_candidate(
    plan: PlannedVisual,
    candidate: EvidenceChartCandidate,
    evidence: EvidenceCollection,
) -> None:
    if candidate.visual_id != plan.visual_id:
        raise ValueError("Evidence 图表候选与计划不一致")
    known = {item.id: item for item in evidence.items}
    units = {point.unit for point in candidate.points}
    if len(units) != 1:
        raise ValueError("Evidence 图表序列单位不一致")
    for point in candidate.points:
        if point.evidence_id not in plan.allowed_evidence_ids:
            raise ValueError("Evidence 图表使用了未授权来源")
        item = known.get(point.evidence_id)
        if item is None:
            raise ValueError("Evidence 图表引用不存在")
        value_tokens = {_number_token(point.value)}
        if float(point.value).is_integer():
            value_tokens.add(str(int(point.value)))
        content = item.content.replace(",", "").replace("，", "")
        if (
            point.label not in content
            or point.unit not in content
            or not any(re.search(rf"(?<!\d){re.escape(token)}(?!\d)", content) for token in value_tokens)
        ):
            raise ValueError(
                f"数据点 {point.label}/{point.value}{point.unit} 无法在 Evidence 中核验"
            )


def _number_token(value: float) -> str:
    return format(value, ".12g")
