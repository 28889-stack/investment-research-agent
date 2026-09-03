from __future__ import annotations

from app.fundamental.schemas import AssumptionStore, EvidenceCollection, FundamentalWriterOutput, validate_references


WRITER_CONTEXT_REFS = [
    "artifact:lead_synthesis", "artifact:writer_plan", "artifact:business_research", "artifact:industry_research",
    "artifact:deep_research",
    "artifact:financial_research", "artifact:valuation_research", "artifact:lead_final_review",
    "artifact:retrieval_package", "artifact:assumptions", "artifact:company_profile",
    "artifact:financial_metrics", "artifact:valuation_result",
]


def validate_writer_output(
    output: FundamentalWriterOutput,
    *,
    symbol: str,
    as_of: str,
    evidence: EvidenceCollection,
    assumptions: AssumptionStore,
    tool_call_count: int,
) -> None:
    if output.symbol != symbol or output.as_of.isoformat() != as_of:
        raise ValueError("Fundamental Writer 身份与当前任务不一致")
    if tool_call_count != 0:
        raise ValueError("Fundamental Writer 禁止调用工具")
    validate_references(output, evidence, assumptions)
