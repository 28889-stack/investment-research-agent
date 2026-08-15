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
    retrieval_notice: str | None = None


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
    industry_types: list[str] = Field(default_factory=list)
    financial_focus: list[str]
    valuation_focus: list[str]
    risks_to_verify: list[str]
    evidence_ids: list[str]


class ResearchFinding(StrictModel):
    claim: str
    evidence_ids: list[str]
    confidence: Literal["low", "medium", "high"]


class DeepResearchTaskCard(StrictModel):
    task_id: str = Field(pattern=r"^deep_\d{2}$")
    topic: str
    scope: str
    research_questions: list[str] = Field(min_length=1)
    priority_fact_types: list[str] = Field(default_factory=list)
    known_material: list[str] = Field(default_factory=list)
    excluded_claims: list[str] = Field(default_factory=list)


class DeepResearchQuery(StrictModel):
    task_id: str = Field(pattern=r"^deep_\d{2}$")
    queries: list[str] = Field(min_length=1, max_length=2)

    @field_validator("queries")
    @classmethod
    def valid_queries(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(item.strip() for item in values if item.strip()))
        if not cleaned or len(cleaned) > 2:
            raise ValueError("每张 Deep 任务卡必须有 1—2 个非空检索词")
        return cleaned


class DeepResearchQueryPlan(StrictModel):
    symbol: str
    queries: list[DeepResearchQuery]


class DeepResearchTopicResult(StrictModel):
    task_id: str = Field(pattern=r"^deep_\d{2}$")
    topic: str
    summary: str
    findings: list[ResearchFinding] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)


class SpecialistResearchOutput(StrictModel):
    symbol: str
    summary: str
    findings: list[ResearchFinding]
    risks: list[str]
    missing_information: list[str]
    topics: list[DeepResearchTopicResult] = Field(default_factory=list)


class LeadReviewOutput(StrictModel):
    symbol: str
    business_status: Literal["accepted", "accepted_with_gaps"]
    industry_status: Literal["accepted", "accepted_with_gaps"]
    key_findings: list[str]
    conflicts: list[str]
    financial_questions: list[str]
    missing_information: list[str]
    followup_research_tasks: list[str] = Field(default_factory=list)
    deep_research_tasks: list[DeepResearchTaskCard] = Field(default_factory=list)


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


class ReportCompositionSection(StrictModel):
    section_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    title: str = Field(min_length=2, max_length=80)
    purpose: str = Field(min_length=4, max_length=500)
    narrative_order: int = Field(ge=1, le=8)
    allowed_evidence_ids: list[str] = Field(default_factory=list)
    allowed_assumption_ids: list[str] = Field(default_factory=list)
    visual_components: list[Literal["chart", "table", "timeline", "callout"]] = Field(default_factory=list)
    writer_group: Literal["business", "industry", "financial"] | None = None


class PlannedVisual(StrictModel):
    visual_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")
    section_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    plugin_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_]{1,79}$")
    analytical_question: str = Field(min_length=4, max_length=500)
    source_mode: Literal["structured", "evidence", "mixed"]
    metric_keys: list[str] = Field(default_factory=list)
    allowed_evidence_ids: list[str] = Field(default_factory=list)
    allowed_assumption_ids: list[str] = Field(default_factory=list)
    preferred_chart_type: Literal[
        "line", "bar", "stacked_bar", "area", "candlestick", "combo",
        "band", "waterfall", "timeline",
    ]
    time_range: str = Field(default="all_available", max_length=80)
    unit_hint: str = Field(default="", max_length=40)
    placement: Literal["before_section", "after_claim", "after_body"] = "after_body"
    caption_focus: str = Field(default="", max_length=500)
    comparison_mode: Literal[
        "time_series", "cross_section", "scenario", "composition"
    ]
    comparison_basis: str = Field(min_length=4, max_length=500)
    priority: int = Field(default=1, ge=1, le=10)


class WriterPlanOutput(StrictModel):
    symbol: str
    as_of: date
    title: str
    executive_focus: str
    sections: list[WriterPlanSection]
    key_findings: list[str]
    risks: list[str]
    missing_information: list[str]
    report_composition: list[ReportCompositionSection] = Field(default_factory=list)
    visual_plan: list[PlannedVisual] = Field(default_factory=list)


class WriterNarrativeSection(StrictModel):
    summary: str
    evidence_ids: list[str]


class WriterAnalyticalSection(WriterNarrativeSection):
    assumption_ids: list[str]


class WrittenReportSection(StrictModel):
    section_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    title: str = Field(min_length=2, max_length=80)
    main_claim: str = Field(min_length=4, max_length=500)
    body: str = Field(min_length=40, max_length=24_000)
    evidence_ids: list[str] = Field(default_factory=list)
    assumption_ids: list[str] = Field(default_factory=list)
    observation_points: list[str] = Field(default_factory=list)


class WriterSectionOutput(StrictModel):
    """A bounded, independently-written portion of the final report."""

    symbol: str
    as_of: date
    section_group: Literal["business", "industry", "financial"]
    sections: list[WrittenReportSection] = Field(min_length=1)


class FinalSynthesisTextEdit(StrictModel):
    """A bounded edit against text already written by a Section Writer."""

    section_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    field: Literal["title", "main_claim", "body"]
    target_text: str = Field(min_length=1, max_length=600)
    replacement_text: str = Field(max_length=1_200)
    reason: Literal["deduplicate", "terminology", "consistency", "clarity"]


class FinalSynthesisTransition(StrictModel):
    before_section_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    text: str = Field(min_length=10, max_length=1_000)


class FinalSynthesisOutput(StrictModel):
    """Editorial instructions; intentionally cannot carry rewritten sections."""

    symbol: str
    as_of: date
    section_order: list[str] = Field(min_length=1)
    text_edits: list[FinalSynthesisTextEdit] = Field(default_factory=list, max_length=20)
    transitions: list[FinalSynthesisTransition] = Field(default_factory=list, max_length=8)
    executive_summary: str = Field(min_length=8, max_length=4_000)
    conclusion: str = Field(min_length=8, max_length=2_000)
    edit_summary: list[str] = Field(default_factory=list, max_length=20)


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
    sections: list[WrittenReportSection] = Field(default_factory=list)


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
        evidence_ids.extend(getattr(section, "evidence_ids", []))
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
        assumption_ids.extend(getattr(section, "assumption_ids", []))
    known_assumptions = {item.id for item in assumptions.items}
    missing_assumptions = set(assumption_ids) - known_assumptions
    if missing_assumptions:
        raise ValueError(f"Assumption ID 不存在: {', '.join(sorted(missing_assumptions))}")
