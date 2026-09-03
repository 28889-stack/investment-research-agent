from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from app.charts.schemas import ChartAnnotation, ChartSeries, ChartSpec, ReportVisuals
from app.technical.schemas import ChartFamily, PatternSignal, TechnicalIndicators


_FAMILY_ORDER: tuple[ChartFamily, ...] = (
    "price_trend",
    "macd",
    "rsi",
    "volume_price",
)


TECHNICAL_CANVAS_RENDERING_NOTES = (
    "机构技术图风格：白色图底、黑灰坐标轴、浅灰网格；价格主区优先，"
    "成交量和指标区次之。X 轴最多显示 6 个等间隔日期，必要时使用月度"
    "短标签；Y 轴每个分区最多 4 个刻度。图例只保留当前读图所需的核心"
    "序列，并避免与形态标注重叠。形态标注使用一条细引线、一个低饱和"
    "紫蓝色标签和简短名称；不叠加多个无关形态，不制造密集的垂直辅助线。"
    "价格、均线、支撑阻力、成交量与均量的颜色在所有技术图中保持一致。"
)


def _values(series: pd.Series) -> list[float | None]:
    return [
        round(float(value), 6) if value is not None and np.isfinite(value) else None
        for value in series
    ]


def _signal_index(labels: list[str], signal: PatternSignal) -> int:
    target = signal.detected_at.isoformat()
    return labels.index(target) if target in labels else len(labels) - 1


def _signal_value(signal: PatternSignal) -> float | None:
    priorities = {
        "price_trend": ("latest_close", "sma5"),
        "macd": ("dif", "histogram"),
        "rsi": ("rsi14",),
        "volume_price": ("volume", "latest_close"),
    }
    for key in priorities[signal.chart_family]:
        if key in signal.trigger_values:
            return signal.trigger_values[key]
    return next(iter(signal.trigger_values.values()), None)


def _detail(signal: PatternSignal) -> str:
    return (
        f"触发：{signal.trigger_rule}；"
        f"确认：{signal.confirmation_rule}；"
        f"失效：{signal.invalidation_rule}"
    )


def _annotations(
    labels: list[str], signals: list[PatternSignal]
) -> list[ChartAnnotation]:
    kind_by_name = {
        "20日突破": "breakout",
        "20日跌破": "breakout",
        "MACD金叉": "cross",
        "MACD死叉": "cross",
        "RSI超买": "threshold",
        "RSI超卖": "threshold",
    }
    return [
        ChartAnnotation(
            label=signal.name,
            index=_signal_index(labels, signal),
            value=_signal_value(signal),
            kind=kind_by_name.get(signal.name, "event"),
            detail=_detail(signal),
        )
        for signal in signals
    ]


def _baseline_series(
    plot: pd.DataFrame, indicators: TechnicalIndicators
) -> list[ChartSeries]:
    levels = indicators.support_resistance
    length = len(plot)
    return [
        ChartSeries(series_id="close", name="收盘价", values=_values(plot["close"]), color="#0A84FF"),
        ChartSeries(series_id="sma5", name="SMA5", values=_values(plot["sma5"]), color="#FF9F0A"),
        ChartSeries(series_id="sma20", name="SMA20", values=_values(plot["sma20"]), color="#34C759"),
        ChartSeries(series_id="sma60", name="SMA60", values=_values(plot["sma60"]), color="#FF453A"),
        ChartSeries(series_id="support20", name="20日支撑", values=[levels.support_20] * length, color="#30A46C"),
        ChartSeries(series_id="support60", name="60日支撑", values=[levels.support_60] * length, color="#237A57"),
        ChartSeries(series_id="resistance20", name="20日阻力", values=[levels.resistance_20] * length, color="#D14D57"),
        ChartSeries(series_id="resistance60", name="60日阻力", values=[levels.resistance_60] * length, color="#A83E4A"),
        ChartSeries(series_id="volume", name="成交量", values=_values(plot["volume"]), style="bar", color="#718096"),
        ChartSeries(series_id="volume-ma20", name="20日均量", values=_values(plot["volume_ma20"]), color="#FF9F0A"),
    ]


def _market_overview(
    plot: pd.DataFrame, labels: list[str], indicators: TechnicalIndicators
) -> ChartSpec:
    return ChartSpec(
        chart_id="technical-market-overview",
        section_id="trend-analysis",
        plugin_id="technical_market_overview",
        chart_type="combo",
        title="行情全景：价格、均线、支撑阻力与成交量",
        analytical_purpose="以统一时间轴呈现价格趋势、关键均线、支撑阻力及量能变化",
        labels=labels,
        series=_baseline_series(plot, indicators),
        unit="价格",
        secondary_unit="成交量",
        explanation="上图区展示收盘价、均线及关键支撑阻力；下图区展示成交量及20日均量。",
        rendering_notes=TECHNICAL_CANVAS_RENDERING_NOTES,
        source_notes=["资料来源：本次行情数据、technical_indicators.json。"],
        data_lineage={
            item.series_id: ["market_data.csv", "technical_indicators.json"]
            for item in _baseline_series(plot, indicators)
        },
        placement="after_body",
    )


