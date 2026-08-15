from __future__ import annotations

from app.fundamental.schemas import FundamentalWriterOutput, LeadSynthesisOutput, RetrievalPackage


def build_targeted_material_index(
    package: RetrievalPackage, evidence_ids: set[str]
) -> list[dict[str, str]]:
    """Return only the cited material requested by the completeness pass."""
    return [
        {
            "evidence_id": item.evidence_id,
            "claim": item.claim,
            "source_name": item.source_name,
            "date": item.date,
            "url": item.url,
        }
        for item in package.items
        if item.evidence_id in evidence_ids
    ]


def uncovered_mainline_sections(
    lead: LeadSynthesisOutput, draft: FundamentalWriterOutput
) -> list[str]:
    """Identify Lead sections not represented in the composed report."""
    written_ids = {section.section_id for section in draft.sections}
    written_text = "\n".join(
        [draft.executive_summary, draft.conclusion]
        + [section.title + section.main_claim + section.body for section in draft.sections]
    )
    missing: list[str] = []
    for section in lead.sections:
        if section.section in written_ids:
            continue
        if section.main_point not in written_text:
            missing.append(section.section)
    return missing
