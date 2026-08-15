from __future__ import annotations

import base64
import html
import json
import os
import re
from pathlib import Path

import bleach
import markdown

from app.charts.schemas import ReportVisuals
from app.charts.runtime import REPORT_CHART_RUNTIME
from app.charts.styles import TECHNICAL_VISION_STYLE
from app.models import ResearchRun
from app.technical.market_data import load_persisted_market_data
from app.technical.schemas import (
    KronosResult,
    TechnicalAssemblyOutput,
    TechnicalIndicators,
    TechnicalResearchOutput,
)


DISCLAIMER = """本报告仅基于历史行情、技术指标和模型预测生成。
技术指标及模型预测不能保证未来表现。
本报告不构成投资建议、交易指令或收益承诺。"""


class ReportError(RuntimeError):
    code = "REPORT_GENERATION_FAILED"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


def _items(values: list[str]) -> str:
    return (
        "\n".join(f"- {_safe_narrative(value)}" for value in values)
        if values
        else "- 无明确项目"
    )


def _authoritative_items(values: list[str]) -> str:
    return "\n".join(f"- {value}" for value in values) if values else "- 无明确项目"


# Agent prose is qualitative only. Any Arabic numeral makes the whole field
# ineligible for the deterministic report; authoritative code-generated
# numbers and pattern names use separate render paths.
EXACT_NUMBER = re.compile(r"\d")


def _safe_narrative(value: str) -> str:
    if EXACT_NUMBER.search(value):
        return "Agent 原叙述含精确数值，已省略；请以本报告确定性数值为准。"
    return value


def _load_visuals(artifact_dir: Path) -> ReportVisuals | None:
    path = artifact_dir / "technical_visuals.json"
    if not path.is_file():
        return None
    try:
        return ReportVisuals.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _canvas_payload(visuals: ReportVisuals) -> dict[str, object]:
    charts: list[dict[str, object]] = []
    for chart in visuals.charts:
        item = chart.model_dump(mode="json")
        item["id"] = item.pop("chart_id")
        charts.append(item)
    return {"version": visuals.version, "charts": charts}


def _signal_rules(detail: str) -> str:
    parts: dict[str, str] = {}
    for item in detail.split("；"):
        label, separator, value = item.partition("：")
        if separator and label in {"触发", "确认", "失效"}:
            parts[label] = value
    return "".join(
        f'<div><strong>{label}</strong><span>{html.escape(parts.get(label, "以指标后续变化持续验证"))}</span></div>'
        for label in ("触发", "确认", "失效")
    )


def _technical_chart_card(chart, chart_number: int) -> str:
    annotation = chart.annotations[0] if chart.annotations else None
    rules = (
        _signal_rules(annotation.detail)
        if annotation is not None
        else '<p class="chart-explanation">价格主区与成交量副区使用同一时间轴，用于观察趋势、关键价位和量能的同步变化。</p>'
    )
    component_class = (
        "chart-component chart-component-overview"
        if chart.chart_id == "technical-market-overview"
        else "chart-component"
    )
    legend = "".join(
        '<span data-series-id="{id}"><i style="background:{color}"></i>{name}</span>'.format(
            id=html.escape(item.series_id, quote=True),
            color=html.escape(item.color, quote=True),
            name=html.escape(item.name),
        )
        for item in chart.series
    )
    return f'''<div class="{component_class}">
<div class="chart-header"><div><p class="chart-kicker">图 {chart_number}：</p><h4>{html.escape(chart.title)}</h4></div><span class="chart-unit">{html.escape(chart.unit)}</span></div>
<div class="chart-legend">{legend}</div>
<div class="chart-stage"><canvas data-chart="{html.escape(chart.chart_id, quote=True)}" aria-label="{html.escape(chart.title, quote=True)}"></canvas></div>
<div class="signal-rules">{rules}</div>
<p class="chart-source">{html.escape(" ".join(chart.source_notes))}</p>
</div>'''


def _baseline_chart_image(artifact_dir: Path) -> str:
    path = artifact_dir / "technical_chart.png"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ReportError("缺少必备行情全景图") from exc
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ReportError("行情全景图格式无效")
    return base64.b64encode(raw).decode("ascii")


def _baseline_chart_card(chart, chart_number: int, image_data: str) -> str:
    return f'''<figure class="technical-market-image-card">
<div class="chart-header"><div><p class="chart-kicker">图 {chart_number}：</p><h4>{html.escape(chart.title)}</h4></div><span class="chart-unit">{html.escape(chart.unit)}</span></div>
<img class="technical-market-image" src="data:image/png;base64,{image_data}" alt="{html.escape(chart.title, quote=True)}">
<figcaption class="chart-source">{html.escape(" ".join(chart.source_notes))}</figcaption>
</figure>'''


