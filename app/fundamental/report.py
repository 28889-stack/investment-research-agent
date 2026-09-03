from __future__ import annotations

import ipaddress
import html
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from app.fundamental.result_manifest import ResultManifestStore
from app.fundamental.schemas import (
    AssumptionStore,
    CompanyProfile,
    EvidenceCollection,
    FinancialData,
    FinancialMetrics,
    FundamentalWriterOutput,
    PlannedVisual,
    ValuationResult,
    validate_references,
)
from app.charts.schemas import ChartSpec, ReportVisuals
from app.charts.runtime import REPORT_CHART_RUNTIME
from app.charts.styles import FUNDAMENTAL_VISION_STYLE
from app.fundamental.visuals import build_default_fundamental_chart_registry


DISCLAIMER = """本报告基于公开资料、历史财务数据、研究假设及简化估值模型生成。
报告中的预测和估值对假设高度敏感，不能保证未来实际结果。
本报告不构成投资建议、交易指令或收益承诺。"""
ASSUMPTION_WARNING = """预测和估值结果依赖上述假设。
实际结果可能因假设变化而显著不同。"""


def _load(model, path: Path):
    return model.model_validate_json(path.read_text(encoding="utf-8"))


def _value(value) -> str:
    return "unavailable" if value is None else str(value)


def _items(values: list[str]) -> str:
    return "\n".join(f"- {value}" for value in values) if values else "- 无"


def _refs(ids: list[str]) -> str:
    return " ".join(f"[{item}]" for item in dict.fromkeys(ids))


