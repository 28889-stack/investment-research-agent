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
    ValuationResult,
    validate_references,
)


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
    if writer.status != "completed":
        raise ValueError("Writer 尚未完成正式报告材料")
    if writer.symbol != company.symbol or writer.as_of != company.as_of:
        raise ValueError("Writer 身份与当前报告不一致")
    validate_references(writer, evidence, assumptions)

    referenced_ids: list[str] = []
    for section in (writer.business, writer.industry, writer.financial, writer.valuation):
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
    labels = [item.period for item in periods]
    visuals = {
        "version": "financial_canvas_v1",
        "charts": [
            {
                "id": "performance",
                "title": "经营规模与盈利趋势",
                "labels": labels,
                "series": [
                    {"name": "营业收入", "values": [item.revenue for item in periods], "color": "#4cc9f0"},
                    {"name": "归母净利润", "values": [item.net_profit_attributable for item in periods], "color": "#f9c74f"},
                ],
            },
            {
                "id": "quality",
                "title": "利润率与现金流质量",
                "labels": labels,
                "series": [
                    {"name": "净利率", "values": [metrics.profitability.get(period, {}).get("net_margin") for period in labels], "color": "#80ed99"},
                    {"name": "自由现金流", "values": [metrics.cash_flow.get(period, {}).get("free_cash_flow") for period in labels], "color": "#ff9f1c"},
                ],
            },
        ],
        "valuation": {
            "pe": valuation.relative.pe.value if valuation.relative.pe.status == "available" else None,
            "pb": valuation.relative.pb.value if valuation.relative.pb.status == "available" else None,
            "ps": valuation.relative.ps.value if valuation.relative.ps.status == "available" else None,
            "dcf": valuation.dcf.per_share_value if valuation.dcf.status == "available" else None,
        },
    }
    visuals_path = directory / "report_visuals.json"
    visuals_tmp = visuals_path.with_name(f".{visuals_path.name}.tmp")
    visuals_tmp.write_text(json.dumps(visuals, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(visuals_tmp, visuals_path)
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
    document = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(company.company_name)} · 基本面分析报告</title><style>{_FINANCIAL_REPORT_STYLE}</style></head>
<body><main class="report-shell" id="fundamental-report" data-report-visuals="{attr_visuals}">
<header class="report-hero"><div><p class="eyebrow">EQUITY RESEARCH · FUNDAMENTAL</p><h1>个股基本面分析报告</h1><p>{html.escape(company.company_name)}（{html.escape(company.symbol)}） · {html.escape(company.industry)} · 数据截止 {html.escape(company.as_of.isoformat())}</p></div><div class="hero-tag">公开资料研究<br><span>非投资建议</span></div></header>
<section class="summary-grid"><article><h2>研究摘要</h2><p>{html.escape(writer.executive_summary)}</p><p class="mainline">{html.escape(writer.conclusion)}</p></article><aside><h2>核心结论</h2><ul>{''.join(f'<li>{html.escape(item)}</li>' for item in writer.key_findings)}</ul></aside></section>
<section class="kpi-grid">{card('营业收入', latest.revenue, f'{latest_period} · 同比 {_value(latest_growth.get("revenue_yoy"))}')} {card('归母净利润', latest.net_profit_attributable, f'同比 {_value(latest_growth.get("net_profit_attributable_yoy"))}')} {card('净利率', latest_profit.get('net_margin'), '盈利质量')} {card('自由现金流', metrics.cash_flow.get(latest_period, {}).get('free_cash_flow'), '现金流质量')}</section>
<section class="section"><div class="section-heading"><span>01</span><h2>商业模式与业务结构</h2></div><p>{html.escape(writer.business.summary)}</p></section>
<section class="section"><div class="section-heading"><span>02</span><h2>行业与产业链分析</h2></div><p>{html.escape(writer.industry.summary)}</p></section>
<section class="chart-grid"><article class="chart-card"><h2>经营规模与盈利趋势</h2><canvas data-chart="performance" aria-label="经营规模与盈利趋势"></canvas></article><article class="chart-card"><h2>利润率与现金流质量</h2><canvas data-chart="quality" aria-label="利润率与现金流质量"></canvas></article></section>
<section class="section"><div class="section-heading"><span>03</span><h2>财务表现</h2></div><p>{html.escape(writer.financial.summary)}</p><div class="metric-table"><span>PE <b>{html.escape(_value(visuals['valuation']['pe']))}</b></span><span>PB <b>{html.escape(_value(visuals['valuation']['pb']))}</b></span><span>PS <b>{html.escape(_value(visuals['valuation']['ps']))}</b></span><span>DCF 每股价值 <b>{html.escape(_value(visuals['valuation']['dcf']))}</b></span></div></section>
<section class="section"><div class="section-heading"><span>04</span><h2>估值分析</h2></div><p>{html.escape(writer.valuation.summary)}</p><p>{html.escape(ASSUMPTION_WARNING)}</p><ul>{assumptions_html}</ul></section>
<section class="section risk"><div class="section-heading"><span>05</span><h2>风险与分歧</h2></div><ul>{''.join(f'<li>{html.escape(item)}</li>' for item in writer.risks + writer.conflicts)}</ul></section>
<section class="section advice"><div class="section-heading"><span>06</span><h2>优化建议</h2></div><p>以下事项可用于后续迭代研究深度和材料覆盖，不影响本报告已完成章节的阅读与判断。</p><ul>{advice_html}</ul></section>
<details class="evidence"><summary>研究证据与来源索引</summary><ul>{evidence_html}</ul></details><footer><p>{html.escape(DISCLAIMER)}</p><p>报告正文由已校验研究产物组织；图表仅使用受信财务、指标与估值数据。</p></footer>
</main><script>{_FINANCIAL_CANVAS_RUNTIME}</script></body></html>'''
    html_path = directory / "fundamental_report.html"
    html_tmp = html_path.with_name(f".{html_path.name}.tmp")
    html_tmp.write_text(document, encoding="utf-8")
    os.replace(html_tmp, html_path)


_FINANCIAL_REPORT_STYLE = """
:root{--ink:#0b172a;--navy:#112b4d;--paper:#f5f7fa;--panel:#fff;--line:#dbe3ed;--muted:#637287;--cyan:#4cc9f0;--gold:#f9c74f}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}.report-shell{max-width:1180px;margin:0 auto;padding:36px 24px 64px}.report-hero{background:linear-gradient(135deg,#0d223d,#153d67);color:#fff;padding:44px;display:flex;justify-content:space-between;gap:24px}.eyebrow{letter-spacing:.14em;font-size:11px;color:#9edff1;margin:0}.report-hero h1{font-size:36px;letter-spacing:-.04em;margin:8px 0}.hero-tag{border:1px solid #6e9fc9;padding:12px 16px;height:max-content;text-align:right}.hero-tag span{font-size:12px;color:#bdd1e5}.summary-grid,.chart-grid{display:grid;grid-template-columns:1.5fr 1fr;gap:18px;margin:18px 0}.summary-grid article,.summary-grid aside,.section,.chart-card,.evidence{background:var(--panel);border:1px solid var(--line);padding:24px}.summary-grid h2,.chart-card h2{font-size:16px;margin:0 0 12px}.mainline{border-left:3px solid var(--cyan);padding-left:12px}.kpi-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0}.kpi{background:var(--panel);border-top:3px solid var(--navy);padding:16px}.kpi span,.kpi small{display:block;color:var(--muted);font-size:12px}.kpi strong{display:block;font-size:22px;margin:4px 0;overflow-wrap:anywhere}.section{margin:18px 0}.section-heading{display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--line);margin-bottom:14px;padding-bottom:10px}.section-heading span{color:#fff;background:var(--navy);font-size:11px;padding:2px 7px}.section-heading h2{font-size:18px;margin:0}.chart-card canvas{height:260px;width:100%;display:block}.metric-table{display:grid;grid-template-columns:repeat(4,1fr);border-top:1px solid var(--line);margin-top:16px}.metric-table span{padding:12px 8px;border-right:1px solid var(--line);color:var(--muted);font-size:12px}.metric-table b{display:block;color:var(--ink);font-size:17px}.risk{border-left:3px solid #d97706}.advice{border-left:3px solid #2563eb}.advice p{color:var(--muted)}details summary{cursor:pointer;font-weight:650}footer{color:var(--muted);font-size:12px;padding:16px 4px}@media(max-width:760px){.report-hero,.summary-grid,.chart-grid{display:block}.hero-tag{margin-top:20px;text-align:left}.kpi-grid{grid-template-columns:repeat(2,1fr)}.metric-table{grid-template-columns:repeat(2,1fr)}.report-shell{padding:0}.report-hero{padding:28px}.section,.summary-grid article,.summary-grid aside,.chart-card,.evidence{border-left:0;border-right:0}}
"""


_FINANCIAL_CANVAS_RUNTIME = """
(()=>{const root=document.querySelector('[data-report-visuals]');if(!root)return;let visuals;try{visuals=JSON.parse(root.dataset.reportVisuals)}catch{return}const draw=(canvas,chart)=>{const rect=canvas.getBoundingClientRect(),ratio=window.devicePixelRatio||1,w=Math.max(320,rect.width),h=260;canvas.width=w*ratio;canvas.height=h*ratio;const c=canvas.getContext('2d');c.scale(ratio,ratio);c.clearRect(0,0,w,h);const values=chart.series.flatMap(s=>s.values).filter(v=>typeof v==='number'&&isFinite(v));if(!values.length)return;const min=Math.min(...values),max=Math.max(...values),span=max-min||1,p={l:42,r:16,t:16,b:34};c.strokeStyle='#dbe3ed';c.lineWidth=1;for(let i=0;i<4;i++){let y=p.t+(h-p.t-p.b)*i/3;c.beginPath();c.moveTo(p.l,y);c.lineTo(w-p.r,y);c.stroke()}chart.series.forEach(s=>{const points=s.values.map((v,i)=>[p.l+(w-p.l-p.r)*(chart.labels.length<2?0:i/(chart.labels.length-1)),p.t+(h-p.t-p.b)*(1-((typeof v==='number'?v:min)-min)/span)]);c.strokeStyle=s.color;c.lineWidth=2;c.beginPath();points.forEach(([x,y],i)=>i?c.lineTo(x,y):c.moveTo(x,y));c.stroke();c.fillStyle=s.color;points.forEach(([x,y])=>{c.beginPath();c.arc(x,y,3,0,Math.PI*2);c.fill()})});c.fillStyle='#637287';c.font='11px sans-serif';chart.labels.forEach((label,i)=>{let x=p.l+(w-p.l-p.r)*(chart.labels.length<2?0:i/(chart.labels.length-1));c.fillText(label,x-12,h-12)});canvas.title=chart.series.map(s=>`${s.name}: ${s.values.join(' / ')}`).join('\n')};root.querySelectorAll('canvas[data-chart]').forEach(canvas=>{const chart=visuals.charts.find(item=>item.id===canvas.dataset.chart);if(chart){draw(canvas,chart);window.addEventListener('resize',()=>draw(canvas,chart),{passive:true})}})})();
"""

# Tooltip interaction is intentionally a second, dependency-free runtime: the
# exported HTML and the in-app fragment each keep chart data local to the file.
_FINANCIAL_CANVAS_RUNTIME += """
(()=>{const root=document.querySelector('[data-report-visuals]');if(!root)return;let visuals;try{visuals=JSON.parse(root.dataset.reportVisuals)}catch{return}root.querySelectorAll('canvas[data-chart]').forEach(canvas=>{const chart=visuals.charts.find(item=>item.id===canvas.dataset.chart);if(!chart)return;const host=canvas.parentElement;host.style.position='relative';const tip=document.createElement('div');tip.hidden=true;tip.style.cssText='position:absolute;z-index:2;max-width:260px;padding:5px 8px;background:#0d223d;color:#fff;font-size:12px;pointer-events:none';host.append(tip);canvas.addEventListener('mousemove',event=>{const rect=canvas.getBoundingClientRect(),ratio=(event.clientX-rect.left)/Math.max(rect.width,1),index=Math.max(0,Math.min(chart.labels.length-1,Math.round(ratio*(chart.labels.length-1))));tip.textContent=`${chart.labels[index]} · ${chart.series.map(s=>`${s.name}: ${s.values[index]??'—'}`).join(' | ')}`;tip.style.left=`${event.clientX-rect.left+12}px`;tip.style.top=`${event.clientY-rect.top+12}px`;tip.hidden=false});canvas.addEventListener('mouseleave',()=>tip.hidden=true)})})();
"""