def _pattern_visuals_html(patterns: list[str], visuals: ReportVisuals | None) -> str:
    if not patterns:
        return '<section class="visual-empty"><p>本次未识别到需要绘制的技术形态。</p></section>'
    charts = visuals.charts if visuals is not None else []
    chart_by_pattern = {
        chart.annotations[0].label: chart
        for chart in charts
        if chart.annotations
    }
    chart_numbers = {
        chart.chart_id: index
        for index, chart in enumerate(
            [item for item in charts if item.status == "generated"], 1
        )
    }
    items: list[str] = []
    for pattern in patterns:
        chart = chart_by_pattern.get(pattern)
        if chart is None or chart.status == "skipped":
            notice = (
                "图表生成失败，保留本次形态文字结论。"
                if visuals is None or chart is not None
                else "该观察没有对应的结构化形态信号，因此不强制制图。"
            )
            chart_html = f'<p class="pattern-notice">{html.escape(notice)}</p>'
        else:
            chart_html = _technical_chart_card(chart, chart_numbers[chart.chart_id])
        items.append(
            f'''<article class="pattern-visual" data-pattern-name="{html.escape(pattern, quote=True)}">
<div class="pattern-heading"><span>本次识别</span><h3>{html.escape(pattern)}</h3></div>
{chart_html}</article>'''
        )
    return '<section class="pattern-visuals">' + "".join(items) + "</section>"


def _market_overview(visuals: ReportVisuals | None):
    if visuals is None:
        raise ReportError("缺少必备行情全景图")
    for chart in visuals.charts:
        if chart.chart_id == "technical-market-overview" and chart.status == "generated":
            return chart
    raise ReportError("必备行情全景图不可用")


def _inject_market_overview(
    rendered: str, visuals: ReportVisuals | None, artifact_dir: Path
) -> str:
    overview = _market_overview(visuals)
    generated = [item for item in visuals.charts if item.status == "generated"]
    chart_number = generated.index(overview) + 1
    card = _baseline_chart_card(
        overview, chart_number, _baseline_chart_image(artifact_dir)
    )
    return re.sub(
        r"(<h2>二、趋势分析</h2>.*?)(?=<h2>三、量价关系</h2>)",
        lambda match: match.group(1) + card,
        rendered,
        count=1,
        flags=re.DOTALL,
    )


def _inject_pattern_visuals(
    rendered: str,
    patterns: list[str],
    visuals: ReportVisuals | None,
) -> str:
    replacement = r"\1" + _pattern_visuals_html(patterns, visuals)
    return re.sub(
        r"(<h2>七、技术形态候选</h2>)\s*<ul>.*?</ul>",
        replacement,
        rendered,
        count=1,
        flags=re.DOTALL,
    )


def _inject_technical_visuals(
    rendered: str,
    patterns: list[str],
    visuals: ReportVisuals | None,
    artifact_dir: Path,
) -> str:
    return _inject_pattern_visuals(
        _inject_market_overview(rendered, visuals, artifact_dir), patterns, visuals
    )


def _safe_markdown_html(report: str) -> str:
    rendered = markdown.markdown(report, extensions=["extra", "sane_lists"], output_format="html")
    return bleach.clean(
        rendered,
        tags={
            "h1", "h2", "h3", "h4", "p", "ul", "ol", "li", "strong",
            "em", "code", "pre", "blockquote", "hr", "br", "table", "thead",
            "tbody", "tr", "th", "td",
        },
        attributes={},
        strip=True,
    )


def _write_html_report(
    run: ResearchRun,
    artifact_dir: Path,
    report: str,
    visuals: ReportVisuals | None,
    patterns: list[str],
) -> Path:
    payload = _canvas_payload(visuals or ReportVisuals())
    attr_visuals = html.escape(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        quote=True,
    )
    report_html = _inject_technical_visuals(
        _safe_markdown_html(report), patterns, visuals, artifact_dir
    )
    document = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(run.security_name or run.resolved_symbol or run.input_symbol)} · 技术面研究报告</title><style>{TECHNICAL_REPORT_STYLE}</style></head>