def _safe_display_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Evidence URL 不安全")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("Evidence URL 不安全")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Evidence URL 不安全")
    port = parsed.port
    if port not in {None, 80, 443}:
        raise ValueError("Evidence URL 不安全")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def generate_fundamental_report(
    directory: Path,
    *,
    run_id: str,
    workflow_version: str,
    writer_profile_version: str,
    writer_model_version: str = "not_configured",
) -> Path:
    directory = Path(directory)
    company = _load(CompanyProfile, directory / "company_profile.json")
    data = _load(FinancialData, directory / "financial_data.json")
    metrics = _load(FinancialMetrics, directory / "financial_metrics.json")
    valuation = _load(ValuationResult, directory / "valuation_result.json")
    evidence = _load(EvidenceCollection, directory / "evidence.json")
    assumptions = _load(AssumptionStore, directory / "assumptions.json")
    writer = _load(FundamentalWriterOutput, directory / "fundamental_writer.json")
    if writer.status not in {"completed", "needs_more_research"}:
        raise ValueError("Writer 状态无效，不能生成正式报告材料")
    if writer.symbol != company.symbol or writer.as_of != company.as_of:
        raise ValueError("Writer 身份与当前报告不一致")
    validate_references(writer, evidence, assumptions)

    referenced_ids: list[str] = []
    for section in (writer.business, writer.industry, writer.financial, writer.valuation):
        referenced_ids.extend(section.evidence_ids)
    for section in writer.sections:
        referenced_ids.extend(section.evidence_ids)
    referenced_ids = list(dict.fromkeys(referenced_ids))
    evidence_by_id = {item.id: item for item in evidence.items}
    evidence_lines: list[str] = []
    for evidence_id in referenced_ids:
        item = evidence_by_id[evidence_id]
        excerpt = " ".join(item.content.split())[:240]
        evidence_lines.extend(
            [
                f"### {item.id}",
                f"- 支持结论：{item.claim}",
                f"- 来源：{item.source_name}",
                f"- 日期：{item.date}",
                f"- 位置：{item.location or '未标注'}",
                f"- URL：{_safe_display_url(item.url)}",
                f"- 来源类型：{item.type}",
                f"- 限长摘要：{excerpt or '无'}",
            ]
        )

    assumption_lines: list[str] = []
    for item in assumptions.items:
        assumption_lines.extend(
            [
                f"### {item.id}",
                f"- 变量：{item.variable}",
                f"- 数值：{item.value}",
                f"- 适用期间：{item.period}",
                f"- 提出节点：{item.owner}",
                f"- 来源：{item.source}",
                f"- Assumption ID：{item.id}",
            ]
        )

    latest_period = data.periods[-1]
    period = latest_period.period
    growth = metrics.growth[period]
    profitability = metrics.profitability[period]
    balance_sheet = metrics.balance_sheet[period]
    cash_flow = metrics.cash_flow[period]
    efficiency = metrics.efficiency[period]
    relative = valuation.relative
    dcf = valuation.dcf
    try:
        manifest = ResultManifestStore(directory, run_id, workflow_version).load()
        writer_version = manifest.results.get("fundamental_writer").version if manifest.results.get("fundamental_writer") else 1
        report_version = (manifest.results.get("fundamental_report").version + 1) if manifest.results.get("fundamental_report") else 1
        artifact_versions = {
            name: manifest.results[name].version
            for name in ("financial_data", "financial_metrics", "valuation_result", "evidence", "assumptions")
            if name in manifest.results
        }
    except ValueError:
        writer_version = 1
        report_version = 1
        artifact_versions = {}

    text = f"""# 个股基本面分析报告

## 一、报告摘要

{writer.executive_summary}

## 二、公司与证券信息

- 公司：{company.company_name}（{company.short_name}）
- 证券代码：{company.symbol}
- 行业：{company.industry}
- 上市日期：{company.listing_date}
- 数据截止日期：{company.as_of.isoformat()}
- 币种/单位：{data.currency}/{data.unit}

## 三、商业模式与业务结构

{writer.business.summary} {_refs(writer.business.evidence_ids)}

## 四、行业与产业链分析

{writer.industry.summary} {_refs(writer.industry.evidence_ids)}

## 五、财务表现

最新年度报告期：{period}

### 收入和利润增长

- 营业收入：{_value(latest_period.revenue)}
- 归母净利润：{_value(latest_period.net_profit_attributable)}
- 营业收入同比：{_value(growth.get('revenue_yoy'))}
- 归母净利润同比：{_value(growth.get('net_profit_attributable_yoy'))}

{writer.financial.summary} {_refs(writer.financial.evidence_ids)}

### 盈利能力

{_items([f"{key}：{_value(value)}" for key, value in profitability.items()])}

### 资产负债

{_items([f"{key}：{_value(value)}" for key, value in balance_sheet.items()])}

### 现金流

{_items([f"{key}：{_value(value)}" for key, value in cash_flow.items()])}

### 运营效率

{_items([f"{key}：{_value(value)}" for key, value in efficiency.items()])}

## 六、核心盈利驱动

{_items(writer.key_findings)}

## 七、关键预测假设

{chr(10).join(assumption_lines) if assumption_lines else '- 无'}

{ASSUMPTION_WARNING}

## 八、估值分析

{writer.valuation.summary} {_refs(writer.valuation.evidence_ids)}

### PE / PB / PS

- PE：{_value(relative.pe.value) if relative.pe.status == 'available' else 'unavailable'}
- PB：{_value(relative.pb.value) if relative.pb.status == 'available' else 'unavailable'}
- PS：{_value(relative.ps.value) if relative.ps.status == 'available' else 'unavailable'}

### 简化 DCF

- 每股价值：{_value(dcf.per_share_value) if dcf.status == 'available' else 'unavailable'}
- 估值区间：{_value(dcf.valuation_range) if dcf.status == 'available' else 'unavailable'}

### 敏感性分析

{_items([f"{key}：{value}" for key, value in dcf.sensitivity.items()])}

## 九、研究证据

{chr(10).join(evidence_lines) if evidence_lines else '- 无'}

## 十、研究冲突与不确定性

冲突：

{_items(writer.conflicts)}

优化建议：

{_items(writer.missing_information)}

结论：{writer.conclusion}

## 十一、主要风险

{_items(writer.risks)}

## 十二、数据和方法限制

- 财务数据源：{data.data_source}
- 公司资料源：{company.data_source}
- 简化估值模型不等同于完整预测模型，结果对输入和假设敏感。
- Evidence 仅展示被正文引用的限长摘要，不展示来源全文。

## 十三、版本信息

- Workflow：{workflow_version}
- 财务指标脚本：{metrics.script_version}
- 估值脚本：{valuation.script_version}
- Writer Profile：{writer_profile_version}
- Writer Model：{writer_model_version}
- Writer Result：v{writer_version}
- Report Result：v{report_version}
- Financial Data Result：v{artifact_versions.get('financial_data', 1)}
- Financial Metrics Result：v{artifact_versions.get('financial_metrics', 1)}
- Valuation Result：v{artifact_versions.get('valuation_result', 1)}
- Evidence Result：v{artifact_versions.get('evidence', 1)}
- Assumption Result：v{artifact_versions.get('assumptions', 1)}
- 数据截止日期：{company.as_of.isoformat()}

## 免责声明

{DISCLAIMER}
"""
    path = directory / "fundamental_report.md"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
    _write_financial_html_report(
        directory,
        company=company,
        data=data,
        metrics=metrics,
        valuation=valuation,
        writer=writer,
        evidence_lines=evidence_lines,
        assumptions=assumptions,
        report_text=text,
    )
    return path


