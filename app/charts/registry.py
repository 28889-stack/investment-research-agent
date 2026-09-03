from __future__ import annotations

import math
from typing import Any, Protocol

from app.charts.schemas import ChartSpec, ReportVisuals


class ChartPlugin(Protocol):
    plugin_id: str

    def build(self, plan: Any, context: dict[str, Any]) -> ChartSpec: ...


class ChartPluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, ChartPlugin] = {}

    def register(self, plugin: ChartPlugin) -> None:
        if plugin.plugin_id in self._plugins:
            raise ValueError(f"图表插件重复注册: {plugin.plugin_id}")
        self._plugins[plugin.plugin_id] = plugin

    def materialize(self, plans: list[Any], context: dict[str, Any]) -> ReportVisuals:
        charts: list[ChartSpec] = []
        for plan in plans:
            plugin = self._plugins.get(plan.plugin_id)
            if plugin is None:
                charts.append(self._skipped(plan, "unknown_plugin"))
                continue
            try:
                chart = plugin.build(plan, context)
                self._validate_comparison(plan, chart)
                charts.append(chart)
            except Exception as exc:
                reason = f"{type(exc).__name__}: {str(exc)}"[:300]
                charts.append(self._skipped(plan, reason))
        return ReportVisuals(charts=charts)

    @staticmethod
    def _validate_comparison(plan: Any, chart: ChartSpec) -> None:
        if not getattr(plan, "comparison_basis", "").strip():
            raise ValueError("图表缺少明确的比较依据")
        if len(chart.labels) < 2:
            raise ValueError("图表至少需要两个可比较的数据点")
        comparable_series = [
            series
            for series in chart.series
            if sum(
                value is not None and math.isfinite(value)
                for value in series.values
            ) >= 2
        ]
        if not comparable_series:
            raise ValueError("图表至少需要一组包含两个数据点的可比较序列")

    @staticmethod
    def _skipped(plan: Any, reason: str) -> ChartSpec:
        return ChartSpec(
            chart_id=plan.visual_id,
            section_id=plan.section_id,
            plugin_id=plan.plugin_id,
            chart_type=plan.preferred_chart_type,
            title=plan.analytical_question,
            analytical_purpose=plan.analytical_question,
            unit=plan.unit_hint,
            evidence_ids=plan.allowed_evidence_ids,
            assumption_ids=plan.allowed_assumption_ids,
            placement=plan.placement,
            status="skipped",
            skip_reason=reason,
        )
