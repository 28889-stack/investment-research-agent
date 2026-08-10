from __future__ import annotations

import json

import pytest

from app.fundamental.schemas import (
    AssumptionItem,
    AssumptionStore,
    EvidenceCollection,
    EvidenceItem,
    FinancialResearchDraft,
    LeadFinalReviewOutput,
    LeadPlanOutput,
    LeadReviewOutput,
    SpecialistResearchOutput,
    ValuationResearchOutput,
    validate_references,
)
from app.runtime.output_validator import OutputValidator
from app.runtime.profiles import ProfileLoader


def test_fundamental_profiles_have_least_privilege(settings) -> None:
    profiles = ProfileLoader(settings.agent_profile_dir)
    lead = profiles.load("fundamental_lead")
    business = profiles.load("business_research")
    industry = profiles.load("industry_research")
    deep = profiles.load("deep_research")
    financial = profiles.load("financial_research")
    valuation = profiles.load("valuation_research")
    lead_synthesis = profiles.load("lead_synthesis")
    writer_planning = profiles.load("writer_planning")

    assert lead.mode == business.mode == industry.mode == deep.mode == "full"
    assert set(lead.allowed_tools) == {
        "get_company_profile",
        "search_research_sources",
        "read_research_source",
    }
    assert set(business.allowed_tools) == set(lead.allowed_tools)
    assert set(industry.allowed_tools) == {
        "search_research_sources",
        "read_research_source",
    }
    assert set(deep.allowed_tools) == set(industry.allowed_tools)
    assert financial.mode == valuation.mode == lead_synthesis.mode == writer_planning.mode == "constrained"
    assert financial.allowed_tools == valuation.allowed_tools == lead_synthesis.allowed_tools == writer_planning.allowed_tools == []
    assert financial.max_iterations == valuation.max_iterations == lead_synthesis.max_iterations == writer_planning.max_iterations == 1


def test_fundamental_output_schemas_are_strict_and_registered() -> None:
    validator = OutputValidator(20_000)
    lead = {
        "symbol": "600519.SH",
        "as_of": "2026-08-05",
        "thesis": "关注品牌壁垒与现金流持续性。",
        "key_questions": ["需求是否稳定"],
        "business_scope": ["产品结构"],
        "industry_scope": ["竞争格局"],
        "financial_focus": ["自由现金流"],
        "valuation_focus": ["DCF"],
        "risks_to_verify": ["需求波动"],
        "evidence_ids": ["ev_001"],
    }
    parsed = validator.validate_for_schema(json.dumps(lead, ensure_ascii=False), "lead_plan_output")
    assert isinstance(parsed, LeadPlanOutput)
    with pytest.raises(Exception) as error:
        validator.validate_for_schema(json.dumps({**lead, "secret": True}), "lead_plan_output")
    assert error.value.code == "FORBIDDEN_FIELD"


def test_specialist_confidence_and_review_status_are_enums() -> None:
    with pytest.raises(ValueError):
        SpecialistResearchOutput(
            symbol="600519.SH",
            summary="x",
            findings=[{"claim": "x", "evidence_ids": [], "confidence": "certain"}],
            risks=[],
            missing_information=[],
        )
    with pytest.raises(ValueError):
        LeadReviewOutput(
            symbol="600519.SH",
            business_status="rejected",
            industry_status="accepted",
            key_findings=[],
            conflicts=[],
            financial_questions=[],
            missing_information=[],
        )


def test_financial_draft_and_valuation_references_are_validated() -> None:
    evidence = EvidenceCollection(
        items=[
            EvidenceItem(
                id="ev_001",
                claim="x",
                content="x",
                source_name="x",
                url="",
                date="2026-01-01",
                location="",
                type="historical_fact",
            )
        ]
    )
    assumptions = AssumptionStore(
        items=[AssumptionItem(id="asm_001", variable="fcf_growth", value=0.08, period="forecast", source="financial_research", owner="financial_research")]
    )
    draft = FinancialResearchDraft(
        symbol="600519.SH",
        summary="x",
        growth_analysis="x",
        profitability_analysis="x",
        cash_flow_analysis="x",
        balance_sheet_analysis="x",
        earnings_drivers=[],
        assumptions=[],
        risks=[],
        evidence_ids=["ev_001"],
        confidence="medium",
    )
    valuation = ValuationResearchOutput(
        symbol="600519.SH",
        summary="x",
        methods_used=["DCF"],
        interpretation="x",
        sensitivity="x",
        risks=[],
        assumption_ids=["asm_001"],
        evidence_ids=["ev_001"],
        confidence="medium",
    )
    validate_references(draft, evidence, assumptions)
    validate_references(valuation, evidence, assumptions)
    with pytest.raises(ValueError, match="Evidence"):
        validate_references(draft.model_copy(update={"evidence_ids": ["ev_999"]}), evidence, assumptions)
    with pytest.raises(ValueError, match="Assumption"):
        validate_references(valuation.model_copy(update={"assumption_ids": ["asm_999"]}), evidence, assumptions)


def test_final_review_can_finish_without_writer_readiness() -> None:
    review = LeadFinalReviewOutput(
        symbol="600519.SH",
        research_thesis="研究主线",
        approved_sections=["business", "industry", "financial", "valuation"],
        key_findings=[],
        conflicts=[],
        missing_information=["仍缺少可比公司数据"],
        report_outline=["业务", "行业", "财务", "估值"],
        ready_for_writer=False,
    )
    assert review.ready_for_writer is False


@pytest.mark.parametrize(
    "summary",
    ["建议立即买入该股票", "买入该股票", "该策略保证获得收益", "We recommend selling the stock", "Sell now"],
)
def test_fundamental_output_allows_source_language_for_intermediate_research(summary) -> None:
    payload = {
        "symbol": "600519.SH",
        "summary": summary,
        "findings": [],
        "risks": [],
        "missing_information": [],
    }

    parsed = OutputValidator(20_000).validate_for_schema(
        json.dumps(payload, ensure_ascii=False), "specialist_research_output"
    )
    assert parsed.summary == summary


def test_specialist_output_allows_attributed_source_rating_and_return_language() -> None:
    payload = {
        "symbol": "601899.SH",
        "summary": "公开研报曾给出买入评级，并对盈利增长和股价上涨作出预测；本节点仅记录来源观点。",
        "findings": [],
        "risks": [],
        "missing_information": [],
    }

    parsed = OutputValidator(20_000).validate_for_schema(
        json.dumps(payload, ensure_ascii=False), "specialist_research_output"
    )
    assert parsed.summary.startswith("公开研报")