def _write_financial_html_report(
    directory: Path,
    *,
    company: CompanyProfile,
    data: FinancialData,
    metrics: FinancialMetrics,
    valuation: ValuationResult,
    writer: FundamentalWriterOutput,
    evidence_lines: list[str],
    assumptions: AssumptionStore,
    report_text: str,
) -> None:
    periods = data.periods
    visuals_path = directory / "report_visuals.json"
    if visuals_path.is_file():
        visuals_model = ReportVisuals.model_validate_json(
            visuals_path.read_text(encoding="utf-8")
        )
    else:
        # Compatibility for direct report generation outside the workflow.
        fallback_plans = [
            PlannedVisual(
                visual_id="visual-performance", section_id="financial-analysis",
                plugin_id="financial_performance_trend",
                analytical_question="经营规模与盈利趋势",
                source_mode="structured",
                metric_keys=["revenue", "net_profit_attributable"],
                preferred_chart_type="combo", unit_hint=data.unit,
                caption_focus="比较收入与归母净利润的变化方向",
                comparison_mode="time_series",
                comparison_basis="比较同一财务口径下收入与归母净利润的跨期变化",
            ),
            PlannedVisual(
                visual_id="visual-profitability", section_id="financial-analysis",
                plugin_id="profitability_quality",
                analytical_question="利润率与股东回报趋势",
                source_mode="structured",
                metric_keys=["gross_margin", "net_margin", "roe"],
                preferred_chart_type="line", unit_hint="比率",
                caption_focus="观察利润率与 ROE 的变化",
                comparison_mode="time_series",
                comparison_basis="比较利润率与 ROE 在相同历史期间内的变化",
            ),
        ]
        visuals_model = build_default_fundamental_chart_registry().materialize(
            fallback_plans,
            {
                "financial_data": data.model_dump(mode="json"),
                "financial_metrics": metrics.model_dump(mode="json"),
                "valuation_result": valuation.model_dump(mode="json"),
            },
        )
        visuals_tmp = visuals_path.with_name(f".{visuals_path.name}.tmp")
        visuals_tmp.write_text(
            visuals_model.model_dump_json(), encoding="utf-8"
        )
        os.replace(visuals_tmp, visuals_path)
    visuals = visuals_model.model_dump(mode="json")
    valuation_snapshot = {
        "pe": valuation.relative.pe.value if valuation.relative.pe.status == "available" else None,
        "pb": valuation.relative.pb.value if valuation.relative.pb.status == "available" else None,
        "ps": valuation.relative.ps.value if valuation.relative.ps.status == "available" else None,
        "dcf": valuation.dcf.per_share_value if valuation.dcf.status == "available" else None,
    }
    latest = periods[-1]
    latest_period = latest.period
    latest_growth = metrics.growth.get(latest_period, {})
    latest_profit = metrics.profitability.get(latest_period, {})
    attr_visuals = html.escape(json.dumps(visuals, ensure_ascii=False, separators=(",", ":")), quote=True)
    card = lambda label, value, note="": f'<div class="kpi"><span>{html.escape(label)}</span><strong>{html.escape(_value(value))}</strong><small>{html.escape(note)}</small></div>'
    evidence_html = "".join(f"<li>{html.escape(line.replace('### ', '').replace('- ', ''))}</li>" for line in evidence_lines) or "<li>无</li>"
    advice = writer.missing_information or []
    advice_html = "".join(f"<li>{html.escape(item)}</li>" for item in advice) or "<li>持续补充可比口径、经营分部和行业高频验证材料。</li>"
    assumptions_html = "".join(
        f"<li>{html.escape(item.variable)}：{html.escape(str(item.value))}（{html.escape(item.period)}）</li>"
        for item in assumptions.items
    ) or "<li>无</li>"
    generated_charts = [chart for chart in visuals_model.charts if chart.status == "generated"]
    chart_numbers = {
        chart.chart_id: index for index, chart in enumerate(generated_charts, 1)
    }

    def charts_for(section_id: str, placement: str) -> str:
        return "".join(
            _chart_card(chart, chart_numbers[chart.chart_id])
            for chart in generated_charts
            if chart.section_id == section_id and chart.placement == placement
        )

    if writer.sections:
        rendered_section_ids = {section.section_id for section in writer.sections}
        thematic_sections_html = "".join(
            f'''{charts_for(section.section_id, "before_section")}<section class="thematic-section">
<div class="section-heading"><span>{index:02d}</span><h2>{html.escape(section.title)}</h2></div>
<p class="section-claim">{html.escape(section.main_claim)}</p>
{charts_for(section.section_id, "after_claim")}
{''.join(f'<p>{html.escape(paragraph)}</p>' for paragraph in section.body.splitlines() if paragraph.strip())}
{charts_for(section.section_id, "after_body")}
<div class="observation"><strong>专题观察</strong><ul>{''.join(f'<li>{html.escape(item)}</li>' for item in section.observation_points) or '<li>结合后续披露持续验证。</li>'}</ul></div>
</section>'''
            for index, section in enumerate(writer.sections, 1)
        )
    else:
        rendered_section_ids = set()
        thematic_sections_html = f'''<section class="thematic-section"><div class="section-heading"><span>01</span><h2>商业模式与业务结构</h2></div><p>{html.escape(writer.business.summary)}</p></section>
<section class="thematic-section"><div class="section-heading"><span>02</span><h2>行业与产业链分析</h2></div><p>{html.escape(writer.industry.summary)}</p></section>
<section class="thematic-section"><div class="section-heading"><span>03</span><h2>财务表现</h2></div><p>{html.escape(writer.financial.summary)}</p></section>
<section class="thematic-section"><div class="section-heading"><span>04</span><h2>估值分析</h2></div><p>{html.escape(writer.valuation.summary)}</p></section>'''
    orphan_charts_html = "".join(
        _chart_card(chart, chart_numbers[chart.chart_id])
        for chart in generated_charts
        if chart.section_id not in rendered_section_ids
    )
    document = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(company.company_name)} · 基本面分析报告</title><style>{_FINANCIAL_REPORT_STYLE}</style></head>