<body><main class="technical-report-shell" id="technical-report" data-report-visuals="{attr_visuals}">
<header class="technical-hero"><div><p class="eyebrow">技术面研究</p><h1>个股技术面分析报告</h1><p>{html.escape(run.security_name or "")} · {html.escape(run.resolved_symbol or "")} · 数据截止 {html.escape(run.as_of)}</p></div><aside>历史行情与当次形态<br><span>非投资建议</span></aside></header>
<article class="technical-copy">{report_html}</article>
</main><script>{TECHNICAL_CANVAS_RUNTIME}</script></body></html>'''
    path = artifact_dir / "technical_report.html"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(document, encoding="utf-8")
    os.replace(temporary, path)
    return path


def generate_technical_report(run: ResearchRun, artifact_dir: Path) -> Path:
    try:
        indicators = TechnicalIndicators.model_validate_json(
            (artifact_dir / "technical_indicators.json").read_text(encoding="utf-8")
        )
        research = TechnicalResearchOutput.model_validate_json(
            (artifact_dir / "technical_research.json").read_text(encoding="utf-8")
        )
        kronos = KronosResult.model_validate_json(
            (artifact_dir / "kronos_result.json").read_text(encoding="utf-8")
        )
        assembly = TechnicalAssemblyOutput.model_validate_json(
            (artifact_dir / "technical_assembly.json").read_text(encoding="utf-8")
        )
        identities = {
            (item.symbol, item.data_version)
            for item in (indicators, research, kronos, assembly)
        }
        if identities != {(run.resolved_symbol, run.data_version)}:
            raise ReportError("报告输入的证券或数据版本不一致")
        market = load_persisted_market_data(
            artifact_dir / "market_data.csv",
            symbol=run.resolved_symbol or "",
            as_of=indicators.as_of,
            expected_data_version=run.data_version or "",
        )
        probability = kronos.direction_probability
        expected_range = kronos.expected_return_range
        levels = indicators.support_resistance
        visuals = _load_visuals(artifact_dir)
        report = f"""# 个股技术面分析报告

## 一、证券与数据说明

- 股票名称：{run.security_name}
- 标准证券代码：{run.resolved_symbol}
- 数据截止日期：{run.as_of}
- 日线数量：{len(market)}
- data_version：`{run.data_version}`

## 二、趋势分析

- 最新收盘价：{indicators.latest_price}
- SMA5：{indicators.trend.sma5}
- SMA20：{indicators.trend.sma20}
- SMA60：{indicators.trend.sma60}
- 均线排列：{indicators.trend.alignment}

{_safe_narrative(research.trend)}

## 三、量价关系

- 最新成交量：{indicators.volume.latest}
- Volume MA5：{indicators.volume.ma5}
- Volume MA20：{indicators.volume.ma20}

{_safe_narrative(research.volume_price)}

## 四、动量指标

- MACD DIF：{indicators.macd.dif}
- MACD DEA：{indicators.macd.dea}
- MACD 柱：{indicators.macd.histogram}
- MACD 状态：{indicators.macd.cross}
- RSI14：{indicators.rsi.rsi14}（{indicators.rsi.state}）
- KDJ：K {indicators.kdj.k} / D {indicators.kdj.d} / J {indicators.kdj.j}（{indicators.kdj.cross}）
- 布林带：上轨 {indicators.bollinger.upper} / 中轨 {indicators.bollinger.middle} / 下轨 {indicators.bollinger.lower}

{_safe_narrative(research.momentum)}

## 五、波动率

- ATR14：{indicators.volatility.atr14}
- 20 日年化历史波动率：{indicators.volatility.annualized_volatility_20:.2%}

{_safe_narrative(research.volatility)}

## 六、支撑位与阻力位

- 20 日支撑位：{levels.support_20}
- 60 日支撑位：{levels.support_60}
- 20 日阻力位：{levels.resistance_20}
- 60 日阻力位：{levels.resistance_60}

{_safe_narrative(research.support_resistance)}

## 七、技术形态候选

{_authoritative_items(indicators.patterns)}

## 八、Kronos 模型结果

- 预测周期：{kronos.horizon}
- 上涨概率：{probability.up:.2%}
- 震荡概率：{probability.flat:.2%}
- 下跌概率：{probability.down:.2%}
- 预期收益区间：{expected_range[0]:.2%} ～ {expected_range[1]:.2%}
- 模型置信度：{kronos.model_confidence:.2%}

## 九、信号一致与冲突

Assembly 摘要：{_safe_narrative(assembly.summary)}

一致信号：

{_items(assembly.agreements)}

冲突信号：

{_items(assembly.conflicts)}

不确定性：

{_items(assembly.uncertainties)}

## 十、短期、中期和长期观察

- 短期：{_safe_narrative(assembly.short_term)}
- 中期：{_safe_narrative(assembly.medium_term)}
- 长期：{_safe_narrative(assembly.long_term)}

结论：{_safe_narrative(assembly.conclusion)}

## 十一、风险与限制

Technical Research 风险：

{_items(research.risks)}

Assembly 风险：

{_items(assembly.risks)}

## 十二、版本信息

- 技术指标脚本版本：`{indicators.script_version}`
- Kronos 模型版本：`{kronos.model_version}`
- 技术工作流版本：`{run.workflow_name}`
- 数据版本：`{indicators.data_version}`

## 免责声明

{DISCLAIMER}
"""
        artifact_dir.mkdir(parents=True, exist_ok=True)
        markdown_path = artifact_dir / "technical_report.md"
        temporary = markdown_path.with_name(f".{markdown_path.name}.tmp")
        temporary.write_text(report, encoding="utf-8")
        os.replace(temporary, markdown_path)
        return _write_html_report(run, artifact_dir, report, visuals, indicators.patterns)
    except ReportError:
        raise
    except Exception as exc:
        raise ReportError("技术面报告生成失败") from exc


def technical_report_is_current(run: ResearchRun, artifact_dir: Path) -> bool:
    path = artifact_dir / "technical_report.html"
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
        indicators = TechnicalIndicators.model_validate_json(
            (artifact_dir / "technical_indicators.json").read_text(encoding="utf-8")
        )
        research = TechnicalResearchOutput.model_validate_json(
            (artifact_dir / "technical_research.json").read_text(encoding="utf-8")
        )
        kronos = KronosResult.model_validate_json(
            (artifact_dir / "kronos_result.json").read_text(encoding="utf-8")
        )
        assembly = TechnicalAssemblyOutput.model_validate_json(
            (artifact_dir / "technical_assembly.json").read_text(encoding="utf-8")
        )
        identities = {
            (item.symbol, item.data_version)
            for item in (indicators, research, kronos, assembly)
        }
        if identities != {(run.resolved_symbol, run.data_version)}:
            return False
        market = load_persisted_market_data(
            artifact_dir / "market_data.csv",
            symbol=run.resolved_symbol or "",
            as_of=indicators.as_of,
            expected_data_version=run.data_version or "",
        )
        probability = kronos.direction_probability
        levels = indicators.support_resistance
        visuals = _load_visuals(artifact_dir)
        if visuals is None:
            return False
        overview = next(
            (
                chart for chart in visuals.charts
                if chart.chart_id == "technical-market-overview"
                and chart.status == "generated"
            ),
            None,
        )
        if overview is None:
            return False
        chart_path = artifact_dir / "technical_chart.png"
        if not chart_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
            return False
        chart_markers = ['class="technical-market-image"', 'data:image/png;base64,']
        chart_markers.extend(
            f'data-chart="{chart.chart_id}"'
            for chart in visuals.charts
            if chart.status == "generated" and chart.annotations
        )
        required = [
            "个股技术面分析报告",
            str(run.resolved_symbol),
            str(run.data_version),
            f"日线数量：{len(market)}",
            indicators.script_version,
            kronos.model_version,
            f"上涨概率：{probability.up:.2%}",
            f"震荡概率：{probability.flat:.2%}",
            f"下跌概率：{probability.down:.2%}",
            f"20 日支撑位：{levels.support_20}",
            f"60 日阻力位：{levels.resistance_60}",
            "data-report-visuals=",
            DISCLAIMER,
            _safe_narrative(research.trend),
            _safe_narrative(research.volume_price),
            _safe_narrative(research.momentum),
            _safe_narrative(research.volatility),
            _safe_narrative(research.support_resistance),
            _safe_narrative(assembly.summary),
            _safe_narrative(assembly.short_term),
            _safe_narrative(assembly.medium_term),
            _safe_narrative(assembly.long_term),
            _safe_narrative(assembly.conclusion),
        ]
        required.extend(chart_markers)
        # Pattern names are deterministic, code-generated facts and may
        # legitimately contain digits (for example, "20日突破").  Only
        # free-form agent prose goes through the precise-number guard.
        required.extend(indicators.patterns)
        for values in (
            research.risks,
            assembly.agreements,
            assembly.conflicts,
            assembly.uncertainties,
            assembly.risks,
        ):
            required.extend(_safe_narrative(value) for value in values)
        return all(value in text for value in required)
    except (OSError, ValueError, ReportError):
        return False


TECHNICAL_REPORT_STYLE = TECHNICAL_VISION_STYLE


TECHNICAL_CANVAS_RUNTIME = REPORT_CHART_RUNTIME
