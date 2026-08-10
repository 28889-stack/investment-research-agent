from __future__ import annotations

import json
import os
from pathlib import Path

from app.fundamental.schemas import (
    AssumptionStore,
    CompanyProfile,
    EvidenceCollection,
    FinancialData,
    FinancialMetrics,
    FinancialResearchOutput,
    LeadFinalReviewOutput,
    LeadPlanOutput,
    LeadReviewOutput,
    SpecialistResearchOutput,
    ValuationResearchOutput,
    ValuationResult,
)


INTRO = """本文档是第四阶段生成的基本面研究工作包，
用于汇总各专业研究节点的结构化结果。
正式报告将在 Fundamental Writer 阶段生成。"""
DISCLAIMER = "本工作包不构成投资建议、交易指令或收益承诺。"


def _load(model, path: Path):
    return model.model_validate_json(path.read_text(encoding="utf-8"))


def _items(values: list[str]) -> str:
    return "\n".join(f"- {item}" for item in values) if values else "- 无"


def generate_research_package(directory: Path) -> Path:
    company = _load(CompanyProfile, directory / "company_profile.json")
    data = _load(FinancialData, directory / "financial_data.json")
    metrics = _load(FinancialMetrics, directory / "financial_metrics.json")
    evidence = _load(EvidenceCollection, directory / "evidence.json")
    assumptions = _load(AssumptionStore, directory / "assumptions.json")
    lead = _load(LeadPlanOutput, directory / "lead_plan.json")
    business = _load(SpecialistResearchOutput, directory / "business_research.json")
    industry = _load(SpecialistResearchOutput, directory / "industry_research.json")
    deep = _load(SpecialistResearchOutput, directory / "deep_research.json")
    review = _load(LeadReviewOutput, directory / "lead_review.json")
    financial = _load(FinancialResearchOutput, directory / "financial_research.json")
    valuation = _load(ValuationResult, directory / "valuation_result.json")
    valuation_research = _load(ValuationResearchOutput, directory / "valuation_research.json")
    final = _load(LeadFinalReviewOutput, directory / "lead_final_review.json")
    latest = data.periods[-1]
    latest_metrics = {
        "growth": metrics.growth[latest.period],
        "profitability": metrics.profitability[latest.period],
        "balance_sheet": metrics.balance_sheet[latest.period],
        "cash_flow": metrics.cash_flow[latest.period],
        "efficiency": metrics.efficiency[latest.period],
    }
    relative = valuation.relative
    ready = (
        "可以进入 Fundamental Writer 阶段"
        if final.ready_for_writer
        else "材料尚不足以进入正式 Writer 阶段"
    )
    evidence_lines = [
        f"- `{item.id}` | {item.source_name} | {item.date} | {item.claim} | {item.url}"
        for item in evidence.items
    ]
    assumption_lines = [
        f"- `{item.id}` | {item.variable} = {item.value} | {item.period} | {item.source}"
        for item in assumptions.items
    ]
    text = f"""# 基本面研究工作包

{INTRO}

## 一、证券与数据说明

- 公司：{company.company_name}（{company.short_name}）
- 标准代码：{company.symbol}
- 行业：{company.industry}
- 数据截止：{company.as_of.isoformat()}
- 财务数据源：{data.data_source}
- 币种/单位：{data.currency}/{data.unit}
- 财务指标脚本：{metrics.script_version}
- 估值脚本：{valuation.script_version}

## 二、Lead 研究主线

{lead.thesis}

关键问题：

{_items(lead.key_questions)}

## 三、公司业务研究

{business.summary}

{_items([item.claim for item in business.findings])}

## 四、行业研究

{industry.summary}

{_items([item.claim for item in industry.findings])}

## 五、财务数据与指标

- 最新期：{latest.period}
- 营业收入：{latest.revenue}
- 归母净利润：{latest.net_profit_attributable}
- 经营现金流：{latest.operating_cash_flow}
- 财务指标 JSON：`{json.dumps(latest_metrics, ensure_ascii=False, sort_keys=True)}`

## 六、Lead 补充任务与深度检索

{deep.summary}

{_items([item.claim for item in deep.findings])}

补充检索待优化事项：

{_items(deep.missing_information)}

## 七、财务研究结论

{financial.summary}

- 增长：{financial.growth_analysis}
- 盈利：{financial.profitability_analysis}
- 现金流：{financial.cash_flow_analysis}
- 资产负债：{financial.balance_sheet_analysis}

## 八、估值计算结果

- PE：{relative.pe.value if relative.pe.status == 'available' else 'unavailable'}
- PB：{relative.pb.value if relative.pb.status == 'available' else 'unavailable'}
- PS：{relative.ps.value if relative.ps.status == 'available' else 'unavailable'}
- DCF 每股价值：{valuation.dcf.per_share_value if valuation.dcf.status == 'available' else 'unavailable'}
- DCF 区间：{valuation.dcf.valuation_range if valuation.dcf.valuation_range else 'unavailable'}
- 敏感性：`{json.dumps(valuation.dcf.sensitivity, ensure_ascii=False, sort_keys=True)}`

## 九、估值研究结论

{valuation_research.summary}

{valuation_research.interpretation}

## 十、关键假设

{chr(10).join(assumption_lines) if assumption_lines else '- 无'}

## 十一、Evidence 索引

{chr(10).join(evidence_lines) if evidence_lines else '- 无'}

## 十二、研究冲突和优化建议

冲突：

{_items(final.conflicts)}

优化建议：

{_items(final.missing_information)}

## 十三、建议报告大纲

{_items(final.report_outline)}

## 十四、是否可以进入 Writer 阶段

{ready}

## 免责声明

{DISCLAIMER}
"""
    path = directory / "fundamental_research_package.md"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
    return path


def research_package_is_current(directory: Path) -> bool:
    path = directory / "fundamental_research_package.md"
    if not path.is_file():
        return False
    try:
        final = _load(LeadFinalReviewOutput, directory / "lead_final_review.json")
        valuation = _load(ValuationResult, directory / "valuation_result.json")
        text = path.read_text(encoding="utf-8")
        required = [
            INTRO,
            DISCLAIMER,
            final.research_thesis,
            str(valuation.relative.pe.value) if valuation.relative.pe.status == "available" else "PE：unavailable",
        ]
        return all(item in text for item in required)
    except (OSError, ValueError):
        return False
