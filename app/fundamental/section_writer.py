from __future__ import annotations

from collections.abc import Iterable

from app.fundamental.schemas import (
    FinalSynthesisOutput,
    FundamentalWriterOutput,
    ReportCompositionSection,
    WriterAnalyticalSection,
    WriterNarrativeSection,
    WriterSectionOutput,
    WrittenReportSection,
)


SECTION_WRITER_CONTEXT_REFS = [
    "artifact:lead_synthesis", "artifact:writer_plan", "artifact:business_research",
    "artifact:industry_research", "artifact:deep_research", "artifact:financial_research",
    "artifact:valuation_research", "artifact:retrieval_package", "artifact:assumptions",
    "artifact:financial_metrics", "artifact:valuation_result",
]

FINAL_SYNTHESIS_CONTEXT_REFS = [
    "artifact:lead_synthesis",
    "artifact:writer_plan",
    "artifact:financial_metrics",
    "artifact:valuation_result",
]


def allocate_report_sections(plan: dict[str, object]) -> dict[str, list[ReportCompositionSection]]:
    """Assign every planned topic to one Writer from its permitted material."""
    groups = {"business": (set(), set()), "industry": (set(), set()), "financial": (set(), set())}
    for item in plan.get("sections", []):
        if not isinstance(item, dict):
            continue
        group = str(item.get("section", ""))
        target = "financial" if group == "valuation" else group
        if target not in groups:
            continue
        evidence, assumptions = groups[target]
        evidence.update(item.get("allowed_evidence_ids", []))
        assumptions.update(item.get("allowed_assumption_ids", []))
    allocated: dict[str, list[ReportCompositionSection]] = {key: [] for key in groups}
    for raw in plan.get("report_composition", []):
        section = ReportCompositionSection.model_validate(raw)
        evidence_ids = set(section.allowed_evidence_ids)
        assumption_ids = set(section.allowed_assumption_ids)
        if section.writer_group is not None:
            target = section.writer_group
        elif assumption_ids:
            target = "financial"
        else:
            scores = {
                group: len(evidence_ids & allowed_evidence)
                for group, (allowed_evidence, _allowed_assumptions) in groups.items()
            }
            target = max(groups, key=lambda group: scores[group])
        if not evidence_ids and not assumption_ids:
            raise ValueError(f"章节 {section.section_id} 缺少可分派的 Evidence 或 Assumption")
        allocated[target].append(section)
    return allocated


def validate_section_output_assignment(
    output: WriterSectionOutput,
    assignments: list[ReportCompositionSection],
) -> None:
    """Keep each parallel Writer inside its exact topic and reference budget."""
    expected = {item.section_id: item for item in assignments}
    actual_ids = [item.section_id for item in output.sections]
    if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(expected):
        raise ValueError(f"{output.section_group} Writer 返回了未分配或重复的专题")
    for section in output.sections:
        assignment = expected[section.section_id]
        unexpected_evidence = set(section.evidence_ids) - set(assignment.allowed_evidence_ids)
        if unexpected_evidence:
            raise ValueError(
                f"章节 {section.section_id} 使用了未分配的 Evidence: {sorted(unexpected_evidence)}"
            )
        unexpected_assumptions = set(section.assumption_ids) - set(
            assignment.allowed_assumption_ids
        )
        if unexpected_assumptions:
            raise ValueError(
                f"章节 {section.section_id} 使用了未分配的 Assumption: "
                f"{sorted(unexpected_assumptions)}"
            )


def _narrative(output: WriterSectionOutput) -> WriterNarrativeSection:
    sections = output.sections
    return WriterNarrativeSection(
        summary="\n\n".join(section.main_claim for section in sections),
        evidence_ids=list(dict.fromkeys(
            evidence_id for section in sections for evidence_id in section.evidence_ids
        )),
    )


def _analytical(output: WriterSectionOutput) -> WriterAnalyticalSection:
    narrative = _narrative(output)
    return WriterAnalyticalSection(
        summary=narrative.summary,
        evidence_ids=narrative.evidence_ids,
        assumption_ids=list(dict.fromkeys(
            assumption_id for section in output.sections for assumption_id in section.assumption_ids
        )),
    )


