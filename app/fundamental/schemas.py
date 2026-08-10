from __future__ import annotations

import math
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CompanyProfile(StrictModel):
    symbol: str
    company_name: str
    short_name: str
    industry: str
    listing_date: str
    business_summary: str
    currency: str
    as_of: date
    data_source: str


class FinancialPeriod(StrictModel):
    period: str
    report_type: str
    published_date: str
    revenue: float | None
    operating_profit: float | None
    net_profit: float | None
    net_profit_attributable: float | None
    total_assets: float | None
    total_liabilities: float | None
    interest_bearing_debt: float | None = None
    shareholders_equity: float | None
    current_assets: float | None = None
    current_liabilities: float | None = None
    cash: float | None
    accounts_receivable: float | None
    inventory: float | None
    operating_cash_flow: float | None
    capital_expenditure: float | None
    basic_eps: float | None
    shares_outstanding: float | None

    @field_validator("period", "published_date")
    @classmethod
    def valid_iso_date(cls, value: str) -> str:
        date.fromisoformat(value)
        return value

    @field_validator(
        "revenue",
        "operating_profit",
        "net_profit",
        "net_profit_attributable",
        "total_assets",
        "total_liabilities",
        "interest_bearing_debt",
        "shareholders_equity",
        "current_assets",
        "current_liabilities",
        "cash",
        "accounts_receivable",
        "inventory",
        "operating_cash_flow",
        "capital_expenditure",
        "basic_eps",
        "shares_outstanding",
    )
    @classmethod
    def finite_number_or_null(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("financial values must be finite or null")
        return value


class FinancialData(StrictModel):
    symbol: str
    as_of: date
    currency: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    data_source: str = Field(min_length=1)
    periods: list[FinancialPeriod] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_periods(self) -> "FinancialData":
        names = [item.period for item in self.periods]
        if len(names) != len(set(names)):
            raise ValueError("period must be unique")
        if names != sorted(names):
            raise ValueError("period must be sorted")
        if not any(
            item.revenue is not None or item.total_assets is not None
            for item in self.periods
        ):
            raise ValueError("core financial fields cannot all be null")
        if any(date.fromisoformat(item.published_date) > self.as_of for item in self.periods):
            raise ValueError("financial period published after as_of")
        return self


class EvidenceItem(StrictModel):
    id: str = Field(pattern=r"^ev_\d{3,}$")
    claim: str
    content: str
    source_name: str
    url: str
    date: str
    location: str
    type: Literal[
        "historical_fact",
        "management_statement",
        "third_party_forecast",
        "analyst_estimate",
    ]


class EvidenceCollection(StrictModel):
    items: list[EvidenceItem]


class ResearchSource(StrictModel):
    result_id: str
    title: str
    url: str
    source_name: str
    date: str
    summary: str
    content: str = ""  # pre-fetched body (akshare news); empty means download on read
    source_kind: str = ""  # announcement | news | research_report | web | financial | technical


class ResearchSearchResults(StrictModel):
    items: list[ResearchSource]


class AssumptionItem(StrictModel):
    id: str = Field(pattern=r"^asm_\d{3,}$")
    variable: str
    value: float
    period: str
    source: str
    owner: str

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("assumption must be finite")
        return value


class AssumptionStore(StrictModel):
    items: list[AssumptionItem]


MetricValue = float | None
MetricGroup = dict[str, dict[str, MetricValue]]


class FinancialMetrics(StrictModel):
    symbol: str
    as_of: date
    script_version: str
    periods: list[str]
    growth: MetricGroup
    profitability: MetricGroup
    balance_sheet: MetricGroup
    cash_flow: MetricGroup
    efficiency: MetricGroup
    missing_metrics: list[str]


class MarketSnapshot(StrictModel):
    symbol: str
    as_of: date
    latest_price: float | None
    market_cap: float | None
    currency: str
    data_source: str

    @field_validator("latest_price", "market_cap")
    @classmethod
    def finite_market_value(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("market values must be finite")
        return value


class RelativeMethod(StrictModel):
    status: Literal["available", "unavailable"]
    value: float | None
    reason: str | None = None


class RelativeValuation(StrictModel):
    pe: RelativeMethod
    pb: RelativeMethod
    ps: RelativeMethod


class DcfResult(StrictModel):
    status: Literal["available", "unavailable"]
    per_share_value: float | None
    valuation_range: tuple[float, float] | None
    sensitivity: dict[str, float]
    reason: str | None = None

    @model_validator(mode="after")
    def ordered_range(self) -> "DcfResult":
        if self.valuation_range and self.valuation_range[0] > self.valuation_range[1]:
            raise ValueError("valuation range lower bound exceeds upper bound")
        return self


class ValuationResult(StrictModel):
    symbol: str
    as_of: date
    script_version: str
    relative: RelativeValuation
    dcf: DcfResult
    assumption_ids: list[str]
    market_snapshot: MarketSnapshot


class LeadPlanOutput(StrictModel):
    symbol: str
    as_of: date
    thesis: str
    key_questions: list[str]
    business_scope: list[str]
    industry_scope: list[str]
    financial_focus: list[str]
    valuation_focus: list[str]
    risks_to_verify: list[str]
    evidence_ids: list[str]


class ResearchFinding(StrictModel):
    claim: str
    evidence_ids: list[str]
    confidence: Literal["low", "medium", "high"]


class SpecialistResearchOutput(StrictModel):
    symbol: str
    summary: str
    findings: list[ResearchFinding]
    risks: list[str]
    missing_information: list[str]


class LeadReviewOutput(StrictModel):
    symbol: str
    business_status: Literal["accepted", "accepted_with_gaps"]
    industry_status: Literal["accepted", "accepted_with_gaps"]
    key_findings: list[str]
    conflicts: list[str]
    financial_questions: list[str]
    missing_information: list[str]
    followup_research_tasks: list[str] = Field(default_factory=list)


class AssumptionProposal(StrictModel):
    variable: str
    value: float
    period: str
    source: str

    @field_validator("value")
    @classmethod
    def finite_proposal(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("assumption proposal must be finite")
        return value


class FinancialResearchDraft(StrictModel):
    symbol: str
    summary: str
    growth_analysis: str
    profitability_analysis: str
    cash_flow_analysis: str
    balance_sheet_analysis: str
    earnings_drivers: list[str]
    assumptions: list[AssumptionProposal]
    risks: list[str]
    evidence_ids: list[str]
    confidence: Literal["low", "medium", "high"]


class FinancialResearchOutput(StrictModel):
    symbol: str
    summary: str
    growth_analysis: str
    profitability_analysis: str
    cash_flow_analysis: str
    balance_sheet_analysis: str
    earnings_drivers: list[str]
    assumption_ids: list[str]
    risks: list[str]
    evidence_ids: list[str]
    confidence: Literal["low", "medium", "high"]


class ValuationResearchOutput(StrictModel):
    symbol: str
    summary: str
    methods_used: list[str]
    interpretation: str
    sensitivity: str
    risks: list[str]
    assumption_ids: list[str]
    evidence_ids: list[str]
    confidence: Literal["low", "medium", "high"]


class LeadFinalReviewOutput(StrictModel):
    symbol: str
    research_thesis: str
    approved_sections: list[Literal["business", "industry", "financial", "valuation"]]
    key_findings: list[str]
    conflicts: list[str]
    missing_information: list[str]
    report_outline: list[str]
    ready_for_writer: bool


class RetrievalPackageItem(StrictModel):
    evidence_id: str
    source_name: str
    source_type: str
    date: str
    claim: str
    url: str
    excerpt: str


class RetrievalPackage(StrictModel):
    symbol: str
    as_of: date
    items: list[RetrievalPackageItem]


class LeadSectionSynthesis(StrictModel):
    section: Literal["business", "industry", "financial", "valuation"]
    main_point: str
    material_usage: str
    allowed_evidence_ids: list[str]
    allowed_assumption_ids: list[str] = Field(default_factory=list)


class LeadSynthesisOutput(StrictModel):
    symbol: str
    as_of: date
    report_mainline: str
    executive_focus: str
    sections: list[LeadSectionSynthesis]
    key_findings: list[str]
    conflicts: list[str]
    risks: list[str]
    missing_information: list[str]


class WriterPlanSection(StrictModel):
    section: Literal["business", "industry", "financial", "valuation"]
    purpose: str
    narrative_order: int = Field(ge=1, le=8)
    allowed_evidence_ids: list[str]
    allowed_assumption_ids: list[str] = Field(default_factory=list)
    visual_emphasis: Literal["none", "trend", "quality", "valuation"] = "none"


class WriterPlanOutput(StrictModel):
    symbol: str
    as_of: date
    title: str
    executive_focus: str
    sections: list[WriterPlanSection]
    key_findings: list[str]
    risks: list[str]
    missing_information: list[str]


class WriterNarrativeSection(StrictModel):
    summary: str
    evidence_ids: list[str]


class WriterAnalyticalSection(WriterNarrativeSection):
    assumption_ids: list[str]


class FundamentalWriterOutput(StrictModel):
    symbol: str
    as_of: date
    status: Literal["completed", "needs_more_research"]
    executive_summary: str
    business: WriterNarrativeSection
    industry: WriterNarrativeSection
    financial: WriterAnalyticalSection
    valuation: WriterAnalyticalSection
    key_findings: list[str]
    conflicts: list[str]
    risks: list[str]
    missing_information: list[str]
    conclusion: str
    disclaimer: str


def validate_references(
    output: BaseModel,
    evidence: EvidenceCollection,
    assumptions: AssumptionStore,
) -> None:
    evidence_ids: list[str] = list(getattr(output, "evidence_ids", []))
    findings = getattr(output, "findings", [])
    for finding in findings:
        evidence_ids.extend(finding.evidence_ids)
    for section_name in ("business", "industry", "financial", "valuation"):
        section = getattr(output, section_name, None)
        if section is not None:
            evidence_ids.extend(section.evidence_ids)
    for section in getattr(output, "sections", []):
        evidence_ids.extend(getattr(section, "allowed_evidence_ids", []))
    known_evidence = {item.id for item in evidence.items}
    missing_evidence = set(evidence_ids) - known_evidence
    if missing_evidence:
        raise ValueError(f"Evidence ID 不存在: {', '.join(sorted(missing_evidence))}")
    assumption_ids: list[str] = list(getattr(output, "assumption_ids", []))
    for section_name in ("financial", "valuation"):
        section = getattr(output, section_name, None)
        if section is not None:
            assumption_ids.extend(section.assumption_ids)
    for section in getattr(output, "sections", []):
        assumption_ids.extend(getattr(section, "allowed_assumption_ids", []))
    known_assumptions = {item.id for item in assumptions.items}
    missing_assumptions = set(assumption_ids) - known_assumptions
    if missing_assumptions:
        raise ValueError(f"Assumption ID 不存在: {', '.join(sorted(missing_assumptions))}")