<body><main class="report-shell" id="fundamental-report" data-report-visuals="{attr_visuals}">
<header class="report-hero"><div><p class="eyebrow">基本面研究</p><h1>个股基本面分析报告</h1><p>{html.escape(company.company_name)}（{html.escape(company.symbol)}） · {html.escape(company.industry)} · 数据截止 {html.escape(company.as_of.isoformat())}</p></div><div class="hero-tag">公开资料研究<br><span>非投资建议</span></div></header>
<section class="summary-grid"><article><h2>研究摘要</h2><p>{html.escape(writer.executive_summary)}</p><p class="mainline">{html.escape(writer.conclusion)}</p></article><aside><h2>核心结论</h2><ul>{''.join(f'<li>{html.escape(item)}</li>' for item in writer.key_findings)}</ul></aside></section>
<section class="kpi-grid">{card('营业收入', latest.revenue, f'{latest_period} · 同比 {_value(latest_growth.get("revenue_yoy"))}')} {card('归母净利润', latest.net_profit_attributable, f'同比 {_value(latest_growth.get("net_profit_attributable_yoy"))}')} {card('净利率', latest_profit.get('net_margin'), '盈利质量')} {card('自由现金流', metrics.cash_flow.get(latest_period, {}).get('free_cash_flow'), '现金流质量')}</section>
{thematic_sections_html}{orphan_charts_html}
<section class="section"><div class="section-heading"><span>DATA</span><h2>财务与估值数据</h2></div><div class="metric-table"><span>PE <b>{html.escape(_value(valuation_snapshot['pe']))}</b></span><span>PB <b>{html.escape(_value(valuation_snapshot['pb']))}</b></span><span>PS <b>{html.escape(_value(valuation_snapshot['ps']))}</b></span><span>DCF 每股价值 <b>{html.escape(_value(valuation_snapshot['dcf']))}</b></span></div><p>{html.escape(ASSUMPTION_WARNING)}</p><ul>{assumptions_html}</ul></section>
<section class="section risk"><div class="section-heading"><span>05</span><h2>风险与分歧</h2></div><ul>{''.join(f'<li>{html.escape(item)}</li>' for item in writer.risks + writer.conflicts)}</ul></section>
<section class="section advice"><div class="section-heading"><span>06</span><h2>优化建议</h2></div><p>以下事项可用于后续迭代研究深度和材料覆盖，不影响本报告已完成章节的阅读与判断。</p><ul>{advice_html}</ul></section>
<details class="evidence"><summary>研究证据与来源索引</summary><ul>{evidence_html}</ul></details><footer><p>{html.escape(DISCLAIMER)}</p><p>报告正文由已校验研究产物组织；图表仅使用受信财务、指标与估值数据。</p></footer>
</main><script>{_FINANCIAL_CANVAS_RUNTIME}</script></body></html>'''
    html_path = directory / "fundamental_report.html"
    html_tmp = html_path.with_name(f".{html_path.name}.tmp")
    html_tmp.write_text(document, encoding="utf-8")
    os.replace(html_tmp, html_path)


def _chart_card(chart: ChartSpec, chart_number: int) -> str:
    sources = "".join(
        f"<li>{html.escape(item)}</li>" for item in chart.source_notes
    )
    observations = "".join(
        f"<li>{html.escape(item)}</li>" for item in chart.observation_points
    )
    legend = "".join(
        f'<span><i></i>{html.escape(item.name)}</span>' for item in chart.series
    )
    return f'''<article class="chart-component chart-card" data-chart-type="{html.escape(chart.chart_type)}">
<div class="chart-header"><div><p class="chart-kicker">图 {chart_number}：</p><h2>{html.escape(chart.title)}</h2></div><span class="chart-unit">{html.escape(chart.unit)}</span></div>
<div class="chart-legend">{legend}</div>
<div class="chart-stage"><canvas data-chart="{html.escape(chart.chart_id)}" aria-label="{html.escape(chart.title)}"></canvas></div>
<p class="chart-explanation">{html.escape(chart.explanation)}</p>
{f'<div class="chart-notes"><strong>观察点</strong><ul>{observations}</ul></div>' if observations else ''}
{f'<details class="chart-sources"><summary>数据来源</summary><ul>{sources}</ul></details>' if sources else ''}
</article>'''


_FINANCIAL_REPORT_STYLE = FUNDAMENTAL_VISION_STYLE


_FINANCIAL_CANVAS_RUNTIME = REPORT_CHART_RUNTIME