def _base_chart(
    *,
    family: ChartFamily,
    plot: pd.DataFrame,
    title: str,
    section_id: str,
    chart_type: str,
    unit: str,
    labels: list[str],
    series: list[ChartSeries],
    signals: list[PatternSignal],
    indicators: TechnicalIndicators,
    secondary_unit: str | None = None,
) -> ChartSpec:
    explanations = [f"{signal.name}：{_detail(signal)}" for signal in signals]
    observations = [
        f"{signal.name}确认条件：{signal.confirmation_rule}；失效条件：{signal.invalidation_rule}"
        for signal in signals
    ]
    return ChartSpec(
        chart_id=f"technical-{family.replace('_', '-')}",
        section_id=section_id,
        plugin_id=f"technical_{family}",
        chart_type=chart_type,
        title=title,
        analytical_purpose="展示本次实际识别的技术形态及其触发位置",
        labels=labels,
        series=[*_baseline_series(plot, indicators), *series],
        unit=unit,
        secondary_unit=secondary_unit or "成交量",
        annotations=_annotations(labels, signals),
        explanation="\n".join(explanations),
        rendering_notes=TECHNICAL_CANVAS_RENDERING_NOTES,
        observation_points=observations,
        source_notes=["资料来源：本次行情数据、technical_indicators.json；仅展示本次实际识别信号。"],
        data_lineage={
            item.series_id: ["market_data.csv", "technical_indicators.json"]
            for item in [*_baseline_series(plot, indicators), *series]
        },
    )


def _price_chart(
    plot: pd.DataFrame,
    labels: list[str],
    signals: list[PatternSignal],
    indicators: TechnicalIndicators,
) -> ChartSpec:
    return _base_chart(
        family="price_trend",
        plot=plot,
        title="价格趋势与均线形态",
        section_id="trend-analysis",
        chart_type="combo",
        unit="价格",
        labels=labels,
        series=[],
        signals=signals,
        indicators=indicators,
    )


def _macd_chart(
    plot: pd.DataFrame,
    labels: list[str],
    signals: list[PatternSignal],
    indicators: TechnicalIndicators,
) -> ChartSpec:
    return _base_chart(
        family="macd",
        plot=plot,
        title="MACD 动量与交叉信号",
        section_id="momentum-analysis",
        chart_type="combo",
        unit="价格",
        labels=labels,
        series=[
            ChartSeries(series_id="macd-dif", name="DIF", values=_values(plot["macd_dif"]), color="#0A84FF"),
            ChartSeries(series_id="macd-dea", name="DEA", values=_values(plot["macd_dea"]), color="#FF9F0A"),
            ChartSeries(series_id="macd-hist", name="MACD柱", values=_values(plot["macd_histogram"]), style="bar", color="#8E8E93"),
        ],
        signals=signals,
        indicators=indicators,
    )


def _rsi_chart(
    plot: pd.DataFrame,
    labels: list[str],
    signals: list[PatternSignal],
    indicators: TechnicalIndicators,
) -> ChartSpec:
    return _base_chart(
        family="rsi",
        plot=plot,
        title="RSI 强弱区间与阈值信号",
        section_id="momentum-analysis",
        chart_type="band",
        unit="价格",
        labels=labels,
        series=[
            ChartSeries(series_id="rsi14", name="RSI14", values=_values(plot["rsi14"]), color="#0A84FF"),
            ChartSeries(series_id="rsi70", name="超买阈值", values=[70.0] * len(labels), color="#A04A4A"),
            ChartSeries(series_id="rsi30", name="超卖阈值", values=[30.0] * len(labels), color="#557A67"),
        ],
        signals=signals,
        indicators=indicators,
    )


def _volume_price_chart(
    plot: pd.DataFrame,
    labels: list[str],
    signals: list[PatternSignal],
    indicators: TechnicalIndicators,
) -> ChartSpec:
    return _base_chart(
        family="volume_price",
        plot=plot,
        title="成交量与价格联动",
        section_id="volume-price-analysis",
        chart_type="combo",
        unit="价格",
        labels=labels,
        series=[],
        signals=signals,
        indicators=indicators,
    )


_BUILDERS = {
    "price_trend": _price_chart,
    "macd": _macd_chart,
    "rsi": _rsi_chart,
    "volume_price": _volume_price_chart,
}


def build_technical_visuals(
    enriched: pd.DataFrame,
    indicators: TechnicalIndicators,
    *,
    max_bars: int = 120,
) -> ReportVisuals:
    plot = enriched.tail(max_bars).copy()
    labels = [pd.Timestamp(value).date().isoformat() for value in plot["date"]]
    charts: list[ChartSpec] = [_market_overview(plot, labels, indicators)]
    chart_number = 0
    for family in _FAMILY_ORDER:
        signals = [
            signal for signal in indicators.signals if signal.chart_family == family
        ]
        for signal in signals:
            chart_number += 1
            try:
                chart = _BUILDERS[family](plot, labels, [signal], indicators)
                charts.append(chart.model_copy(update={
                    "chart_id": f"technical-{family.replace('_', '-')}-{chart_number:02d}",
                    "title": f"{signal.name} · {chart.title}",
                }))
            except Exception as exc:
                charts.append(
                    ChartSpec(
                        chart_id=f"technical-{family.replace('_', '-')}-{chart_number:02d}",
                        section_id="technical-patterns",
                        plugin_id=f"technical_{family}",
                        chart_type="combo",
                        title=f"{signal.name} · 形态解释图",
                        annotations=_annotations(labels, [signal]),
                        status="skipped",
                        skip_reason=f"{type(exc).__name__}: 当前形态图未生成",
                    )
                )
    return ReportVisuals(charts=charts)


def atomic_write_visuals(visuals: ReportVisuals, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(visuals.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)