def compose_section_outputs(
    *,
    symbol: str,
    as_of: str,
    outputs: Iterable[WriterSectionOutput],
    executive_summary: str,
    key_findings: list[str],
    conflicts: list[str],
    risks: list[str],
    missing_information: list[str],
) -> FundamentalWriterOutput:
    """Deterministically assemble disjoint sections without inventing facts."""
    by_group = {output.section_group: output for output in outputs}
    required = {"business", "industry", "financial"}
    if set(by_group) != required:
        raise ValueError("Composer 必须收到业务、行业和财务三个章节包")
    if any(output.symbol != symbol or output.as_of.isoformat() != as_of for output in by_group.values()):
        raise ValueError("章节 Writer 身份与当前任务不一致")
    financial = _analytical(by_group["financial"])
    sections = [
        section
        for group in ("business", "industry", "financial")
        for section in by_group[group].sections
    ]
    return FundamentalWriterOutput(
        symbol=symbol,
        as_of=as_of,
        status="completed",
        executive_summary=executive_summary,
        business=_narrative(by_group["business"]),
        industry=_narrative(by_group["industry"]),
        financial=financial,
        valuation=financial,
        key_findings=key_findings,
        conflicts=conflicts,
        risks=risks,
        missing_information=missing_information,
        conclusion="报告结论应结合章节证据、风险与持续观察项理解。",
        disclaimer="本报告不构成投资建议、交易指令或收益承诺。",
        sections=sections,
    )


def apply_final_synthesis_edits(
    *,
    symbol: str,
    as_of: str,
    outputs: Iterable[WriterSectionOutput],
    edits: FinalSynthesisOutput,
    key_findings: list[str],
    conflicts: list[str],
    risks: list[str],
    optimization_suggestions: list[str],
) -> FundamentalWriterOutput:
    """Apply bounded editorial operations while retaining Writer-authored bodies."""
    by_group = {output.section_group: output for output in outputs}
    required = {"business", "industry", "financial"}
    if set(by_group) != required:
        raise ValueError("Composer 必须收到业务、行业和财务三个章节包")
    if edits.symbol != symbol or edits.as_of.isoformat() != as_of:
        raise ValueError("Final Synthesis 身份与当前任务不一致")
    if any(
        output.symbol != symbol or output.as_of.isoformat() != as_of
        for output in by_group.values()
    ):
        raise ValueError("章节 Writer 身份与当前任务不一致")

    originals = {
        section.section_id: section
        for group in ("business", "industry", "financial")
        for section in by_group[group].sections
    }
    if (
        len(edits.section_order) != len(set(edits.section_order))
        or set(edits.section_order) != set(originals)
    ):
        raise ValueError("Final Synthesis 的 section_order 必须且只能包含全部 Writer 专题")

    edited = {
        section_id: section.model_copy(deep=True)
        for section_id, section in originals.items()
    }
    for instruction in edits.text_edits:
        section = edited.get(instruction.section_id)
        if section is None:
            raise ValueError(f"Final Synthesis 编辑了未知专题: {instruction.section_id}")
        current = getattr(section, instruction.field)
        if current.count(instruction.target_text) != 1:
            raise ValueError(
                f"Final Synthesis 局部编辑目标必须在 {instruction.section_id} 中精确出现一次"
            )
        setattr(
            section,
            instruction.field,
            current.replace(instruction.target_text, instruction.replacement_text, 1),
        )

    seen_transitions: set[str] = set()
    for transition in edits.transitions:
        if transition.before_section_id not in edited:
            raise ValueError(
                f"Final Synthesis 过渡指向未知专题: {transition.before_section_id}"
            )
        if transition.before_section_id in seen_transitions:
            raise ValueError("每个专题前最多添加一个过渡段")
        seen_transitions.add(transition.before_section_id)
        section = edited[transition.before_section_id]
        section.body = f"{transition.text}\n\n{section.body}"

    # Re-validate after applying text patches so title/body length and strict
    # section shape remain deterministic at the Composer boundary.
    edited = {
        section_id: WrittenReportSection.model_validate(section.model_dump(mode="json"))
        for section_id, section in edited.items()
    }
    grouped_outputs: dict[str, WriterSectionOutput] = {}
    for group in ("business", "industry", "financial"):
        grouped_outputs[group] = WriterSectionOutput(
            symbol=symbol,
            as_of=as_of,
            section_group=group,
            sections=[edited[item.section_id] for item in by_group[group].sections],
        )
    financial = _analytical(grouped_outputs["financial"])
    return FundamentalWriterOutput(
        symbol=symbol,
        as_of=as_of,
        status="completed",
        executive_summary=edits.executive_summary,
        business=_narrative(grouped_outputs["business"]),
        industry=_narrative(grouped_outputs["industry"]),
        financial=financial,
        valuation=financial,
        key_findings=key_findings,
        conflicts=conflicts,
        risks=risks,
        missing_information=optimization_suggestions,
        conclusion=edits.conclusion,
        disclaimer="本报告不构成投资建议、交易指令或收益承诺。",
        sections=[edited[section_id] for section_id in edits.section_order],
    )
